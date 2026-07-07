#!/usr/bin/env python3
"""perp(trade.xyz) vs KRX 주식선물 프리미엄 자동매매 봇.

mode=monitor : 신호를 텔레그램 알림으로만 (수동 매매)
mode=live    : 자동 주문 — 선물 시장가 매수 → 체결확인 → 체결계약×10주 perp 숏
               청산은 역순. 한쪽 실패 시 즉시 언와인드.

프리미엄 (체결가능가·실제환율):
  진입 = perp_bid × USDKRW ÷ 선물_ask − 1 ≥ entry_threshold
  청산 = perp_ask × USDKRW ÷ 선물_bid − 1 ≤ exit_threshold

안전장치:
  - 단일가/이상호가 가드 (역전·스프레드·프리미엄 sanity)
  - autotrader/PAUSE 파일 존재 시 신호 무시 (킬스위치: touch autotrader/PAUSE)
  - 일일 사이클 제한 (live.max_cycles_per_day)
  - 기동 시 HL 실포지션 vs state.json 대조, 불일치면 live 거부
  - 주문 진행 중 재신호 차단

실행: venv/bin/python -m autotrader.main
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from autotrader.kis import KISClient, KISQuoteStream, load_keys
from autotrader.hl import HLQuoteStream, HLTrader

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
PAUSE_PATH = os.path.join(BASE_DIR, "PAUSE")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("auto")


def load_dotenv():
    path = os.path.join(os.path.dirname(BASE_DIR), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def send(self, text):
        if not self.token:
            log.info("[TG생략] %s", text.replace("\n", " / "))
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as e:
            log.error("텔레그램 실패: %s", e)


class Executor:
    """live 모드 듀얼레그 주문 실행. 진입: 선물 먼저(체결확인) → perp 매칭."""

    def __init__(self, cfg, kis: KISClient, hl: HLTrader, tg):
        self.cfg = cfg
        self.kis = kis
        self.hl = hl
        self.tg = tg
        self.pair = cfg["pair"]
        self.mult = cfg["pair"]["contract_multiplier"]

    def _wait_fill(self, odno, want, timeout=15):
        """체결수량 폴링. timeout 내 최대 체결수량 반환."""
        deadline = time.time() + timeout
        filled = 0
        while time.time() < deadline:
            q = self.kis.filled_qty(odno)
            if q is not None:
                filled = max(filled, q)
                if filled >= want:
                    break
            time.sleep(1)
        return filled

    def enter(self, engine, prem):
        code = self.pair["futures_code"]
        coin = self.pair["perp_coin"]
        n = self.cfg["strategy"]["max_contracts"]
        self.tg.send(f"⚙️ [진입 실행] 선물 {code} {n}계약 시장가 매수 (프리미엄 {prem*100:+.2f}%)")

        ok, odno = self.kis.order(code, "buy", n, price=0)
        if not ok:
            self.tg.send(f"❌ 선물 주문 실패: {odno}\n→ 진입 중단 (flat 유지)")
            return

        filled = self._wait_fill(odno, n)
        if filled == 0:
            ok_c, msg = self.kis.cancel(odno, code, n)
            self.tg.send(f"❌ 선물 미체결(15s) → 취소 {'성공' if ok_c else '실패:'+msg}\n→ 진입 중단")
            return
        if filled < n:
            self.kis.cancel(odno, code, n - filled)

        size = filled * self.mult
        self.hl.ensure_leverage(coin)
        ok2, res = self.hl.market_order(coin, is_buy=False, size=size)
        if not ok2:
            ok3, od2 = self.kis.order(code, "sell", filled, price=0)
            self.tg.send(f"🚨 perp 숏 실패({res})\n→ 선물 언와인드 {'주문완료' if ok3 else '❌실패 — 수동 개입 필요!'}")
            return

        engine.state = {"position": "open", "contracts": filled, "perp_size": float(res),
                        "entry_prem": prem, "entry_ts": time.time(),
                        "entry_date": datetime.now(KST).strftime("%Y-%m-%d")}
        engine._save_state()
        self.tg.send(f"✅ [진입 완료] 선물 {filled}계약 매수 + perp 숏 {res}주\n"
                     f"진입 프리미엄 {prem*100:+.2f}%")

    def exit(self, engine, prem):
        code = self.pair["futures_code"]
        coin = self.pair["perp_coin"]
        n = int(engine.state.get("contracts", 0))
        perp_size = float(engine.state.get("perp_size", n * self.mult))
        if n <= 0:
            engine.state = {"position": "flat"}
            engine._save_state()
            return
        self.tg.send(f"⚙️ [청산 실행] 선물 {n}계약 매도 + perp 커버 (프리미엄 {prem*100:+.2f}%)")

        ok, odno = self.kis.order(code, "sell", n, price=0)
        if not ok:
            self.tg.send(f"❌ 선물 매도 실패: {odno}\n→ 청산 중단, 다음 신호에 재시도")
            return
        filled = self._wait_fill(odno, n)
        if filled == 0:
            self.kis.cancel(odno, code, n)
            self.tg.send("❌ 선물 매도 미체결 → 취소, 다음 신호에 재시도")
            return

        cover = perp_size * (filled / n)
        ok2, res = self.hl.market_close(coin, cover)
        if not ok2:
            self.tg.send(f"🚨 perp 커버 실패({res}) — perp 숏 {cover}주 잔존, 수동 확인 필요!")

        if filled >= n:
            entry_prem = engine.state.get("entry_prem", 0)
            engine.state = {"position": "flat"}
            engine._save_state()
            self.tg.send(f"✅ [청산 완료] 캡처 스프레드 {(entry_prem-prem)*100:+.2f}%p")
        else:
            engine.state["contracts"] = n - filled
            engine.state["perp_size"] = perp_size - cover
            engine._save_state()
            self.tg.send(f"⚠️ 부분 청산 {filled}/{n} — 잔여 {n-filled}계약 유지")


class Engine:
    def __init__(self, cfg, tg):
        self.cfg = cfg
        self.tg = tg
        self.pair = cfg["pair"]
        self.executor = None      # live 모드에서 주입
        self.fut_bid = self.fut_ask = 0.0
        self.perp_bid = self.perp_ask = 0.0
        self.fx = 0.0
        self.fut_ts = self.perp_ts = 0.0
        self.state = self._load_state()
        self.last_alert_ts = 0.0
        self.last_status_ts = 0.0
        self.executing = False
        self.cycles = {"date": "", "count": 0}
        self.paused_notified = False

    def _load_state(self):
        try:
            return json.load(open(STATE_PATH))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"position": "flat"}

    def _save_state(self):
        json.dump(self.state, open(STATE_PATH, "w"), ensure_ascii=False, indent=1)

    def on_fut(self, code, bid, ask):
        self.fut_bid, self.fut_ask, self.fut_ts = bid, ask, time.time()
        self.evaluate()

    def on_perp(self, coin, bid, ask):
        self.perp_bid, self.perp_ask, self.perp_ts = bid, ask, time.time()
        self.evaluate()

    def in_session(self):
        now = datetime.now(KST)
        if now.weekday() >= 5:
            return False
        s = dtime.fromisoformat(self.cfg["session"]["start"])
        e = dtime.fromisoformat(self.cfg["session"]["end"])
        return s <= now.time() <= e

    def fresh(self):
        now = time.time()
        return (self.fx > 0 and self.fut_bid > 0 and self.perp_bid > 0
                and now - self.fut_ts < 30 and now - self.perp_ts < 30)

    def quotes_sane(self):
        strat = self.cfg["strategy"]
        if self.fut_ask <= self.fut_bid or self.perp_ask <= self.perp_bid:
            return False
        fut_mid = (self.fut_ask + self.fut_bid) / 2
        if (self.fut_ask - self.fut_bid) / fut_mid > strat.get("max_fut_spread", 0.003):
            return False
        return True

    def days_to_expiry(self):
        exp = date.fromisoformat(self.pair["futures_expiry"])
        return (exp - datetime.now(KST).date()).days

    def premiums(self):
        entry = self.perp_bid * self.fx / self.fut_ask - 1
        exit_ = self.perp_ask * self.fx / self.fut_bid - 1
        return entry, exit_

    def _normalize_cycles(self):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if self.cycles["date"] != today:
            self.cycles = {"date": today, "count": 0}

    def _cycle_ok(self):
        self._normalize_cycles()
        limit = self.cfg.get("live", {}).get("max_cycles_per_day", 3)
        return self.cycles["count"] < limit

    def _register_cycle(self):
        self._normalize_cycles()
        self.cycles["count"] += 1

    def paused(self):
        if os.path.exists(PAUSE_PATH):
            if not self.paused_notified:
                self.paused_notified = True
                self.tg.send("⏸️ PAUSE 파일 감지 — 신호 무시 중 (해제: rm autotrader/PAUSE)")
            return True
        self.paused_notified = False
        return False

    def evaluate(self):
        if self.executing or not (self.fresh() and self.in_session() and self.quotes_sane()):
            return
        entry, exit_ = self.premiums()
        strat = self.cfg["strategy"]
        now = time.time()

        if abs(entry) > strat.get("premium_sanity", 0.05):
            if now - self.last_alert_ts >= strat.get("alert_cooldown_sec", 600):
                self.last_alert_ts = now
                log.warning("프리미엄 sanity 초과 %+.2f%% — 스킵", entry * 100)
                self.tg.send(f"⚠️ 프리미엄 {entry*100:+.1f}% — 비정상 호가로 판단, 신호 무시\n"
                             f"선물 {self.fut_bid:,.0f}/{self.fut_ask:,.0f} perp {self.perp_bid:.2f}/{self.perp_ask:.2f} fx {self.fx:,.1f}")
            return

        if now - self.last_status_ts >= self.cfg.get("status_log_sec", 60):
            self.last_status_ts = now
            log.info("선물 %.0f/%.0f perp %.2f/%.2f fx %.1f | 진입 %+.3f%% 청산 %+.3f%% [%s]",
                     self.fut_bid, self.fut_ask, self.perp_bid, self.perp_ask, self.fx,
                     entry * 100, exit_ * 100, self.state["position"])

        if self.paused():
            return

        cooldown = strat.get("alert_cooldown_sec", 600)
        if self.state["position"] == "flat" and entry >= strat["entry_threshold"]:
            if now - self.last_alert_ts >= cooldown and self._cycle_ok():
                self.last_alert_ts = now
                self._dispatch("enter", entry)
        elif self.state["position"] == "open" and exit_ <= strat["exit_threshold"]:
            if now - self.last_alert_ts >= cooldown:
                self.last_alert_ts = now
                self._dispatch("exit", exit_)

    def _dispatch(self, action, prem):
        if self.cfg["mode"] == "monitor":
            d2e = self.days_to_expiry()
            if action == "enter":
                self.tg.send(f"🚨 [진입신호/모니터] {self.pair['name']}\n"
                             f"perp vs 선물 프리미엄 {prem*100:+.2f}%\n"
                             f"선물 ask {self.fut_ask:,.0f} / perp bid ${self.perp_bid:.2f} (fx {self.fx:,.1f})\n"
                             f"→ 수동: 선물 {self.pair['futures_code']} 매수 + perp 숏"
                             + (f"\n⚠️ 만기 D-{d2e}" if d2e <= 3 else ""))
                self.state["position"] = "open"
            else:
                self.tg.send(f"✅ [청산신호/모니터] {self.pair['name']}\n"
                             f"perp vs 선물 프리미엄 {prem*100:+.2f}%\n"
                             f"→ 수동: 선물 매도 + perp 숏 커버")
                self.state["position"] = "flat"
            self._save_state()
            return

        # live: 블로킹 주문을 별도 스레드로 (WS 루프 정지 방지)
        self.executing = True
        if action == "enter":
            self._register_cycle()

        async def run():
            try:
                fn = self.executor.enter if action == "enter" else self.executor.exit
                await asyncio.to_thread(fn, self, prem)
            except Exception as e:
                log.exception("실행기 오류")
                self.tg.send(f"🚨 실행기 예외: {e} — 포지션 수동 확인 필요")
            finally:
                self.executing = False

        asyncio.get_running_loop().create_task(run())


async def fx_loop(engine, interval):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=1d&interval=1m"
    naver = ("https://m.stock.naver.com/front-api/marketIndex/productDetail"
             "?category=exchange&reutersCode=FX_USDKRW")
    headers = {"User-Agent": "Mozilla/5.0"}
    while True:
        try:
            px = requests.get(url, headers=headers, timeout=10).json()[
                "chart"]["result"][0]["meta"]["regularMarketPrice"]
            engine.fx = float(px)
        except Exception:
            try:
                d = requests.get(naver, headers=headers, timeout=10).json()
                engine.fx = float(d["result"]["closePrice"].replace(",", ""))
            except Exception as e:
                log.warning("FX 갱신 실패: %s", e)
        await asyncio.sleep(interval)


async def watchdog(engine, kis, tg):
    """호가 정지 시: WS는 변동시에만 push하므로 먼저 REST로 재확인, 그래도 실패면 경고."""
    code = engine.pair["futures_code"]
    coin = engine.pair["perp_coin"]
    warned = False
    while True:
        await asyncio.sleep(30)
        if not engine.in_session():
            warned = False
            continue
        problems = []
        if time.time() - engine.fut_ts > 60:
            try:
                b, a, _, _ = await asyncio.to_thread(kis.asking_price, code)
                engine.fut_bid, engine.fut_ask, engine.fut_ts = b, a, time.time()
                log.info("선물 호가 REST 갱신 (WS 무변동): %.0f/%.0f", b, a)
            except Exception as e:
                problems.append(f"KIS선물({e})")
        if time.time() - engine.perp_ts > 60:
            try:
                r = requests.post("https://api.hyperliquid.xyz/info",
                                  json={"type": "l2Book", "coin": coin}, timeout=10).json()
                bid = float(r["levels"][0][0]["px"]); ask = float(r["levels"][1][0]["px"])
                engine.perp_bid, engine.perp_ask, engine.perp_ts = bid, ask, time.time()
                log.info("perp 호가 REST 갱신: %.2f/%.2f", bid, ask)
            except Exception as e:
                problems.append(f"HLperp({e})")
        if problems and not warned:
            tg.send(f"⚠️ 시세 수신 장애 (REST 재확인도 실패): {', '.join(problems)}")
            warned = True
        elif not problems:
            warned = False


def live_preflight(cfg, kis, tg):
    """live 모드 사전점검. 실패 시 monitor로 강등."""
    errors = []
    if not kis.account or "-" not in kis.account:
        errors.append("KIS_ACCOUNT 미설정 (예: 12345678-01)")
    wallet = os.environ.get("HL_WALLET_ADDRESS", "")
    pkey = os.environ.get("HL_PRIVATE_KEY", "")
    if not wallet or not pkey:
        errors.append("HL_WALLET_ADDRESS / HL_PRIVATE_KEY 미설정")
    hl = None
    if not errors:
        hl = HLTrader(wallet, pkey, cfg["hyperliquid"].get("leverage", 3))
        if not hl.exchange:
            errors.append("hyperliquid-python-sdk 미설치 (pip install hyperliquid-python-sdk)")
    if not errors:
        pos = hl.position(cfg["pair"]["perp_coin"])
        state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {"position": "flat"}
        expected = -(state.get("perp_size", 0)) if state.get("position") == "open" else 0.0
        if pos is not None and abs(pos - expected) > 0.01:
            errors.append(f"포지션 불일치: HL 실포지션 {pos} vs state 기대값 {expected} — state.json 수동 정리 필요")
    if errors:
        tg.send("⛔ live 사전점검 실패 → monitor 모드로 강등\n- " + "\n- ".join(errors))
        cfg["mode"] = "monitor"
        return None
    d2e = (date.fromisoformat(cfg["pair"]["futures_expiry"]) - datetime.now(KST).date()).days
    if d2e <= cfg["session"].get("roll_days_before_expiry", 2):
        tg.send(f"⛔ 만기 D-{d2e} — 신규 진입 위험. config를 차월물({cfg['pair'].get('next_futures_code','?')})로 갱신 후 재시작 권장\n→ monitor 모드로 강등")
        cfg["mode"] = "monitor"
        return None
    return hl


async def main():
    load_dotenv()
    cfg = json.load(open(os.path.join(BASE_DIR, "config.json")))
    tg = Telegram()
    engine = Engine(cfg, tg)

    key, sec, acct = load_keys(cfg.get("kis_secrets_path", ""))
    kis = KISClient(key, sec, acct)
    kis.token()

    if cfg["mode"] == "live":
        hl_trader = live_preflight(cfg, kis, tg)
        if hl_trader:
            engine.executor = Executor(cfg, kis, hl_trader, tg)

    try:
        b, a, bq, aq = kis.asking_price(cfg["pair"]["futures_code"])
        engine.fut_bid, engine.fut_ask, engine.fut_ts = b, a, time.time()
        log.info("초기 선물 호가 %s: %.0f/%.0f (잔량 %d/%d)", cfg["pair"]["futures_code"], b, a, bq, aq)
    except Exception as e:
        log.warning("초기 호가 실패(장외?): %s", e)

    kis_stream = KISQuoteStream(kis, [cfg["pair"]["futures_code"]], engine.on_fut)
    hl_stream = HLQuoteStream([cfg["pair"]["perp_coin"]], engine.on_perp)

    d2e = engine.days_to_expiry()
    mode_label = "🔴 LIVE(실거래)" if cfg["mode"] == "live" else "monitor"
    tg.send(f"🤖 자동매매 봇 시작 (mode={mode_label})\n"
            f"{cfg['pair']['name']} 선물 {cfg['pair']['futures_code']} (만기 D-{d2e}) vs {cfg['pair']['perp_coin']}\n"
            f"진입 +{cfg['strategy']['entry_threshold']*100:.1f}% / 청산 {cfg['strategy']['exit_threshold']*100:.1f}% / "
            f"{cfg['strategy']['max_contracts']}계약\n"
            f"일시정지: touch autotrader/PAUSE")

    await asyncio.gather(
        kis_stream.run(),
        hl_stream.run(),
        fx_loop(engine, cfg.get("fx_poll_sec", 30)),
        watchdog(engine, kis, tg),
    )


if __name__ == "__main__":
    asyncio.run(main())
