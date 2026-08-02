# Installs a Windows Scheduled Task that keeps `cl reporter-daemon` running
# whenever a user is logged in. The daemon:
#   - drains the outbox to the VPS
#   - emits AccountState heartbeats every 5 minutes
#
# The task auto-starts at logon and auto-restarts on failure. Because the
# poster requires an interactive desktop (Chrome) and posts run under the
# same user session anyway, this piggybacks on that assumption.
#
# Usage:  Right-click -> Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-reporter-daemon.ps1)

$ErrorActionPreference = "Stop"

# Registration goes through the shared helper, which verifies the task actually
# landed instead of trusting the cmdlet. See _scheduled_task.ps1.
. "$PSScriptRoot\_scheduled_task.ps1"

$TaskName = "CL Reporter Daemon"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Error "uv.exe not found in PATH."
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Project root: $projectRoot"

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

Install-ClScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Drains the reporter outbox to the VPS and emits AccountState heartbeats." `
    -ExpectedExecute $uv `
    -ExpectedWorkingDirectory $projectRoot

# Start it right now too so we don't have to wait for a re-logon. Checked
# rather than assumed: this daemon is what drains the outbox, so a silent
# failure to start looks exactly like "nothing has gone wrong yet".
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running") {
    Write-Host "  Started, and running now." -ForegroundColor Green
} else {
    Write-Host "  WARNING: started it, but the task reports '$state', not 'Running'." -ForegroundColor Yellow
    Write-Host "           Until it runs, nothing drains the outbox to the dashboard."
    Write-Host "           Check:  Get-ScheduledTaskInfo -TaskName '$TaskName'"
}

Write-Host "  Auto-restarts every minute if it exits."
Write-Host "  To stop:    Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "  To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
