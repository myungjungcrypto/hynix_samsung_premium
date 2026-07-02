#!/usr/bin/env python3
"""KIS 국내 주식선물 시세 프로브 — Phase 1 실측용.

하이닉스 주식선물 호가·현재가를 KIS REST로 조회하고,
현물(네이버)·perp(Hyperliquid)·환율(Yahoo)과 3중 비교한다.

확인 목적:
  1. KIS 국내선물옵션 시세 API 동작 (appkey 권한 포함)
  2. 주식선물 종목코드 형식 (A50607 vs 50607)
  3. 최근월물 호가 스프레드·잔량 (유동성) — 장중(08:45~15:45)에 실행해야 의미 있음
  4. 선물 베이시스(선물/현물−1)와 perp 프리미엄(perp×환율/선물−1) 실측

사용 (EC2, oil 봇 키 재사용):
    venv/bin/python scripts/kis_futures_probe.py
    venv/bin/python scripts/kis_futures_probe.py --code A11607   # 삼성전자 선물

키 로드 순서: 환경변수 KIS_APPKEY/KIS_APPSECRET → ~/rwa_arbitrage/config/secrets.yaml
(참고: KIS는 유효기간 내 토큰 재요청 시 동일 토큰을 반환하므로 oil 봇과 동시 사용 안전)
"""
import argparse
import json
import os
import re
import sys
import urllib.request

BASE = "https://openapi.koreainvestment.com:9443"
HL_INFO = "https://api.hyperliquid.xyz/info"
UA = "Mozilla/5.0 (probe)"


def load_keys(secrets_path):
    key = os.environ.get("KIS_APPKEY", "")
    sec = os.environ.get("KIS_APPSECRET", "")
    if key and sec:
        return key, sec
    path = os.path.expanduser(secrets_path)
    if os.path.exists(path):
        text = open(path).read()
        kis_block = text.split("kis:")[1] if "kis:" in text else text
        m_key = re.search(r'app_key:\s*["\']?([^"\'\n]+)', kis_block)
        m_sec = re.search(r'app_secret:\s*["\']?([^"\'\n]+)', kis_block)
        if m_key and m_sec:
            return m_key.group(1).strip(), m_sec.group(1).strip()
    sys.exit("KIS 키를 찾을 수 없음: KIS_APPKEY/KIS_APPSECRET 환경변수 또는 --secrets 경로 확인")


def http_json(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method,
                                 data=json.dumps(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_token(key, sec):
    d = http_json(f"{BASE}/oauth2/tokenP",
                  headers={"Content-Type": "application/json"},
                  data={"grant_type": "client_credentials", "appkey": key, "appsecret": sec})
    if "access_token" not in d:
        sys.exit(f"토큰 발급 실패: {d}")
    return d["access_token"]


def kis_get(path, tr_id, params, token, key, sec):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": key, "appsecret": sec, "tr_id": tr_id, "custtype": "P",
    }
    return http_json(f"{BASE}{path}?{qs}", headers=headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="A50607", help="주식선물 종목코드 (기본: SK하이닉스 2026-07물)")
    ap.add_argument("--spot", default="000660", help="기초자산 현물 코드")
    ap.add_argument("--perp", default="xyz:SKHX", help="Hyperliquid perp 코인")
    ap.add_argument("--secrets", default="~/rwa_arbitrage/config/secrets.yaml")
    a = ap.parse_args()

    key, sec = load_keys(a.secrets)
    token = get_token(key, sec)
    print(f"[1] 토큰 OK")

    # 종목코드 형식 후보 순회
    candidates = [a.code, a.code.lstrip("A")]
    got = None
    for code in candidates:
        try:
            d = kis_get("/uapi/domestic-futureoption/v1/quotations/inquire-asking-price",
                        "FHMIF10010000",
                        {"FID_COND_MRKT_DIV_CODE": "JF", "FID_INPUT_ISCD": code},
                        token, key, sec)
            if d.get("rt_cd") == "0":
                got = code
                print(f"[2] 호가 조회 OK (종목코드 형식: {code})")
                print("=== 호가 output1 ===")
                print(json.dumps(d.get("output1", {}), ensure_ascii=False, indent=1))
                print("=== 호가 output2 ===")
                print(json.dumps(d.get("output2", {}), ensure_ascii=False, indent=1))
                break
            print(f"    {code}: rt_cd={d.get('rt_cd')} msg={d.get('msg1')}")
        except Exception as e:
            print(f"    {code}: 오류 {e}")
    if not got:
        sys.exit("호가 조회 실패 — tr_id/코드형식/권한 확인 필요")

    # 현재가 (거래량·미결제 확인)
    try:
        d = kis_get("/uapi/domestic-futureoption/v1/quotations/inquire-price",
                    "FHMIF10000000",
                    {"FID_COND_MRKT_DIV_CODE": "JF", "FID_INPUT_ISCD": got},
                    token, key, sec)
        print("[3] 현재가 조회:", "OK" if d.get("rt_cd") == "0" else d.get("msg1"))
        for k in ("output1", "output2", "output3"):
            if d.get(k):
                print(f"=== 현재가 {k} ===")
                print(json.dumps(d[k], ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"[3] 현재가 조회 오류: {e}")

    # 3중 비교: 현물(네이버) + perp(HL) + 환율(Yahoo)
    try:
        spot_d = http_json(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{a.spot}",
                           headers={"User-Agent": UA})["datas"][0]
        spot = float(spot_d["closePrice"].replace(",", ""))
        book = http_json(HL_INFO, headers={"Content-Type": "application/json"},
                         data={"type": "l2Book", "coin": a.perp}, method="POST")
        hb = float(book["levels"][0][0]["px"]); ha = float(book["levels"][1][0]["px"])
        fx = http_json("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=1d&interval=1m",
                       headers={"User-Agent": UA})["chart"]["result"][0]["meta"]["regularMarketPrice"]
        perp_mid = (hb + ha) / 2 * fx
        print(f"\n[4] 3중 비교 (참고: 장외시간이면 현물·선물은 마지막 체결가)")
        print(f"    현물(네이버) {spot:,.0f}원 / perp mid {perp_mid:,.0f}원 (환율 {fx:,.1f})")
        print(f"    perp vs 현물 프리미엄: {(perp_mid/spot-1)*100:+.2f}%")
        print(f"    → 위 호가 output의 선물 매도/매수호가와 비교해 선물 베이시스·perp vs 선물 프리미엄 계산")
    except Exception as e:
        print(f"[4] 비교 데이터 오류: {e}")


if __name__ == "__main__":
    main()
