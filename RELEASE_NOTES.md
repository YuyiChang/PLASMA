# PLASMA Release Notes

**PLASMA** — Platform for LSL-based Acquisition of Sensor Metrics and Analytics

---

## 🚀 vNext (unreleased)

### 🧠 Live-plot browser memory

- The MSense **Signal Quality** / live-stream, **IMU / Orientation**, and
  **Data Dashboard** plots no longer grow the browser tab's memory until it
  reloads (Safari) or crashes (Chrome). Root cause was Gradio's `gr.Plot`
  recreating the whole Plotly `<div>` on every timer tick without ever calling
  `Plotly.purge` (gradio#10252). Fixes, layered:
  - a page-level `MutationObserver` purges every discarded Plotly graph div
    (freeing its handlers + WebGL context);
  - each plot's refresh **timer runs only while its tab is on screen** — off-tab
    it pauses, resuming on return (the plot freezes on its last frame);
  - every plot handler returns `gr.skip()` when nothing changed, so an
    idle-but-visible tab does no Plotly work;
  - the on-screen trace is **downsampled** to ~3000 points (peaks preserved);
    a **"Downsample plot for speed"** toggle in the SQC "📈 Plot options"
    accordion turns it off for a full-rate zoomable trace. The saved snapshot /
    CSV / XDF are never decimated.
- Server side: a closed browser tab now stops any running live stream; a live
  stream's rolling buffer is released when it ends; per-channel plot buffers
  gained a hard size cap.

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
- **ACF3 (`ac:v3`) accelerometer extraction now reports firmware-dropped
  samples.** A forward jump in `first_sample_sequence` between `ACB1` blocks is
  decoded as the count of samples the firmware dropped (per the format spec):
  it is logged and totalled (`Samples lost to firmware drops` in
  `session_summary.txt`, `ExtractionReport.dropped`), but the missing rows are
  not fabricated, so `Counter` keeps its true gap and clock-sync is unaffected.
  A backwards / implausibly large jump now fails sequence validation like a bad
  block CRC (`--strict` raises; otherwise the valid prefix is kept). Multi-chunk
  sessions report a `first_sample_sequence` break at a chunk boundary and a
  chunk `0000` that does not start at sequence 0. Files with no drops decode
  byte-identically to before.
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
- **Two MSense wristbands with the same BLE Name no longer collapse into one.**
  The driver now keys every per-wristband structure (memo row, SQC/live state,
  capabilities, LSL outlet, gyro bias) by the wristband's **address** instead of
  its Name, so two unrenamed factory-default units both connect, both record and
  both show a memo row. `"Name (Nickname)"` still labels every row and message
  unchanged — give the two a Nickname each to tell them apart on screen. The
  Configuration tab warns if the same address is listed on two enabled rows.

### 🚦 Session-memo status classification

- Device / stream status colours are now driven by one model
  (`plasma/status.py`, documented in `docs/failure-levels.md`) instead of
  scattered keyword matching. An internal ECAM-style 3-level severity
  (advisory / caution / warning) maps to an Airbus-style colour vocabulary —
  green (healthy), neutral (status), **blue** (advisory + action), amber
  (caution), **red** (warning), grey (external). Levels are internal;
  the operator sees only the colour.
- **A sensor that stops mid-collection is now always red** (phase-gated). A
  normal operator Stop is neutral, not red — a completed session and a crashed
  sensor no longer look identical.
- **A device row now reflects its own recorded stream going silent** — if the
  LSL stream a device publishes stalls (`🟡`) or is lost (`🔴`) during a
  recording, that device's memo row goes amber → red, instead of staying green
  with a frozen sample count.
- The irregular **journaler** stream no longer shows a false "🟡 stale" alarm
  a few seconds after each marker — the recorder's staleness threshold is now
  rate-aware (periodic streams: seconds; event streams: minutes).

### 🎛️ Headless control & durable fault history

- A running PLASMA now exposes a small typed JSON API (`/status`, `/start`,
  `/stop`, `/mark`, `/events`) and ships a **`plasma-ctl`** CLI, so a session
  can be driven and monitored from a script or an unattended rig on the same
  machine — it operates the same live session as an open browser tab.
  **Localhost only, no auth.**
- `plasma-ctl start` / `/start` **report outcomes**: a device that fails to
  construct, or a sensor that silently isn't collecting, comes back as
  `ok: false` with the error — previously these were only a browser toast or an
  `INFO` log line while the UI still said "Collection in progress".
- New **`events.jsonl`** — an append-only, machine-readable fault history
  written to both `<data_dir>/events.jsonl` (all sessions, survives restarts)
  and `<session_dir>/events.jsonl` (travels with the `.xdf`). It records
  session start/stop, phase changes, `fault` / `recover` transitions (from the
  internal failure-level model), recorder state, and a 30 s heartbeat.
- `fault` / `recover` transitions also push **`[FAULT]` / `[RECOVER]` markers**
  onto the journaler LSL stream, so a mid-collection sensor drop or an
  uncertain session boundary is visible inside the recording.
- **MSense SQC snapshot / live-stream transfer errors** (`rejected: BUSY`,
  `error: no START_ACK`, live `stalled`, …) are now folded into the same
  L1–L3 model — a failed headless contact check shows up in `/status`
  `worst_level`, `events.jsonl`, and as a `[FAULT]` marker. `/status` carries
  the raw `sqc` / `live_stream` state per wristband; trigger a snapshot with
  the auto-named `/_request` endpoint (see `docs/headless.md`).
- Genuine device / recorder failures are now logged at `WARNING` / `ERROR`
  (were `INFO`), so the session log is level-filterable.
- See `docs/headless.md`.

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

### 📦 Packaging

- **New release binary: `PLASMA_Linux_arm64`** — a PyInstaller build for
  **NVIDIA Jetson Orin** (aarch64 Linux), alongside the existing x86-64 Linux,
  macOS arm64 and Windows binaries. Built in CI on GitHub's `ubuntu-22.04-arm`
  runner, so it targets **JetPack 6** (Ubuntu 22.04 / glibc 2.35); on JetPack 5
  install from source. `liblsl` comes from the sccn/liblsl release `.deb`
  (conda-forge has no aarch64 build). The **qb2 LiDAR plugin is not bundled**
  (`blickfeld-qb2` is sdist-only on aarch64) — it still appears in the catalog
  and only errors if a LiDAR is Initialized.
- `app_linux.spec` now builds natively for whichever arch the runner is
  (`PLASMA_Linux_x64` / `PLASMA_Linux_arm64`) from one spec file.

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
