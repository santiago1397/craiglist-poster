# Shared registration helper for the CL scheduled tasks.
#
# Dot-source it:  . "$PSScriptRoot\_scheduled_task.ps1"
#
# It exists because all four install scripts printed a green "Task installed."
# after failing to install anything. Observed on the production poster:
#
#   Unregister-ScheduledTask : Access is denied.
#   Register-ScheduledTask : Cannot create a file when that file already exists.
#   ...
#   Task 'CL Stats Sync' installed.        <- green, and false
#
# `$ErrorActionPreference = "Stop"` does not reliably convert errors from the
# CIM-backed ScheduledTasks cmdlets into terminating ones, so execution ran
# straight past both failures. The operator was told the scrape was scheduled;
# it was not, and the dashboard's stats quietly went stale for two days.
#
# The rule here is the one the rest of this project follows: a green message
# that is not true is worse than a red one. So this helper does not trust the
# registration call at all. It reads the task back afterwards and compares what
# Windows actually stored against what was asked for. If they disagree, or the
# task is missing, it exits non-zero and says why.

function Install-ClScheduledTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]   $TaskName,
        [Parameter(Mandatory)][ciminstance] $Action,
        [Parameter(Mandatory)][ciminstance[]] $Trigger,
        [Parameter(Mandatory)][ciminstance] $Settings,
        [Parameter(Mandatory)][ciminstance] $Principal,
        [Parameter(Mandatory)][string]   $Description,
        # Compared against what Windows stored, so a task that silently kept an
        # older definition is caught rather than reported as fresh.
        [Parameter(Mandatory)][string]   $ExpectedExecute,
        [Parameter(Mandatory)][string]   $ExpectedWorkingDirectory
    )

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task '$TaskName'..."
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        } catch {
            # Access denied here means the task belongs to another user, or this
            # shell is not elevated. Registration is guaranteed to fail next
            # with "already exists", so stop now and say the useful thing.
            Write-Host ""
            Write-Host "FAILED: could not remove the existing '$TaskName' task." -ForegroundColor Red
            Write-Host "  $($_.Exception.Message)"
            Write-Host ""
            Write-Host "  The task already exists and this session may not own it."
            Write-Host "  Re-run this script from an ELEVATED PowerShell:"
            Write-Host "    right-click Windows PowerShell -> Run as administrator"
            Write-Host ""
            Write-Host "  Its current definition:"
            Write-Host "    (Get-ScheduledTask -TaskName '$TaskName').Actions | Format-List"
            Write-Host "    (Get-ScheduledTask -TaskName '$TaskName').Principal | Format-List"
            exit 1
        }
    }

    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -Principal $Principal `
            -Description $Description `
            -ErrorAction Stop | Out-Null
    } catch {
        Write-Host ""
        Write-Host "FAILED: could not register '$TaskName'." -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)"
        Write-Host ""
        Write-Host "  If this says the task already exists, the removal above did"
        Write-Host "  not take effect -- re-run from an elevated PowerShell."
        exit 1
    }

    # Read it back. Everything above can report success without having stored
    # what was asked for, which is the entire reason this helper exists.
    $installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $installed) {
        Write-Host ""
        Write-Host "FAILED: '$TaskName' is not present after registering it." -ForegroundColor Red
        Write-Host "  Windows accepted the call and stored nothing. Re-run elevated."
        exit 1
    }

    # @() so a null Actions collection becomes an empty array rather than
    # throwing on [0] -- an indexing error here would abort mid-verification and
    # leave the caller with no verdict at all, which is the failure this whole
    # helper exists to prevent.
    $actions = @($installed.Actions)
    if ($actions.Count -eq 0) {
        Write-Host ""
        Write-Host "FAILED: '$TaskName' was registered with no action." -ForegroundColor Red
        Write-Host "  Windows stored the task but not the command to run, so it would"
        Write-Host "  fire daily and do nothing. Remove it and re-run elevated:"
        Write-Host "    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
        exit 1
    }

    $storedExecute = [string]$actions[0].Execute
    $storedWorkDir = [string]$actions[0].WorkingDirectory
    $storedArgs    = [string]$actions[0].Arguments

    # Paths are compared case-insensitively and without a trailing slash:
    # Windows round-trips "C:\foo" and "C:\Foo\" as the same directory, and a
    # false mismatch that blocks a correct install is its own kind of lie.
    function Test-SamePath([string] $a, [string] $b) {
        return ($a.TrimEnd('\', '/')) -ieq ($b.TrimEnd('\', '/'))
    }

    $mismatch = @()
    if (-not (Test-SamePath $storedExecute $ExpectedExecute)) {
        $mismatch += "  Execute:          stored '$storedExecute'`n                    wanted '$ExpectedExecute'"
    }
    if (-not (Test-SamePath $storedWorkDir $ExpectedWorkingDirectory)) {
        $mismatch += "  WorkingDirectory: stored '$storedWorkDir'`n                    wanted '$ExpectedWorkingDirectory'"
    }
    if ($mismatch.Count -gt 0) {
        Write-Host ""
        Write-Host "FAILED: '$TaskName' exists but does not match this project." -ForegroundColor Red
        $mismatch | ForEach-Object { Write-Host $_ }
        Write-Host ""
        Write-Host "  A task pointing at a stale path fails to launch, and a task that"
        Write-Host "  never launches cannot report an error -- it just silently does"
        Write-Host "  nothing. Remove it and re-run elevated:"
        Write-Host "    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
        exit 1
    }

    $who = [string]$installed.Principal.UserId
    if (-not $who) { $who = [string]$installed.Principal.GroupId }
    $logon = [string]$installed.Principal.LogonType

    Write-Host ""
    Write-Host "Task '$TaskName' installed and verified." -ForegroundColor Green
    Write-Host "  Runs as:     $who ($logon)"
    Write-Host "  Command:     $storedExecute $storedArgs"
    Write-Host "  Working dir: $storedWorkDir"

    # Interactive tasks do not run while that account is signed out. The stats
    # scrape missing two days on the production poster looked exactly like a
    # broken scraper, and was really nobody being logged in at 06:00.
    if ($logon -eq "Interactive") {
        Write-Host "  NOTE: runs only while '$who' is signed in to this PC." -ForegroundColor Yellow
    }
}
