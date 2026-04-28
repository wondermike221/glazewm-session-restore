# glazewm-restore

Background script that restores GlazeWM workspace-to-monitor layout after
monitor wake events on Windows 11.

## Problem
GlazeWM scrambles workspaces across monitors when monitors sleep/wake because
Windows briefly reports them as disconnected. `bind_to_monitor` in the config
is not an option since the same config is shared between home (2 monitors) and
work (different layout).

## Approach
- Subscribe to GlazeWM's WebSocket IPC at ws://127.0.0.1:6123
- Re-snapshot workspace→monitor layout on every `workspace-activated`,
  `workspace-deactivated`, and `monitor-updated` event
- Register a Win32 power broadcast hook (pure ctypes) to detect display wake
- On wake, wait WAKE_DELAY seconds for GlazeWM to settle, then issue
  `move-workspace --direction` commands to restore layout

## Key files
- `glazewm_restore.py` — main script, run with `uv run glazewm_restore.py`

## Dependencies
- Python 3.11+, uv, websockets (declared inline via PEP 723 script metadata)
- No pywin32 — power events done with pure ctypes

## GlazeWM IPC
- WebSocket on ws://127.0.0.1:6123
- `query monitors` → JSON with monitors[] sorted by x coord
- `sub --events all` → push events with eventType field
- `glazewm.exe command move-workspace --direction <left|right>`
