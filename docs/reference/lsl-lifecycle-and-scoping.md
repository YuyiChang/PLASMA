# LSL stream lifecycle & scoping — notes and future work

!!! warning "Internal design notes"
    This is a developer design document about known rough edges, not operator
    guidance. Status: **not implemented.**

Status: **not implemented.** PLASMA currently runs on the default liblsl
session and the built-in recorder captures *every* LSL stream visible on the
machine/LAN. This document records the known rough edges and the agreed
direction so the work can be picked up later.

---

## Current lifecycle

### Outlets (PLASMA → network)

| Outlet | Created | Destroyed |
|---|---|---|
| Journal `"PLASMA"` (`open_journal_outlet`) | `IntegratedPanel.__init__`, once | never before process exit |
| bitalino / shimmer / qb2 / pupil ×3 | device `__init__` (from `init_devices`) | only when the device object is garbage-collected |
| `MsenseOutlet` ×N | `MotionSenseHRV.connect_devices()` (from `__init__`) | `disconnect()` does `self.active_outlets = {}`, then GC |

`pylsl.StreamOutlet` has no `.close()` — it stops advertising only when its
last Python reference drops and `__del__` → `lsl_destroy_outlet` runs.

### Inlets (network → PLASMA)

| Consumer | Lifetime |
|---|---|
| `IntegratedPanel._lsl_resolver` (`ContinuousResolver`) | created once in `__init__`; never reset; entries self-expire on liblsl's forget timeout (~5 s) |
| `SessionRecorder` inlets + its own `ContinuousResolver` | created on Start; one inlet per resolved stream; **kept for the whole session** even after a source vanishes (marked `🔴 lost`); `status()` returns the full accumulated list until Stop |

---

## Known gaps

1. **Re-Initialize does not reliably tear down old outlets.**
   `init_devices()` calls `dev.stop()` + `dev.disconnect()` then
   `self.available_devices = []`. But base `PlasmaDevice.disconnect()` is a
   no-op and bitalino/shimmer/qb2/pupil never null `self.outlet`; they rely on
   GC of the device object. A streaming thread stuck in a blocking read
   (pupil's `receive_eye_events()`, etc.) keeps the device — and its outlet —
   alive indefinitely. Result: same-named zombie outlets accumulate; a
   resolver/recorder can bind the stale one.

2. **`MotionSenseHRV.disconnect()` never calls `_stop_ble_loop()`** — the
   dedicated BLE asyncio event-loop thread leaks on every re-init (holds the
   old `MotionSenseHRV` alive; the outlets themselves are dereferenced via
   `active_outlets = {}` so usually still collected).

3. **The pre-Start resolver is never cleared** — stale entries (including
   PLASMA's own just-killed outlets) linger until liblsl forgets them.

4. **The recorder accumulates streams for the whole session by design** —
   nothing short of Stop drops a `🔴 lost` stream from `status()` / the memo
   panel.

5. **Test contamination.** `plasma/tests/test_lsl_recorder.py` creates real
   loopback/multicast LSL outlets (`Ext_…`, `Own_…`, `Markers_…`, `Early_…`,
   `Late_…`, `Keep_…`, `Drop_…`). Running `pytest` on the same machine/LAN
   while a PLASMA recording is active **writes those test streams into the
   `.xdf`** — the recorder is on the default session, so is `pytest`. Verified.

---

## Future work

### A. Outlet teardown + external-stream refresh on Initialize (small)

- Give every device an explicit outlet release (fold into `disconnect()` or a
  new `release_streams()`): null **all** outlet refs (`self.outlet = None`,
  `self.active_outlets.clear()`), not just stop the thread. Null the outlet
  independent of the streaming thread actually exiting — its next
  `push_sample()` then throws and the daemon thread dies quietly.
- `init_devices()`: after the stop/disconnect loop, `gc.collect()` + a short
  settle (~0.3 s) before constructing new same-named outlets.
- `MotionSenseHRV.disconnect()` → also `self._stop_ble_loop()` (idempotent).
- `init_devices()` → `self._lsl_resolver = None` so `_external_lsl_streams()`
  rebuilds a fresh `ContinuousResolver` (flushes stale external entries).
- **Guard:** block or warn on Initialize while `self.lsl_recorder` is active —
  destroying a PLASMA outlet that the recorder holds an inlet to can crash
  liblsl (`lsl_destroy_outlet` abort — hit in the test suite).

### B. liblsl SessionID scoping (the main one)

liblsl `[lab] SessionID` in `lsl_api.cfg` hard-partitions the network: an
outlet and an inlet see each other **only** if their SessionIDs match.
Verified on this machine — a resolver on session `A` sees only session-`A`
streams, not the default-session PLASMA app running alongside.

- pylsl 1.17.6 has **no programmatic setter**. The only mechanism: write a cfg
  file and point `LSLAPICFG` (env var, a file path) at it **before the first
  liblsl call**. Search order otherwise: `$LSLAPICFG` → `./lsl_api.cfg` →
  `~/lsl_api.cfg` → `/etc/lsl_api/lsl_api.cfg`; loaded once, lazily.
- `app_context` gains `lsl_session_id: str`, default `"PLASMA"`; `"default"`
  (or empty) restores today's "capture the whole network" behaviour. Wrapper
  apps (YAMS) override it via `configure()` like `journal_stream`.
- Bootstrap at the very top of `plasma/__main__.py` (and the PyInstaller
  entrypoint), before `plugins.load_plugins()` and before any module-level
  `import pylsl`:
  - if `lsl_session_id` is non-default and the user hasn't set
    `LSLAPICFG`/`~/lsl_api.cfg`: write `<data_dir>/lsl_api.cfg` with
    `[lab]\nSessionID = <id>` and set `os.environ["LSLAPICFG"]`.
  - if the user already has their own config, defer to it; warn on SessionID
    mismatch.
  - if `"pylsl" in sys.modules` already, warn that it's too late.
  - one startup log line explaining the scope and the `=default` opt-out.
- Effect: the recording contains exactly what PLASMA (plus anything the
  operator explicitly enrolls in the `PLASMA` session) produced — reproducible
  regardless of what else is on the LAN. A separate LabRecorder can be scoped
  to `SessionID = PLASMA` to capture the same set, or left default and
  isolated.
- Caveats: ordering is load-bearing (needs a guard so a future module-level
  `import pylsl` doesn't silently break it); `_external_lsl_streams()`'s
  "🛰 external" rows change meaning to "enrolled in the PLASMA session but not
  one of our devices"; SessionID filters connections, not multicast chatter.

### C. Test-harness SessionID isolation (do independently of B)

`plasma/conftest.py`: in a `pytest_configure` hook (before pylsl import), write
a throwaway `lsl_api.cfg` with `SessionID = pytest-<pid>-<rand>` and set
`LSLAPICFG`. Test outlets ↔ test inlets still see each other (same process
config) but become invisible to any real PLASMA app / LabRecorder, and vice
versa. This is correct regardless of what B's product default ends up being.
