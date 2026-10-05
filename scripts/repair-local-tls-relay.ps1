param([string]$Python = "$env:USERPROFILE\anaconda3\python.exe", [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$directory = Join-Path $env:LOCALAPPDATA 'CialloChat\tls-relay'
$target = Join-Path $directory 'local-tls-relay.py'
$configPath = Join-Path $directory 'relay.json'
$pidPath = Join-Path $directory 'relay.pid'
foreach ($path in @($Python, $target, $configPath)) {
    if (!(Test-Path -LiteralPath $path)) { throw "File not found: $path. Run install-local-tls-relay.ps1 first." }
}
function Show-Checks($report) {
    foreach ($check in $report.checks) {
        $state = if ($check.ok) { 'PASS' } else { 'FAIL' }
        Write-Host "[$state] $($check.name): $($check.detail)"
    }
}
$report = (& $Python $target --config $configPath --check) | ConvertFrom-Json
Show-Checks $report
if ($report.ok) { Write-Host 'Connection is healthy. No restart or configuration change needed.'; exit 0 }
if ($CheckOnly) { exit 1 }
$settings = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
try { $live = Invoke-RestMethod "$($settings.controller)/configs" } catch {
    throw 'Start FLYCLOUD, enable TUN and rule mode, then run this shortcut again.'
}
if (!$live.tun.enable -or $live.mode -ne 'rule') {
    throw 'Enable TUN and rule mode in FLYCLOUD, then run this shortcut again.'
}
$running = $null
if (Test-Path -LiteralPath $pidPath) {
    $relayPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$relayPid"
    if ($running -and (!$running.CommandLine -or !$running.CommandLine.Contains($target))) {
        throw 'Cannot verify the recorded process. No process was stopped.'
    }
}
if ($running -and !($report.checks | Where-Object { $_.name -eq 'relay' -and $_.ok })) {
    Stop-Process -Id $relayPid
    $running = $null
}
if (!$running) {
    $backgroundPython = Join-Path (Split-Path $Python) 'pythonw.exe'
    if (!(Test-Path -LiteralPath $backgroundPython)) { $backgroundPython = $Python }
    [void](Start-Process -FilePath $backgroundPython -WindowStyle Hidden -ArgumentList @('"' + $target + '"', '--config', '"' + $configPath + '"'))
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -State Listen -LocalPort $settings.listen_port -ErrorAction SilentlyContinue) { break }
    }
}
& $Python $target --config $configPath --repair-routes
if ($LASTEXITCODE -ne 0) { throw 'Route repair failed. Check relay.log.' }
$report = (& $Python $target --config $configPath --check) | ConvertFrom-Json
Show-Checks $report
if (!$report.ok) { throw 'Local repair completed, but the connection is still failing. Keep this diagnostic output for further investigation.' }
Write-Host 'Connection repaired. Retry OBS streaming.'
