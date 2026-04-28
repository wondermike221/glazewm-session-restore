#Requires -Version 5.1
<#
.SYNOPSIS
    Deploys glazewm-restore as a background Task Scheduler job.

.DESCRIPTION
    1. Installs uv if not already on PATH.
    2. Copies glazewm-restore.py to ~\.glzr\glazewm\.
    3. Creates a hidden VBScript launcher (no console flash on logon).
    4. Registers (or updates) a Task Scheduler task that runs at logon.

    Idempotent — safe to re-run after updates.

.PARAMETER ScriptDir
    Where to install the Python script. Defaults to ~\.glzr\glazewm.

.PARAMETER TaskName
    Name of the scheduled task. Defaults to "GlazeWM Session Restore".

.PARAMETER Uninstall
    Remove the scheduled task and installed files.

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -Uninstall
#>
param(
    [string] $ScriptDir  = "$env:USERPROFILE\.glzr\glazewm",
    [string] $TaskName   = "GlazeWM Session Restore",
    [switch] $Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

function Write-Step([string]$msg) { Write-Host "  » $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

# --------------------------------------------------------------------------- #
# Uninstall path
# --------------------------------------------------------------------------- #

if ($Uninstall) {
    Write-Host "`nUninstalling GlazeWM Session Restore..." -ForegroundColor Magenta

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Write-Step "Removing scheduled task '$TaskName'"
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "Task removed."
    } else {
        Write-Warn "Task '$TaskName' not found — skipping."
    }

    $vbs = Join-Path $ScriptDir "glazewm-restore.vbs"
    if (Test-Path $vbs) {
        Write-Step "Removing VBS launcher"
        Remove-Item $vbs -Force
        Write-Ok "Removed $vbs"
    }

    # Leave the .py and log in place so the user keeps their history.
    Write-Host "`nDone. Log and script left in $ScriptDir" -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------------------- #
# Install uv
# --------------------------------------------------------------------------- #

Write-Host "`nDeploying GlazeWM Session Restore" -ForegroundColor Magenta

Write-Step "Checking for uv..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "uv not found — installing via winget..."
    winget install --id astral-sh.uv --exact --accept-source-agreements --accept-package-agreements
    # Refresh PATH for this session
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "uv still not found after install. Restart your shell and re-run."
    }
    Write-Ok "uv installed."
} else {
    Write-Ok "uv found: $(uv --version)"
}

# --------------------------------------------------------------------------- #
# Copy script to target directory
# --------------------------------------------------------------------------- #

Write-Step "Creating target directory: $ScriptDir"
New-Item -ItemType Directory -Force -Path $ScriptDir | Out-Null

$srcScript = Join-Path $PSScriptRoot "glazewm-restore.py"
if (-not (Test-Path $srcScript)) {
    Write-Error "glazewm-restore.py not found next to deploy.ps1 ($PSScriptRoot)"
}

$destScript = Join-Path $ScriptDir "glazewm-restore.py"
Write-Step "Copying glazewm-restore.py → $destScript"
Copy-Item $srcScript $destScript -Force
Write-Ok "Script installed."

# --------------------------------------------------------------------------- #
# Create VBScript launcher (suppresses console window on Task Scheduler start)
# --------------------------------------------------------------------------- #

$vbsPath = Join-Path $ScriptDir "glazewm-restore.vbs"
Write-Step "Writing VBS launcher → $vbsPath"

# Resolve uv path so the VBS doesn't depend on PATH being fully loaded at logon
$uvExe = (Get-Command uv).Source
$vbsContent = @"
' Silent launcher for glazewm-restore — no console window.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """""$uvExe"""" run """"$destScript""""", 0, False
Set shell = Nothing
"@

Set-Content -Path $vbsPath -Value $vbsContent -Encoding UTF8
Write-Ok "VBS launcher written."

# --------------------------------------------------------------------------- #
# Register (or update) Task Scheduler task
# --------------------------------------------------------------------------- #

Write-Step "Registering scheduled task '$TaskName'..."

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$vbsPath`""

# Run at logon of the current user
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `   # no time limit
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Warn "Task already exists — updating."
    Set-ScheduledTask `
        -TaskName $TaskName `
        -Action   $action `
        -Trigger  $trigger `
        -Settings $settings `
        -Principal $principal | Out-Null
} else {
    Register-ScheduledTask `
        -TaskName  $TaskName `
        -Action    $action `
        -Trigger   $trigger `
        -Settings  $settings `
        -Principal $principal | Out-Null
}

Write-Ok "Task '$TaskName' registered."

# --------------------------------------------------------------------------- #
# Optionally start right now
# --------------------------------------------------------------------------- #

$startNow = Read-Host "`nStart the task now? [Y/n]"
if ($startNow -ne "n" -and $startNow -ne "N") {
    Write-Step "Starting task..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "Task state: $state"
}

Write-Host "`nDone! Log file: $($env:USERPROFILE)\.glzr\glazewm\restore.log`n" -ForegroundColor Green
