# Run a session

The **Session Dashboard** is the recording console. Left column: what to
record. Right column: run controls, the journaler, and the live memo.

## 1 · Session info

![Session Dashboard, ready to initialise](../assets/screenshots/session-dashboard.png){ .shot }

- **Subject ID** — format `sub-XXXX` (X integer).
- **Session ID** — format `ses-YY`.
- **Participant encoding** (read-only) — `XXXXYY`, computed from the two IDs. It
  is written into the wristbands and into every output path, so set the IDs
  **before** you Start. Editing them later is fine — PLASMA re-applies the new
  identity at Start.

## 2 · Device initialization

- **Select sensor(s)** — the device types to use this run (from the
  [Configuration](configure.md) catalog).
- **📼 Record all LSL streams to XDF** — leave on. While the session runs, the
  built-in recorder resolves every LSL stream on the machine — PLASMA's own
  outlets and anything else — and writes them to one `.xdf` file. No
  LabRecorder needed.
- **🚦 Initialize selected device(s)** — connects each device and announces its
  streams. Do this *after* any BLE scanning on the Configuration tab. The memo
  header changes to **"Ready to start"** and a row appears per device.

![Memo panel after Initialize](../assets/screenshots/memo-ready.png){ .shot }

## 3 · Start

Press **Start ▶️**. PLASMA:

- creates the session folder
  `<home>/data/<sub>/<ses>/<enc>_<YYMMDD_HHMMSS>/`,
- tells every device to start,
- spins up the XDF recorder.

The memo header becomes **"Collection in progress"** with a running
`HH:MM:SS` clock, and each device row turns green with a live latest-value and
a `📼 N samp @ X Hz → …xdf` recording line.

![Memo panel during collection](../assets/screenshots/memo-collecting.png){ .shot }

## 4 · Journaler

Open the **🗒️ Journaler** accordion to drop timestamped markers onto a
dedicated LSL stream that is recorded alongside the sensors (so they land in the
`.xdf` too).

![The Journaler accordion](../assets/screenshots/journaler.png){ .shot }

- **Marker text** + **✍️ Send marker** — a free-text event ("subject seated",
  "block 2 start").
- **🚩 Flag** — a one-click `[FLAG]` marker for "something happened here, note
  it later".

## 5 · Stop

Press **Stop 🛑**. Every device is told to stop, the recorder flushes and
finalises the `.xdf`, and the memo header becomes **"Collection stopped"**. A
device reporting "stopped" is now expected — its row goes neutral, not red.

![Memo panel after Stop](../assets/screenshots/memo-stopped.png){ .shot }

## Where the data is

```text
<home>/data/<sub>/<ses>/<enc>_<YYMMDD_HHMMSS>/
├── plasma_recording_<YYMMDD_HHMMSS>.xdf   # every stream, one file
├── events.jsonl                           # this session's fault history
└── msense/                                # MSense .txt dumps, SQC snapshots
```

Full layout and the daily session log: [Files & paths](../reference/files-and-paths.md).

## 6 · Review

Watch the memo through the run and check it at the end. Any row that ends
**amber** or **red** is telling you something — the
[memo engine](memo-engine.md) page explains the colours, and
[abnormal handling](abnormal-handling.md) is the fix-it runbook.

---

Next: [Signal-quality checks](signal-quality.md).
