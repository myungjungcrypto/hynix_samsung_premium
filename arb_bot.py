#!/usr/bin/env python3
"""
trade.xyz (Hyperliquid xyz dex) perp vs KRX 현물 프리미엄 재정거래 알림 봇.

전략:
  - perp(USD) * 실제 USD/KRW 환율 = perp 원화 환산가
  - 프리미엄 = perp 원화 환산가 / KRX 현물가 - 1
  - 프리미엄 >= entry_threshold  → "숏 + 현물 매수" 진입 알림
  - 진입 후 프리미엄 <= exit_threshold → 청산 알림
  - 현물 체결이 가능한 시간에만 모니터링:
      KRX 정규장 09:00~15:30 + NXT 프리마켓 08:00~08:50 / 애프터마켓 15:30~20:00 (KST, 휴장일 제외)
    시간 창(08:00~20:00) 안에서도 네이버 세션 상태(정규장/NXT OPEN)를 확인해
    동시호가 갭·임시휴장 시간에는 알림을 보내지 않음.
    NXT 시간대에는 NXT 체결가를 현물가로 사용.

텔레그램 명령:
  /status        현재 프리미엄 조회 (장외 시간에도 동작, 현물은 마지막 체결가)
  /open SKHX     해당 종목 상태를 '진입함'으로 수동 변경
  /flat SKHX     해당 종목 상태를 '청산함'으로 수동 변경
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
NAVER_STOCK_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
YAHOO_FX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=1d&interval=1m"
NAVER_FX_URL = (
    "https://m.stock.naver.com/front-api/marketIndex/productDetail"
    "?category=exchange&reutersCode=FX_USDKRW"
)
TG_API = "https://api.telegram.org/bot{token}/{method}"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) arb-alert-bot"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("arb")


# ---------------------------------------------------------------- config / state

def load_dotenv():
    """같은 폴더의 .env 를 환경변수로 로드 (이미 설정된 값은 유지)."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    load_dotenv()
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_token", "")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
        sys.exit(1)
    return cfg


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- market data

def fetch_hl_books(session, coins):
    """각 perp의 (bid, ask, mid)를 반환. 가격 단위는 USD(USDC)."""
    out = {}
    for coin in coins:
        r = session.post(HL_INFO_URL, json={"type": "l2Book", "coin": coin}, timeout=10)
        r.raise_for_status()
        bids, asks = r.json()["levels"]
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        out[coin] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    return out


def fetch_spot(session, code):
    """네이버 실시간 시세. (가격, 체결가능여부, 세션라벨) 반환.

    KRX 정규장이 열려 있으면 정규장 가격, 아니면 NXT(넥스트레이드)
    프리마켓(08:00~08:50)/애프터마켓(15:30~20:00) 가격(overMarketPriceInfo)을 사용.
    """
    r = session.get(NAVER_STOCK_URL.format(code=code), headers=UA, timeout=10)
    r.raise_for_status()
    d = r.json()["datas"][0]

    if d.get("marketStatus") == "OPEN":
        return float(d["closePrice"].replace(",", "")), True, "정규장"

    over = d.get("overMarketPriceInfo") or {}
    if over.get("overMarketStatus") == "OPEN" and over.get("overPrice"):
        # stale 가드: 네이버는 NXT 세션 상태를 OPEN으로 두고 마지막 체결가를 계속 반환함.
        # - 아침 08:00 개장 직후: 전일 체결가 잔존 (localTradedAt = 전일)
        # - 15:20(NXT 메인 종료)~15:40(애프터 개장): 15:20 체결가 잔존
        # → localTradedAt(실체결 시각)이 최근 N초 이내일 때만 체결 가능으로 판정
        max_age = 180  # 초. NXT 얇은 유동성 감안한 기본값
        try:
            traded_dt = datetime.fromisoformat(over.get("localTradedAt", ""))
            age = (datetime.now(KST) - traded_dt).total_seconds()
        except (ValueError, TypeError):
            age = None
        if age is None or age > max_age:
            return float(over["overPrice"].replace(",", "")), False, "NXT 스테일(최근체결없음)"
        label = {
            "PRE_MARKET": "NXT 프리마켓",
            "AFTER_MARKET": "NXT 애프터마켓",
        }.get(over.get("tradingSessionType", ""), "NXT")
        return float(over["overPrice"].replace(",", "")), True, label

    # 둘 다 닫힘 — 마지막 체결가만 참고용으로 반환
    last = over.get("overPrice") or d.get("closePrice", "0")
    return float(str(last).replace(",", "")), False, "장외"


def fetch_usdkrw(session):
    """실제 USD/KRW 환율. Yahoo(실시간 인터뱅크) 우선, 실패 시 네이버(하나은행 고시)."""
    try:
        r = session.get(YAHOO_FX_URL, headers=UA, timeout=10)
        r.raise_for_status()
        px = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        if px and px > 0:
            return float(px), "yahoo"
    except Exception as e:
        log.warning("yahoo FX 실패: %s", e)
    r = session.get(NAVER_FX_URL, headers=UA, timeout=10)
    r.raise_for_status()
    px = float(r.json()["result"]["closePrice"].replace(",", ""))
    return px, "naver(하나은행)"


# ---------------------------------------------------------------- KRX session

def is_krx_session(cfg, now=None):
    now = now or datetime.now(KST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in cfg.get("krx_holidays", []):
        return False
    start = dtime.fromisoformat(cfg.get("session_start", "09:00"))
    end = dtime.fromisoformat(cfg.get("session_end", "15:30"))
    return start <= now.time() <= end


def next_session_open(cfg, now=None):
    now = now or datetime.now(KST)
    start = dtime.fromisoformat(cfg.get("session_start", "09:00"))
    d = now
    for _ in range(30):
        candidate = d.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if (
            candidate > now
            and candidate.weekday() < 5
            and candidate.strftime("%Y-%m-%d") not in cfg.get("krx_holidays", [])
        ):
            return candidate
        d = datetime.fromtimestamp(d.timestamp() + 86400, tz=KST)
    return now


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)
        self.offset = 0
        self.session = requests.Session()

    def send(self, text):
        try:
            r = self.session.post(
                TG_API.format(token=self.token, method="sendMessage"),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if not r.ok:
                log.error("telegram send 실패: %s", r.text[:200])
        except Exception as e:
            log.error("telegram send 예외: %s", e)

    def poll_commands(self):
        """새 명령 메시지 목록 반환 (논블로킹에 가깝게 timeout=1)."""
        cmds = []
        try:
            r = self.session.get(
                TG_API.format(token=self.token, method="getUpdates"),
                params={"offset": self.offset + 1, "timeout": 1},
                timeout=5,
            )
            for u in r.json().get("result", []):
                self.offset = max(self.offset, u["update_id"])
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                if text and str(msg.get("chat", {}).get("id")) == self.chat_id:
                    cmds.append(text)
        except Exception as e:
            log.warning("telegram getUpdates 실패: %s", e)
        return cmds


# ---------------------------------------------------------------- core logic

def compute_premiums(pair, books, spot, fx):
    b = books[pair["hl_coin"]]
    return {
        "spot": spot,
        "fx": fx,
        "perp_bid_usd": b["bid"],
        "perp_ask_usd": b["ask"],
        "perp_mid_krw": b["mid"] * fx,
        # 진입(숏)은 bid에 체결되므로 bid 기준, 청산(매수)은 ask 기준으로 보수적으로 계산
        "entry_prem": b["bid"] * fx / spot - 1,
        "exit_prem": b["ask"] * fx / spot - 1,
        "mid_prem": b["mid"] * fx / spot - 1,
    }


def fmt_status_line(pair, p):
    sess = f" [{p['session']}]" if p.get("session") else ""
    return (
        f"<b>{pair['name']}</b>{sess}\n"
        f"  perp {p['perp_mid_krw']:,.0f}원 / 현물 {p['spot']:,.0f}원\n"
        f"  프리미엄 {p['mid_prem']*100:+.2f}% (숏진입 {p['entry_prem']*100:+.2f}% / 청산 {p['exit_prem']*100:+.2f}%)"
    )


def handle_commands(tg, cmds, cfg, state, session):
    for cmd in cmds:
        parts = cmd.split()
        name = parts[0].lower().lstrip("/")  # "/status" 와 "status" 모두 허용
        if name == "status":
            try:
                fx, fx_src = fetch_usdkrw(session)
                books = fetch_hl_books(session, [p["hl_coin"] for p in cfg["pairs"]])
                lines = []
                in_session = is_krx_session(cfg)
                for pair in cfg["pairs"]:
                    spot, tradable, sess_label = fetch_spot(session, pair["krx_code"])
                    p = compute_premiums(pair, books, spot, fx)
                    p["session"] = sess_label
                    st = state.get(pair["key"], {}).get("position", "flat")
                    lines.append(fmt_status_line(pair, p) + f"\n  상태: {st}")
                note = "" if in_session else "\n⚠️ 장외 시간 — 현물가는 마지막 체결가 기준"
                tg.send(f"📊 <b>현재 상태</b> (환율 {fx:,.1f}, {fx_src}){note}\n\n" + "\n\n".join(lines))
            except Exception as e:
                tg.send(f"status 조회 실패: {e}")
        elif name in ("open", "flat") and len(parts) >= 2:
            target = parts[1].upper()
            matched = [p for p in cfg["pairs"] if p["key"] == target]
            if not matched:
                tg.send(f"모르는 종목: {target} (사용 가능: " + ", ".join(p["key"] for p in cfg["pairs"]) + ")")
                continue
            key = matched[0]["key"]
            state.setdefault(key, {})["position"] = "open" if name == "open" else "flat"
            state[key]["last_alert_ts"] = 0
            save_state(state)
            tg.send(f"✅ {matched[0]['name']} 상태 → {state[key]['position']}")
        else:
            tg.send("명령: status / open SKHX / flat SKHX (open·flat은 종목 SKHX 또는 SMSN)")


def check_pair(tg, cfg, state, pair, p, now_ts):
    key = pair["key"]
    st = state.setdefault(key, {"position": "flat", "last_alert_ts": 0})
    cooldown = cfg.get("alert_cooldown_sec", 1800)
    entry_th = pair.get("entry_threshold", cfg.get("entry_threshold", 0.01))
    exit_th = pair.get("exit_threshold", cfg.get("exit_threshold", 0.001))

    if st["position"] == "flat" and p["entry_prem"] >= entry_th:
        if now_ts - st.get("last_alert_ts", 0) >= cooldown:
            tg.send(
                f"🚨 <b>[진입] {pair['name']}</b> [{p.get('session', '')}]\n"
                f"프리미엄 {p['entry_prem']*100:+.2f}% (기준 {entry_th*100:.2f}%)\n"
                f"perp(bid) ${p['perp_bid_usd']:,.2f} → 환산 {p['perp_bid_usd']*p['fx']:,.0f}원 / 현물 {p['spot']:,.0f}원\n"
                f"환율 {p['fx']:,.1f}\n"
                f"→ trade.xyz 숏 + 현물 매수\n"
                f"(자동으로 진입상태 전환 — 실제로 안 들어갔으면 flat {key})"
            )
            st["position"] = "open"          # 자동 전환: 이제 청산 알림 감시
            st["last_alert_ts"] = now_ts
            save_state(state)

    elif st["position"] == "open" and p["exit_prem"] <= exit_th:
        if now_ts - st.get("last_alert_ts", 0) >= cooldown:
            tg.send(
                f"✅ <b>[청산] {pair['name']}</b> [{p.get('session', '')}]\n"
                f"프리미엄 {p['exit_prem']*100:+.2f}% (기준 {exit_th*100:.2f}% 이하)\n"
                f"perp(ask) ${p['perp_ask_usd']:,.2f} → 환산 {p['perp_ask_usd']*p['fx']:,.0f}원 / 현물 {p['spot']:,.0f}원\n"
                f"→ 숏 청산 + 현물 매도\n"
                f"(자동으로 청산상태 전환 — 아직 보유 중이면 open {key})"
            )
            st["position"] = "flat"          # 자동 전환: 다시 진입 알림 감시
            st["last_alert_ts"] = now_ts
            save_state(state)


def main():
    cfg = load_config()
    state = load_state()
    tg = Telegram(cfg["telegram_token"], cfg["telegram_chat_id"])
    session = requests.Session()
    poll = cfg.get("poll_interval_sec", 15)
    consecutive_errors = 0
    session_open_notified = None  # 마지막으로 장시작 알림 보낸 날짜

    log.info("봇 시작. pairs=%s", [p["key"] for p in cfg["pairs"]])
    tg.send("🤖 재정거래 알림 봇 시작됨")

    while True:
        try:
            cmds = tg.poll_commands()
            if cmds:
                handle_commands(tg, cmds, cfg, state, session)

            now = datetime.now(KST)
            if not is_krx_session(cfg, now):
                session_open_notified = None
                nxt = next_session_open(cfg, now)
                wait = min(max((nxt - now).total_seconds(), poll), 60)
                log.info("장외 시간. 다음 개장: %s (%.0f초 대기)", nxt.strftime("%m-%d %H:%M"), wait)
                time.sleep(wait)
                continue

            fx, fx_src = fetch_usdkrw(session)
            books = fetch_hl_books(session, [p["hl_coin"] for p in cfg["pairs"]])
            now_ts = time.time()

            today = now.strftime("%Y-%m-%d")
            premiums = {}
            for pair in cfg["pairs"]:
                spot, tradable, sess_label = fetch_spot(session, pair["krx_code"])
                if not tradable:
                    # 시간상 모니터링 창인데 KRX/NXT 둘 다 닫힘(동시호가 갭, 임시휴장 등) — 알림 보내지 않음
                    log.info("%s 체결 불가 세션(%s), 스킵", pair["key"], sess_label)
                    continue
                p = compute_premiums(pair, books, spot, fx)
                p["session"] = sess_label
                premiums[pair["key"]] = (pair, p)
                log.info(
                    "%s [%s] spot=%.0f perp_krw=%.0f prem=%+.3f%% fx=%.1f(%s)",
                    pair["key"], sess_label, spot, p["perp_mid_krw"], p["mid_prem"] * 100, fx, fx_src,
                )
                check_pair(tg, cfg, state, pair, p, now_ts)

            if session_open_notified != today and premiums:
                lines = [fmt_status_line(pair, p) for pair, p in premiums.values()]
                tg.send(f"🔔 <b>장 시작 — 모니터링 가동</b> (환율 {fx:,.1f})\n\n" + "\n\n".join(lines))
                session_open_notified = today

            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.exception("루프 오류 (%d회 연속)", consecutive_errors)
            if consecutive_errors == cfg.get("error_alert_after", 5):
                tg.send(f"⚠️ 데이터 조회가 {consecutive_errors}회 연속 실패 중: {e}")

        time.sleep(poll)


if __name__ == "__main__":
    main()
