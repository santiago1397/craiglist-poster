# Installs a Windows Scheduled Task that runs `cl post` every 4 hours
# between 9:00 AM and 5:00 PM, every day. The Python script's own cooldowns
# and posting window handle skipping — fire-and-forget.
#
# Usage:  Right-click → Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-schedule.ps1)

$ErrorActionPreference = "Stop"

$TaskName = "CL Auto Post"

# Locate uv.exe
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Error "uv.exe not found in PATH. Install uv first or add it to PATH."
    exit 1
}

# Project root = parent of this script's folder
$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Project root: $projectRoot"
Write-Host "uv path:      $uv"

# Remove existing task with same name (idempotent re-install)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run cl post" `
    -WorkingDirectory $projectRoot

# Daily trigger starting at 9:00 AM, repeating every 4 hours for 8 hours
# → fires at 9:00 AM, 1:00 PM, 5:00 PM (3 fires).
# One post per fire, one per account per day.
$startTime = (Get-Date).Date.AddHours(9)
$trigger = New-ScheduledTaskTrigger -Daily -At $startTime
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $startTime `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Hours 8)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Run as current logged-in user, only when user is logged on (browser needs a desktop)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Craigslist auto-poster. Skips when cooldowns/window block posting." | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' installed." -ForegroundColor Green
Write-Host "  Fires daily at: 9:00 AM, 1:00 PM, 5:00 PM"
Write-Host "  Working dir:    $projectRoot"
Write-Host ""
Write-Host "To stop:    scripts\uninstall-schedule.ps1"
Write-Host "To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To inspect: open Task Scheduler (taskschd.msc)"
