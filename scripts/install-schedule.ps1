# Installs a Windows Scheduled Task that runs `cl post` every 4 hours
# between 9:00 AM and 5:00 PM, Monday through Friday. The Python script's own
# cooldowns, weekday gate, and posting window handle skipping -- fire-and-forget.
#
# Usage:  Right-click -> Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-schedule.ps1)

$ErrorActionPreference = "Stop"

# Registration goes through the shared helper, which verifies the task actually
# landed instead of trusting the cmdlet. See _scheduled_task.ps1.
. "$PSScriptRoot\_scheduled_task.ps1"

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

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run cl post" `
    -WorkingDirectory $projectRoot

# Weekly trigger Mon-Fri at 9:00 AM, repeating every 4 hours for 8 hours
# -> fires at 9:00 AM, 1:00 PM, 5:00 PM on weekdays only (3 fires/day).
# One post per fire, one per account per day.
$startTime = (Get-Date).Date.AddHours(9)
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $startTime
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

Install-ClScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Craigslist auto-poster. Skips when cooldowns/window block posting." `
    -ExpectedExecute $uv `
    -ExpectedWorkingDirectory $projectRoot

Write-Host "  Fires Mon-Fri at: 9:00 AM, 1:00 PM, 5:00 PM"
Write-Host ""
Write-Host "To stop:    scripts\uninstall-schedule.ps1"
Write-Host "To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To inspect: open Task Scheduler (taskschd.msc)"
