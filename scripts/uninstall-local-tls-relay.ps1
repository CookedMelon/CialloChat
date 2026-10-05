param([string]$Python = "$env:USERPROFILE\anaconda3\python.exe")
$ErrorActionPreference = 'Stop'
$directory = Join-Path $env:LOCALAPPDATA 'CialloChat\tls-relay'
$target = Join-Path $directory 'local-tls-relay.py'
$configPath = Join-Path $directory 'relay.json'
$pidPath = Join-Path $directory 'relay.pid'
if (Test-Path -LiteralPath $pidPath) {
    $relayPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$relayPid"
    if ($process) {
        if (!$process.CommandLine -or !$process.CommandLine.Contains($target)) {
            throw 'Cannot verify the running relay process; no process was stopped.'
        }
        Stop-Process -Id $relayPid
    }
}
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entry = Get-ItemProperty -Path $runKey -Name 'CialloChatTlsRelay' -ErrorAction SilentlyContinue
if ($entry -and $entry.CialloChatTlsRelay.Contains($target)) {
    Remove-ItemProperty -Path $runKey -Name 'CialloChatTlsRelay'
}
$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CialloChat-Repair.lnk'
if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut }
if (Test-Path -LiteralPath $configPath) {
    & $Python $target --config $configPath --restore-routes
    if ($LASTEXITCODE -ne 0) { throw 'Could not restore the original proxy routes; see relay.log.' }
}
Write-Output 'Relay stopped; startup entry removed; original proxy configuration restored.'
