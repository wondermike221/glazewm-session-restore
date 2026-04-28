#Requires -Version 5.1
<#
.SYNOPSIS
    Immediately turns off all monitors.

.DESCRIPTION
    Sends SC_MONITORPOWER via Win32 SendMessage to put displays to sleep
    right now, without waiting for the Windows power timeout. Any key press
    or mouse movement wakes them again, which fires the same display-on
    power notification that glazewm-restore listens for.

    Useful for:
      - Testing glazewm-restore without waiting for monitors to sleep naturally
      - Quickly sleeping displays when leaving the desk (pair with Win+L to lock)

.PARAMETER Lock
    Lock the workstation immediately before sleeping the displays.

.EXAMPLE
    .\sleep-displays.ps1
    .\sleep-displays.ps1 -Lock
#>
param([switch] $Lock)

Add-Type -MemberDefinition @'
[DllImport("user32.dll")]
public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
'@ -Name NativeMethods -Namespace Win32 -ErrorAction SilentlyContinue

if ($Lock) {
    rundll32.exe user32.dll,LockWorkStation
}

# Brief pause so you can lift your hand off the mouse before the display sleeps
Start-Sleep -Milliseconds 500

# HWND_BROADCAST = -1, WM_SYSCOMMAND = 0x0112, SC_MONITORPOWER = 0xF170, lParam 2 = off
[Win32.NativeMethods]::SendMessage([IntPtr]-1, 0x0112, [IntPtr]0xF170, [IntPtr]2) | Out-Null
