#!/usr/bin/env python3
"""HL 주문 경로 실검증 — 봇과 동일한 방식으로 초소형 주문 왕복.

xyz:SMSN 1주 숏 → 즉시 커버. 수수료 몇 센트로 다음을 한 번에 검증:
  1) 새 API Wallet 키 서명이 실제 주문에서 통하는지
  2) 스팟 잔고가 API 주문에서도 증거금으로 쓰이는지 (통합 잔고 여부)
     - 성공하면 이체 불필요 확정
     - "Insufficient margin"이면 Spot→trade.xyz 이체가 필요하다는 확정

실행 (윈도우): venv\\Scripts\\python windows\\hl_test_order.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from autotrader.main import load_dotenv
from autotrader.hl import HLTrader

COIN = "xyz:SMSN"
SIZE = 1.0

load_dotenv()
wallet = os.environ.get("HL_WALLET_ADDRESS", "")
pkey = os.environ.get("HL_PRIVATE_KEY", "")
if not wallet or not pkey:
    sys.exit("HL_WALLET_ADDRESS / HL_PRIVATE_KEY 를 .env에 설정하세요")

hl = HLTrader(wallet, pkey, leverage=3, perp_dexs=["xyz"])
if not hl.exchange:
    sys.exit("SDK 미설치")

ok, msg = hl.can_trade(COIN)
print(f"[1] 자산 매핑: {ok} ({msg})")

print(f"[2] {COIN} {SIZE}주 시장가 숏 시도...")
ok, res = hl.market_order(COIN, is_buy=False, size=SIZE)
print(f"    결과: ok={ok} {res}")
if not ok:
    print("\n→ 주문 거부. 사유가 margin이면 Spot→trade.xyz 이체 필요 확정.")
    sys.exit(1)

print(f"[3] 즉시 커버(청산)...")
ok2, res2 = hl.market_close(COIN, SIZE)
print(f"    결과: ok={ok2} {res2}")
if not ok2:
    print("⚠️ 커버 실패 — trade.xyz 앱에서 SMSN 숏 1주를 수동으로 닫아주세요!")
    sys.exit(1)

print("\n✅ 왕복 성공 — API 키·주문 경로 정상, 이체 불필요(통합 잔고가 API에도 적용됨)")
print("   xyz 계좌 잔고 조회:", hl.balance("xyz"))
