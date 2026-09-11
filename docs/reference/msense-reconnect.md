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
| **Drop detected** | bleak `disconnected_callback` → `_on_unexpected_disconnect(name, client)` | `🔌 disconnected` | **L2 / amber** | nothing yet; waits for the next sweep. The callback is guarded — it ignores a `client` that is not the current `_live_client[name]`, and collapses repeats to one per `DISC_LOG_DEBOUNCE_S` |
| **Retrying** | watchdog sweep every `RECONNECT_SWEEP_S = 10 s`, if `not p.is_connected` → `_reconnect_peripheral` | `🔌 disconnected` (unchanged) | **L2 / amber** | retire old client → (Linux) `bluetoothctl remove` the peer → sleep 1.5 s → fresh `BleakClient` → `connect()` (bounded 15 s) → re-negotiate MTU + re-subscribe NUS / battery / ENMO / IMU |
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
| `DISC_LOG_DEBOUNCE_S` | `5.0 s` | `msense/device.py` | max one `disconnected unexpectedly` log + memo update per wristband per window |
| `CLIENT_RETIRE_TIMEOUT_S` | `5.0 s` | `msense/device.py` | bound on the best-effort `disconnect()` of a client being abandoned |

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

1. Look up the current client (`active_devices[name]`, else `_live_client[name]`);
   bail if gone or `SQC_AUTO_RECONNECT` is off.
2. **`_retire_client(name, old)`** — drop the old client's disconnect callback
   (`_backend.set_disconnected_callback(None)`, best-effort), remove it from
   `_live_client`, and `disconnect()` it (bounded by `CLIENT_RETIRE_TIMEOUT_S`).
   A retired client can no longer drive `_on_unexpected_disconnect` even if it
   keeps firing.
3. **`_bluez_release_peer(addr)`** — Linux only: `bluetoothctl remove <addr>`
   so `bluetoothd` forgets the peer (clears any half-open ACL + stale device
   object + GATT cache). No-op on macOS / Windows.
4. Sleep 1.5 s.
5. Build a **fresh** `BleakClient` (`_make_client`); set `_live_client[name]` to
   it. Reusing a client across a disconnect is unreliable on CoreBluetooth, and
   on BlueZ a stale client keeps a live D-Bus subscription.
6. `connect()` (bounded by `BLE_OP_TIMEOUT_S`).
   - **On failure:** log, `_retire_client(name, peripheral)` (so the failed
     attempt doesn't leak), set `memo.sts = "🔌 disconnected"`, return. The
     device is left in `active_devices` so the next sweep retries it.
   - **On success:** store the new client in `active_devices[name]` **and**
     `_live_client[name]`.
7. Re-negotiate MTU (`_ensure_mtu`), then re-subscribe: NUS, battery, and —
   if a recording session is running — ENMO and (when enabled) the IMU
   stream.
8. Set `memo.sts = "🔄 reconnected"`.

`connect_devices` and `disconnect()` retire every client they drop, and
`connect_devices` clears `_live_client` up front — a re-scan / re-connect
from the Configuration tab never leaks the previous pass's clients.

---

## Linux / BlueZ failure mode

Observed on a Jetson Orin Nano (`data_test/jetson`, 4 wristbands, ~1 h): a
load/RF event dropped several wristbands at once, and the reconnect loop then
turned a recoverable blip into a dead Bluetooth stack that **a PLASMA restart
could not clear** — only a `systemctl restart bluetooth` / adapter power-cycle
did.

**The chain**

1. Failed/aborted reconnects left the old `BleakClient` undisposed. On BlueZ
   each holds a D-Bus `PropertiesChanged` subscription on the peer, so every
   `Connected` blip (and BlueZ blips repeatedly — it races its own auto-connect
   with PLASMA's explicit `connect()` → `br-connection-canceled`) fanned out as
   **20–40 `_on_unexpected_disconnect` calls per attempt** for one wristband.
2. That storm saturated the single BLE event loop, so the next
   `connect()` hit `BLE_OP_TIMEOUT_S` → another undisposed client + another
   half-open ACL → the storm grew.
3. Half-open ACLs / pending connections at the `bluetoothd` + kernel level
   **outlive the PLASMA process**, so a restart reconnected straight into
   `br-connection-canceled` / transient "device not found."

Discovery was never the problem — every scan, including after each restart,
listed all four wristbands with correct addresses.

**Signatures in the log**

| Text | Meaning |
|---|---|
| `disconnected unexpectedly` repeated 20–40× at ~0.5 s for one wristband | the callback storm (undisposed clients) |
| `BLE op timed out after 15s` | `connect()` starved on the saturated event loop |
| `[org.bluez.Error.Failed] br-connection-canceled` | BlueZ already has a pending/half-open connection to that peer |
| `failed to discover services, device disconnected` | ACL connected, peer dropped during GATT discovery |
| `D-Bus call timed out` | BlueZ/D-Bus already sluggish on this host |

**Mitigations now in the driver** (fixes 1–3 of the consolidated proposal)

- **One live client per wristband** (`_live_client`); `_on_unexpected_disconnect`
  ignores any other → the storm cannot come from retired clients.
- **Debounce** (`DISC_LOG_DEBOUNCE_S`) → at most one log + memo update per
  wristband per 5 s even if the *current* client blips.
- **`_retire_client`** on every abandoned client → its callback is dropped and
  its link closed, so D-Bus subscriptions and BlueZ links don't accumulate.
- **`_bluez_release_peer`** before each reconnect (Linux) → `bluetoothctl
  remove` clears the half-open ACL / stale object that survived a restart.

**Still on the list** (not yet implemented — tiers 2–4 of the proposal):
adapter power-cycle / `systemctl restart bluetooth` escalation + a manual
"Reset Bluetooth" button; parallel (capped) reconnects off the watchdog thread
with back-off; stop the tight loop after ~2 min; Jetson-side power-management /
BlueZ-version / MTU tuning.
