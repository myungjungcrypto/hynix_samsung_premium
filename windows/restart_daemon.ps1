# 키움 데몬 재시작 (기존 프로세스 종료 후 기동)
$root = "C:\hynix_samsung_premium"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*kiwoom_daemon*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 3

Start-Process -FilePath "$root\venv32\Scripts\python.exe" `
  -ArgumentList "windows\kiwoom_daemon.py" `
  -WorkingDirectory $root `
  -WindowStyle Minimized `
  -RedirectStandardOutput "$root\logs\daemon.log" `
  -RedirectStandardError "$root\logs\daemon.err.log"
