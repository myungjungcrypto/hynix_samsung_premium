"""Hyperliquid(trade.xyz) 클라이언트 — 실시간 l2Book WS + 주문(Phase 3).

시세: wss://api.hyperliquid.xyz/ws 에 l2Book 구독 → best bid/ask 콜백.
주문: hyperliquid-python-sdk 사용 (Phase 3에서 활성화, 격리마진 3배).
"""
import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("hl")

WS_URL = "wss://api.hyperliquid.xyz/ws"


class HLQuoteStream:
    """l2Book 구독. on_quote(coin, bid, ask) 콜백."""

    def __init__(self, coins, on_quote):
        self.coins = coins
        self.on_quote = on_quote
        self.last_msg_ts = 0.0

    async def run(self):
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    for coin in self.coins:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin},
                        }))
                    log.info("HL WS 연결, 구독: %s", self.coins)
                    async for raw in ws:
                        self.last_msg_ts = time.time()
                        self._handle(raw)
            except Exception as e:
                log.warning("HL WS 재접속 (%s)", e)
                await asyncio.sleep(5)

    def _handle(self, raw):
        msg = json.loads(raw)
        if msg.get("channel") != "l2Book":
            return
        data = msg.get("data", {})
        coin = data.get("coin", "")
        levels = data.get("levels", [[], []])
        try:
            bid = float(levels[0][0]["px"])
            ask = float(levels[1][0]["px"])
        except (IndexError, KeyError, ValueError):
            return
        self.on_quote(coin, bid, ask)


class HLTrader:
    """주문 실행 (Phase 3). hyperliquid-python-sdk 필요.

    사용 전 요구사항:
      - pip install hyperliquid-python-sdk
      - secrets: wallet_address + private_key (API wallet 권장)
      - 격리마진 3배 설정 후 주문
    """

    def __init__(self, wallet_address, private_key, leverage=3):
        self.wallet = wallet_address
        self.leverage = leverage
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            import eth_account
            account = eth_account.Account.from_key(private_key)
            self.exchange = Exchange(account, account_address=wallet_address)
            self.info = Info(skip_ws=True)
        except ImportError:
            self.exchange = None
            log.warning("hyperliquid-python-sdk 미설치 — 주문 비활성 (모니터 전용)")

    def ensure_leverage(self, coin):
        if not self.exchange:
            return False
        self.exchange.update_leverage(self.leverage, coin, is_cross=False)
        return True

    def market_order(self, coin, is_buy, size, slippage=0.005):
        """IOC 시장가성 주문. 반환 (성공, 체결수량 or 에러)."""
        if not self.exchange:
            return False, "SDK 미설치"
        try:
            res = self.exchange.market_open(coin, is_buy, size, None, slippage)
            statuses = res.get("response", {}).get("data", {}).get("statuses", [])
            filled = sum(float(s["filled"]["totalSz"]) for s in statuses if "filled" in s)
            if filled > 0:
                return True, filled
            return False, str(statuses)
        except Exception as e:
            return False, str(e)
