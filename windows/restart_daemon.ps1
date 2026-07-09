# 키움 데몬 재시작 (로그는 앱이 직접 logs\daemon.log에 이어씀)
$root = "C:\hynix_samsung_premium"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*kiwoom_daemon*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 3

Start-Process -FilePath "$root\venv32\Scripts\python.exe" `
  -ArgumentList "windows\kiwoom_daemon.py" `
  -WorkingDirectory $root `
  -WindowStyle Minimized
