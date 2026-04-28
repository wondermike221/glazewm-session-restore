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

.PARAMETER NonInteractive
    Skip all prompts; automatically start the task after registration.
    Used by chezmoi run scripts.

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -NonInteractive
    .\deploy.ps1 -Uninstall
#>
param(
    [string] $ScriptDir      = "$env:USERPROFILE\.glzr\glazewm",
    [string] $TaskName       = "GlazeWM Session Restore",
    [switch] $Uninstall,
    [switch] $NonInteractive
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

    # Remove old VBS launcher if still present from a previous install
    $vbs = Join-Path $ScriptDir "glazewm-restore.vbs"
    if (Test-Path $vbs) {
        Write-Step "Removing legacy VBS launcher"
        Remove-Item $vbs -Force
        Write-Ok "Removed $vbs"
    }

    $shortcutPath = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Sleep Displays.lnk"
    if (Test-Path $shortcutPath) {
        Write-Step "Removing Start Menu shortcut"
        Remove-Item $shortcutPath -Force
        Write-Ok "Removed shortcut."
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

$srcSleepScript = Join-Path $PSScriptRoot "sleep-displays.ps1"
if (Test-Path $srcSleepScript) {
    $destSleepScript = Join-Path $ScriptDir "sleep-displays.ps1"
    Write-Step "Copying sleep-displays.ps1 → $destSleepScript"
    Copy-Item $srcSleepScript $destSleepScript -Force
    Write-Ok "sleep-displays.ps1 installed."

    # Create a Start Menu shortcut so it's reachable from PowerToys Run / Win+R
    $shortcutPath = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Sleep Displays.lnk"
    Write-Step "Creating Start Menu shortcut → $shortcutPath"
    $shell = New-Object -ComObject WScript.Shell
    $lnk   = $shell.CreateShortcut($shortcutPath)
    $lnk.TargetPath     = "powershell.exe"
    $lnk.Arguments      = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$destSleepScript`""
    $lnk.WorkingDirectory = $ScriptDir
    $lnk.Description    = "Sleep all displays immediately (glazewm-restore test / quick leave)"
    $lnk.Save()
    Write-Ok "Shortcut created."
}

# --------------------------------------------------------------------------- #
# Create PowerShell launcher (hidden window, blocking so task restarts on crash)
# --------------------------------------------------------------------------- #
# VBScript is deprecated in Windows 11 — using PowerShell instead.
# bWaitOnReturn was False in the old VBS, which meant the task exited immediately
# and the RestartCount setting never fired on Python crashes.
# Now the task runs pwsh.exe directly (blocking), so if Python dies the task
# ends and Task Scheduler restarts it up to 5 times.

$uvExe = (Get-Command uv).Source
Write-Step "Registering scheduled task '$TaskName'..."

$action = New-ScheduledTaskAction `
    -Execute "pwsh.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -Command `"& '$uvExe' run '$destScript'`""

# Run at logon of the current user
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
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

if ($NonInteractive) {
    $startNow = "y"
} else {
    $startNow = Read-Host "`nStart the task now? [Y/n]"
}

if ($startNow -ne "n" -and $startNow -ne "N") {
    Write-Step "Starting task..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "Task state: $state"
}

Write-Host "`nDone! Log file: $($env:USERPROFILE)\.glzr\glazewm\restore.log`n" -ForegroundColor Green
