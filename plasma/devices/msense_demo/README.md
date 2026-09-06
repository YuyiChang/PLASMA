# MSense Demo (simulated)

A fake MSense wristband for development, demos and CI. A bleak-shaped
`FakePeripheral` generates synthetic notifications that drive the **real**
`MotionSenseHRV` driver — the NUS `StreamSession` FSM, the no-progress watchdog,
`_reconnect_peripheral`, the LSL outlet, the ECG/PPG decoders and the journaler
all run unmodified. No hardware, no Bluetooth.

## Enabling it

Demo mode is off by default. Turn it on either way:

- **Configuration tab → Device catalog → "Demo mode"**, then **restart PLASMA**
  (the plugin registry is built once at startup). The setting is saved to
  `plasma_device_config.json`.
- **`PLASMA_DEMO=1`** in the environment — overrides the saved flag, handy for a
  wrapper app or CI.

With demo mode on, **"MSense Demo (simulated)"** appears in the sensor catalog
(disabled by default). Enable it, Apply, then Initialize on the Session
dashboard. Configure the simulated wristbands (name, `PPG`/`ECG`, IMU stream,
injected fault) in the Configuration tab's "MSense Demo (simulated)" section.

> Enabling **both** "MSense Wristbands" and "MSense Demo (simulated)" at once is
> unsupported — they share the "🍠 YAMS (MSense Tools)" tab, which binds to
> whichever device initialised first.

## What it simulates

| | |
|---|---|
| BLE connect / disconnect / reconnect | ✅ |
| Live ENMO + battery + IMU-orientation → LSL / XDF / dashboards | ✅ |
| ECG/PPG signal-quality (SQC) snapshot with realistic waveforms | ✅ |
| Offline toolkit (USB download, `.bin` extraction, clock-sync, viewer) | ❌ out of scope |

## Fault injection

Per wristband, via the **Fault** column:

| id | effect |
|---|---|
| `none` | healthy wristband |
| `device_not_found` | absent from the scan → Initialize leaves it `⛔ device not found` |
| `disconnect_reconnect` | ~15 s after Start the link drops (`🔌 disconnected`); the watchdog reconnects it (`🔄 reconnected`) and streaming resumes |
| `sqc_error` | an SQC snapshot gets START_ACK + a few DATA frames then goes silent → the no-progress watchdog CANCELs + reconnects, tab shows `⚠️ stream stalled` |

More faults (rejected `RESULT`, dropped/reordered `DATA`, `STORAGE_ERROR`,
garbled `START_ACK`, MTU stuck at 23, decode failure, reconnect-fails …) are a
one-entry-each addition in `faults.py` + `fake_stream.sqc_frames`.

## Files

| file | role |
|---|---|
| `__init__.py` | `register()` — gated on `device_config.demo_mode` |
| `device.py` | `MSenseDemo(MotionSenseHRV)` + `FakePeripheral` |
| `config.py` | the `plugins.msense_demo` blob + Configuration-tab section |
| `faults.py` | the fault catalogue |
| `fake_stream.py` | synthetic waveforms, packed-record encoders, SQC frame assembly |

The NUS message builders are shared with the protocol tests via
`plasma/devices/msense/nus_sim.py`.
