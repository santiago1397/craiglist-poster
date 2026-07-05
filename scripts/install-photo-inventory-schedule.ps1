# Installs a Windows Scheduled Task that runs `cl photo-inventory` once a day
# at 03:00 local. Emits one PhotoInventory event per account into the reporter
# outbox — the daemon flushes it to the VPS on its own cadence.
#
# Usage:  Right-click → Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File install-photo-inventory-schedule.ps1)

$ErrorActionPreference = "Stop"

$TaskName = "CL Photo Inventory"

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
    -Argument "run cl photo-inventory" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At 3am

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Emits PhotoInventory events with per-account photo/cover counts." | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' installed." -ForegroundColor Green
Write-Host "  Fires daily at 03:00."
Write-Host "  To run once now: Start-ScheduledTask -TaskName '$TaskName'"
