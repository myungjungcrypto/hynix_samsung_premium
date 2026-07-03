#!/usr/bin/env python3
"""perp(trade.xyz) vs KRX 주식선물 프리미엄 자동매매 봇.

Phase 2: mode=monitor — 양쪽 WS 실시간 호가로 프리미엄 계산, 임계값 도달 시 텔레그램 알림.
Phase 3: mode=live — 진입/청산 자동 주문 (선물 체결 확인 후 perp 숏 매칭).

프리미엄 정의 (체결가능가 기준, 실제환율 사용):
  진입 = perp_bid × USDKRW ÷ 선물_ask − 1   (≥ +1%: perp 숏 + 선물 매수)
  청산 = perp_ask × USDKRW ÷ 선물_bid − 1   (≤ 0%: 양쪽 청산)
선물 상시 베이시스(~0.5%)는 왕복 상쇄되므로 차감하지 않는다 (perp vs 선물 직접 비교).

실행 (EC2):
  venv/bin/python -m autotrader.main            # autotrader/config.json 사용
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
from autotrader.hl import HLQuoteStream

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")

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


class Engine:
    """실시간 호가 수신 → 프리미엄 계산 → 신호/알림 (monitor) 또는 주문 (live)."""

    def __init__(self, cfg, tg):
        self.cfg = cfg
        self.tg = tg
        self.pair = cfg["pair"]
        self.fut_bid = self.fut_ask = 0.0
        self.perp_bid = self.perp_ask = 0.0
        self.fx = 0.0
        self.fut_ts = self.perp_ts = 0.0
        self.state = self._load_state()
        self.last_alert_ts = 0.0
        self.last_status_ts = 0.0

    def _load_state(self):
        try:
            return json.load(open(STATE_PATH))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"position": "flat"}

    def _save_state(self):
        json.dump(self.state, open(STATE_PATH, "w"), ensure_ascii=False, indent=1)

    # ---- 콜백 ----
    def on_fut(self, code, bid, ask):
        self.fut_bid, self.fut_ask, self.fut_ts = bid, ask, time.time()
        self.evaluate()

    def on_perp(self, coin, bid, ask):
        self.perp_bid, self.perp_ask, self.perp_ts = bid, ask, time.time()
        self.evaluate()

    # ---- 세션/신선도 ----
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

    def days_to_expiry(self):
        exp = date.fromisoformat(self.pair["futures_expiry"])
        return (exp - datetime.now(KST).date()).days

    # ---- 핵심 ----
    def premiums(self):
        entry = self.perp_bid * self.fx / self.fut_ask - 1
        exit_ = self.perp_ask * self.fx / self.fut_bid - 1
        return entry, exit_

    def quotes_sane(self):
        """단일가(경매) 구간·장애 시 나타나는 비정상 호가 차단.

        - 호가 역전(ask <= bid): 경매 호가장 특성 — 체결가능가 아님
        - 스프레드 과대: 유동성 붕괴/스트림 오류
        """
        strat = self.cfg["strategy"]
        if self.fut_ask <= self.fut_bid or self.perp_ask <= self.perp_bid:
            return False
        fut_mid = (self.fut_ask + self.fut_bid) / 2
        if (self.fut_ask - self.fut_bid) / fut_mid > strat.get("max_fut_spread", 0.003):
            return False
        return True

    def evaluate(self):
        if not (self.fresh() and self.in_session() and self.quotes_sane()):
            return
        entry, exit_ = self.premiums()
        strat = self.cfg["strategy"]
        now = time.time()

        # 데이터 이상 감지: 프리미엄이 비상식적으로 크면 신호가 아니라 오류
        if abs(entry) > strat.get("premium_sanity", 0.05):
            if now - self.last_alert_ts >= strat.get("alert_cooldown_sec", 600):
                self.last_alert_ts = now
                log.warning("프리미엄 sanity 초과 %+.2f%% — 데이터 이상 의심, 스킵", entry * 100)
                self.tg.send(f"⚠️ 프리미엄 {entry*100:+.1f}% — 비정상 호가로 판단, 신호 무시\n"
                             f"선물 {self.fut_bid:,.0f}/{self.fut_ask:,.0f} perp {self.perp_bid:.2f}/{self.perp_ask:.2f} fx {self.fx:,.1f}")
            return

        # 주기적 상태 로그
        if now - self.last_status_ts >= self.cfg.get("status_log_sec", 60):
            self.last_status_ts = now
            log.info("선물 %.0f/%.0f perp %.2f/%.2f fx %.1f | 진입프리미엄 %+.3f%% 청산 %+.3f%% [%s]",
                     self.fut_bid, self.fut_ask, self.perp_bid, self.perp_ask, self.fx,
                     entry * 100, exit_ * 100, self.state["position"])

        cooldown = strat.get("alert_cooldown_sec", 600)
        if self.state["position"] == "flat" and entry >= strat["entry_threshold"]:
            if now - self.last_alert_ts >= cooldown:
                self.last_alert_ts = now
                self.act_entry(entry)
        elif self.state["position"] == "open" and exit_ <= strat["exit_threshold"]:
            if now - self.last_alert_ts >= cooldown:
                self.last_alert_ts = now
                self.act_exit(exit_)

    def act_entry(self, prem):
        d2e = self.days_to_expiry()
        roll_note = f"\n⚠️ 만기 D-{d2e}" if d2e <= self.cfg["session"]["roll_days_before_expiry"] + 1 else ""
        if self.cfg["mode"] == "monitor":
            self.tg.send(
                f"🚨 [진입신호/모니터] {self.pair['name']}\n"
                f"perp vs 선물 프리미엄 {prem*100:+.2f}%\n"
                f"선물 ask {self.fut_ask:,.0f} / perp bid {self.perp_bid:.2f} (fx {self.fx:,.1f})\n"
                f"→ 수동: 선물 {self.pair['futures_code']} 매수 + perp 숏{roll_note}")
            self.state["position"] = "open"
            self._save_state()
        else:
            self.tg.send("⛔ live 모드 주문은 Phase 3에서 활성화됩니다")

    def act_exit(self, prem):
        if self.cfg["mode"] == "monitor":
            self.tg.send(
                f"✅ [청산신호/모니터] {self.pair['name']}\n"
                f"perp vs 선물 프리미엄 {prem*100:+.2f}%\n"
                f"→ 수동: 선물 매도 + perp 숏 커버")
            self.state["position"] = "flat"
            self._save_state()
        else:
            self.tg.send("⛔ live 모드 주문은 Phase 3에서 활성화됩니다")


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


async def watchdog(engine, kis_stream, hl_stream, tg):
    """장중인데 시세가 30초 이상 끊기면 알림."""
    warned = False
    while True:
        await asyncio.sleep(30)
        if not engine.in_session():
            warned = False
            continue
        stale = []
        if time.time() - engine.fut_ts > 60:
            stale.append("KIS선물")
        if time.time() - engine.perp_ts > 60:
            stale.append("HLperp")
        if stale and not warned:
            tg.send(f"⚠️ 시세 수신 끊김: {','.join(stale)}")
            warned = True
        elif not stale:
            warned = False


async def main():
    load_dotenv()
    cfg = json.load(open(os.path.join(BASE_DIR, "config.json")))
    tg = Telegram()
    engine = Engine(cfg, tg)

    key, sec, acct = load_keys(cfg.get("kis_secrets_path", ""))
    kis = KISClient(key, sec, acct)
    kis.token()

    # 기동 시 REST로 초기 호가 (WS 첫 틱 전 공백 방지)
    try:
        b, a, bq, aq = kis.asking_price(cfg["pair"]["futures_code"])
        engine.fut_bid, engine.fut_ask, engine.fut_ts = b, a, time.time()
        log.info("초기 선물 호가 %s: %.0f/%.0f (잔량 %d/%d)", cfg["pair"]["futures_code"], b, a, bq, aq)
    except Exception as e:
        log.warning("초기 호가 실패(장외?): %s", e)

    kis_stream = KISQuoteStream(kis, [cfg["pair"]["futures_code"]], engine.on_fut)
    hl_stream = HLQuoteStream([cfg["pair"]["perp_coin"]], engine.on_perp)

    d2e = engine.days_to_expiry()
    tg.send(f"🤖 자동매매 봇 시작 (mode={cfg['mode']})\n"
            f"{cfg['pair']['name']} 선물 {cfg['pair']['futures_code']} (만기 D-{d2e}) vs {cfg['pair']['perp_coin']}\n"
            f"진입 +{cfg['strategy']['entry_threshold']*100:.1f}% / 청산 {cfg['strategy']['exit_threshold']*100:.1f}%")

    await asyncio.gather(
        kis_stream.run(),
        hl_stream.run(),
        fx_loop(engine, cfg.get("fx_poll_sec", 30)),
        watchdog(engine, kis_stream, hl_stream, tg),
    )


if __name__ == "__main__":
    asyncio.run(main())
