"""KIS 국내 주식선물 클라이언트 — 인증 / 실시간 호가 WS / REST 호가 / 주문.

TR 정리 (2026-07 실측·공식예제 확인):
  - REST 호가:  GET /uapi/domestic-futureoption/v1/quotations/inquire-asking-price
                tr_id FHMIF10010000, FID_COND_MRKT_DIV_CODE=JF, FID_INPUT_ISCD=A50607
  - REST 현재가: GET .../quotations/inquire-price, tr_id FHMIF10000000
  - WS 실시간 호가: H0ZFASP0 (tr_key=종목코드) — split('^') 후
                idx0=종목코드, idx1=시각, idx2~11=매도1~10호가, idx12~21=매수1~10호가
  - 주문: POST /uapi/domestic-futureoption/v1/trading/order
                tr_id TTTO1101U(실전 주간) / VTTO1101U(모의)
"""
import asyncio
import json
import logging
import os
import re
import time

import requests
import websockets

log = logging.getLogger("kis")

BASE = "https://openapi.koreainvestment.com:9443"
WS_URL = "ws://ops.koreainvestment.com:21000"


def load_keys(secrets_path):
    """환경변수 KIS_APPKEY/KIS_APPSECRET 우선, 없으면 oil 봇 secrets.yaml에서 로드."""
    key = os.environ.get("KIS_APPKEY", "")
    sec = os.environ.get("KIS_APPSECRET", "")
    acct = os.environ.get("KIS_ACCOUNT", "")
    if key and sec:
        return key, sec, acct
    path = os.path.expanduser(secrets_path)
    if os.path.exists(path):
        text = open(path).read()
        blk = text.split("kis:")[1] if "kis:" in text else text
        m_key = re.search(r'app_key:\s*["\']?([^"\'\n]+)', blk)
        m_sec = re.search(r'app_secret:\s*["\']?([^"\'\n]+)', blk)
        m_acct = re.search(r'account_number:\s*["\']?([^"\'\n#]+)', blk)
        if m_key and m_sec:
            return (m_key.group(1).strip(), m_sec.group(1).strip(),
                    acct or (m_acct.group(1).strip() if m_acct else ""))
    raise RuntimeError("KIS 키 없음: KIS_APPKEY/KIS_APPSECRET 또는 secrets.yaml 확인")


class KISClient:
    def __init__(self, app_key, app_secret, account=""):
        self.key = app_key
        self.sec = app_secret
        self.account = account  # "12345678-01"
        self._token = ""
        self._token_exp = 0.0
        self._approval = ""

    # ---------------- auth ----------------
    def token(self):
        if self._token and time.time() < self._token_exp:
            return self._token
        r = requests.post(f"{BASE}/oauth2/tokenP", json={
            "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.sec,
        }, timeout=15)
        d = r.json()
        if "access_token" not in d:
            raise RuntimeError(f"KIS 토큰 실패: {d}")
        self._token = d["access_token"]
        self._token_exp = time.time() + 23 * 3600
        log.info("KIS access_token 발급 (23h)")
        return self._token

    def approval_key(self):
        if self._approval:
            return self._approval
        r = requests.post(f"{BASE}/oauth2/Approval", json={
            "grant_type": "client_credentials", "appkey": self.key, "secretkey": self.sec,
        }, timeout=15)
        self._approval = r.json()["approval_key"]
        return self._approval

    def _headers(self, tr_id):
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.key, "appsecret": self.sec,
            "tr_id": tr_id, "custtype": "P",
        }

    # ---------------- REST 시세 ----------------
    def asking_price(self, code):
        """(bid1, ask1, bid_qty1, ask_qty1) — 폴백/기동 시 초기값용."""
        r = requests.get(
            f"{BASE}/uapi/domestic-futureoption/v1/quotations/inquire-asking-price",
            headers=self._headers("FHMIF10010000"),
            params={"FID_COND_MRKT_DIV_CODE": "JF", "FID_INPUT_ISCD": code}, timeout=10)
        d = r.json()
        if d.get("rt_cd") != "0":
            raise RuntimeError(f"호가 조회 실패: {d.get('msg1')}")
        o = d["output2"]
        return (float(o["futs_bidp1"]), float(o["futs_askp1"]),
                int(o["bidp_rsqn1"]), int(o["askp_rsqn1"]))

    # ---------------- 주문 (Phase 3에서 사용) ----------------
    def order(self, code, side, qty, price=0, paper=False):
        """주식선물 주문. side: 'buy'|'sell', price=0이면 시장가.

        반환: (성공여부, 주문번호 or 에러메시지)
        """
        if not self.account or "-" not in self.account:
            return False, "계좌번호(KIS_ACCOUNT, 예 12345678-01) 미설정"
        cano, prdt = self.account.split("-")
        tr_id = "VTTO1101U" if paper else "TTTO1101U"
        body = {
            "ORD_PRCS_DVSN_CD": "02",
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "SLL_BUY_DVSN_CD": "02" if side == "buy" else "01",
            "SHTN_PDNO": code,
            "ORD_QTY": str(qty),
            "UNIT_PRICE": str(price),
            "NMPR_TYPE_CD": "02" if price == 0 else "01",
            "KRX_NMPR_CNDT_CD": "0",
            "ORD_DVSN_CD": "02" if price == 0 else "01",
        }
        r = requests.post(f"{BASE}/uapi/domestic-futureoption/v1/trading/order",
                          headers=self._headers(tr_id), json=body, timeout=10)
        d = r.json()
        if d.get("rt_cd") == "0":
            return True, d.get("output", {}).get("ODNO", "")
        return False, d.get("msg1", str(d))

    def filled_qty(self, odno, paper=False):
        """주문번호의 총 체결수량 조회 (inquire-ccnl). 실패 시 None."""
        if not self.account or "-" not in self.account:
            return None
        cano, prdt = self.account.split("-")
        today = time.strftime("%Y%m%d")
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "STRT_ORD_DT": today, "END_ORD_DT": today,
            "SLL_BUY_DVSN_CD": "00", "CCLD_NCCS_DVSN": "00",
            "SORT_SQN": "DS", "STRT_ODNO": "",
            "PDNO": "", "MKET_ID_CD": "",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }
        try:
            r = requests.get(f"{BASE}/uapi/domestic-futureoption/v1/trading/inquire-ccnl",
                             headers=self._headers("VTTO5201R" if paper else "TTTO5201R"),
                             params=params, timeout=10)
            d = r.json()
            if d.get("rt_cd") != "0":
                log.warning("체결조회 실패: %s", d.get("msg1"))
                return None
            for row in d.get("output1", []):
                if str(row.get("odno", row.get("ODNO", ""))).lstrip("0") == str(odno).lstrip("0"):
                    for k in ("tot_ccld_qty", "ccld_qty", "TOT_CCLD_QTY"):
                        if k in row:
                            return int(float(row[k]))
                    log.warning("체결수량 필드 미확인, row=%s", row)
                    return None
            return 0  # 주문번호 미발견 = 아직 반영 전
        except Exception as e:
            log.warning("체결조회 오류: %s", e)
            return None

    def cancel(self, odno, code, qty, paper=False):
        """미체결 주문 취소 (order-rvsecncl)."""
        if not self.account or "-" not in self.account:
            return False, "계좌 미설정"
        cano, prdt = self.account.split("-")
        body = {
            "ORD_PRCS_DVSN_CD": "02", "CANO": cano, "ACNT_PRDT_CD": prdt,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORGN_ODNO": str(odno), "ORD_QTY": str(qty),
            "UNIT_PRICE": "0", "NMPR_TYPE_CD": "02",
            "KRX_NMPR_CNDT_CD": "0", "RMN_QTY_YN": "Y",
            "FUOP_ITEM_DVSN_CD": "", "ORD_DVSN_CD": "02",
        }
        tr_id = "VTTO1103U" if paper else "TTTO1103U"
        try:
            r = requests.post(f"{BASE}/uapi/domestic-futureoption/v1/trading/order-rvsecncl",
                              headers=self._headers(tr_id), json=body, timeout=10)
            d = r.json()
            return d.get("rt_cd") == "0", d.get("msg1", "")
        except Exception as e:
            return False, str(e)


class KISQuoteStream:
    """H0ZFASP0 실시간 호가 구독. on_quote(code, bid, ask) 콜백."""

    def __init__(self, client: KISClient, codes, on_quote):
        self.client = client
        self.codes = codes
        self.on_quote = on_quote
        self.last_msg_ts = 0.0

    async def run(self):
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    for code in self.codes:
                        await ws.send(json.dumps({
                            "header": {"approval_key": self.client.approval_key(),
                                       "custtype": "P", "tr_type": "1",
                                       "content-type": "utf-8"},
                            "body": {"input": {"tr_id": "H0ZFASP0", "tr_key": code}},
                        }))
                    log.info("KIS WS 연결, 구독: %s", self.codes)
                    async for raw in ws:
                        self.last_msg_ts = time.time()
                        await self._handle(ws, raw)
            except Exception as e:
                log.warning("KIS WS 재접속 (%s)", e)
                await asyncio.sleep(5)

    async def _handle(self, ws, raw):
        if raw.startswith("{"):
            msg = json.loads(raw)
            tr = msg.get("header", {}).get("tr_id", "")
            if tr == "PINGPONG":
                await ws.send(raw)  # echo
            else:
                log.info("KIS WS 제어: %s", msg.get("body", {}).get("msg1", raw[:120]))
            return
        # 실시간 데이터: "0|H0ZFASP0|001|f1^f2^..."
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != "H0ZFASP0":
            return
        f = parts[3].split("^")
        try:
            code, ask1, bid1 = f[0], float(f[2]), float(f[12])
        except (IndexError, ValueError):
            log.warning("H0ZFASP0 파싱 실패: %s", raw[:150])
            return
        if bid1 > 0 and ask1 > 0:
            self.on_quote(code, bid1, ask1)
