# 키움 데몬 재시작 (기존 프로세스 종료 후 기동, 로그는 이어쓰기)
$root = "C:\hynix_samsung_premium"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*kiwoom_daemon*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 3

Start-Process -FilePath "cmd.exe" `
  -ArgumentList "/c", "`"$root\venv32\Scripts\python.exe`" windows\kiwoom_daemon.py >> `"$root\logs\daemon.log`" 2>&1" `
  -WorkingDirectory $root `
  -WindowStyle Minimized
