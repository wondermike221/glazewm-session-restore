#Requires -Version 5.1
<#
.SYNOPSIS
    Watches the glazewm-restore log during a manual monitor power-cycle test.

.DESCRIPTION
    Tails the log in real time and reports whether the freeze→restore
    sequence fires correctly. You trigger the event manually (physical
    monitor power button, or natural timeout).

    Recommended test procedure:
      1. Run this script in a terminal (it starts watching immediately).
      2. Press the POWER BUTTON on the Dell monitor to turn it OFF.
      3. Wait 3-5 seconds.
      4. Press the power button again to turn it ON.
      5. Watch this script report the sequence as it happens.
      6. Press Ctrl+C to exit once you see the result.

    For the natural-timeout test: run this script, then leave the machine
    and come back after the monitors sleep. The result will be waiting.

.PARAMETER TimeoutSeconds
    Give up waiting after this many seconds with no result. Default: 120.

.PARAMETER LogFile
    Path to the restore log. Default: auto-detected from ~/.glzr/glazewm/restore.log
#>
param(
    [int]    $TimeoutSeconds = 120,
    [string] $LogFile = "$env:USERPROFILE\.glzr\glazewm\restore.log"
)

Set-StrictMode -Version Latest

if (-not (Test-Path $LogFile)) {
    Write-Error "Log file not found: $LogFile — is glazewm-restore running?"
}

function Write-Event([string]$msg, [string]$color = "Cyan") {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $msg" -ForegroundColor $color
}

# Start from current end of log
$startLine = (Get-Content $LogFile | Measure-Object -Line).Lines

Write-Host ""
Write-Host "glazewm-restore test watcher" -ForegroundColor Magenta
Write-Host "  Log     : $LogFile"
Write-Host "  Timeout : ${TimeoutSeconds}s"
Write-Host ""
Write-Host "  ► Now press the Dell monitor POWER BUTTON (or wait for natural sleep)" -ForegroundColor Yellow
Write-Host "  ► Press Ctrl+C to exit`n" -ForegroundColor DarkGray

$deadline  = (Get-Date).AddSeconds($TimeoutSeconds)
$seenFreeze   = $false
$seenRestore  = $false
$seenMinimize = $false
$result       = $null

while ((Get-Date) -lt $deadline -and -not $result) {
    Start-Sleep -Milliseconds 300

    $all = Get-Content $LogFile -ErrorAction SilentlyContinue
    if ($all.Count -le $startLine) { continue }

    $newLines = $all[$startLine..($all.Count - 1)]
    $startLine = $all.Count

    foreach ($line in $newLines) {
        # Always echo new lines
        $color = "Gray"
        if ($line -match "FREEZING|FROZEN")          { $color = "Yellow"  }
        if ($line -match "reconnect|UNFROZEN")        { $color = "Cyan"    }
        if ($line -match "Restore|Un-minim")          { $color = "Green"   }
        if ($line -match "WARN|ERROR|failed")         { $color = "Red"     }
        if ($line -match "monitor-updated")           { $color = "Magenta" }
        Write-Host "  $line" -ForegroundColor $color

        # Track sequence
        if ($line -match "FREEZING|snapshot FROZEN")        { $seenFreeze   = $true }
        if ($line -match "Un-minimizing")                   { $seenMinimize = $true }
        if ($line -match "Restore complete")                { $seenRestore  = $true; $result = "PASS" }
        if ($line -match "nothing to move")                 { $seenRestore  = $true; $result = "PASS_NOOP" }
        if ($line -match "Restore failed")                  { $result = "FAIL" }
    }
}

Write-Host ""
Write-Host "── Result ──────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  Freeze triggered : $(if ($seenFreeze)   {'✓'} else {'✗ NOT SEEN — monitor-updated may not have fired, or count did not change'})"
Write-Host "  Windows restored : $(if ($seenRestore)  {'✓'} else {'✗ NOT SEEN'})"
Write-Host "  Windows unminimized : $(if ($seenMinimize) {'✓'} else {'-  (none were minimized)'})"

switch ($result) {
    "PASS"      { Write-Host "`n  PASS — restore ran and moved workspaces back." -ForegroundColor Green }
    "PASS_NOOP" { Write-Host "`n  PASS (noop) — restore ran; layout was already correct." -ForegroundColor Green }
    "FAIL"      { Write-Host "`n  FAIL — restore attempted but errored. Check log above." -ForegroundColor Red }
    $null {
        if (-not $seenFreeze) {
            Write-Host "`n  INCONCLUSIVE — no freeze fired." -ForegroundColor Yellow
            Write-Host "  Likely cause: GlazeWM did not report a monitor count change." -ForegroundColor Yellow
            Write-Host "  Check the 'monitor-updated: count N→N' lines above." -ForegroundColor Yellow
        } else {
            Write-Host "`n  INCONCLUSIVE — freeze fired but restore never completed." -ForegroundColor Yellow
            Write-Host "  Monitor may not have reconnected, or wake event was not received." -ForegroundColor Yellow
        }
    }
}
Write-Host "────────────────────────────────────────────────────────────`n" -ForegroundColor DarkGray
