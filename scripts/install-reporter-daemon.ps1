# Installs a Windows Scheduled Task that keeps `cl reporter-daemon` running
# whenever a user is logged in. The daemon:
#   - drains the outbox to the VPS
#   - emits AccountState heartbeats every 5 minutes
#
# The task auto-starts at logon and auto-restarts on failure. Because the
# poster requires an interactive desktop (Chrome) and posts run under the
# same user session anyway, this piggybacks on that assumption.
#
# Usage:  Right-click → Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-reporter-daemon.ps1)

$ErrorActionPreference = "Stop"

$TaskName = "CL Reporter Daemon"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Error "uv.exe not found in PATH."
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Project root: $projectRoot"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run cl reporter-daemon" `
    -WorkingDirectory $projectRoot

# Start at logon of the current user
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Drains the reporter outbox to the VPS and emits AccountState heartbeats." | Out-Null

# Start it right now too so we don't have to wait for a re-logon
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Task '$TaskName' installed and started." -ForegroundColor Green
Write-Host "  Auto-restarts every minute if it exits."
Write-Host "  To stop:    Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "  To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
