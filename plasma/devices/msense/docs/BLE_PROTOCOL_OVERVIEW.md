# MSense BLE protocol — overview

This is the map: every GATT service/characteristic a MotionSense wristband
exposes, what PLASMA's `MotionSenseHRV` driver (`plasma/devices/msense/device.py`)
does with each one, and where the detailed wire-format spec for each piece
already lives. Read this first, then follow a link below for the byte-level
contract of the piece you're touching — this document does not repeat framing
details that are specified elsewhere in this folder.

A connected wristband exposes four GATT services:

| Service | UUID (base) | Purpose |
|---|---|---|
| Control | `da39c930-1d81-48e2-9c68-d0ae4bbd351f` | start/stop collection, time sync, participant encoding, flash erase |
| Streaming ("update_service") | `da39c950-1d81-48e2-9c68-d0ae4bbd351f` | live ENMO notify; demo IMU (accel + delta quaternion) notify |
| Battery (standard BT SIG) | `0000180f-0000-1000-8000-00805f9b34fb` | battery percentage, read + notify |
| Nordic UART Service (NUS) | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | on-demand bounded ECG/PPG raw snapshot (the "SQC" pull) |

All four are independent — a wristband can be mid-recording (Control +
Streaming services active) while also answering an SQC pull (NUS) on the same
connection; nothing here multiplexes onto a shared characteristic.

## Control service — `da39c930…`

| Characteristic | UUID | Direction | Used for |
|---|---|---|---|
| Collection start/stop | `da39c931…` | write `uint32` (0/1) | toggles the firmware's recording state |
| Unix time | `da39c932…` | write `uint64` | sets the wristband's RTC on session start |
| Participant encoding | `da39c933…` | write/read `uint32` | tags recorded files with a participant code |
| Flash erase | `da39c934…` | write `uint8` | write `68` (`ERASE_CODE`) to wipe onboard flash |

Driver entry points: `collection_ctl(name, start)` (writes unix time +
participant encoding once, then start/stop), `write_enc(enc)` (arbitrary
write-then-read-back of the participant code), `erase_flash_data(passcode)`
(gated on the `68` passcode, then clears `active_devices`/`active_outlets`
locally since the erase resets the wristband's BLE stack).

## Streaming service — `da39c950…`

| Characteristic | UUID | Direction | Rate | Used for |
|---|---|---|---|---|
| ENMO | `da39c951…` | notify | one packet per on-flash IMU record | live Euclidean-norm-minus-one magnitude + packet counter, pushed to an LSL outlet (`MsenseOutlet`) |
| IMU stream (demo) | `da39c953…` | notify | 32 Hz, 20-byte packets | live accel + delta-rotation quaternion — see `IMU_STREAM_BLE_CHARACTERISTIC.md` for the packet layout and the quaternion's incremental (not absolute) semantics |

Both are subscribed from `collection_ctl(name, start=True)` — i.e. only while
a recording session is active — via `register_enmo`/`register_imu_stream`.
IMU stream subscription is additionally gated on `name in
self.imu_stream_devices` (only wristbands running the demo firmware expose
it; subscription failure there is expected and non-fatal). Notifications are
handled off the BLE callback thread by `enmo_handler`/`imu_stream_handler`,
one callback per wristband — every connected wristband streams ENMO/IMU
concurrently with every other one; there's no sequencing here, unlike NUS
below.

## Battery service — standard `0x180F`/`0x2A19`

Read once on connect (`connect_devices`) and re-subscribed for notify
(`register_battery`) whenever a session starts or a link is recovered. Handled
by `battery_handler` — a single raw percentage byte.

## Nordic UART Service — `6e400001…`

The one service with a structured, stateful request/response protocol instead
of a plain read/notify value: the Central sends a fixed `START` command and
the firmware streams back a fixed 96 KiB raw ECG or PPG payload (a
pre-buffered *history* window, then a live-acquired *forward* window),
framed into `START_ACK` / `DATA` / `END` / `RESULT` notifications. Full
byte-level spec, handshake state machine, timeouts, and status/error codes:

- **`NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md`** — the authoritative protocol
  spec (Central's job: discover, subscribe, `START`, validate `DATA`
  sequencing/phase boundary, confirm `END`).
- `ECG_PPG_SIGNAL_QUALITY_BLE_NUS.md` — points at the above (a stale, guessed
  earlier version of this protocol has since been superseded) and maps each
  piece to its host-side implementation file.
- Record payload layouts once decoded: `PPG_PACKED_16_BYTE_FORMAT.md`,
  `ECG_TEMP_DATA_FORMAT.md`.

Driver side: `plasma/devices/msense/nus_stream.py` is the pure protocol codec
(`StreamSession`, no BLE); `device.py`'s `register_nus_notify`,
`request_sqc_snapshot`, `_nus_data_handler`, `_finish_sqc_snapshot` drive it
over BLE per wristband, keyed by `self.sqc_state[name]`.

### Scheduling an SQC pull across multiple wristbands

Unlike ENMO/IMU/battery (always concurrent across every connected wristband),
an SQC snapshot is a heavier burst transfer, so how it's scheduled across
*multiple* wristbands is a host-side policy choice, not part of the wire
protocol. `request_all_sqc_snapshots(stream_mode=...)` offers three:

| Mode | Behavior | Trade-off |
|---|---|---|
| `sequential` (default) | one wristband fully finishes before the next starts | safest — least radio contention on the Mac's single, time-sliced BLE adapter |
| `parallel` | every wristband's `START` fires back-to-back, all stream concurrently | fastest wall-clock time; each wristband's own transfer is slower and stalls are more likely |
| `hybrid` | pipeline: start one wristband, and once its *history* phase gives way to the lighter live *forward* phase, start the next (they now run side by side); repeat | only one wristband is ever in the heavy history burst at a time — a middle ground |

The underlying per-device state machine, BLE notify callbacks, and the
no-progress watchdog (below) are already fully independent per wristband —
all three modes exercise the same per-device machinery, they just differ in
when `request_sqc_snapshot(name)` is called for each one. See
`_run_sqc_sequential` / `_run_sqc_parallel` / `_run_sqc_hybrid` in `device.py`.

### Stall recovery

`_sqc_watchdog_loop` (1 Hz, one thread per `MotionSenseHRV` instance) watches
every wristband currently `"receiving"` for `SQC_NOPROGRESS_TIMEOUT_S` (5s) of
silence — the observed firmware failure mode is the ECG peripheral's BLE TX
buffers staying wedged (`-ENOMEM` on outbound notifications) while inbound
writes still work. On a stall it `CANCEL`s, disconnects, and reconnects
(`_sqc_recover` → `_reconnect_peripheral`). The same loop also runs a periodic
auto-reconnect sweep over every connected wristband regardless of SQC
activity. Note: if multiple wristbands stall in the same 1 Hz tick, their
recovery runs serially within that tick (a documented latency cost, not a
correctness issue) — more likely to matter under `parallel`/`hybrid` mode
than `sequential`.

## Connection lifecycle

`connect_devices()` iterates the configured device list sequentially (one
`simplepyble.Peripheral.connect()` at a time, since there's a single BLE
adapter to issue connect calls through), and per wristband: connects,
subscribes NUS notify (`register_nus_notify`), and reads battery once. Once
anything is connected it starts the shared watchdog (`_ensure_sqc_threads`).
`start()`/`stop()` (recording session) then loop over every already-connected
wristband calling `collection_ctl`, which is what actually subscribes
ENMO/battery/IMU-stream notify and toggles the firmware's recording state —
so "connected" and "recording" are separate states, and NUS/SQC pulls work in
either.

## Where to look next

| Topic | File |
|---|---|
| NUS/SQC wire protocol (authoritative) | `NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md` |
| NUS/SQC host-side implementation map | `ECG_PPG_SIGNAL_QUALITY_BLE_NUS.md` |
| Demo IMU-stream characteristic packet layout | `IMU_STREAM_BLE_CHARACTERISTIC.md` |
| PPG record layout (post-decode) | `PPG_PACKED_16_BYTE_FORMAT.md` |
| ECG record layout (post-decode) | `ECG_TEMP_DATA_FORMAT.md` |
| Accelerometer on-flash binary format | `ACCELEROMETER_BINARY_FORMAT.md` |
| `GyroX/Y/Z` → `QuatX/Y/Z` CSV column rename | `migration_gyro_to_quat.md` |
| Converting recorded binaries to CSV | `data_extraction.md` |
