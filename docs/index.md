---
hide:
  - navigation
---

# PLASMA Playbook

**PLASMA** — *Platform for LSL-based Acquisition of Sensor Metrics and Analytics* —
runs a multi-device study session from one browser window: initialise every
sensor, stream every channel over [Lab Streaming Layer](https://labstreaminglayer.org/),
and record the whole room to a single **XDF** file.

This site is the **operator's handbook**: how to install PLASMA, configure it,
pair MSense wristbands, run a session, read the memo panel, and recover when
something goes wrong. It does not cover the MSense BLE wire protocol — see the
files under `plasma/devices/msense/docs/` in the repository for that.

<div class="grid cards" markdown>

- :material-download: **[Install & first launch](playbook/install.md)**
  From source or a prebuilt binary; what PLASMA writes to disk.

- :material-cog: **[Configure PLASMA](playbook/configure.md)**
  The device catalog, network addresses, demo mode, restart / shut down.

- :material-watch: **[Pair MSense wristbands](playbook/pair-msense.md)**
  Scan, add, enable, and the IMU-stream column.

- :material-play-circle: **[Run a session](playbook/run-a-session.md)**
  IDs, initialise, start, journaler markers, stop — and where the data lands.

- :material-heart-pulse: **[Signal-quality checks](playbook/signal-quality.md)**
  The contact check before you trust a recording.

- :material-toolbox: **[MSense tools (YAMS)](playbook/msense-tools.md)**
  Orientation, flash erase, download, extract, clock-sync, viewer.

- :material-palette: **[The memo engine](playbook/memo-engine.md)**
  How the panel colour-codes every device and stream — and what the colours mean.

- :material-alert: **[Abnormal handling](playbook/abnormal-handling.md)**
  A symptom → cause → action runbook for the faults you'll actually hit.

</div>

## The arc of a session

<div class="grid cards" markdown>

- **1 · Configure**
  In **Configuration**, enable the devices this study uses and fill in their
  addresses. Click *Apply*, then *Refresh list* on the dashboard.

- **2 · Initialise**
  In **Session Dashboard**, enter the subject and session IDs, select the
  devices for this run, and click **Initialize**. Each device connects and
  announces its LSL streams.

- **3 · Record**
  Press **Start**. The built-in recorder writes every session stream to one XDF
  file; the memo panel shows, live, that each device is producing data. Press
  **Stop** when the session ends.

- **4 · Review**
  Check the memo for any device that ended amber or red, then open the XDF in
  your analysis tool. MSense onboard data is pulled and extracted to CSV from
  the **YAMS** tab.

</div>

## Devices

Each sensor ships as a plugin and streams over LSL, so every channel lands in
the recording time-stamped against the same session clock. Enable only the ones
a study needs.

| Device | Modality | Extra |
|---|---|---|
| MSense Wristbands | BLE motion (ENMO / IMU) + ECG/PPG signal quality | `msense` — multi-unit, own tab |
| qb2 LiDAR | Depth / spatial sensing | `qb2` |
| Pupil Labs IMU | Head motion (Realtime API) | `pupil` |
| Pupil Labs Eye Events | Blinks & fixations | `pupil` |
| ShimmerGSR | Galvanic skin response | `shimmer` |
| OBS Recorder | Video capture (via obs-websocket) | `obs` |
| Bitalino | Biosignals (ECG, EEG, EMG, EDA) | opt-in — needs PyBluez |

!!! info "Adding your own"
    A sensor is a `PlasmaDevice` subclass plus one entry in
    `plasma/plugins.py` — core code never imports a concrete device. See
    [developing a sensor plugin](https://github.com/YuyiChang/PLASMA/blob/main/plasma/devices/README.md);
    `plasma/devices/msense/` is the full-featured reference.

## Headless operation

A running PLASMA also exposes a small typed API and a `plasma-ctl` CLI for
scripted or unattended rigs — see [Headless control & API](reference/headless.md).
