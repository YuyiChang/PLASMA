# PLASMA Release Notes

**PLASMA** — Platform for LSL-based Acquisition of Sensor Metrics and Analytics

---

## 🚀 vNext (unreleased)

### 📡 MSense sensor stream v0 + ECB2 ECG blocks

- **New shared sensor-stream protocol (v0)** replaces the protocol-v1 NUS path.
  Version byte `0`; commands `START` (FINITE) / `STOP` / `START_INFINITY`;
  16-byte `START_ACK` (no device/git metadata); **byte-offset-addressed `DATA`**
  (no sequence/phase/record-index); 2-byte `END` / `RESULT`. FINITE is now
  **128 KiB** (32 KiB rolling history + 96 KiB future). This is a **hard
  cut-over** — the wristband firmware must be updated to match; there is no
  fallback. Old `.ecg` / `.ppg` v1 capture blobs still decode offline.
- **New ECG record format — `ECB2`**: the streamed ECG payload is a sequence of
  4096-byte CRC-32/ISO-HDLC blocks (1358 samples each, `ETAG`/`PTAG`), not
  12-byte MAX30001 frames. Leading all-zero history slots are skipped as
  "unavailable history". PPG's packed 16-byte record is unchanged.
- **Offline extraction** of downloaded ECG NAND data now supports the `ECF2`
  block-container file (`ecg:block_v2` format spec, auto-detected by magic):
  4 MiB chunks of `ECB2` blocks → `ECG` / `ETAG` / `PTAG` / `Counter` / `CDCT`
  CSV, with multi-chunk recordings stitched into one continuous timeline
  (ordered by `chunk_index`, split by `recording_id`; missing chunks / sample
  gaps reported). Old 12-byte `framed` `.ecg` / `.bin` files still decode.
- **Continuous live stream (`START_INFINITY`)** — a new "▶️ Start live stream"
  control in the YAMS → ECG/PPG Signal Quality tab opens a continuous ECG/PPG
  stream decoded into a rolling in-memory plot. **Not** recorded to disk or
  LSL/XDF in this build (opt-in, per wristband).
- Product (ECG vs PPG) is now taken from the advertised `MSense4ECG` /
  `MSense4PPG` name — v0 `START_ACK` carries no product identity.
- No-progress watchdog default raised 5 s → **15 s** (spec §7); STOP replaces
  the v1 CANCEL, and a STOPped stream discards its partial tail.
- Acquisition enable/disable (`da39c931…`) is now a **one-byte** write, per the
  v0 firmware howto.
- **Collection Stop is now confirmed** — after the stop write the driver reads
  `da39c931` back (`0` = stopped, `1` = still recording), retries once, and on
  failure shows `⚠️ still recording — stop unconfirmed` on the wristband's memo
  row and writes an `[ACQ] … UNCONFIRMED` journal marker (so a session whose
  end boundary is uncertain is visible in the XDF). A per-device outcome list
  is on the YAMS → Control sub-tab. Older firmware whose `da39c931` isn't
  readable is reported as *not verifiable*, never as a failure.

### 🔌 Restart / Shut down

- The Configuration tab now has **Restart PLASMA** and **Shut down PLASMA**
  buttons at the bottom (two-click to confirm). Restart relaunches with the
  saved config — the intended way to pick up a **Demo mode** or catalog change.
- Both run a clean teardown first. `IntegratedPanel` now flushes a running
  LSL→XDF recording (stream footers + clock offsets) and closes device
  connections on **any** exit — Ctrl-C and window-close included, which
  previously left the `.xdf` unfinalized.

### 🧪 Simulated MSense device

- New **"Demo mode"** switch in the Configuration tab (or `PLASMA_DEMO=1`) adds a
  fully simulated MSense wristband to the sensor catalog — no hardware, no
  Bluetooth. It connects, streams live ENMO / battery / IMU-orientation to a real
  LSL outlet (recorded to XDF, shown on both dashboards and the IMU panel), and
  serves realistic ECG/PPG signal-quality snapshots, all through the real driver
  and the existing "🍠 YAMS (MSense Tools)" tab.
- Its Configuration section can inject faults per device — *device not found*,
  *drop BLE mid-session then auto-reconnect*, *SQC stream stall* — to exercise the
  watchdog / reconnect / journaler paths without a wristband.
- Offline features (USB download, `.bin` extraction) are out of scope for the
  simulated device.

---

## 🚀 v2.0.0

A major release focused on a rebuilt MSense BLE stack, a built-in LSL→XDF
recorder, an HTML session memo, and a hardened packaging / distribution setup.

### ⚠️ Breaking changes

- **MSense BLE backend swapped to [bleak](https://github.com/hbldh/bleak).** The
  live-device driver (`plasma/devices/msense/device.py`) no longer uses
  `simplepyble`; `ble_scan.py` still does. On Linux/BlueZ the driver now
  negotiates the ATT MTU explicitly — without it `mtu_size` stays pinned at 23.
- **Writable-state location is now resolved once at import.** Order:
  `$PLASMA_HOME` → a per-OS user-data dir when running as a packaged app
  (`~/Library/Application Support/PLASMA`, `%LOCALAPPDATA%\PLASMA`,
  `~/.local/share/plasma`) → the working directory when running from source.
  Set `PLASMA_HOME` before launching to override.
- **Devices are now plugins.** Integrations load through `plasma/plugins.py`;
  all MSense code lives under `plasma/devices/msense/`.

### 📡 MSense wristbands

- BLE core overhauled onto bleak with **automatic reconnection** after link loss.
- **BlueZ MTU negotiation** so large NUS payloads are not fragmented to 23-byte
  writes on Linux.
- **Signal-quality check (SQC)** stream stabilized: fixed a BLE freeze, added a
  no-progress watchdog with telemetry, made the notification callback
  non-blocking, and added an **adaptive ACK** cadence with revised SQC options.
- **LSL journaler during SQC** — SQC sessions now emit a timestamped LSL journal
  stream alongside the raw data.
- Data downloader: custom paths accept **multiple directories**, and extraction
  recovers cleanly from **truncated / broken `.bin`** files.
- Reworked MSense tab layout.

### 🎬 Recording & session memo

- **Built-in LSL→XDF recorder** (`plasma/lsl_recorder.py`, `plasma/xdf_writer.py`)
  with a recording-capacity indicator. Every PLASMA-created outlet is tagged via
  `mark_plasma_origin(info)` so the recorder can identify its own streams.
- New **HTML session memo panel** (`gr.HTML`) with themed device-status colours.
- LSL lifecycle & scoping documented in `docs/lsl-lifecycle-and-scoping.md`.

### 📦 Packaging & CI

- Version is **single-sourced** from `plasma.__version__` and validated
  (PEP 440) by `plasma/tests/test_packaging.py`.
- Package discovery moved to setuptools; PyInstaller hook dirs registered through
  the `pyinstaller40` entry-point group so device plugins survive the frozen
  build.
- **Linux build** added to `.github/workflows/build.yml`, plus a post-build
  smoke test (`.github/smoke_test.sh`) on all three platforms.

### 🐛 Bug fixes

- Fixed duplicate notifications when an MSense device restarts.
- Fixed build import errors in the frozen app.
- Fixed device-name mismatch in the memo status display.

### Known issues

- LiDAR IP address must still be set manually.
- The LSL→XDF recorder captures every stream on the default liblsl session, so a
  concurrent `pytest` run or another lab tool can leak into a recording; see
  `docs/lsl-lifecycle-and-scoping.md`.

---

## 🎉 v1.0.0

First stable release: a full multi-device acquisition pipeline, a graphical
session dashboard, and a JSON configuration system in one self-contained
desktop app.

### 🔌 Device support

Seven sensor integrations out of the box, all streaming over **LSL** for
synchronized, time-stamped acquisition:

| Device | Modality |
|---|---|
| MSense Wristbands | BLE motion + heart rate |
| qb2 LiDAR | Depth / spatial sensing |
| Pupil Labs IMU | Head motion (Realtime API) |
| Pupil Labs Eye Events | Blinks & fixations |
| ShimmerGSR | Galvanic skin response |
| OBS Recorder | Video capture |
| Bitalino | Biosignals (ECG, EEG, EMG, EDA) |

### 🖥️ Session dashboard

- Subject ID / Session ID entry with automatic **participant encoding** (`XXXXYY`)
- Per-session device selection and one-click initialization
- **Start / Stop** data collection controls
- Live per-device parameter viewer

### ⚙️ Configuration tab

- Device catalog — toggle any device on or off; enabled devices appear in the
  dashboard (**Apply**, then **Refresh list**)
- MSense wristband pairing — editable name ↔ UUID / MAC table, no hardcoded
  values in source
- Configurable IP addresses for qb2 LiDAR and Pupil Labs Realtime API
- Import / Export the full configuration as a `.json` file

### 🔒 Security

- Device UUIDs, MAC and IP addresses live only in `plasma_device_config.json`,
  never in source; the file is git-ignored and created on first launch.

### 🐛 Bug fixes

- MSense 32-bit counter rollover handling
- qb2 async event loop on initialization
- Pupil Labs eye event stream stability
- Device name mismatch in memo status display
- MSense BLE adapter initialization error on re-connect

---

## 📦 Distribution

Pre-built bundles for macOS, Linux and Windows are attached to each GitHub
release, built in CI by `.github/workflows/build.yml`
(`app_macos.spec` / `app_linux.spec` / `app_windows.spec`).

To run from source:

```bash
conda create -n plasma python=3.12
conda activate plasma
pip install -e ".[all]"
python -m plasma
```
