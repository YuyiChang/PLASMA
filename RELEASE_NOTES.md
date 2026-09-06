# PLASMA Release Notes

**PLASMA** — Platform for LSL-based Acquisition of Sensor Metrics and Analytics

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
