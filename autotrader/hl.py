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

    def __init__(self, wallet_address, private_key, leverage=3, perp_dexs=None):
        """perp_dexs: 거래할 builder dex 목록 (예: ["xyz"]). 미지정 시 기본 dex만 로드되어
        trade.xyz 종목(xyz:SKHX 등)의 자산 매핑이 없어 주문이 KeyError로 실패한다."""
        self.wallet = wallet_address
        self.leverage = leverage
        dexs = [""] + [d for d in (perp_dexs or []) if d]
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            import eth_account
            account = eth_account.Account.from_key(private_key)
            self.exchange = Exchange(account, account_address=wallet_address, perp_dexs=dexs)
            self.info = Info(skip_ws=True, perp_dexs=dexs)
        except ImportError:
            self.exchange = None
            log.warning("hyperliquid-python-sdk 미설치 — 주문 비활성 (모니터 전용)")

    def can_trade(self, coin):
        """코인이 자산 매핑에 있는지 (dex 로드 확인). (가능여부, 메시지)"""
        if not self.exchange:
            return False, "SDK 미설치"
        try:
            self.info.name_to_asset(coin)
            return True, "ok"
        except Exception as e:
            return False, f"{coin} 자산 매핑 없음 (dex 미로드?): {e}"

    def ensure_leverage(self, coin):
        """격리 레버리지 설정. 실패해도 주문은 진행 가능하므로 False만 반환 (헤지 우선)."""
        if not self.exchange:
            return False
        try:
            self.exchange.update_leverage(self.leverage, coin, is_cross=False)
            return True
        except Exception as e:
            log.warning("레버리지 설정 실패(%s): %s — 기존 설정으로 주문 진행", coin, e)
            return False

    def _clearinghouse(self, dex=""):
        """dex별 perps 계좌 상태. HIP-3 dex(xyz 등)는 증거금 계좌가 분리되어 있어
        기본 조회로는 잔고/포지션이 안 보인다 — 반드시 dex 지정 조회."""
        payload = {"type": "clearinghouseState", "user": self.wallet}
        if dex:
            payload["dex"] = dex
        import requests
        base = getattr(self.info, "base_url", "https://api.hyperliquid.xyz")
        r = requests.post(f"{base}/info", json=payload, timeout=10)
        return r.json()

    @staticmethod
    def _dex_of(coin):
        return coin.split(":")[0] if ":" in coin else ""

    def spot_usdc(self):
        """Spot USDC 잔고. 통합 잔고 모델에서 실질 거래 가능액. 실패 시 None."""
        if not self.exchange:
            return None
        try:
            import requests
            base = getattr(self.info, "base_url", "https://api.hyperliquid.xyz")
            r = requests.post(f"{base}/info",
                              json={"type": "spotClearinghouseState", "user": self.wallet},
                              timeout=10)
            for b in r.json().get("balances", []):
                if b.get("coin") == "USDC":
                    return float(b.get("total", 0))
            return 0.0
        except Exception as e:
            log.warning("spot 잔고 조회 실패: %s", e)
            return None

    def balance(self, dex=""):
        """해당 dex perps 계좌: (총액 USD, 가용 증거금 USD). 실패 시 None."""
        if not self.exchange:
            return None
        try:
            st = self._clearinghouse(dex)
            ms = st.get("marginSummary", {})
            return float(ms.get("accountValue", 0)), float(st.get("withdrawable", 0))
        except Exception as e:
            log.warning("잔고 조회 실패(dex=%s): %s", dex, e)
            return None

    def position(self, coin):
        """해당 코인 포지션 수량 (숏이면 음수, 없으면 0). 코인의 dex 계좌에서 조회."""
        if not self.exchange:
            return None
        try:
            state = self._clearinghouse(self._dex_of(coin))
        except Exception as e:
            log.warning("포지션 조회 실패(%s): %s", coin, e)
            return None
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0))
        return 0.0

    @staticmethod
    def _parse_fill(res):
        """주문 응답에서 (체결수량, 평균단가, oid) 추출."""
        statuses = res.get("response", {}).get("data", {}).get("statuses", [])
        total_sz = 0.0
        total_val = 0.0
        oid = None
        for s in statuses:
            f = s.get("filled")
            if f:
                sz = float(f["totalSz"])
                total_sz += sz
                total_val += sz * float(f.get("avgPx", 0))
                oid = f.get("oid", oid)
        avg = total_val / total_sz if total_sz > 0 else 0.0
        return total_sz, avg, oid

    def market_order(self, coin, is_buy, size, slippage=0.005):
        """IOC 시장가성 주문. 반환 (성공, {size, avg_px, oid} or 에러문자열)."""
        if not self.exchange:
            return False, "SDK 미설치"
        try:
            res = self.exchange.market_open(coin, is_buy, size, None, slippage)
            sz, avg, oid = self._parse_fill(res)
            if sz > 0:
                return True, {"size": sz, "avg_px": avg, "oid": oid}
            return False, str(res.get("response", {}).get("data", {}).get("statuses", res))
        except Exception as e:
            return False, str(e)

    def market_close(self, coin, size=None, slippage=0.005):
        """포지션 청산 (reduce). 반환 (성공, {size, avg_px, oid} or 에러문자열)."""
        if not self.exchange:
            return False, "SDK 미설치"
        try:
            res = self.exchange.market_close(coin, size, None, slippage)
            sz, avg, oid = self._parse_fill(res)
            if sz > 0:
                return True, {"size": sz, "avg_px": avg, "oid": oid}
            return False, str(res.get("response", {}).get("data", {}).get("statuses", res))
        except Exception as e:
            return False, str(e)

    def fills_for_oids(self, oids, require=None, retries=5, wait_sec=2):
        """주문 ID들의 실측 수수료·실현손익 합산. 반환 {fee, closed_pnl, matched:set}.

        체결 직후에는 user_fills 인덱스 반영이 늦을 수 있어, require의 oid가
        전부 잡힐 때까지 재시도한다 (레이스 방지 — closedPnl 0 오보의 원인).
        """
        if not self.exchange:
            return None
        want = {int(o) for o in oids if o is not None}
        need = {int(o) for o in (require or []) if o is not None}
        for attempt in range(retries):
            try:
                fills = self.info.user_fills(self.wallet)
            except Exception as e:
                log.warning("user_fills 조회 실패: %s", e)
                return None
            fee = closed = 0.0
            matched = set()
            for f in fills:
                oid = int(f.get("oid", -1))
                if oid in want:
                    fee += float(f.get("fee", 0))
                    closed += float(f.get("closedPnl", 0))
                    matched.add(oid)
            if need.issubset(matched) and matched:
                return {"fee": fee, "closed_pnl": closed, "matched": matched}
            time.sleep(wait_sec)
        if matched:
            log.warning("user_fills 일부만 매칭: %s (필요 %s)", matched, need)
            return {"fee": fee, "closed_pnl": closed, "matched": matched}
        return None

    def funding_between(self, coin, start_ms, end_ms):
        """보유 기간 동안 해당 코인의 펀딩 수취 합계 (USDC, +면 수취). 실패 시 None."""
        if not self.exchange:
            return None
        try:
            recs = self.info.user_funding_history(self.wallet, int(start_ms), int(end_ms))
            total = 0.0
            for r in recs:
                d = r.get("delta", {})
                if d.get("coin") == coin:
                    total += float(d.get("usdc", 0))
            return total
        except Exception as e:
            log.warning("user_funding 조회 실패: %s", e)
            return None
