# 작업 스케줄러 등록 — 관리자 PowerShell에서 1회 실행
# 로그온 시 + 매일 아침(키움 점검 후) 자동 재시작. "로그온한 사용자로만 실행" (OCX 요건).
$root = "C:\hynix_samsung_premium"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

$ps = "powershell.exe"
$daemonCmd = "-ExecutionPolicy Bypass -File $root\windows\restart_daemon.ps1"
$botCmd = "-ExecutionPolicy Bypass -File $root\windows\restart_bot.ps1"

# 기존 등록 제거 (재실행 안전)
foreach ($t in "kiwoom-daemon-logon", "kiwoom-daemon-0730", "autotrader-logon", "autotrader-0735") {
  schtasks /delete /tn $t /f 2>$null
}

# 로그온 시 자동 시작
schtasks /create /tn "kiwoom-daemon-logon" /tr "$ps $daemonCmd" /sc onlogon /f
schtasks /create /tn "autotrader-logon"   /tr "$ps $botCmd"    /sc onlogon /delay 0001:00 /f

# 매일 아침 재시작 (키움 새벽 점검 후 세션 초기화 + 버전처리 흡수)
schtasks /create /tn "kiwoom-daemon-0730" /tr "$ps $daemonCmd" /sc daily /st 07:30 /f
schtasks /create /tn "autotrader-0735"    /tr "$ps $botCmd"    /sc daily /st 07:35 /f

Write-Host "`n등록 완료. 확인:"
schtasks /query /fo table | Select-String "kiwoom|autotrader"
