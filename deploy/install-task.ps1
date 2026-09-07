<#
    Installs a Windows Scheduled Task that emails the shift passdown daily.

    This is the "no hosting request" delivery model. It runs entirely under your
    own user account -- no admin rights, no server, no firewall change.

    Usage (normal PowerShell, NOT elevated):
        cd C:\Apps\ShiftRisk
        .\deploy\install-task.ps1                    # default 07:00 and 19:00
        .\deploy\install-task.ps1 -Times "06:45"     # custom
        .\deploy\install-task.ps1 -Remove

    Requirements at run time:
      * You must be logged in (the task is set to run only when you are).
      * Outlook must be running, because EMAIL_TRANSPORT=outlook sends through
        your profile. If Outlook is closed the job exits 1 and logs why.
#>
[CmdletBinding()]
param(
    [string[]]$Times = @("07:00", "19:00"),
    [string]$TaskName = "ShiftRisk Daily Passdown",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$Script = Join-Path $AppDir "daily_passdown.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    } else {
        Write-Host "No task named '$TaskName' found." -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $Python)) { throw "Python not found at $Python. Create the venv first." }
if (-not (Test-Path $Script)) { throw "daily_passdown.py not found at $Script." }

# pythonw.exe runs without a console window so the task is invisible in use.
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $AppDir

$triggers = foreach ($t in $Times) {
    try { $parsed = [datetime]::ParseExact($t, "HH:mm", $null) }
    catch { throw "Invalid time '$t'. Use 24-hour HH:mm, e.g. 07:00." }
    New-ScheduledTaskTrigger -Daily -At $parsed
}

# RunOnlyIfNetworkAvailable: no point running MMS sync with no network.
# StartWhenAvailable: catches up if the laptop was asleep at the trigger time.
$settingsSet = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# Interactive principal = runs as you, with your Outlook profile and Kerberos
# ticket. This is exactly why no service account is needed.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settingsSet -Principal $principal -Force `
    -Description "Syncs MMS + Outlook and emails the Shift Risk passdown. Requires Outlook to be running." | Out-Null

Write-Host ""
Write-Host "Installed '$TaskName'" -ForegroundColor Green
Write-Host "  Runs at : $($Times -join ', ') daily"
Write-Host "  As      : $env:USERDOMAIN\$env:USERNAME (only while logged in)"
Write-Host "  Logs    : $AppDir\data\logs\"
Write-Host ""
Write-Host "Test it now with:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "NOTE: Outlook must be open when it fires, or the send will fail." -ForegroundColor Yellow
