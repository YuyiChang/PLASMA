# Files & paths

Where PLASMA keeps everything, and the shape of the files a session produces.

## The home directory

All writable state lives under one **home directory**, resolved once at startup:

| Condition | Home |
|---|---|
| `$PLASMA_HOME` set | that path |
| Prebuilt binary — macOS | `~/Library/Application Support/PLASMA` |
| Prebuilt binary — Windows | `%LOCALAPPDATA%\PLASMA` |
| Prebuilt binary — Linux | `$XDG_DATA_HOME/plasma` or `~/.local/share/plasma` |
| From source | the current working directory |

```text
<home>/
├── plasma_device_config.json          # device catalog, addresses, demo flag
├── plasma_gyro_bias.json              # MSense gyro-bias calibration (created on first calibrate)
├── task.txt                           # optional — journaler activity labels, one per line
└── data/
    ├── <YYYY-MM-DD>_plasma_session.log   # one per calendar day, appended
    ├── events.jsonl                      # rolling fault history, all sessions
    └── <sub>/<ses>/<enc>_<YYMMDD_HHMMSS>/    # one folder per Start
        ├── plasma_recording_<YYMMDD_HHMMSS>.xdf
        ├── events.jsonl                     # this session's slice of the fault history
        └── msense/                          # MSense .txt dumps, SQC snapshots
```

`<enc>` is the participant encoding: the first integer in `<sub>` × 100 + the
first integer in `<ses>` (e.g. `sub-4021`, `ses-02` → `402102`).

## `plasma_device_config.json`

```json
{
  "enabled_devices": ["MSense Wristbands", "ShimmerGSR"],
  "ip_qb2_lidar": "",
  "ip_pupil_labs": "",
  "demo_mode": false,
  "plugins": {
    "msense": { "...": "name ↔ address pairs" },
    "msense_demo": { "devices": [
      {"Name": "DEMO-PPG-01", "Nickname": "left wrist", "Sensor": "PPG",
       "Enabled": true, "IMU Stream": true, "Fault": "none"}
    ]}
  }
}
```

Written by the Configuration tab; safe to hand-edit while PLASMA is closed.
Import / export from the Configuration tab moves it between machines.

## `events.jsonl`

Append-only, one JSON object per line. Written to both `<home>/data/events.jsonl`
(every session) and the per-session folder (from Start onward).

```json
{
  "ts": 1725830000.12,
  "iso": "2026-09-08T20:53:16-04:00",
  "phase": "COLLECTING",
  "event": "fault",
  "device": "MSense Wristbands",
  "source": "DEMO-PPG-01",
  "level": 2,
  "level_name": "L2",
  "category": "caution",
  "sts": "🔌 disconnected",
  "detail": "",
  "session_dir": "/…/data/sub-4021/ses-02/402102_260908_205316"
}
```

| `event` | When |
|---|---|
| `session_start` / `session_stop` | phase entered COLLECTING / STOPPED |
| `phase` | any phase transition |
| `fault` | a source went amber or red |
| `recover` | a source that had been amber/red dropped back |
| `recorder` | the XDF recorder's state changed |
| `device_added` / `device_removed` | a source appeared / went away |
| `heartbeat` | every 30 s — worst level + phase, so a monitor can tell "alive and nominal" from "dead" |

`level` is the internal 0–3 severity ([failure levels](failure-levels.md)).

## `plasma-ctl` cheat-sheet

```bash
plasma-ctl status [--json]
plasma-ctl start --sub sub-4021 --ses ses-02 --devices "MSense Wristbands" [--no-record]
plasma-ctl stop
plasma-ctl mark "subject seated"
plasma-ctl watch  [--interval 1] [--fail-on L2|L3]
plasma-ctl events [--since TS] [--min-level L2|L3] [--follow] [--json]
```

Target URL: `$PLASMA_URL` or `http://127.0.0.1:7860`. Exit codes: `0` ok,
`1` a control call reported failure, `2` PLASMA unreachable, `3` a `--fail-on`
threshold was crossed. Full API: [Headless control & API](headless.md).

## Session log

`<home>/data/<YYYY-MM-DD>_plasma_session.log` — one file per calendar day,
appended across runs, format `%(asctime)s [%(levelname)s] %(message)s`, also
echoed to the console. Genuine device / recorder failures are `WARNING` /
`ERROR`; everything else is `INFO`.
