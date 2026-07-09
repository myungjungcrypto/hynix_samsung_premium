# autotrader 재시작 (로그는 앱이 직접 logs\autotrader.log에 이어씀)
$root = "C:\hynix_samsung_premium"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*autotrader.main*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 3

Start-Process -FilePath "$root\venv\Scripts\python.exe" `
  -ArgumentList "-m", "autotrader.main" `
  -WorkingDirectory $root `
  -WindowStyle Minimized
