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
    uv run glazewm-restore.py --debug     # verbose logging + console output

Or deploy as a background Task Scheduler job:
    deploy.ps1
"""

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import faulthandler
import json
import logging
import shutil
import subprocess
import threading
import uuid as _uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# CLI args (parsed early so logging is configured before anything runs)
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GlazeWM workspace restore daemon")
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging and echo all events to stdout",
    )
    p.add_argument(
        "--wake-delay", type=float, default=3.0, metavar="SECS",
        help="Seconds to wait after display-on before restoring (default: 3.0)",
    )
    return p.parse_args()

ARGS = _parse_args()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# Locate glazewm.exe — check PATH first, then common install locations.
def _find_glazewm() -> Path:
    found = shutil.which("glazewm")
    if found:
        return Path(found)
    candidates = [
        Path(r"C:\Program Files\glzr.io\GlazeWM\cli\glazewm.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "GlazeWM" / "glazewm.exe",
        Path(r"C:\Program Files\GlazeWM\glazewm.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "glazewm.exe not found on PATH or in common install locations. "
        "Make sure GlazeWM is installed and on your PATH."
    )

GLAZEWM_EXE = _find_glazewm()
IPC_URI = "ws://127.0.0.1:6123"
LOG_FILE = Path.home() / ".glzr" / "glazewm" / "restore.log"
WAKE_DELAY = ARGS.wake_delay

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

_log_level = logging.DEBUG if ARGS.debug else logging.INFO
_handlers: list[logging.Handler] = [
    logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
]
if ARGS.debug:
    _handlers.append(logging.StreamHandler())  # also print to console

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

# Live snapshot: workspace name → monitor index (0 = leftmost).
# Updated on every layout IPC event UNLESS displays are sleeping.
_saved: dict[str, int] = {}
_saved_lock = threading.Lock()

# Snapshot captured the moment displays go off — this is what we restore to.
# Set by the power-event thread OR the IPC loop (whichever fires first).
_pre_sleep: dict[str, int] = {}

# When True, _ipc_loop skips updating _saved (displays are sleeping/waking).
_frozen = False
_frozen_lock = threading.Lock()

# Previous monitor count — used by _ipc_loop to detect disconnects before
# the power notification arrives (the IPC event fires first in practice).
_prev_monitor_count: int = 0
_prev_monitor_count_lock = threading.Lock()

# Prevents duplicate restore coroutines when both the IPC reconnect event
# and the power-on notification both fire for the same wake cycle.
_restore_pending = False
_restore_pending_lock = threading.Lock()

# asyncio event loop reference — set once the loop starts
_loop: asyncio.AbstractEventLoop | None = None

# Win32-side monitor count — initialised from GetSystemMetrics at thread start,
# then updated on every WM_DISPLAYCHANGE. Separate from _prev_monitor_count (IPC)
# so the two detection paths don't race each other.
_win32_monitor_count: int = 0
_win32_monitor_count_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# GlazeWM CLI helpers
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
        log.debug("_ws_query: dropped interleaved event %s while waiting for response to %r",
                  msg.get("eventType"), command)


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


def _monitor_summary(monitors: list[dict]) -> str:
    """Human-readable monitor list for debug logs."""
    sorted_monitors = sorted(monitors, key=lambda m: m.get("x", 0))
    parts = []
    for i, m in enumerate(sorted_monitors):
        ws_names = [w.get("name", "?") for w in m.get("children", [])]
        parts.append(f"mon{i}(x={m.get('x',0)})=[{','.join(ws_names)}]")
    return "  ".join(parts) if parts else "(no monitors)"


def _do_restore(saved: dict[str, int], monitors: list[dict]) -> None:
    sorted_monitors = sorted(monitors, key=lambda m: m.get("x", 0))
    current: dict[str, int] = {}
    for idx, mon in enumerate(sorted_monitors):
        for ws in mon.get("children", []):
            name = ws.get("name")
            if name:
                current[name] = idx

    log.info("Restore — target: %s  current: %s", saved, current)

    any_moved = False
    for ws_name, target_idx in saved.items():
        current_idx = current.get(ws_name)
        if current_idx is None:
            log.warning("Restore: workspace %r not found in current layout — skipping", ws_name)
            continue
        if current_idx == target_idx:
            log.debug("Restore: %r already on monitor %d — skip", ws_name, target_idx)
            continue
        direction = "right" if current_idx < target_idx else "left"
        steps = abs(target_idx - current_idx)
        log.info("Restore: moving %r %s×%d (mon %d → mon %d)",
                 ws_name, direction, steps, current_idx, target_idx)
        for _ in range(steps):
            _run_cmd("command", f"focus --workspace {ws_name}")
            _run_cmd("command", f"move-workspace --direction {direction}")
        any_moved = True

    if not any_moved:
        log.info("Restore: nothing to move — layout already matches saved state.")


# --------------------------------------------------------------------------- #
# GlazeWM IPC WebSocket loop
# --------------------------------------------------------------------------- #

# All events — logged in debug mode so we can see the full sequence
_ALL_EVENTS_LOGGED = True

async def _ipc_loop() -> None:
    """
    Connects to GlazeWM IPC, subscribes to all events, and keeps _saved
    up-to-date. Reconnects automatically if GlazeWM restarts.

    Key design: monitor disconnect is detected HERE (via count change on
    monitor-updated) rather than waiting for the power notification, because
    the IPC event fires first. The power notification is a backup.
    """
    import websockets

    global _prev_monitor_count

    while True:
        try:
            async with websockets.connect(IPC_URI) as ws:
                log.info("Connected to GlazeWM IPC.")

                # 1. Initial snapshot
                data = await _ws_query(ws, "query monitors")
                monitors = data.get("data", {}).get("monitors", [])
                snapshot = _parse_snapshot(monitors)
                with _saved_lock:
                    _saved.clear()
                    _saved.update(snapshot)
                with _prev_monitor_count_lock:
                    _prev_monitor_count = len(monitors)
                log.info("Initial snapshot: %s  [%d monitor(s)]", snapshot, len(monitors))
                log.debug("Initial monitor layout: %s", _monitor_summary(monitors))

                # 2. Subscribe
                sub_ack = await _ws_query(ws, "sub --events all")
                log.info("Subscribed (id=%s)", sub_ack.get("data", {}).get("subscriptionId"))

                # 3. Event loop
                LAYOUT_EVENTS = {
                    "workspace-activated",
                    "workspace-deactivated",
                    "monitor-updated",
                    "wm-restarted",
                }

                async for message in ws:
                    event = json.loads(message)
                    event_type = event.get("eventType", "")

                    if ARGS.debug:
                        log.debug("IPC event: %s", event_type)

                    if event_type not in LAYOUT_EVENTS:
                        continue

                    with _frozen_lock:
                        frozen = _frozen

                    if frozen:
                        log.info(
                            "SNAPSHOT FROZEN — ignoring %s. _saved=%s  _pre_sleep=%s",
                            event_type, _saved, _pre_sleep,
                        )
                        continue

                    # Query current monitor state
                    data = await _ws_query(ws, "query monitors")
                    monitors = data.get("data", {}).get("monitors", [])
                    new_count = len(monitors)

                    with _prev_monitor_count_lock:
                        prev_count = _prev_monitor_count
                        _prev_monitor_count = new_count

                    # ── Disconnect detected ──────────────────────────────────
                    # The IPC event fires before the power notification arrives,
                    # so we freeze here to guarantee _saved isn't overwritten
                    # with the scrambled post-disconnect layout.
                    if event_type == "monitor-updated":
                        log.info(
                            "monitor-updated: count %d→%d  layout: %s",
                            prev_count, new_count, _monitor_summary(monitors),
                        )

                    if event_type == "monitor-updated" and prev_count > 0 and new_count < prev_count:
                        log.info(
                            "IPC disconnect: monitor count %d→%d — "
                            "FREEZING snapshot NOW (power notification is too late).",
                            prev_count, new_count,
                        )
                        _freeze_snapshot()
                        continue  # Do NOT update _saved

                    # ── Reconnect detected ───────────────────────────────────
                    # Handles cases where no power-ON notification arrives
                    # (e.g. NirSoft software disconnect, or rapid reconnect).
                    if event_type == "monitor-updated" and prev_count > 0 and new_count > prev_count and _pre_sleep:
                        log.info(
                            "IPC reconnect: monitor count %d→%d — "
                            "scheduling restore (IPC-triggered).",
                            prev_count, new_count,
                        )
                        _unfreeze_snapshot()
                        _schedule_restore()
                        continue  # Don't update _saved — restore will handle it

                    # ── Normal snapshot update ───────────────────────────────
                    snapshot = _parse_snapshot(monitors)
                    with _saved_lock:
                        old = dict(_saved)
                        _saved.clear()
                        _saved.update(snapshot)

                    log.info("Snapshot updated on %s: %s", event_type, snapshot)
                    if ARGS.debug and snapshot != old:
                        log.debug("  was: %s", old)
                        log.debug("  monitor detail: %s", _monitor_summary(monitors))

        except (OSError, Exception) as e:
            log.warning("IPC disconnected (%s). Retrying in 5s...", e)
            await asyncio.sleep(5)


# --------------------------------------------------------------------------- #
# Restore trigger
# --------------------------------------------------------------------------- #

def _freeze_snapshot() -> None:
    """
    Called from power-event thread when displays go off.
    Captures the current layout as _pre_sleep and freezes further updates.
    """
    global _pre_sleep
    with _frozen_lock:
        global _frozen
        _frozen = True
    with _saved_lock:
        _pre_sleep = dict(_saved)
    log.info("Display off — snapshot FROZEN. Pre-sleep layout: %s", _pre_sleep)


def _unfreeze_snapshot() -> None:
    """Called from power-event thread when displays come back on."""
    with _frozen_lock:
        global _frozen
        _frozen = False
    log.info("Display on — snapshot UNFROZEN.")


def _schedule_restore() -> None:
    """
    Called from the Win32 message thread or the IPC loop.
    Deduplicates: if a restore is already pending (e.g. both the IPC
    reconnect event and the power-ON notification fire for the same wake),
    the second call is silently dropped.
    """
    global _restore_pending
    if _loop is None:
        return
    with _restore_pending_lock:
        if _restore_pending:
            log.info("Restore already pending — ignoring duplicate trigger.")
            return
        _restore_pending = True
    restore_target = dict(_pre_sleep) if _pre_sleep else None
    asyncio.run_coroutine_threadsafe(_restore_after_delay(restore_target), _loop)


async def _restore_after_delay(restore_target: dict[str, int] | None) -> None:
    global _restore_pending
    import websockets

    if restore_target:
        log.info("Wake detected — will restore to pre-sleep layout %s in %.1fs",
                 restore_target, WAKE_DELAY)
    else:
        log.warning("Wake detected but no pre-sleep snapshot — falling back to _saved.")
        with _saved_lock:
            restore_target = dict(_saved)

    await asyncio.sleep(WAKE_DELAY)

    try:
        async with websockets.connect(IPC_URI) as ws:
            data = await _ws_query(ws, "query monitors")
            monitors = data.get("data", {}).get("monitors", [])
            log.info("Post-wake monitor layout: %s", _monitor_summary(monitors))
            _do_restore(restore_target, monitors)
            await _unminimize_windows(ws)
            log.info("Restore complete.")
    except Exception as e:
        log.error("Restore failed: %s", e)
    finally:
        with _restore_pending_lock:
            _restore_pending = False


async def _unminimize_windows(ws) -> None:
    """
    Un-minimize any windows that Windows minimized when monitors disconnected.
    GlazeWM doesn't restore them automatically, so tiled layouts look broken
    even after workspaces are moved back to the correct monitor.
    """
    try:
        data = await _ws_query(ws, "query windows")
        windows = data.get("data", {}).get("windows", [])
        for win in windows:
            if win.get("state", "").lower() == "minimized":
                win_id = win.get("id")
                title = win.get("title", "?")[:60]
                log.info("Un-minimizing: %r (id=%s)", title, win_id)
                await _ws_query(ws, f"command --id {win_id} toggle-minimized")
    except Exception as e:
        log.warning("Un-minimize pass failed: %s", e)


# --------------------------------------------------------------------------- #
# Windows power-event hook (pure ctypes, no pywin32)
# --------------------------------------------------------------------------- #

WM_POWERBROADCAST        = 0x0218
WM_DISPLAYCHANGE         = 0x007E   # fired when display configuration changes
PBT_APMSUSPEND           = 0x0004
PBT_APMRESUMESUSPEND     = 0x0007
PBT_APMRESUMEAUTOMATIC   = 0x0012
PBT_POWERSETTINGCHANGE   = 0x8013

SM_CMONITORS = 80   # GetSystemMetrics index for monitor count

# Display state values in PBT_POWERSETTINGCHANGE
_DISPLAY_OFF     = 0
_DISPLAY_DIM     = 1
_DISPLAY_ON      = 2

# GUID_CONSOLE_DISPLAY_STATE = {6FE69556-704A-47A0-8F24-C28D936FDA47}
_DISPLAY_GUID = _uuid.UUID("6FE69556-704A-47A0-8F24-C28D936FDA47")

_DISPLAY_STATE_NAMES = {0: "OFF", 1: "DIM", 2: "ON"}


class _POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", ctypes.c_byte * 16),
        ("DataLength",   ctypes.c_ulong),
        ("Data",         ctypes.c_ulong),
    ]


# On 64-bit Windows, LRESULT and LPARAM are LONG_PTR (64-bit signed).
# ctypes.wintypes.LPARAM is c_long (32-bit), which overflows on x64 when
# Windows passes a struct pointer as lparam. Use c_ssize_t (pointer-sized).
_LRESULT = ctypes.c_ssize_t
_LPARAM  = ctypes.c_ssize_t

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    _LRESULT, ctypes.wintypes.HWND, ctypes.c_uint,
    ctypes.wintypes.WPARAM, _LPARAM,
)

# Set argtypes on DefWindowProcW to match — prevents the same overflow
# when we forward unhandled messages back to the default handler.
ctypes.windll.user32.DefWindowProcW.restype  = _LRESULT
ctypes.windll.user32.DefWindowProcW.argtypes = [
    ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, _LPARAM,
]

# Sequence counter — every power event gets an incrementing number so
# you can spot ordering issues in the log immediately.
_power_seq = 0
_power_seq_lock = threading.Lock()

def _next_seq() -> int:
    global _power_seq
    with _power_seq_lock:
        _power_seq += 1
        return _power_seq


def _make_wnd_proc():
    def wnd_proc(hwnd, msg, wparam, lparam):
        # Blanket try/except: a ctypes callback that raises an unhandled
        # exception causes a hard segfault that kills the entire process with
        # no log output. The Dell U3219Q sends a storm of USB messages when
        # its power button is pressed (fingerprint reader, KVM hub, DP Alt-Mode
        # all cycling together), so we must never let any of them crash us.
        try:
            if msg == WM_POWERBROADCAST:
                seq = _next_seq()

                if wparam == PBT_APMSUSPEND:
                    log.info("[PWR #%d] System SUSPEND", seq)
                    _freeze_snapshot()

                elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    label = "RESUME_SUSPEND" if wparam == PBT_APMRESUMESUSPEND else "RESUME_AUTOMATIC"
                    log.info("[PWR #%d] System %s", seq, label)
                    _unfreeze_snapshot()
                    _schedule_restore()

                elif wparam == PBT_POWERSETTINGCHANGE:
                    try:
                        setting = ctypes.cast(
                            ctypes.c_void_p(lparam),
                            ctypes.POINTER(_POWERBROADCAST_SETTING),
                        ).contents
                        state = setting.Data
                        state_name = _DISPLAY_STATE_NAMES.get(state, f"UNKNOWN({state})")
                        log.info("[PWR #%d] Display state → %s", seq, state_name)

                        if state == _DISPLAY_OFF:
                            _freeze_snapshot()
                        elif state == _DISPLAY_ON:
                            _unfreeze_snapshot()
                            _schedule_restore()
                        elif state == _DISPLAY_DIM:
                            with _frozen_lock:
                                currently_frozen = _frozen
                            if currently_frozen:
                                log.info("[PWR #%d] Display DIM while frozen — treating as wake", seq)
                                _unfreeze_snapshot()
                                _schedule_restore()
                            else:
                                log.debug("[PWR #%d] Display DIM (not frozen — ignored)", seq)

                    except Exception as e:
                        log.warning("[PWR #%d] Could not parse POWERSETTINGCHANGE: %s", seq, e)

                else:
                    log.debug("[PWR #%d] WM_POWERBROADCAST wparam=0x%X (unhandled)", seq, wparam)

            elif msg == WM_DISPLAYCHANGE:
                # WM_DISPLAYCHANGE is broadcast to ALL top-level (non-message-only)
                # windows when the display configuration changes — monitor
                # connect/disconnect, resolution change, etc.  It fires at the same
                # instant GlazeWM learns about the change, so freezing here prevents
                # the IPC loop from overwriting _saved with the scrambled layout.
                seq = _next_seq()
                new_count = ctypes.windll.user32.GetSystemMetrics(SM_CMONITORS)
                with _win32_monitor_count_lock:
                    global _win32_monitor_count
                    old_count = _win32_monitor_count
                    _win32_monitor_count = new_count
                with _frozen_lock:
                    currently_frozen = _frozen
                log.info(
                    "[DISP #%d] WM_DISPLAYCHANGE monitors %d→%d (frozen=%s)",
                    seq, old_count, new_count, currently_frozen,
                )
                if old_count > 0 and new_count < old_count and not currently_frozen:
                    log.info("[DISP #%d] Monitor count dropped — FREEZING snapshot NOW", seq)
                    _freeze_snapshot()
                elif old_count > 0 and new_count > old_count and currently_frozen:
                    log.info("[DISP #%d] Monitor count recovered — scheduling restore", seq)
                    _unfreeze_snapshot()
                    _schedule_restore()

            elif msg == 0x0002:  # WM_DESTROY
                ctypes.windll.user32.PostQuitMessage(0)

            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        except Exception:
            # Log the crash but return 0 so Windows doesn't kill the process.
            log.exception("CRASH in wndproc (msg=0x%X wparam=0x%X) — recovered", msg, wparam)
            return 0

    return WNDPROCTYPE(wnd_proc)


def _power_event_thread() -> None:
    """Runs a hidden Win32 message loop to receive power broadcast events."""
    try:
        _power_event_thread_inner()
    except Exception:
        log.exception("FATAL: power event thread crashed — power notifications disabled.")


def _power_event_thread_inner() -> None:
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

    # IMPORTANT: do NOT use HWND_MESSAGE (-3) as the parent here.
    # Message-only windows are excluded from broadcast message routing, so they
    # never receive WM_DISPLAYCHANGE (or WM_DEVICECHANGE, etc.).  A real
    # WS_POPUP window with hWndParent=None receives all broadcasts while still
    # being invisible (we never call ShowWindow).
    WS_POPUP = 0x80000000
    user32.CreateWindowExW.restype = ctypes.wintypes.HWND
    hwnd = user32.CreateWindowExW(
        0, "GlazeWMRestoreWatcher", "GlazeWM Restore Watcher",
        WS_POPUP, 0, 0, 1, 1, None, None, None, None,
    )

    # Seed the Win32 monitor count so WM_DISPLAYCHANGE can detect direction.
    with _win32_monitor_count_lock:
        global _win32_monitor_count
        _win32_monitor_count = user32.GetSystemMetrics(SM_CMONITORS)
    log.info("Initial Win32 monitor count: %d", _win32_monitor_count)

    guid_bytes = _DISPLAY_GUID.bytes_le
    guid = (ctypes.c_byte * 16)(*guid_bytes)
    ctypes.windll.user32.RegisterPowerSettingNotification(
        hwnd, ctypes.byref(guid), 0
    )

    log.info("Power event thread running (HWND=%d) — receiving WM_DISPLAYCHANGE.", hwnd)

    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _install_crash_logger() -> None:
    """Log any unhandled exception to the file before the process dies."""
    import sys
    orig = sys.excepthook
    def _hook(exc_type, exc_value, exc_tb):
        log.critical("UNHANDLED EXCEPTION — process will exit.", exc_info=(exc_type, exc_value, exc_tb))
        logging.shutdown()
        orig(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook

    # Also catch exceptions on non-main threads (Python 3.8+)
    orig_thread = threading.excepthook
    def _thread_hook(args):
        log.critical("UNHANDLED EXCEPTION in thread %s", args.thread, exc_info=(
            args.exc_type, args.exc_value, args.exc_traceback))
        orig_thread(args)
    threading.excepthook = _thread_hook


def main() -> None:
    global _loop

    _install_crash_logger()

    # faulthandler writes a C-level traceback to crash.log on segfault / stack
    # overflow — catches native SEH faults that Python's try/except cannot see.
    _crash_file = LOG_FILE.parent / "crash.log"
    try:
        faulthandler.enable(file=open(str(_crash_file), "w"), all_threads=True)
        log.info("faulthandler enabled — crash dumps → %s", _crash_file)
    except Exception as e:
        log.warning("faulthandler.enable failed: %s", e)

    log.info("glazewm-restore starting (debug=%s, wake_delay=%.1fs).", ARGS.debug, WAKE_DELAY)
    log.info("glazewm.exe: %s", GLAZEWM_EXE)

    t = threading.Thread(target=_power_event_thread, daemon=True)
    t.start()

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ipc_loop())
    except KeyboardInterrupt:
        log.info("Shutting down.")
    except Exception:
        log.exception("FATAL: main loop crashed.")
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
