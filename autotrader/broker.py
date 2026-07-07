"""선물 주문 브로커 추상화 — Executor는 이 인터페이스만 사용.

구현체:
  KISBroker        : KIS REST 직접 주문 (계좌 있으면 사용)
  KiwoomHTTPBroker : 같은 서버의 kiwoom-daemon(OCX 래퍼)에 HTTP 위임

인터페이스 (모두 동기 함수, Executor가 스레드에서 호출):
  order(code, side, qty, price=0) -> (ok: bool, order_no_or_err: str)
  filled_qty(order_no)            -> int | None   (None=조회실패)
  cancel(order_no, code, qty)     -> (ok, msg)
  health()                        -> (ok, msg)    (기동 전 점검용)
"""
import logging
import os

import requests

log = logging.getLogger("broker")


class KISBroker:
    def __init__(self, kis_client):
        self.kis = kis_client

    def order(self, code, side, qty, price=0):
        return self.kis.order(code, side, qty, price)

    def filled_qty(self, order_no):
        return self.kis.filled_qty(order_no)

    def cancel(self, order_no, code, qty):
        return self.kis.cancel(order_no, code, qty)

    def health(self):
        if not self.kis.account or "-" not in self.kis.account:
            return False, "KIS_ACCOUNT 미설정"
        try:
            self.kis.token()
            return True, "ok"
        except Exception as e:
            return False, str(e)


class KiwoomHTTPBroker:
    """windows/kiwoom_daemon.py 가 여는 localhost HTTP 서버 호출."""

    def __init__(self, base_url="http://127.0.0.1:8899", token=""):
        self.base = base_url.rstrip("/")
        self.token = token or os.environ.get("KIWOOM_GW_TOKEN", "")
        self.s = requests.Session()

    def _headers(self):
        return {"X-Token": self.token}

    def order(self, code, side, qty, price=0):
        try:
            r = self.s.post(f"{self.base}/order", json={
                "code": code, "side": side, "qty": qty, "price": price,
            }, headers=self._headers(), timeout=15)
            d = r.json()
            if d.get("ok"):
                return True, str(d.get("order_no", ""))
            return False, d.get("error", str(d))
        except Exception as e:
            return False, f"gateway: {e}"

    def filled_qty(self, order_no):
        try:
            r = self.s.get(f"{self.base}/fill/{order_no}",
                           headers=self._headers(), timeout=10)
            d = r.json()
            return int(d["filled"]) if d.get("ok") else None
        except Exception as e:
            log.warning("fill 조회 실패: %s", e)
            return None

    def cancel(self, order_no, code, qty):
        try:
            r = self.s.post(f"{self.base}/cancel", json={
                "order_no": order_no, "code": code, "qty": qty,
            }, headers=self._headers(), timeout=15)
            d = r.json()
            return bool(d.get("ok")), d.get("error", "")
        except Exception as e:
            return False, f"gateway: {e}"

    def health(self):
        try:
            r = self.s.get(f"{self.base}/health", headers=self._headers(), timeout=5)
            d = r.json()
            if d.get("ok") and d.get("connected"):
                return True, f"키움 연결됨 (계좌 {d.get('account','?')})"
            return False, d.get("error", "키움 미로그인")
        except Exception as e:
            return False, f"kiwoom-daemon 접속 불가: {e}"


def make_broker(cfg, kis_client=None):
    b = cfg.get("broker", {"type": "kis"})
    if b.get("type") == "kiwoom_gw":
        return KiwoomHTTPBroker(b.get("gateway_url", "http://127.0.0.1:8899"))
    return KISBroker(kis_client)
