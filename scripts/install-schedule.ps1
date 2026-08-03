# Installs a Windows Scheduled Task that runs `cl post` eight times a weekday --
# hourly from 8:00 AM to 11:00 AM, then hourly from 2:00 PM to 5:00 PM. The
# server's own cooldowns, caps, weekday gate and posting window handle skipping,
# so this is fire-and-forget.
#
# Four accounts x two posts each = eight ads a day. Longest-idle-first selection
# on the server makes each account take one morning fire and one afternoon fire
# without anything here having to know which is which.
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

# Two Mon-Fri blocks, each starting on the hour and repeating hourly for three
# more hours -> 8:00/9:00/10:00/11:00 and 14:00/15:00/16:00/17:00, eight fires on
# weekdays only. One post per fire.
#
# Keep these hours in step with TASK_FIRE_HOURS in
# backend/app/services/queue.py (the schedule forecast) and POSTING_SLOT_HOURS in
# src/craigslist_auto/edit_worker.py (the edit worker's stay-out-of-the-way
# guard). All three describe the same eight fires.
#
# The midday gap is not decoration: fire k and fire k+4 land six hours apart,
# which is the clearance the server's five-hour same-account cooldown needs for
# an account to take both a morning and an afternoon slot.
$morningStart   = (Get-Date).Date.AddHours(8)
$afternoonStart = (Get-Date).Date.AddHours(14)

$morning = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $morningStart
$morning.Repetition = (New-ScheduledTaskTrigger -Once -At $morningStart `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 3)).Repetition

$afternoon = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $afternoonStart
$afternoon.Repetition = (New-ScheduledTaskTrigger -Once -At $afternoonStart `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 3)).Repetition

$trigger = @($morning, $afternoon)

# The 30-minute limit matters more now that fires are an hour apart rather than
# four: it is what stops a hung run overlapping the next one. New-ScheduledTask-
# SettingsSet defaults MultipleInstances to IgnoreNew, so if a run does overrun,
# Windows drops the new fire instead of starting a second Chrome -- which is the
# behaviour we want, since two posting runs against one profile is exactly what
# the browser lease exists to prevent. STALE_CLAIM_MINUTES (45) on the server
# still outlives this limit, so a killed run's draft is rescued at the next fire.
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

Write-Host "  Fires Mon-Fri at: 8, 9, 10, 11 AM and 2, 3, 4, 5 PM (8 per day)"
Write-Host ""
Write-Host "To stop:    scripts\uninstall-schedule.ps1"
Write-Host "To pause:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To inspect: open Task Scheduler (taskschd.msc)"
