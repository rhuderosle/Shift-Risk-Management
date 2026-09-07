# Installs the Shift Risk Management System as a Windows Service using NSSM.
#
# Run from an elevated PowerShell prompt on the target server:
#     .\deploy\install-service.ps1 -ServiceAccount "AMR\svc-shiftrisk"
#
# NSSM (https://nssm.cc) must be on PATH or passed via -NssmPath. A service account
# is strongly recommended over LocalSystem: the MMS connector authenticates to
# the passdown host with the service's Windows identity, so that account's
# entitlements determine what data the dashboard can show.

[CmdletBinding()]
param(
    [string]$ServiceName = "ShiftRiskDashboard",
    [string]$ServiceAccount = "",
    [string]$NssmPath = "nssm.exe",
    [string]$AppRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$python = Join-Path $AppRoot ".venv\Scripts\python.exe"
$runner = Join-Path $AppRoot "run.py"
$logDir = Join-Path $AppRoot "logs"

foreach ($p in @($python, $runner)) {
    if (-not (Test-Path $p)) { throw "Not found: $p" }
}
if ($AppRoot -like "*OneDrive*") {
    Write-Warning "AppRoot is inside OneDrive. File sync can corrupt a live SQLite database - move the app to a local path such as C:\Apps\ShiftRisk before serving multiple users."
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

& $NssmPath install $ServiceName $python $runner
& $NssmPath set $ServiceName AppDirectory $AppRoot
& $NssmPath set $ServiceName DisplayName "Shift Risk Management System"
& $NssmPath set $ServiceName Description "AI-powered shift passdown and risk escalation dashboard"
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppStdout (Join-Path $logDir "service.out.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $logDir "service.err.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 10485760

if ($ServiceAccount) {
    $cred = Get-Credential -UserName $ServiceAccount -Message "Password for $ServiceAccount"
    & $NssmPath set $ServiceName ObjectName $ServiceAccount $cred.GetNetworkCredential().Password
}

Write-Host ""
Write-Host "Installed service '$ServiceName'." -ForegroundColor Green
Write-Host "Before starting, confirm in .env:"
Write-Host "  AUTH_MODE=header           (single_user leaves every endpoint open)"
Write-Host "  EMAIL_TRANSPORT=smtp       (outlook COM cannot run headless)"
Write-Host "  BIND_HOST=127.0.0.1        (the reverse proxy is the only ingress)"
Write-Host "  DB_PATH=<a local, non-OneDrive path>"
Write-Host ""
Write-Host "Then: Start-Service $ServiceName"
