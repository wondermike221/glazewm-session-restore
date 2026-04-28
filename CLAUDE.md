# glazewm-restore

Script restores GlazeWM workspace-to-monitor layout after monitor wake on Windows 11.

## Problem
GlazeWM scrambles workspaces when monitors sleep/wake — Windows briefly report disconnected. `bind_to_monitor` not option; same config shared between home (2 monitors) and work (different layout).

## Approach
- Subscribe GlazeWM WebSocket IPC at ws://127.0.0.1:6123
- Re-snapshot workspace→monitor on every `workspace-activated`, `workspace-deactivated`, `monitor-updated` event
- Register Win32 power broadcast hook (pure ctypes) for display wake
- On wake: wait WAKE_DELAY seconds for GlazeWM settle, issue `move-workspace --direction` to restore layout

## Key files
- `glazewm_restore.py` — main script, run with `uv run glazewm_restore.py`

## Dependencies
- Python 3.11+, uv, websockets (inline via PEP 723 script metadata)
- No pywin32 — power events via pure ctypes

## GlazeWM IPC
- WebSocket on ws://127.0.0.1:6123
- `query monitors` → JSON with monitors[] sorted by x coord
- `sub --events all` → push events with eventType field
- `glazewm.exe command move-workspace --direction <left|right>`