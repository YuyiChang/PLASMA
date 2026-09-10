# MSense reconnect behaviour (internal)

!!! note "Internal design notes"
    Developer reference for how the MSense driver handles a dropped BLE link.
    Operator-facing guidance is in
    [Abnormal handling → BLE drop mid-session](../playbook/abnormal-handling.md).
    Severity/colour rules live in [Failure levels](failure-levels.md).

All reconnect work runs on the **SQC watchdog thread**
(`_sqc_watchdog_loop`, 1 Hz), never on the BLE callback thread. The driver
never gives up on its own: a dropped wristband is retried every
`RECONNECT_SWEEP_S` for the life of the session. There is **no distinct
"reconnect failed" status** — the row stays `🔌 disconnected` and only its
*colour* escalates.

---

## Timeline of a single BLE drop (phase = COLLECTING)

| Stage | Trigger | `memo.sts` | Level / colour | What PLASMA is doing |
|---|---|---|---|---|
| **Drop detected** | bleak `disconnected_callback` → `_on_unexpected_disconnect` | `🔌 disconnected` | **L2 / amber** | nothing yet; waits for the next sweep |
| **Retrying** | watchdog sweep every `RECONNECT_SWEEP_S = 10 s`, if `not p.is_connected` → `_reconnect_peripheral` | `🔌 disconnected` (unchanged) | **L2 / amber** | `disconnect()` → sleep 1.5 s → fresh `BleakClient` → `connect()` (bounded 15 s) → re-negotiate MTU + re-subscribe NUS / battery / ENMO / IMU |
| **Attempt fails** | `connect()` raises or times out | `🔌 disconnected` (unchanged) | **L2 / amber** | logs `reconnect FAILED`, returns; device stays in `active_devices`, retried next sweep |
| **Extended outage** | recorded LSL stream silent → `🟡 stale`, then `> STALE_LOST_AGE ≈ 30 s` → `health_level()` returns L3, folded into the device row | `🔌 disconnected` (still unchanged) | **L3 / red** | still retrying every 10 s — only the colour escalates |
| **Attempt succeeds** | `connect()` returns | `🔄 reconnected` | **NONE / green** | re-subscribed; streaming resumes (gap remains in the recording) |

During **SETUP** (nothing recording yet) a wristband that drops and cannot
reconnect stays at **L2 / amber** — there is no recorded stream to go stale,
and nothing is being lost.

---

## Governing constants / flags

| Name | Value | Where | Effect |
|---|---|---|---|
| `RECONNECT_SWEEP_S` | `10.0 s` | `msense/device.py` | watchdog reconnect-sweep cadence |
| `BLE_OP_TIMEOUT_S` | `15.0 s` | `msense/device.py` | per-call bound on `disconnect()` / `connect()` |
| `STALE_LOST_AGE` | `30.0 s` | `plasma/status.py` | stale recorded stream → L3; drives the amber → red escalation |
| `self.auto_reconnect` | `True` | `msense/device.py` (instance) | master switch for the watchdog sweep |
| `SQC_AUTO_RECONNECT` | `True` | `msense/device.py` (module) | master switch inside `_reconnect_peripheral` |

---

## Does it ever stop retrying?

| Condition | Retries continue? |
|---|---|
| Attempt failed once / many times | **Yes** — no counter, no back-off |
| Row has escalated to L3 / red | **Yes** — the level is display-only |
| `auto_reconnect` or `SQC_AUTO_RECONNECT` set `False` | No |
| Session `disconnect()` called (sets `_sqc_threads_stopped`, clears `active_devices`) | No — the watchdog loop exits |

---

## Entry points that call `_reconnect_peripheral`

| Caller | `reason` string | Context |
|---|---|---|
| watchdog connection sweep | `"connection watchdog"` | link down, checked every `RECONNECT_SWEEP_S` |
| `_sqc_recover` | `"stall: <reason>"` | SQC snapshot stream wedged (no-progress timeout) |
| live-stream watchdog | `"live stall"` | live-view stream stalled |
| `_sqc_quick_check` grace finalize | `"quick-mode grace finalize"` | quick-mode STOP received no END |
| `reconnect_all()` (manual) | `"manual reconnect"` | operator: **Control → Advanced → 🔄 Reconnect now** |

---

## `_reconnect_peripheral` sequence

1. Look up the current client in `active_devices[name]`; bail if gone or
   `SQC_AUTO_RECONNECT` is off.
2. `disconnect()` the old client (bounded; failure logged, not fatal).
3. Sleep 1.5 s.
4. Build a **fresh** `BleakClient` (`_make_client`) — reusing a client across
   a disconnect is unreliable on macOS CoreBluetooth.
5. `connect()` (bounded by `BLE_OP_TIMEOUT_S`).
   - **On failure:** log, set `memo.sts = "🔌 disconnected"`, return. The
     device is left in `active_devices` so the next sweep retries it.
   - **On success:** store the new client in `active_devices[name]`.
6. Re-negotiate MTU (`_ensure_mtu`), then re-subscribe: NUS, battery, and —
   if a recording session is running — ENMO and (when enabled) the IMU
   stream.
7. Set `memo.sts = "🔄 reconnected"`.
