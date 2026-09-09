# Abnormal handling

A runbook for the faults you'll actually hit. Each one: what the
[memo panel](memo-engine.md) shows, what caused it, and what to do. Every
scenario below is reproducible with the simulated MSense device — set the
wristband's **Fault** column in Configuration → "MSense Demo (simulated)".

!!! tip "Watch it headlessly"
    `plasma-ctl watch --fail-on L3` prints every colour change and exits
    non-zero the moment anything goes red — the building block for an
    unattended rig.

---

## Wristband not found at Initialize

![Memo: device not found](../assets/screenshots/abnormal-device-not-found.png){ .shot }

| | |
|---|---|
| **Memo** | device row **red**, `device not found` |
| **Cause** | the wristband wasn't advertising / in range / paired when you pressed Initialize |
| **Do** | check it's on and charged; re-scan under Configuration → "MSense wristbands" (**before** Initialize — single BLE radio); confirm the address in the table; Initialize again |
| **Demo fault** | `device_not_found` |

---

## BLE drop mid-session

![Memo: disconnected](../assets/screenshots/abnormal-disconnected.png){ .shot }

| | |
|---|---|
| **Memo** | row **amber**, `disconnected` → then **green**, `reconnected` |
| **Cause** | the link dropped (range, interference, a busy radio) |
| **Do** | usually nothing — auto-reconnect brings it back within ~10 s and streaming resumes. The gap is in the recording; note it with a 🚩 journal flag. If it keeps dropping, move the base station closer. |
| **Demo fault** | `disconnect_reconnect` |

![Memo: reconnected](../assets/screenshots/abnormal-reconnected.png){ .shot }

### …that doesn't come back

| | |
|---|---|
| **Memo** | row **red**, `reconnect failed` |
| **Cause** | the reconnect sweeps were exhausted |
| **Do** | the wristband is out for the rest of the session. **🎛️ Control → Advanced → 🔄 Reconnect now** for a manual attempt; otherwise power-cycle the wristband and re-Initialize (this starts a new session folder). |

---

## SQC snapshot stalls

![SQC tab: stalled](../assets/screenshots/abnormal-sqc-stalled-tab.png){ .shot }

| | |
|---|---|
| **Where** | YAMS → 📡 Signal Quality status line; the wristband's memo row also goes **amber** |
| **Memo / tab** | `❌ stalled (no notification for …s) — reconnected, press Request to retry` |
| **Cause** | the wristband's transmit buffer wedged mid-transfer; the no-progress watchdog stopped and reconnected it |
| **Do** | press **📡 Snapshot all wristbands** again. If it stalls repeatedly, switch **Streaming mode** to **Sequential**, or use **Capture mode → History Only** for a shorter transfer. A stalled *snapshot* does not affect a running *session* recording. |
| **Demo fault** | `sqc_error` (snapshots), `stream_stall` (live streams) |

---

## Acquisition stop unconfirmed

![Memo: stop unconfirmed](../assets/screenshots/abnormal-acq-stop-unconfirmed.png){ .shot }

| | |
|---|---|
| **Memo** | row **amber**, `still recording — stop unconfirmed` |
| **Where else** | YAMS → 🎛️ Control → **Acquisition-stop confirmation** lists it as `unconfirmed · device may still be recording`; the journal gets `[ACQ] <name> stop UNCONFIRMED` |
| **Cause** | after Stop, PLASMA reads the wristband's acquisition flag back to confirm it actually halted (the Bluetooth ack alone doesn't prove it). It retried once and the flag still read "recording" — likely a firmware issue |
| **Do** | the session's **end boundary is uncertain** — the wristband may have kept writing to its own flash past your Stop. Power-cycle the wristband before the next session. When you download its onboard data, expect extra tail samples after your intended stop time. |
| **Demo fault** | `acq_stop_ignored` |

![Control tab: acquisition-stop confirmation](../assets/screenshots/abnormal-acq-stop-control.png){ .shot }

An **unverifiable** result (`❓`) — older firmware whose flag isn't readable —
is *not* a failure, just "couldn't check". A bare `🛑` on the memo row means the
same.

---

## Recorder unavailable

| | |
|---|---|
| **Symptom** | a toast: *"LSL recording unavailable (liblsl/pylsl missing?)"*; no `.xdf` is written; the memo's 📼 row is **red** |
| **Cause** | `liblsl` isn't installed (see [Install](install.md)), or the recording file couldn't be opened |
| **Do** | **Stop is still safe** — devices keep their own onboard recordings. Fix `liblsl` (`conda install -c conda-forge liblsl`), restart, and re-run. Don't rely on this session's XDF. |

---

## A recorded stream goes silent

| | |
|---|---|
| **Memo** | a green device row turns **amber** then **red** while its sample count stops climbing |
| **Cause** | the device driver still thinks it's fine, but the LSL stream it publishes has gone stale (`🟡`) or lost (`🔴`) |
| **Do** | check that device — cable, power, the app it depends on (OBS, Pupil, LiDAR). The memo caught it; the frozen sample count would not have. |

---

## Device fails to start

| | |
|---|---|
| **Memo** | row **red**, `start failed` — right after you pressed Start |
| **Cause** | the device connected at Initialize but its `start()` threw (firmware state, a dependency not ready) |
| **Do** | Stop, re-Initialize that device, Start again. Check the session log
  (`<home>/data/<date>_plasma_session.log`) for the exception. |

---

## When to Restart vs Shut down

- **Restart** (Configuration → Power) — after a demo-mode or catalog change, or
  when the app is in a weird state but you want to keep going. It reloads config
  from disk and flushes any running recording first.
- **Shut down** — you're done. Clean exit, recording finalised.
- Closing the browser tab or `Ctrl-C` also finalises a running recording (that
  wasn't always true — it is as of v2.x).

## The paper trail

Every fault above is in two places you can read after the fact:

- **`<home>/data/events.jsonl`** — machine-readable, one JSON line per change,
  survives restarts. `plasma-ctl events --min-level L2` or
  `tail -f`. Schema: [Files & paths](../reference/files-and-paths.md#eventsjsonl).
- **`<home>/data/<date>_plasma_session.log`** — the free-text session log;
  genuine failures are logged at `WARNING` / `ERROR`.
