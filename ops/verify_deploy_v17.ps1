# 部署自验证:每日 08:30 Chanlun-QMT-DailyRestart 重启后,验证 web 服务已载入 v17 新口径。
# 用法:手动跑 powershell -NoProfile -ExecutionPolicy Bypass -File 本文件;全过输出 DEPLOY-OK。
$ok = $true
$p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
     Where-Object { $_.CommandLine -match 'chanlun_chart' } | Select-Object -First 1
if ($null -eq $p) { Write-Output 'FAIL: web app.py 进程不存在'; $ok = $false }
else {
    try {
        $st = [Management.ManagementDateTimeConverter]::ToDateTime($p.CreationDate)
        $gate = (Get-Date).Date.AddHours(8.5)
        if ((Get-Date) -gt $gate -and $st -lt $gate) { Write-Output "WARN: 进程启动于 $st 早于今日 08:30(可能未重启)"; $ok = $false }
        else { Write-Output "OK: web 进程 PID=$($p.ProcessId) 启动于 $st" }
    } catch { Write-Output "OK: web 进程 PID=$($p.ProcessId)(启动时间解析失败,忽略)" }
}
try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9900/' -UseBasicParsing -TimeoutSec 10; Write-Output "OK: 9900 响应 $($r.StatusCode)" }
catch { Write-Output "FAIL: 9900 无响应"; $ok = $false }
Set-Location D:\project\chanlun-pro
$has = git log --oneline -20 | Select-String -Pattern '52959b2d|3d54b0b2'
if ($has) { Write-Output 'OK: HEAD 含 v17 口径提交链' } else { Write-Output 'FAIL: HEAD 不含 v17 提交'; $ok = $false }
$v = Select-String -Path src\chanlun\recursive_bt\backtest\live_backtest.py -Pattern '_SIGNAL_CACHE_VERSION = "(v\d+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
Write-Output "INFO: 信号缓存版本=$v (期望 v17)"
if ($v -ne 'v17') { $ok = $false }
if ($ok) { Write-Output 'DEPLOY-OK' } else { Write-Output 'DEPLOY-CHECK-FAILED' }