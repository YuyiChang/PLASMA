# PLASMA Release Notes

**PLASMA** — Platform for LSL-based Acquisition of Sensor Metrics and Analytics

---

## 🚀 v2.2.4 (unreleased)

### 🍺 Self-updating Homebrew tap

- **The `yuyichang/homebrew-plasma` tap now bumps its own `plasma` cask.** A
  daily scheduled job living in that repo (not this one) checks PLASMA's
  latest tagged release and opens a PR updating `version`/`sha256` when it's
  out of date — no more running `brew bump-cask-pr` by hand after every
  release. Runs on the tap repo's own default `GITHUB_TOKEN`, so no
  cross-repo credential was needed to set this up.

### 🪣 Scoop bucket for Windows

- **`scoop install plasma` from the new
  [`YuyiChang/scoop-plasma`](https://github.com/YuyiChang/scoop-plasma)
  bucket** installs the Windows build with a Start Menu shortcut and a
  `plasma` command, and **skips the SmartScreen "Windows protected your PC"
  warning** (Scoop's own download carries no Mark-of-the-Web). A
  `plasma-nightly` manifest tracks the rolling nightly. The bucket bumps
  itself the same way the Homebrew tap does. Manifests are kept in
  `packaging/scoop/`.

### 🔏 Optional Windows codesigning

- **CI can now codesign the Windows `.exe`/installer** with a self-signed
  certificate (`.github/codesign_windows.ps1`), once two secrets are
  configured — see `docs/reference/release-process.md`. Until then it's a
  no-op and Windows assets keep shipping unsigned exactly as before. Note
  this does **not** remove the SmartScreen warning (that requires a
  CA-issued certificate); it buys tamper-evidence and a consistent signer
  identity across releases instead.

---

## 🚀 v2.2.3

### 🩹 MSense wristband fixes

- **Chatty/idle wristbands no longer flood the console.** A connected
  wristband with no active SQC snapshot or live-view session (e.g. boot
  chatter, or firmware that exposes the NUS characteristic but doesn't speak
  the SQC/live protocol) used to log one `[SQC <addr>] rx NB ignored (no
  active stream)` line per notification. That debug path now coalesces
  repeated ignored notifications from the same device into a single
  rolled-up line every few seconds instead of one per packet.
- **Rescanning after a firmware update now picks up the new advertised
  name.** Appending scan results to the MSense wristband table used to skip
  any address already listed, silently keeping its old Name even after a
  firmware update changed what the device advertises — the only way to pick
  up the new name was Overwrite, which also reset every other wristband's
  Nickname/Enabled/IMU Stream. Append now refreshes the Name of any
  already-listed address to match the freshly scanned name, leaving those
  per-device settings untouched.

### 🛠 CI / nightly build

- **Nightly build now actually builds off `dev`.** The `schedule` trigger
  has no branch context of its own, so every checkout step (and the commit
  hash stamped into the binary) was silently resolving to the repo's default
  branch instead of `dev` as the comment claimed. `build.yml` now pins
  `ref: dev` explicitly for the `schedule` event on every job, and derives
  the stamped commit hash from the checked-out tree (`git rev-parse HEAD`)
  rather than `github.sha`, which was wrong for the same reason.

### 🍺 Nightly Homebrew cask

- **`brew install --cask yuyichang/plasma/plasma@nightly`** installs the
  rolling nightly build off `dev`, for anyone who wants fixes before the
  next versioned release (expect it to be less stable). It conflicts with
  the tagged `plasma` cask — only one can be installed at a time. Points at
  the fixed `nightly` release tag `build.yml` republishes daily, so —
  unlike `plasma` — this cask needs no per-release version/sha256 bump;
  since Homebrew has no version number to detect a new nightly on its own,
  `brew reinstall --cask plasma@nightly` is how you pull the latest build.

### 📦 Onedir packaging + native installers

- **Faster launch — no more per-run extraction.** All PyInstaller builds
  (macOS, Windows, Linux) switched from onefile to onedir: the frozen app
  now ships as a folder (executable + support files) instead of a single
  self-extracting binary, so launch no longer re-extracts the whole bundle
  to a temp directory every time. This is also what was silently causing
  the frozen-build font-cache churn fixed in v2.2.2 — that fix stays in
  place, but the underlying temp-dir volatility it worked around is gone.
- **New Windows installer.** `PLASMA_Windows_x64_Setup.exe`, built with
  Inno Setup, installs PLASMA with a Start Menu shortcut, an optional
  Desktop icon, and an uninstaller. Unsigned, so SmartScreen still warns on
  first run (same as the previous raw `.exe`).
- **New macOS installer.** `PLASMA_MacOS_arm64.dmg` — open it and drag
  PLASMA.app into Applications.
- The raw, installer-free download is still available for every platform —
  now a `.zip`/`.tar.gz` of the onedir folder instead of a single file — and
  the Homebrew cask continues to work as before (its install path was
  updated internally to match the new onedir layout).
- **CI can now codesign + notarize the macOS `.app`**
  (`.github/codesign_notarize_macos.sh`), once five signing/notarization
  secrets are configured — see `docs/reference/release-process.md`. Until
  then it's a no-op and every macOS asset (raw zip, `.app.zip`, `.dmg`)
  keeps shipping unsigned/unnotarized exactly as before, same
  Gatekeeper right-click-Open step.

---

## 🚀 v2.2.2

### 🍺 Homebrew cask fix

- **Fixed `postflight` deprecation** in the `yuyichang/homebrew-plasma` cask
  (introduced in v2.2.1) — Homebrew Cask moved to a declarative
  `postflight_steps` mini-DSL, which has no `appdir` method of its own;
  paths now go through its `{{appdir}}` template-token expansion instead of
  Ruby string interpolation.
- In-repo cask template (`packaging/homebrew/plasma.rb`) now seeds an
  obviously-fake placeholder `sha256` (64 zeros) instead of
  `"REPLACE_WITH_SHA256"` — the latter isn't valid hex, so `brew
  bump-cask-pr` can't find-and-replace it on the very first real release
  (`Checksum` always downcases internally, so it searches for the
  lower-cased text, which never matched the literal placeholder).

### 🐛 Frozen-build font cache churn

- **Matplotlib no longer rebuilds its font cache on every single launch** of
  a frozen (PyInstaller) build. Root cause: all three `app_*.spec` files
  build in onefile mode, which re-extracts to a fresh temp directory on
  every launch; Matplotlib's persistent font cache recorded the absolute
  paths of its own bundled fonts, which lived inside that volatile
  directory, so every launch saw those paths gone and rebuilt from scratch.
  Fixed by excluding Matplotlib's bundled fonts from the frozen build
  (`spec_common.strip_mpl_bundled_fonts`) so it only ever indexes stable
  system fonts — at the cost of losing its bundled DejaVu Sans/STIX as a
  font choice.

---

## 🚀 v2.2.1

### 🍺 Homebrew distribution (macOS)

- **`brew install --cask yuyichang/plasma/plasma`** installs PLASMA as a
  proper `/Applications/PLASMA.app` (plus a `plasma` command on `$PATH`) —
  no Python environment or `liblsl` install required. Apple silicon only.
- The `.app` is a thin launcher, not a PyInstaller `BUNDLE()`: PyInstaller's
  own bundler sets `LSBackgroundOnly=True` for a `console=True` build, which
  would hide the app entirely (no Dock icon, no Terminal, no output) and
  turn any startup failure silent. Instead `.github/build_macos_app.sh`
  wraps the frozen console binary in an app whose launcher opens it inside a
  visible Terminal window — built in CI (`build.yml`) alongside the existing
  binary, published as `PLASMA_MacOS_arm64.app.zip`.
- Since the binary isn't code-signed or notarized, the cask clears the
  `com.apple.quarantine` attribute on install so Gatekeeper doesn't block
  the first run.
- The `pip install plasma-app[desktop]` Desktop-icon shortcut
  (`plasma-install-shortcut`) now pins its working directory to the same
  per-OS app-data dir a frozen build uses
  (`~/Library/Application Support/PLASMA` on macOS), instead of whatever a
  Desktop icon happens to default to — so config/data end up in the same
  place regardless of how PLASMA was launched.
- Playbook gained a "From PyPI (pip)" install tab
  (`docs/playbook/install.md`) alongside the existing prebuilt-binary/
  from-source paths.

---

## 🚀 v2.2.0

### 🫀 ECG extraction rewrite (MSense4ECG-XXXXX)

- **ECG (`ecg:block_v2` / `ECF2`) extraction replaced with a clean-room,
  CRC-validated decoder**, locked to that single on-disk format — the old
  pre-v0 `framed` 12-byte ECG layout is retired along with its decoder.
  Output columns are now exactly `SampleIndex`, `RtcTick`, `ECG`, `ETAG`,
  `PTAG` (no `Counter`/`CDCT`/`init_CDCT`/`Datetime`). A reserved `ETAG`
  (>3) is now logged as a warning rather than stopping decoding — the
  block's own CRC already confirms byte-level integrity — which is
  intentionally more lenient than the live sensor-stream's decoder.
- **Accelerometer extraction (`ac:v3` / `ACF3`, the same chest device's IMU)
  rewritten to match**: output columns locked to `SampleSequence`,
  `RtcTickEstBlock`, `RtcTickEst`, `AccX`, `AccY`, `AccZ` (raw counts are now
  converted to g directly in the decoder). `RtcTickEst` is a per-sample
  piecewise-linear ramp between consecutive blocks' RTC anchors, computed
  across the whole session — including across chunk boundaries.
- Output filenames are unchanged (still `<sub>_<ses>_..._ecg.csv` /
  `_ac.csv`, no version suffix). The wristband's own PPG/AC formats
  (`ppg:*`, `ac:legacy`/`v2`) are unaffected by this rewrite.

### 🗂️ Extraction pipeline

- **`CDCT`/`init_CDCT`/`Datetime` are now off by default** for PPG-device
  output (`ppg:legacy/v2/packed16`, `ac:legacy/v2`) — matching
  `ecg:block_v2`/`ac:v3`, which never had them. A new **"Include
  CDCT/Datetime"** checkbox (`--include_cdct` on the CLI) turns them back on
  for anyone still feeding `clocksync.py` from these CSVs.
- **New `README.txt` table** — "PPG-device file start times (UTC)" — always
  records each PPG-device input file's (`ppg:*`, `ac:legacy`/`v2`) start
  time (from its filename), regardless of the option above, so that
  information survives even with the row-level columns dropped.

---

## 🚀 v2.1.3

### 🔌 MSense reconnect hardening

- **Fixed a Linux/BlueZ reconnect storm that could wedge a wristband
  permanently.** On BlueZ, a failed or aborted reconnect left stale D-Bus
  disconnect subscriptions and half-open ACLs behind; on a Jetson this was
  observed to make `disconnected_callback` fire 20-40x per attempt for one
  wristband, saturating the single BLE event loop so the next `connect()`
  timed out — a self-reinforcing collapse that a PLASMA restart could not
  clear. Fixed by keeping exactly **one "live" client per wristband**
  (callbacks from any retired client are now ignored), debouncing the
  disconnect log/memo update to once per wristband per 5 s, and — on Linux
  only — running `bluetoothctl remove` on the peer before each reconnect
  attempt so BlueZ can't hand back a stale device object. See
  `docs/reference/msense-reconnect.md` for the full reconnect timeline and
  the entry points that trigger it.
- New developer reference doc walks the complete BLE-drop → retry →
  reconnect timeline, its governing constants (`RECONNECT_SWEEP_S`,
  `BLE_OP_TIMEOUT_S`, `STALE_LOST_AGE`), and every caller of
  `_reconnect_peripheral`.

### 🚦 Session-memo status classification

- **Transient `Initializing… / Starting… / Stopping…` status** now shows on
  the session banner and per-device rows while a slow, multi-second
  operation (MSense BLE connect, `collection_ctl` write, stop-confirm
  readback) is still in flight — previously the banner showed nothing until
  the whole device loop finished. Rendered in the existing **purple
  "guidance"** colour, orthogonal to severity, and excluded from the phase
  gate so it can never be mistaken for a fault or escalate `worst_level`.

### 🗂️ Extraction pipeline

- **Feather (Arrow IPC) is now the default extraction output format**,
  replacing CSV — ~20-30x faster to write and ~2-4x smaller on disk for
  these numeric-heavy AC/ECG tables. CSV is still available (pick it
  explicitly if downstream tooling needs plain text — e.g. the YAMS
  clock-sync tool only reads CSV).
- The downloader **extracts directly from the copied `.bin` files** instead
  of zipping them and immediately re-opening that same zip for extraction —
  a raw zip is still built, but only as a fallback if extraction wasn't
  requested or produced no output.

### 🧾 Build identity & logging

- Every session now logs a **launch banner** — `PLASMA v2.0.0 (a1b2c3d) on
  macOS-14.5-arm64-arm-64bit, Python 3.12.4` — so a bug report or support log
  unambiguously identifies the version, git commit, and host OS/Python that
  produced it (`plasma/build_info.py`).
- **Fixed session log corruption on Windows.** The log file was opened with
  the OS default text encoding (a legacy codepage on Windows), which can't
  represent the glyphs (🔌 🟢 🔴 ⚠️ …) used throughout status/journal
  messages — `logging` silently dropped every record it couldn't encode,
  including `[FAULT]` / `[RECOVER]` journal markers. The log file is now
  always opened as **UTF-8**; the console stream degrades unencodable
  glyphs to a backslash escape instead of raising.
- CI's nightly build now stamps the git commit hash before freezing, same as
  a tagged release build.

### 🖱️ Desktop shortcut

- New optional `desktop` extra + `plasma-install-shortcut` console script
  drops a double-clickable PLASMA icon on the Desktop (and Start Menu / app
  launcher) for a `pip install`-ed PLASMA — a lighter alternative to the
  standalone PyInstaller bundle for anyone running from a Python
  environment. `pip install -e ".[desktop]"` then `plasma-install-shortcut`;
  see `plasma-install-shortcut --help` for `--no-terminal` / `--no-startmenu`.

### 📦 Packaging & CI

- CI's nightly scheduled build now publishes a rolling **"nightly"
  pre-release** off the default branch's HEAD, in addition to the existing
  tagged-release builds.
- New `.github/workflows/publish.yml` builds the sdist/wheel and publishes
  to PyPI on a version tag via **Trusted Publishing (OIDC)** — no stored API
  token — gated on the test suite passing and the tag matching
  `plasma.__version__`.

---

## 🚀 v2.1.2

### 🐛 Bug fixes

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
- **Two MSense wristbands with the same BLE Name no longer collapse into one.**
  The driver now keys every per-wristband structure (memo row, SQC/live state,
  capabilities, LSL outlet, gyro bias) by the wristband's **address** instead of
  its Name, so two unrenamed factory-default units both connect, both record and
  both show a memo row. `"Name (Nickname)"` still labels every row and message
  unchanged — give the two a Nickname each to tell them apart on screen. The
  Configuration tab warns if the same address is listed on two enabled rows.

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

## 🚀 v2.1.1

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

---

## 🚀 v2.1.0

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
