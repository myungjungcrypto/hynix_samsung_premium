#!/usr/bin/env python3
"""KRX/NXT 세션 전환·체결 여부 검증용 폴링 로거.

지정한 KST 시간 창 동안 네이버 폴링 API를 10초 간격으로 기록한다.
체결 여부는 accumulatedTradingVolume 증가 + localTradedAt 갱신으로 판별.

사용 (EC2):
    nohup venv/bin/python scripts/session_probe.py 0750 0910 logs/probe_am.jsonl > /dev/null 2>&1 &
    # 아침 07:50~09:10: NXT 프리마켓 개장(08:00) stale 여부 + 08:50~09:00 갭 확인
"""
import json
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CODES = ["000660", "005930"]

start_hm = sys.argv[1] if len(sys.argv) > 1 else "0750"
end_hm = sys.argv[2] if len(sys.argv) > 2 else "0910"
out_path = sys.argv[3] if len(sys.argv) > 3 else f"probe_{start_hm}.jsonl"


def now():
    return datetime.now(KST)


def fetch(code):
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["datas"][0]


while now().strftime("%H%M") < start_hm:
    time.sleep(15)

with open(out_path, "w") as f:
    while now().strftime("%H%M") <= end_hm:
        for code in CODES:
            try:
                d = fetch(code)
                over = d.get("overMarketPriceInfo") or {}
                rec = {
                    "t": now().strftime("%H:%M:%S"),
                    "code": code,
                    "mStat": d.get("marketStatus"),
                    "close": d.get("closePrice"),
                    "mVol": d.get("accumulatedTradingVolume"),
                    "mTradedAt": (d.get("localTradedAt") or "")[11:19],
                    "sess": over.get("tradingSessionType"),
                    "oStat": over.get("overMarketStatus"),
                    "oPrice": over.get("overPrice"),
                    "oVol": over.get("accumulatedTradingVolume"),
                    "oTradedAt": (over.get("localTradedAt") or "")[11:19],
                }
            except Exception as e:
                rec = {"t": now().strftime("%H:%M:%S"), "code": code, "err": str(e)}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        time.sleep(10)

print("probe done:", out_path)
