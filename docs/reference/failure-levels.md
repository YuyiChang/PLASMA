# PLASMA instrumentation failure levels

**Internal design doc.** Levels 1–3 are never shown to the operator — the memo
panel renders only a colour. The levels exist to make the *software logic*
consistent: one classifier (`plasma/status.py`), one table, predictable
sticky-vs-auto-clearing behaviour.

One important design feature of PLASMA is to provide a centralized status display when instrumenting a wide array of sensors. The design is heavily inspired by modern aviation glass cockpit, especially [Airbus ECAM](https://en.wikipedia.org/wiki/Electronic_centralised_aircraft_monitor).

| ECAM | PLASMA |
|---|---|
| L3 Warning — red, continuous chime, immediate action, sticky until resolved + acknowledged | **L3** — capture stopped mid-collection / unrecoverable / integrity compromised |
| L2 Caution — amber, single chime, awareness now, deferred action, auto-clears | **L2** — interrupted but recovering, or a one-click operator fix |
| L1 Caution — amber, no attention-getter, monitor, redundancy loss | **L1** — capture continues; degradation or drift |
| Advisory — white pulsing, a value drifting out of range | folded into **L1** |
| Flight-phase inhibition (T/O, LDG) | **phase gate** — SETUP / COLLECTING / STOPPED |

---

## The model

`plasma/status.py` `classify(sts, phase, *, stream_health, srate, stale_age)`
returns **`(Level, category)`**:

### Internal severity `Level`

| Level | Meaning for a data source | Sticky? | Escalation |
|---|---|---|---|
| **L3 — WARNING** | Capture is stopped **during an active collection**, unrecoverable, or the recording's integrity is compromised. PLASMA will not fix it without operator action. | yes | — |
| **L2 — CAUTION** | Interrupted/degraded, but PLASMA is autonomously recovering it, or one obvious operator action restores it, with the rest of the session intact. | no — auto-clears on recovery | → L3 if recovery fails / a timeout expires |
| **L1 — ADVISORY** | Capture continues nominally; a redundancy is reduced or a monitored value is drifting toward a limit. | no — clears on resolve | → L2 if the value crosses a hard limit |
| **NONE** | Healthy, idle/ready, a *cleared* caution, a clean operator-initiated stop, external streams. | — | — |

### Presentation `category` → colour (`CATEGORY_HEX`)

| Category | Colour | Hex | Shown for |
|---|---|---|---|
| `healthy` | green | `#15803d` | capturing normally |
| `info` | neutral ("white") | `#4b5563` | status / idle / ready / an **expected** stop |
| `advisory` | blue | `#2563eb` | L1 that implies an operator action (re-Initialize, reconnect, retry, low battery) |
| `caution` | amber | `#a16207` | L2, and monitor-only L1 |
| `warning` | red | `#b91c1c` | L3 |
| `guidance` | purple | `#7c3aed` | a special operator instruction, orthogonal to level: a sub-line accent (recorder stats), or a transient Initializing/Starting/Stopping message on the session banner or a device row while a slow multi-second operation is still in flight |
| `external` | grey | `#6b7280` | an LSL stream on the network that isn't ours |

`info` = "white" in the aviation sense of *uncoloured / default text*, rendered
as a neutral grey that reads on both light and dark themes.

### Phase gate

`phase` comes from `IntegratedPanel.phase` (`_collection_started` /
`_collection_stopped`):

| started | stopped | phase |
|---|---|---|
| — | — | `SETUP` |
| ✓ | — | `COLLECTING` |
| ✓ | ✓ | `STOPPED` |

```
COLLECTING:  a device reporting "stopped" / "🟥" / "🛑", OR whose recorded stream
             goes 🔴 lost / stays 🟡 stale past STALE_LOST_AGE
                 → L3.  "the sensor stopped mid-collection" is ALWAYS a warning.

STOPPED:     a device reporting "stopped" is EXPECTED → NONE (info).
             Only an abnormal stop alerts — AcquisitionStopNotConfirmed → L2,
             an exception during stop → L2.

SETUP:       "not collecting" is normal → suppressed to NONE.
             A construction/connect failure that blocks this device → L3.
```

### Decision flow (per status-change event for one source)

```
0. PHASE GATE — can short-circuit to L3 or NONE.
1. captured & healthy now?                                    → NONE (healthy)
2. PLASMA actively recovering it? (reconnect in flight;
   watchdog STOP+reconnect; stale age < STALE_LOST_AGE)       → L2  (→ L3 on failure/timeout)
3. one obvious operator action restores it, session intact?   → L2
4. capture stopped / unrecoverable / integrity compromised?   → L3
5. capture continues, but a capability is lost / a value
   is drifting:   needs an action → L1 advisory (blue)
                  monitor only   → L1 caution (amber)
```

### Escalation timers (L2 → L3)

- `🔌 disconnected` stays **L2** for as long as the link is down — the 10 s
  watchdog keeps retrying and never gives up on its own, so there is no
  attempt counter, no back-off, and no separate "reconnect failed" status.
  The row escalates to **L3 / red** through the recorder-stream path: the
  wristband's recorded stream goes `🟡 stale` and then, past `STALE_LOST_AGE`
  (≈30 s), `health_level` returns L3 and the device-row fold picks it up.
  Same message, colour only — an extended disconnect that PLASMA is still
  working on. A successful sweep writes `🔄 reconnected` → back to healthy.
  (During SETUP, before any recording, a wristband that drops and can't
  reconnect stays L2 — nothing is being lost yet.) Full mechanism:
  [MSense reconnect behaviour](msense-reconnect.md).
- recorder `🟡 stale` on a periodic stream → still no data past `STALE_LOST_AGE`
  (≈30 s) → L3.
- battery advisory → below a hard floor → L2 → dropout → L3.

Constants live in `plasma/status.py`: `STALE_S` (3 s), `IRREGULAR_STALE_S`
(300 s), `STALE_LOST_AGE` (30 s).

---

## The full map

Every status string a driver writes onto `PlasmaMemo.sts`, plus the recorder's
per-stream `health`. `class` = the pre-model `_status_class` bucket.

### Device construction / connection

| Status string | Source | class → **level / colour** |
|---|---|---|
| `⛔ {exc}` / `❌ Fault {exc}` / `❌ Fault` / `FAULT: {exc}` / `🚫 FAULT` | qb2 / shimmer / pupil_labs / obs / `PlasmaDemoDevice` `__init__` | err → **L3 / warning** |
| `⛔ connect failed` | `msense/device.py` `connect_devices` | err → **L3 / warning** |
| `⛔ device not found` | `msense/device.py` `connect_devices` | err → **L3 / warning** |

### Transient transitions (Initialize / Start / Stop)

`init_devices()`, `start_collection()` and `stop_collection()`
(`integrated_panel.py`) each loop over every device — for MSense a real,
sequential multi-second BLE operation per wristband (connect, `collection_ctl`
write, stop-confirm readback) — and previously only updated the session
banner *after* the whole loop finished, with no interim text. These three
statuses fill that gap; `stop_like=False` keeps them out of the phase gate
(unlike the real `"stopped"` rule below).

| Status string | Source | class → **level / colour** |
|---|---|---|
| `Initializing...` | `IntegratedPanel.init_devices` (banner); `msense/device.py` `connect_devices` (per-wristband `memo.transition`, cleared in a `finally`) | ok → NONE / **guidance** |
| `Starting...` | `IntegratedPanel.start_collection` (banner); `msense/device.py` `start` (per-wristband `memo.transition`) | ok → NONE / **guidance** |
| `Stopping...` | `IntegratedPanel.stop_collection` (banner); `msense/device.py` `stop` (per-wristband `memo.transition`) | ok → NONE / **guidance** |

The per-wristband version renders as the row's purple sub-line
(`.m-sub.guidance`), replacing `.latest` only while set — it never touches
`.sts` and is never fed through `classify()`, so it can't affect
`plasma-ctl events` / `worst_level`.

### Collection start / stop

| Status string | Source | class → **level / colour** |
|---|---|---|
| `🟢` (collecting) | every `start()` | ok → NONE / **healthy** |
| `⚠️ start failed` | `msense/device.py` `start` | warn → **L3 / warning** (silently not collecting mid-session) |
| `🛑 stopped` / `🟥` / `Collection stopped` — phase STOPPED | msense / `template.py` / panel | err → **NONE / info** |
| same, phase COLLECTING (unexpected) | — | → **L3 / warning** |
| `🛑` bare (acq-stop **unverifiable**) | `msense/device.py` `stop` | err → **L1 / advisory** |
| `⚠️ still recording — stop unconfirmed` | `msense/device.py` `stop` | warn → **L2 / caution** |
| `⚠️ stop failed` | `msense/device.py` `stop` | warn → **L2 / caution** |
| `FAULT: {exc}` (OBS record cmd) | `obs.py` | err → **L3 / warning** |

### Link / stream recovery (phase COLLECTING)

| Status string | Source | class → **level / colour** |
|---|---|---|
| `🔌 disconnected` | `_on_unexpected_disconnect`; kept as-is by `_reconnect_peripheral` when a reconnect attempt fails | warn → **L2 / caution** — auto-reconnect retries every 10 s. The device row escalates to **L3 / warning** (same string, red) once its recorded stream has been silent past `STALE_LOST_AGE` (≈30 s). |
| `🔄 reconnected` | `_reconnect_peripheral` (ok) | ok → NONE / **healthy** (clears the L2/L3) |
| `⚠️ stream stalled` | `_sqc_recover` | warn → **L2 / caution** |
| `🎯 Calibrating...` | `start_gyro_calibration` | warn → **L1 / info** |
| `✅ Bias saved` | `_finish_gyro_calibration` | ok → NONE / info |
| `🧨 erased — re-Initialize` | `erase_flash_data` | warn → **L1 / advisory** |

### Recorder (`plasma/lsl_recorder.py`)

| Condition | class today | **level / colour** |
|---|---|---|
| `state == "unavailable"` | err | **L3 / warning** — no XDF at all |
| `state == "stopped"` after an operator Stop | err | **NONE / info** |
| stream `🟡 stale`, **periodic** (`srate > 0`), > `STALE_S` | (device row: invisible) | **L2 / caution**, → **L3** past `STALE_LOST_AGE` |
| stream `🟡 stale`, **irregular** (`srate == 0`) — journaler, eye events | orphan→warn (false alarm) | **NONE** until `IRREGULAR_STALE_S`, then **L1 / info** |
| stream `🔴 lost` (`LostError`) | orphan→err; device row: invisible | **L3 / warning** — now also on the device row |

`_stale_after(srate)` in `lsl_recorder.py` gives the rate-aware threshold; the
device-row fold in `build_memo_html` calls `classify(..., stream_health=…)`.

### Drift / degradation advisories (not yet on the memo panel)

| Condition | **level / colour** |
|---|---|
| battery < ~20 % | **L1 / advisory**; < ~10 % → **L2 / caution** |
| elevated malformed fraction / falling CRC-ok fraction / low SQC throughput | **L1 / caution** |
| one of N wristbands stalled while the rest stream | **L1 / caution** (coverage loss; the device itself still shows its own L2/L3) |

### MSense SQC / live-stream states (SQC tab, not the memo panel)

`plasma/status.py` `sqc_level(status, error)` — the `status` + `error` fields of
`MotionSenseHRV.get_sqc_status()` / `get_live_stream_status()`. Not shown on the
memo panel, but **folded into the per-source level in `plasma/api.py`
`session_status()`** (and hence `worst_level`, `events.jsonl`, `[FAULT]`
markers) so a headless supervisor sees a failed contact check. See
`headless.md`.

| status | **level / colour** |
|---|---|
| `rejected` + `BUSY` / `NOT_SUBSCRIBED` / `MTU_TOO_SMALL` / `INVALID_COMMAND` / `WRONG_SESSION` | **L2 / caution** |
| `rejected` + `NOT_RECORDING` | **L1 / advisory** |
| `error` — no START_ACK / decode failed / protocol violation | **L2 / caution** |
| live `error` — `stalled` / request failed | **L2 / caution** |
| `idle` / `requesting` / `receiving` / `finishing` / `ready` / `streaming` / `stopped` / `unavailable` | **NONE** |

### External network streams

| `🛰` grey rows | **NONE / external** |

---

## Adding a new status

1. Pick the level from the decision flow. Pick the colour: red = L3, amber = L2
   and monitor-only L1, blue = L1 that needs an action, neutral = not a failure.
2. Add a rule to `_RULES` in `plasma/status.py` (glyph or keyword predicate,
   ordered — first match wins; `stop_like=True` for anything that means "not
   running").
3. Add the string to `CASES` in `plasma/tests/test_status_levels.py`.
4. If it's a "not running" state, make sure the driver only sets it when a stop
   is genuinely expected — otherwise the phase gate turns it red mid-collection,
   which is usually what you want.
