param(
    [string]$Python = "$env:USERPROFILE\anaconda3\pythonw.exe",
    [string]$MihomoConfig = "$env:APPDATA\FLYCLOUD\FLYCLOUD\config.yaml",
    [string]$Controller = 'http://127.0.0.1:9090',
    [string]$ServerName = 'chat.v50to.cc',
    [string]$UpstreamAddress = '120.55.167.91',
    [int]$ListenPort = 19443
)
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'local-tls-relay.py'
foreach ($path in @($Python, $MihomoConfig, $source,
        (Join-Path $PSScriptRoot 'repair-local-tls-relay.ps1'),
        (Join-Path $PSScriptRoot 'uninstall-local-tls-relay.ps1'))) {
    if (!(Test-Path -LiteralPath $path)) { throw "File not found: $path" }
}
[void][Net.IPAddress]::Parse($UpstreamAddress)
[void](Invoke-RestMethod "$Controller/version")
$directory = Join-Path $env:LOCALAPPDATA 'CialloChat\tls-relay'
[void](New-Item -ItemType Directory -Force -Path $directory)
$target = Join-Path $directory 'local-tls-relay.py'
$configPath = Join-Path $directory 'relay.json'
$pidPath = Join-Path $directory 'relay.pid'
if (Test-Path -LiteralPath $pidPath) {
    $oldPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $oldProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid"
    if ($oldProcess.CommandLine -and $oldProcess.CommandLine.Contains($target)) {
        Stop-Process -Id $oldPid
    }
}
$secretBytes = New-Object byte[] 32
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
$generator.GetBytes($secretBytes)
$generator.Dispose()
$settings = @{
    listen_port = $ListenPort
    username = 'ciallochat'
    password = [Convert]::ToBase64String($secretBytes)
    upstream_ip = $UpstreamAddress
    server_name = $ServerName
    allowed_ports = @(443, 15347)
    controller = $Controller
    mihomo_config = $MihomoConfig
    pid_file = $pidPath
}
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($configPath, ($settings | ConvertTo-Json), $utf8)
Copy-Item -LiteralPath $source -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'repair-local-tls-relay.ps1') -Destination $directory -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall-local-tls-relay.ps1') -Destination $directory -Force
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'CialloChat-Repair.lnk'))
$consolePython = Join-Path (Split-Path $Python) 'python.exe'
$launcher = Join-Path $directory 'repair-local-tls-relay.cmd'
$launcherText = "@echo off`r`nchcp 65001 >nul`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0repair-local-tls-relay.ps1`" -Python `"$consolePython`"`r`npause`r`n"
[IO.File]::WriteAllText($launcher, $launcherText, $utf8)
$shortcut.TargetPath = "$env:SystemRoot\System32\cmd.exe"
$shortcut.Arguments = '/c ""' + $launcher + '""'
$shortcut.WorkingDirectory = $directory
$shortcut.Save()
$startup = '"' + $Python + '" "' + $target + '" --config "' + $configPath + '"'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
[void](New-Item -Path $runKey -Force)
[void](New-ItemProperty -Path $runKey -Name 'CialloChatTlsRelay' -Value $startup -PropertyType String -Force)
$process = Start-Process -FilePath $Python -ArgumentList @('"' + $target + '"', '--config', '"' + $configPath + '"') -PassThru
for ($attempt = 0; $attempt -lt 15; $attempt++) {
    Start-Sleep -Seconds 1
    $rules = (Invoke-RestMethod "$Controller/rules").rules
    if ($rules.Count -ge 2 -and $rules[0].proxy -eq 'CialloChat-TLS' -and $rules[1].proxy -eq 'CialloChat-TLS') {
        Write-Output "Local TLS relay installed. PID: $($process.Id). Starts at Windows login."
        exit 0
    }
}
throw "Relay did not become ready. Check $directory\relay.log."
