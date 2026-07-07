# Windows EC2 통합 서버 셋업 (키움 주문 + 봇 전체)

리눅스 EC2를 대체하는 단일 Windows 서버 구성. 한 서버에서 세 프로세스가 돈다:

| 프로세스 | Python | 역할 |
|---|---|---|
| `kiwoom_daemon.py` | **32bit** | 키움 OCX 래퍼 — 선물 주문/체결/취소 (localhost HTTP) |
| `autotrader` | 64bit | KIS 시세 WS + HL 시세/주문 + 전략 + 텔레그램 |
| `arb_bot.py` | 64bit | 현물 프리미엄 알림봇 (기존) |

## 1. EC2 준비

- Windows Server 2022, **서울 리전**, t3.medium 이상 (OCX+HTS가 은근 무거움), 디스크 50GB
- 보안그룹: 인바운드는 **내 IP의 RDP(3389)만**. 8899는 열지 않는다 (localhost 전용)
- 시간대: `Set-TimeZone "Korea Standard Time"` (봇은 어차피 KST 계산이지만 로그 가독성)

## 2. 소프트웨어 설치

1. **Python 64bit** (3.11+) — autotrader/arb_bot용
2. **Python 32bit** (3.10 등) — 키움용. 설치 시 경로 구분 (예: `C:\Python310-32`)
3. git, 그리고 이 레포 clone
4. **키움 OpenAPI+** 설치 (키움 홈페이지 → Open API) + **KOA Studio**(테스트용, 선택)
5. 키움 홈페이지에서 **Open API 사용 신청** + 모의투자 신청

```powershell
# 64bit venv (프로젝트 루트에서)
py -3.11 -m venv venv
venv\Scripts\pip install -r requirements.txt hyperliquid-python-sdk

# 32bit venv
C:\Python310-32\python.exe -m venv venv32
venv32\Scripts\pip install PyQt5 flask
```

## 3. 키움 자동 로그인 (핵심)

1. `venv32\Scripts\python windows\kiwoom_daemon.py` 첫 실행 → 로그인 창에서 수동 로그인
2. 로그인 후 화면 우하단 트레이의 OpenAPI 아이콘 우클릭 → **계좌비밀번호 저장** → 비번 입력 + **AUTO 체크**
3. 이후 재시작부터는 로그인 창 없이 자동 로그인됨
4. **버전처리**: 키움 업데이트가 있으면 로그인이 멈출 수 있음 → 아침 자동 재시작(아래 5번)이 처리 시도, 실패 시 텔레그램 워치독 알림으로 감지

⚠️ OCX는 **로그인된 데스크톱 세션**이 필요하다:
- EC2 → 시스템: 자동 로그온 설정 (`netplwiz`에서 "사용자 이름/암호 입력 체크 해제")
- RDP 종료 시 **로그오프하지 말고 연결 끊기(X 버튼)** — 세션이 살아 있어야 함

## 4. 환경변수 (.env — 레포 루트)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
KIS_APPKEY=...            # 시세용 (계좌 무관)
KIS_APPSECRET=...
HL_WALLET_ADDRESS=0x...
HL_PRIVATE_KEY=0x...      # API Wallet 키
KIWOOM_ACCOUNT=1234567890 # 키움 선물옵션 계좌 (daemon용, 시스템 환경변수로도 등록)
KIWOOM_GW_TOKEN=아무긴문자열  # daemon과 봇 공유 (선택)
```

`autotrader/config.json`: `"broker": {"type": "kiwoom_gw"}` 로 변경.

## 5. 상시 가동 (작업 스케줄러)

작업 스케줄러(taskschd.msc)에 3개 작업 등록. 공통: "사용자가 로그온할 때만 실행" (OCX 때문),
"가장 높은 권한", 실패 시 1분 후 재시작.

| 작업 | 트리거 | 실행 |
|---|---|---|
| kiwoom-daemon | 로그온 시 + **매일 07:30** (재시작) | `venv32\Scripts\python.exe windows\kiwoom_daemon.py` |
| autotrader | 로그온 시 + 매일 07:35 | `venv\Scripts\python.exe -m autotrader.main` |
| arb-bot | 로그온 시 | `venv\Scripts\python.exe arb_bot.py` |

07:30 재시작 이유: 키움 새벽 점검(~06:30) 후 세션 초기화 + 버전처리 반영.
(기존 재시작 명령은 `schtasks /end /tn <이름>` 후 `/run`, 또는 PowerShell 스크립트로 kill+start)

## 6. 검증 순서

1. `curl http://127.0.0.1:8899/health` → `connected: true` 확인
2. **키움 모의투자**로 데몬 단독 테스트: `/order`(1계약) → `/fill` → `/cancel`
3. autotrader `mode: monitor`로 하루 — 시세·신호 정상 확인
4. `mode: live` + 모의계좌로 진입→청산 1사이클
5. 실계좌 1계약 소액 시작

## 트러블슈팅

- `/health`가 `connected: false`: 자동로그인 풀림 — RDP 접속해 daemon 창 확인 (버전처리/비번만료)
- 주문번호 미수신: 체잔 이벤트 FID 매핑 문제 가능 — daemon 로그의 "체잔:" 라인을 확인해 리포트
- 봇에서 `gateway: Connection refused`: daemon 미기동 — 작업 스케줄러 이력 확인
