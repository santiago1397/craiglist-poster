# Installs a Windows Scheduled Task that runs `cl photo-inventory` once a day
# at 03:00 local. Emits one PhotoInventory event per account into the reporter
# outbox -- the daemon flushes it to the VPS on its own cadence.
#
# Usage:  Right-click -> Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-photo-inventory-schedule.ps1)

$ErrorActionPreference = "Stop"

# Registration goes through the shared helper, which verifies the task actually
# landed instead of trusting the cmdlet. See _scheduled_task.ps1.
. "$PSScriptRoot\_scheduled_task.ps1"

$TaskName = "CL Photo Inventory"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Error "uv.exe not found in PATH."
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "Project root: $projectRoot"

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run cl photo-inventory" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At 3am

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Install-ClScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Emits PhotoInventory events with per-account photo/cover counts." `
    -ExpectedExecute $uv `
    -ExpectedWorkingDirectory $projectRoot

Write-Host "  Fires daily at 03:00."
Write-Host "  To run once now: Start-ScheduledTask -TaskName '$TaskName'"
