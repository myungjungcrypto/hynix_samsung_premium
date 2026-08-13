# 워치독 스케줄 등록 — 관리자 PowerShell에서 1회 실행
$root = "C:\hynix_samsung_premium"

schtasks /delete /tn "trading-watchdog" /f 2>$null
schtasks /create /tn "trading-watchdog" `
  /tr "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File $root\windows\watchdog.ps1" `
  /sc minute /mo 5 /f

Write-Host "등록 완료 — 5분마다 데몬/봇 생존 확인, 다운 시 자동 재기동 + 텔레그램 통보"
schtasks /query /tn "trading-watchdog"
