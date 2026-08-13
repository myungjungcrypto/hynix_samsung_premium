# 자가치유 워치독 — 5분마다 실행 (스케줄 작업)
# 데몬/봇이 죽어있으면 재기동하고 텔레그램으로 통보. 메모리 잔량도 기록.
$root = "C:\hynix_samsung_premium"
$log = "$root\logs\watchdog.log"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# .env에서 텔레그램 설정 로드
$tgToken = ""; $tgChat = ""
if (Test-Path "$root\.env") {
    foreach ($line in Get-Content "$root\.env" -Encoding UTF8) {
        if ($line -match '^TELEGRAM_BOT_TOKEN=(.+)$') { $tgToken = $Matches[1].Trim() }
        if ($line -match '^TELEGRAM_CHAT_ID=(.+)$') { $tgChat = $Matches[1].Trim() }
    }
}
function Notify($msg) {
    Log "TG: $msg"
    if ($tgToken -and $tgChat) {
        try {
            Invoke-RestMethod -Uri "https://api.telegram.org/bot$tgToken/sendMessage" `
                -Method Post -Body @{ chat_id = $tgChat; text = "🛡️ [워치독] $msg" } -TimeoutSec 10 | Out-Null
        } catch { Log "TG 전송 실패: $_" }
    }
}

# 메모리 잔량 기록 (원인 추적용 — 고갈 추세면 여기 남음)
$os = Get-CimInstance Win32_OperatingSystem
$freeMB = [math]::Round($os.FreePhysicalMemory / 1024)
$totalMB = [math]::Round($os.TotalVisibleMemorySize / 1024)

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
$daemonUp = ($procs | Where-Object { $_.CommandLine -like "*kiwoom_daemon*" }) -ne $null
$botUp = ($procs | Where-Object { $_.CommandLine -like "*autotrader.main*" }) -ne $null

# 데몬은 프로세스 + health 응답까지 확인
$daemonHealthy = $false
if ($daemonUp) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8899/health" -TimeoutSec 5
        $daemonHealthy = $h.ok
    } catch { $daemonHealthy = $false }
}

Log "check: daemon=$daemonUp/$daemonHealthy bot=$botUp mem=${freeMB}/${totalMB}MB"

if (-not $daemonUp -or -not $daemonHealthy) {
    Notify "키움 데몬 다운 감지 (proc=$daemonUp health=$daemonHealthy, 가용메모리 ${freeMB}MB) → 재기동"
    powershell -ExecutionPolicy Bypass -File "$root\windows\restart_daemon.ps1"
    Start-Sleep -Seconds 25
}

if (-not $botUp) {
    Notify "autotrader 다운 감지 (가용메모리 ${freeMB}MB) → 재기동"
    powershell -ExecutionPolicy Bypass -File "$root\windows\restart_bot.ps1"
}

# 메모리 위험 수위 경고 (재발 원인 후보 추적)
if ($freeMB -lt 400) {
    Notify "⚠️ 가용 메모리 ${freeMB}MB — 고갈 임박. 프로세스 상위:"
    $top = Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 |
        ForEach-Object { "$($_.ProcessName) $([math]::Round($_.WorkingSet64/1MB))MB" }
    Notify ($top -join ", ")
}
