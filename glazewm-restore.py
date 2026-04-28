# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets"]
# ///
"""
glazewm-restore.py

Maintains a live snapshot of your workspace→monitor layout by subscribing to
GlazeWM's IPC event stream. On monitor wake, restores the layout.

Run with:
    uv run glazewm-restore.py

Or deploy as a background Task Scheduler job:
    deploy.ps1
"""

import asyncio
import ctypes
import ctypes.wintypes
import json
import logging
import subprocess
import threading
import uuid as _uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

GLAZEWM_EXE = Path.home() / "AppData" / "Local" / "Programs" / "GlazeWM" / "glazewm.exe"
IPC_URI = "ws://127.0.0.1:6123"
LOG_FILE = Path.home() / ".glzr" / "glazewm" / "restore.log"
WAKE_DELAY = 3.0  # seconds to wait after wake before restoring

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Shared state (written by IPC loop, read by power-event thread)
# --------------------------------------------------------------------------- #

# Maps workspace name → monitor index (0 = leftmost)
_saved: dict[str, int] = {}
_saved_lock = threading.Lock()

# asyncio event loop reference — set once the loop starts
_loop: asyncio.AbstractEventLoop | None = None

# --------------------------------------------------------------------------- #
# GlazeWM CLI helpers (used only for restore commands)
# --------------------------------------------------------------------------- #

def _run_cmd(*args: str) -> None:
    subprocess.run([str(GLAZEWM_EXE), *args], capture_output=True, timeout=5)


# --------------------------------------------------------------------------- #
# IPC helper — filters interleaved events so query responses aren't confused
# --------------------------------------------------------------------------- #

async def _ws_query(ws, command: str) -> dict:
    """Send a command and return its response, silently dropping any interleaved events."""
    await ws.send(command)
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if "eventType" not in msg:
            return msg
        # An event arrived before the response; discard it — the outer
        # listener loop will re-snapshot when the next event arrives.


# --------------------------------------------------------------------------- #
# Layout snapshot & restore
# --------------------------------------------------------------------------- #

def _parse_snapshot(monitors: list[dict]) -> dict[str, int]:
    """Build {workspace_name: monitor_idx} from a monitors list, sorted L→R."""
    sorted_monitors = sorted(monitors, key=lambda m: m.get("x", 0))
    mapping: dict[str, int] = {}
    for idx, mon in enumerate(sorted_monitors):
        for ws in mon.get("children", []):
            name = ws.get("name")
            if name:
                mapping[name] = idx
    return mapping


def _do_restore(saved: dict[str, int], monitors: list[dict]) -> None:
    sorted_monitors = sorted(monitors, key=lambda m: m.get("x", 0))
    current: dict[str, int] = {}
    for idx, mon in enumerate(sorted_monitors):
        for ws in mon.get("children", []):
            name = ws.get("name")
            if name:
                current[name] = idx

    for ws_name, target_idx in saved.items():
        current_idx = current.get(ws_name)
        if current_idx is None or current_idx == target_idx:
            continue
        direction = "right" if current_idx < target_idx else "left"
        steps = abs(target_idx - current_idx)
        for _ in range(steps):
            _run_cmd("command", f"focus --workspace {ws_name}")
            _run_cmd("command", f"move-workspace --direction {direction}")
        log.info("Restored workspace %s → monitor %d", ws_name, target_idx)


# --------------------------------------------------------------------------- #
# GlazeWM IPC WebSocket loop
# --------------------------------------------------------------------------- #

async def _ipc_loop() -> None:
    """
    Connects to GlazeWM IPC, subscribes to all events, and keeps _saved
    up-to-date. Reconnects automatically if GlazeWM restarts.
    """
    import websockets  # imported here so the script fails loudly if missing

    while True:
        try:
            async with websockets.connect(IPC_URI) as ws:
                log.info("Connected to GlazeWM IPC.")

                # 1. Take an initial snapshot
                data = await _ws_query(ws, "query monitors")
                monitors = data.get("data", {}).get("monitors", [])
                snapshot = _parse_snapshot(monitors)
                with _saved_lock:
                    _saved.clear()
                    _saved.update(snapshot)
                log.info("Initial snapshot: %s", snapshot)

                # 2. Subscribe to all events
                sub_ack = await _ws_query(ws, "sub --events all")
                log.info("Subscribed (id=%s)", sub_ack.get("data", {}).get("subscriptionId"))

                # 3. Listen and re-snapshot on relevant events
                LAYOUT_EVENTS = {
                    "workspace-activated",
                    "workspace-deactivated",
                    "monitor-updated",
                    "wm-restarted",
                }

                async for message in ws:
                    event = json.loads(message)
                    event_type = event.get("eventType", "")

                    if event_type not in LAYOUT_EVENTS:
                        continue

                    # Re-query monitors to get the fresh layout
                    data = await _ws_query(ws, "query monitors")
                    monitors = data.get("data", {}).get("monitors", [])
                    snapshot = _parse_snapshot(monitors)
                    with _saved_lock:
                        _saved.clear()
                        _saved.update(snapshot)
                    log.info("Snapshot updated on %s: %s", event_type, snapshot)

        except (OSError, Exception) as e:
            log.warning("IPC disconnected (%s). Retrying in 5s...", e)
            await asyncio.sleep(5)


# --------------------------------------------------------------------------- #
# Restore trigger (called from the power-event thread via the asyncio loop)
# --------------------------------------------------------------------------- #

def _schedule_restore() -> None:
    """Called from the Win32 message thread. Posts restore onto the asyncio loop."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_restore_after_delay(), _loop)


async def _restore_after_delay() -> None:
    log.info("Wake detected — restoring in %.1fs...", WAKE_DELAY)

    # Capture the pre-wake layout NOW, before the IPC loop re-snapshots the
    # scrambled post-wake state during the delay.
    with _saved_lock:
        saved = dict(_saved)

    await asyncio.sleep(WAKE_DELAY)

    # Re-query current (post-scramble) monitor state, then restore to saved
    import websockets
    try:
        async with websockets.connect(IPC_URI) as ws:
            data = await _ws_query(ws, "query monitors")
            monitors = data.get("data", {}).get("monitors", [])
            _do_restore(saved, monitors)
            log.info("Restore complete.")
    except Exception as e:
        log.error("Restore failed: %s", e)


# --------------------------------------------------------------------------- #
# Windows power-event hook (pure ctypes, no pywin32)
# --------------------------------------------------------------------------- #

# WM_POWERBROADCAST codes
WM_POWERBROADCAST        = 0x0218
PBT_APMSUSPEND           = 0x0004
PBT_APMRESUMESUSPEND     = 0x0007
PBT_APMRESUMEAUTOMATIC   = 0x0012
PBT_POWERSETTINGCHANGE   = 0x8013

# GUID_CONSOLE_DISPLAY_STATE = {6FE69556-704A-47A0-8F24-C28D936FDA47}
# bytes_le gives the Windows-native mixed-endian struct layout required by
# RegisterPowerSettingNotification (Data1/2/3 little-endian, Data4 as-is).
_DISPLAY_GUID = _uuid.UUID("6FE69556-704A-47A0-8F24-C28D936FDA47")


class _POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", ctypes.c_byte * 16),
        ("DataLength",   ctypes.c_ulong),
        ("Data",         ctypes.c_ulong),
    ]


WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.wintypes.HWND, ctypes.c_uint,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
)


def _make_wnd_proc():
    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                log.info("System suspend detected.")
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                log.info("System resume detected.")
                _schedule_restore()
            elif wparam == PBT_POWERSETTINGCHANGE:
                try:
                    setting = ctypes.cast(
                        lparam, ctypes.POINTER(_POWERBROADCAST_SETTING)
                    ).contents
                    state = setting.Data
                    if state == 0:      # display off
                        log.info("Display off.")
                    elif state == 2:    # display on
                        log.info("Display on.")
                        _schedule_restore()
                except Exception as e:
                    log.warning("Could not parse display state: %s", e)
        elif msg == 0x0002:  # WM_DESTROY
            ctypes.windll.user32.PostQuitMessage(0)
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROCTYPE(wnd_proc)


def _power_event_thread() -> None:
    """Runs a hidden Win32 message loop to receive power broadcast events."""
    user32 = ctypes.windll.user32

    proc = _make_wnd_proc()

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style",         ctypes.c_uint),
            ("lpfnWndProc",   WNDPROCTYPE),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     ctypes.wintypes.HINSTANCE),
            ("hIcon",         ctypes.wintypes.HICON),
            ("hCursor",       ctypes.wintypes.HICON),
            ("hbrBackground", ctypes.wintypes.HBRUSH),
            ("lpszMenuName",  ctypes.wintypes.LPCWSTR),
            ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ]

    wndclass = WNDCLASS()
    wndclass.lpfnWndProc   = proc
    wndclass.lpszClassName = "GlazeWMRestoreWatcher"
    user32.RegisterClassW(ctypes.byref(wndclass))

    HWND_MESSAGE = ctypes.wintypes.HWND(-3)
    hwnd = user32.CreateWindowExW(
        0, "GlazeWMRestoreWatcher", "GlazeWM Restore Watcher",
        0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None
    )

    # Register for display-state power notifications (correct mixed-endian bytes)
    guid_bytes = _DISPLAY_GUID.bytes_le
    guid = (ctypes.c_byte * 16)(*guid_bytes)
    ctypes.windll.user32.RegisterPowerSettingNotification(
        hwnd, ctypes.byref(guid), 0
    )

    log.info("Power event thread running (HWND=%d).", hwnd)

    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    global _loop

    log.info("glazewm-restore starting.")

    # Start Win32 power-event thread
    t = threading.Thread(target=_power_event_thread, daemon=True)
    t.start()

    # Run the asyncio event loop (IPC + restore scheduling) on the main thread
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ipc_loop())
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
