# Removes the "CL Auto Post" Scheduled Task.
#
# Usage:  Right-click → Run with PowerShell
#         (or: powershell -ExecutionPolicy Bypass -File uninstall-schedule.ps1)

$ErrorActionPreference = "Stop"

$TaskName = "CL Auto Post"

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Task '$TaskName' is not installed. Nothing to do."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed." -ForegroundColor Green
