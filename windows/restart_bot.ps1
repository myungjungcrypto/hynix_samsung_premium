# autotrader 재시작 (기존 프로세스 종료 후 기동)
$root = "C:\hynix_samsung_premium"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*autotrader.main*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 3

Start-Process -FilePath "$root\venv\Scripts\python.exe" `
  -ArgumentList "-m autotrader.main" `
  -WorkingDirectory $root `
  -WindowStyle Minimized `
  -RedirectStandardOutput "$root\logs\autotrader.log" `
  -RedirectStandardError "$root\logs\autotrader.err.log"
