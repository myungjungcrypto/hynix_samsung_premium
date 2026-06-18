# trade.xyz ↔ KRX 프리미엄 재정거래 알림 봇

trade.xyz(Hyperliquid xyz dex)의 SK하이닉스(`xyz:SKHX`)·삼성전자(`xyz:SMSN`) 무기한 선물 가격을
**실제 USD/KRW 환율**(테더 환율 아님)로 원화 환산한 뒤, KRX 현물가 대비 프리미엄을 계산해서
텔레그램으로 진입/청산 알림을 보낸다.

- **진입 알림**: 프리미엄(perp bid × 환율 / 현물가 − 1) ≥ `entry_threshold` (기본 +1.0%)
  → trade.xyz 숏 + 현물 매수
- **청산 알림**: 진입 상태에서 프리미엄(perp ask 기준) ≤ `exit_threshold` (기본 +0.1%)
  → 숏 청산 + 현물 매도
- **현물 체결이 가능한 시간에만 모니터링** — 현물을 살 수 없는 시간에는 알림 없음.
  - KRX 정규장: 평일 09:00~15:30
  - NXT(넥스트레이드) 프리마켓 08:00~08:50 / 애프터마켓 15:30~20:00 — 이 시간대에는 **NXT 체결가**를 현물가로 사용
  - 모니터링 창은 평일 08:00~20:00 KST(휴장일 제외)이고, 그 안에서도 네이버 세션 상태
    (정규장 OPEN 또는 NXT OPEN)를 확인하므로 동시호가 갭(08:50~09:00 등)·임시휴장은 자동으로 걸러진다.
  - 알림과 `/status`에 어느 세션 가격인지(`정규장` / `NXT 프리마켓` / `NXT 애프터마켓`) 표시된다.

## 데이터 소스

| 데이터 | 소스 |
|---|---|
| perp 호가 (bid/ask) | Hyperliquid `/info` `l2Book` (`xyz:SKHX`, `xyz:SMSN`) |
| 현물가 | 네이버 증권 실시간 폴링 API (000660, 005930) — 정규장 가격 + NXT 시간대는 `overMarketPriceInfo` |
| USD/KRW 환율 | Yahoo Finance `KRW=X` (실시간 인터뱅크), 실패 시 네이버 하나은행 고시 환율로 폴백 |

## 텔레그램 봇 준비

1. 텔레그램에서 `@BotFather` → `/newbot` → 토큰 발급
2. 만든 봇에게 아무 메시지나 한번 보낸 뒤:
   ```
   curl "https://api.telegram.org/bot<토큰>/getUpdates"
   ```
   응답의 `message.chat.id` 가 `TELEGRAM_CHAT_ID`.

## EC2 배포 (Amazon Linux 2023, pm2)

```bash
ssh -i <키>.pem ec2-user@<EC2_IP>

# 코드 받기 (깃헙이 저장소 — 업데이트는 git pull)
git clone https://github.com/myungjungcrypto/hynix_samsung_premium.git
cd hynix_samsung_premium
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 환경변수 설정 (.env 는 봇이 직접 읽음)
cp .env.example .env
vi .env   # 토큰, chat id 입력
chmod 600 .env

# 동작 테스트 (텔레그램으로 "봇 시작됨" 메시지가 와야 함)
venv/bin/python arb_bot.py
# Ctrl+C 로 종료 후 pm2 등록

# pm2 등록 (크래시 시 자동 재시작)
pm2 start ecosystem.config.js
pm2 save                 # 재부팅 후에도 자동 시작 (pm2 startup 설정이 안 돼 있으면 안내대로 한번 실행)

# 로그 확인
pm2 logs arb-bot
pm2 status
```

업데이트 배포:

```bash
cd ~/hynix_samsung_premium && git pull && pm2 restart arb-bot
```

서버 시간대는 무관하다(코드가 항상 `Asia/Seoul` 기준으로 판단).

## 텔레그램 명령

슬래시는 붙여도 되고 안 붙여도 된다 (`status` = `/status`).

| 명령 | 동작 |
|---|---|
| `status` | 현재가(perp 원화환산/현물)·프리미엄·환율·포지션 상태 조회 (장외에도 동작, 현물은 마지막 체결가) |
| `open SKHX` | 진입 완료로 표시 → 이후 청산 알림 활성화 |
| `flat SKHX` | 청산 완료로 표시 → 이후 진입 알림 활성화 |

진입/청산 상태는 **자동 전환**된다: 진입 알림이 뜨면 봇이 자동으로 `open` 상태가 되어
이후 청산 알림을 감시하고, 청산 알림이 뜨면 자동으로 `flat`이 되어 다음 진입을 감시한다.
실제로는 진입하지 않았는데 알림만 떴다면 `flat SKHX`로, 아직 보유 중인데 청산 알림이 떴다면
`open SKHX`로 수동 보정하면 된다. 상태는 `state.json`에 저장되어 재시작해도 유지된다.

## 설정 (`config.json`)

- `entry_threshold` / `exit_threshold`: 종목별 진입/청산 프리미엄 기준 (소수, 0.01 = 1%)
- `poll_interval_sec`: 폴링 주기 (기본 15초)
- `alert_cooldown_sec`: 같은 알림 반복 최소 간격 (기본 30분)
- `krx_holidays`: KRX 휴장일 목록. **연말에 다음 해 휴장일을 KRX 공지 기준으로 갱신할 것**
  (대체공휴일·임시공휴일은 매년 달라짐). 현재 2026년 잔여 휴장일이 들어있다.

## 주의

- 알림 전용이며 자동 주문은 하지 않는다.
- NXT 프리/애프터마켓은 정규장보다 유동성이 얕아 표시 가격에 다 체결되지 않을 수 있다.
  NXT 시간대 알림은 호가를 직접 확인하고 진입할 것.
- 프리미엄은 perp 체결가능가(진입=bid, 청산=ask) 기준으로 보수적으로 계산하지만,
  현물 호가 스프레드·수수료·환율 변동(USD 숏 노출)은 반영하지 않는다.
- 네이버 시세는 비공식 API라 스키마가 바뀔 수 있다. 5회 연속 조회 실패 시 텔레그램으로 오류 알림이 온다.
