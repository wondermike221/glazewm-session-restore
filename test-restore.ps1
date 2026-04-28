#Requires -Version 5.1
<#
.SYNOPSIS
    Automated end-to-end test for glazewm-restore using MultiMonitorTool.

.DESCRIPTION
    Disables one monitor (simulating disconnect), waits, re-enables it, then
    reads the restore log to verify the sequence fired correctly.

.PARAMETER Monitor
    Which monitor to disconnect: Left (Dell U3219Q, DISPLAY2) or Right (G27QC A, DISPLAY1).
    Default: Left

.PARAMETER DisableSeconds
    Seconds to leave the monitor disabled. Default: 5

.PARAMETER WakeDelay
    Must match the --wake-delay the daemon is running with. Default: 3.0

.PARAMETER MmtPath
    Path to MultiMonitorTool.exe. Default: ~/Downloads/MultiMonitorTool.exe
#>
param(
    [ValidateSet("Left","Right")] [string] $Monitor      = "Left",
    [int]    $DisableSeconds = 5,
    [float]  $WakeDelay      = 3.0,
    [string] $MmtPath        = "$env:USERPROFILE\Downloads\MultiMonitorTool.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$LogFile = "$env:USERPROFILE\.glzr\glazewm\restore.log"

# Monitor names from MultiMonitorTool /stext output
$Monitors = @{
    Left  = @{ Name = "\\.\DISPLAY2"; Label = "Dell U3219Q (left, monitor idx 0)"  }
    Right = @{ Name = "\\.\DISPLAY1"; Label = "G27QC A (primary/right, monitor idx 1)" }
}

$target = $Monitors[$Monitor]

if (-not (Test-Path $MmtPath)) {
    Write-Error "MultiMonitorTool.exe not found at $MmtPath"
}

function Write-Step([string]$msg) { Write-Host "  » $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

# Tail the log from this point forward
$logLineBefore = (Get-Content $LogFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines

function Get-NewLogLines {
    $all = Get-Content $LogFile -ErrorAction SilentlyContinue
    if ($all.Count -gt $logLineBefore) {
        $all[$logLineBefore..($all.Count - 1)]
    }
}

Write-Host "`nglazewm-restore end-to-end test" -ForegroundColor Magenta
Write-Host "  Monitor : $($target.Label)"
Write-Host "  Disable : ${DisableSeconds}s  Wake delay: ${WakeDelay}s"
Write-Host "  Log     : $LogFile`n"

# ── 1. Disable monitor ──────────────────────────────────────────────────────
Write-Step "Disabling $($target.Name)..."
& $MmtPath /disable $target.Name
Start-Sleep -Seconds $DisableSeconds

# ── 2. Check freeze fired ───────────────────────────────────────────────────
$lines = Get-NewLogLines
$freezeLine = $lines | Select-String "FREEZING|snapshot FROZEN"
if ($freezeLine) {
    Write-Ok "Freeze detected:`n    $($freezeLine.Line)"
} else {
    Write-Warn "No freeze line found yet — daemon may not have seen the disconnect."
    Write-Warn "New log lines so far:"
    $lines | ForEach-Object { Write-Host "    $_" }
}

# ── 3. Re-enable monitor ────────────────────────────────────────────────────
Write-Step "Re-enabling $($target.Name)..."
& $MmtPath /enable $target.Name

# Wait for wake delay + restore + buffer
$totalWait = [int]($WakeDelay + 8)
Write-Step "Waiting ${totalWait}s for restore to complete..."
Start-Sleep -Seconds $totalWait

# ── 4. Check restore fired ──────────────────────────────────────────────────
$lines = Get-NewLogLines

Write-Host "`n── New log entries since test started ────────────────────────────" -ForegroundColor DarkGray
$lines | ForEach-Object { Write-Host "  $_" }
Write-Host "──────────────────────────────────────────────────────────────────" -ForegroundColor DarkGray

# Evaluate result
$restoreComplete = $lines | Select-String "Restore complete"
$nothingToMove   = $lines | Select-String "nothing to move"
$restoreFailed   = $lines | Select-String "Restore failed"

Write-Host ""
if ($restoreComplete) {
    Write-Ok "PASS — restore completed."
} elseif ($nothingToMove) {
    Write-Ok "PASS — restore ran, nothing needed moving (layout already correct)."
} elseif ($restoreFailed) {
    Write-Warn "FAIL — restore attempted but errored."
    $restoreFailed | ForEach-Object { Write-Host "    $($_.Line)" -ForegroundColor Red }
} else {
    Write-Warn "INCONCLUSIVE — no restore line found. Check log above for clues."
}
