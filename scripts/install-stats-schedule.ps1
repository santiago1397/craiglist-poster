# Installs a Windows Scheduled Task that runs `cl stats-sync` daily at 6:00 AM
# local time (America/New_York on this machine). Morning-after snapshots let
# CL's stat-lag (~1-6h+) settle fully, so day-over-day deltas are accurate.
#
# Usage:  Right-click -> Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-stats-schedule.ps1)

$ErrorActionPreference = "Stop"

# Registration goes through the shared helper, which verifies the task actually
# landed instead of trusting the cmdlet. See _scheduled_task.ps1.
. "$PSScriptRoot\_scheduled_task.ps1"

$TaskName = "CL Stats Sync"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Error "uv.exe not found in PATH. Install uv first or add it to PATH."
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Project root: $projectRoot"
Write-Host "uv path:      $uv"

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run cl stats-sync" `
    -WorkingDirectory $projectRoot

# Daily at 6:00 AM local. Morning-after snapshots capture the previous day's
# fully-settled counters (CL stats can lag more than 2h).
$startTime = (Get-Date).Date.AddHours(6)
$trigger = New-ScheduledTaskTrigger -Daily -At $startTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Install-ClScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Craigslist stats scraper. Snapshots each account's active postings once per day." `
    -ExpectedExecute $uv `
    -ExpectedWorkingDirectory $projectRoot

Write-Host "  Fires daily at: 6:00 AM"
Write-Host ""
Write-Host "To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
