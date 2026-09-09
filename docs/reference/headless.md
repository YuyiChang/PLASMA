# Headless control & situational awareness

PLASMA is a browser app, but a running instance also exposes a small typed
JSON API and ships a CLI (`plasma-ctl`) so it can be driven and monitored from
a script, a cron job, or a supervisor on the same machine. Everything operates
the **one live session** — a `plasma-ctl` call and an open browser tab see the
same devices, the same recorder, the same status.

**Localhost only.** The endpoints are bound wherever `app.launch()` binds
(`127.0.0.1:7860` by default) with no authentication. Do not expose the port.

---

## What was and wasn't possible before this layer

`gradio_client` alone could already *drive* PLASMA — the dashboard handlers are
auto-named (`/get_participant_encoding`, `/init_devices`, `/start_collection`,
`/stop_collection`) — but:

* it could not **observe** the app — the only status view is the memo panel,
  rendered by a `gr.Timer.tick` handler, which is not a callable endpoint;
* it could not learn about **errors** — `gr.Warning` is a browser toast,
  `start_collection` logged device failures at `INFO` and still reported
  `"Collection in progress"`, and `gr.Error` was never raised.

`plasma/api.py` adds the missing observability as an additive layer (no GUI
change) and `register()` wires five curated endpoints.

---

## Endpoints

Registered in `plasma.__main__.main()` via `plasma.api.register(ip)`. Call them
with `gradio_client`:

```python
from gradio_client import Client
c = Client("http://127.0.0.1:7860")

c.predict(api_name="/status")
c.predict("sub-1001", "ses-02", ["MSense Wristbands"], True, api_name="/start")
c.predict(api_name="/stop")
c.predict("subject seated", api_name="/mark")
c.predict(0.0, 0, api_name="/events")          # since_ts, min_level
```

| `api_name` | Signature | Returns |
|---|---|---|
| `/status` | `() ` | the full [status object](#status-object) |
| `/start` | `(sub: str, ses: str, devices: list[str], record: bool = True)` | `{ok, errors, status}` |
| `/stop` | `()` | `{ok, errors, status}` |
| `/mark` | `(text: str)` | `{ok, marker}` |
| `/events` | `(since: float = 0.0, min_level: int = 0)` | `list` of [event records](#event-record) |

`/start` and `/stop` **report outcomes**: `ok` is `false` and `errors` is
populated when a device fails to construct or a source ends up at L3 (a sensor
that silently isn't collecting), or when a stop is unconfirmed / fails.
`device` catalog names must match the Configuration tab exactly.

The Configuration **Restart / Shut down** buttons are deliberately kept off the
API (`api_name=False`).

---

## `plasma-ctl`

Installed as a console script (`pip install -e .`). Target URL is `$PLASMA_URL`
or `http://127.0.0.1:7860`.

```
plasma-ctl status [--json]
plasma-ctl start --sub sub-1001 --ses ses-02 --devices "MSense Wristbands" [--no-record]
plasma-ctl stop
plasma-ctl mark "subject seated"
plasma-ctl watch [--interval 1] [--fail-on L3]
plasma-ctl events [--since TS] [--min-level L2] [--follow] [--fail-on L3] [--json]
```

Exit codes: `0` ok · `1` a control call reported `ok: false` · `2` PLASMA
unreachable · `3` a `--fail-on` threshold was crossed. `watch --fail-on L3` is
the building block for an unattended rig — it polls `/status`, prints level
changes, and exits non-zero the moment anything reaches the threshold.

---

## MSense SQC snapshot / live-stream health checks

The **SQC snapshot** (FINITE, 128 KiB) and the **live stream** (INFINITY) are
triggered through Gradio's *auto-named* endpoints — the SQC-tab button handlers,
**not** `plasma/api.py`. They return only a status string, so the pattern is
**trigger via the auto-named endpoint, then read the outcome from `/status`**,
which now folds the transfer state into the L1–L3 failure model.

### 1. Make sure the MSense device is connected

A full collection is *not* required — `init_devices` connects the wristbands:

```python
c.predict(["MSense Wristbands"], api_name="/init_devices")   # auto-named
```

### 2. Trigger

```python
# FINITE snapshot of every connected wristband
#   mode:        "All" | "History Only" | "Custom"
#   max_seconds: only used by "Custom" (STOP after N s of forward data)
#   stream_mode: "Sequential" | "Parallel" | "Hybrid"
c.predict("History Only", 0, "Sequential", api_name="/_request")

c.predict(api_name="/_cancel")        # cancel all in-flight snapshots

# INFINITY continuous stream — rolling in-memory only, not recorded to XDF/LSL
c.predict(api_name="/_live_start")
c.predict(api_name="/_live_stop")
```

These names are derived from the handler functions (`_request`, `_cancel`,
`_live_start`, `_live_stop`); they are **not** part of the curated API and can
shift if that panel is refactored — confirm with `Client(...).view_api()`.

### 3. Parse the result from `/status`

Each MSense source in `/status` carries:

| field | shape |
|---|---|
| `sqc` | `{"status", "phase", "bytes_received", "bytes_total", "error", "saved_path", "diag", ...}` — `status` ∈ `idle` · `requesting` · `receiving` · `finishing` · `ready` · `rejected` · `error` · `unavailable` |
| `live_stream` | `{"status", "error", "product", "diag"}` — `status` ∈ `idle` · `requesting` · `streaming` · `stopping` · `stopped` · `error` |
| `level` / `category` / `level_reason` | the SQC / live state **folded into the source's failure level** |

Health / failure mapping (`plasma.status.sqc_level`, mirrors
`docs/failure-levels.md`):

| SQC / live state | level | meaning |
|---|---|---|
| `ready` (snapshot complete) · `streaming` · any in-flight state · `idle` | **NONE** | healthy / nothing to do |
| `rejected` + `NOT_RECORDING` | **L1 / advisory** | wristband isn't recording — one operator action |
| `rejected` + `BUSY` / `NOT_SUBSCRIBED` / `MTU_TOO_SMALL` / `INVALID_COMMAND` / `WRONG_SESSION` | **L2 / caution** | retry after clearing the condition |
| `error` — no START_ACK / decode failed / protocol violation / live `stalled` | **L2 / caution** | transfer failed |

SQC statuses `ready` · `error` · `rejected` · `idle` · `unavailable` are
**terminal** — poll `/status` until every source reaches one.

```python
c.predict("History Only", 0, "Sequential", api_name="/_request")

TERMINAL = {"ready", "error", "rejected", "idle", "unavailable"}
while True:
    st = c.predict(api_name="/status")
    srcs = [s for d in st["devices"] for s in d["sources"] if s.get("sqc")]
    if all(s["sqc"]["status"] in TERMINAL for s in srcs):
        break
    time.sleep(1)

for s in srcs:
    q = s["sqc"]
    if q["status"] == "ready":
        print(s["name"], "OK", q.get("saved_path"))
    elif s["level"] >= 2:                       # L2/L3 — a real failure
        print(s["name"], "FAIL", s.get("level_reason"))
    elif s["level"] == 1:                       # L1 — advisory
        print(s["name"], "advisory", s.get("level_reason"))
```

Equivalently, watch `/events`: an SQC / live-stream error now raises a `fault`
event (and a `[FAULT]` LSL marker) like any other L2/L3, with `sts` carrying
the reason (`"sqc error: no START_ACK within 8s"`). `plasma-ctl status` prints
the `sqc:` / `live_stream:` line and the `→` reason under each source, and
`plasma-ctl watch --fail-on L2` / `plasma-ctl events --fail-on L2` trip on it.

---

## Durable fault history — `events.jsonl`

`SessionEventLog` diffs successive `/status` snapshots (1 Hz daemon thread) and
appends one JSON line per change to **two** files:

* `<data_dir>/events.jsonl` — every session, append-only, survives restarts.
  `tail -f` it, or read it via `/events`.
* `<session_dir>/events.jsonl` — a per-session copy, written once a collection
  starts, so it travels with the `.xdf`.

`fault` and `recover` transitions are **also** pushed onto the journaler LSL
stream as `[FAULT] <device> <category>: <status>` / `[RECOVER] <device>`
markers, so an uncertain session boundary or a mid-collection sensor drop is
visible inside the recording itself.

### Event record

```json
{
  "ts": 1725830000.12,
  "iso": "2026-09-08T20:53:16-04:00",
  "phase": "COLLECTING",
  "event": "fault",
  "device": "ShimmerGSR",
  "source": "ShimmerGSR",
  "level": 3,
  "level_name": "L3",
  "category": "warning",
  "sts": "❌ Fault [Errno 2] could not open port COM5",
  "detail": "",
  "session_dir": "/…/data/sub-1001/ses-02/100102_260908_205316"
}
```

| `event` | When |
|---|---|
| `session_start` / `session_stop` | phase entered COLLECTING / STOPPED |
| `phase` | any phase transition |
| `fault` | a source rose to L2 or L3 — a device/link failure, a silent recorded stream, or an SQC / live-stream transfer error (see `docs/failure-levels.md`); `sts` carries the reason |
| `recover` | a source that had been ≥ L2 dropped back to ≤ L1 |
| `recorder` | the XDF recorder's state changed |
| `device_added` / `device_removed` | a source appeared / went away |
| `heartbeat` | every 30 s — carries `worst_level` + phase, so a monitor can tell "alive and nominal" from "dead" |

Levels 1–3 are the internal ECAM-style severity model documented in
`docs/failure-levels.md`; they are not shown in the GUI but are the right thing
for a headless supervisor to gate on.

---

## Status object

```jsonc
{
  "app":      {"name": "PLASMA", "version": "2.0.0"},
  "phase":    "COLLECTING",                     // SETUP | COLLECTING | STOPPED
  "session":  {"sub_id", "ses_id", "participant_enc",
               "session_dir", "sts", "elapsed_s"},
  "recorder": {"state", "summary", "file", "streams": [...]},  // state: idle|recording|stopped|unavailable
  "devices": [
    {"tag": "MSense Wristbands", "worst_level": 3, "worst_level_name": "L3",
     "sources": [
       {"name", "key", "sts", "latest",
        "level": 3, "level_name": "L3", "category": "warning",
        "level_reason": "sqc error: no START_ACK within 8s",  // set when SQC/live drove the level
        "stream": {"name","type","health","n_samples","srate", ...} | null,
        "acq_stop": {...},                        // MSense only, best-effort
        "sqc": {...}, "live_stream": {...}}       // MSense SQC / live-stream transfer state
     ]}
  ],
  "external_streams":       [...],               // LSL on the network, pre-record
  "other_recorded_streams": [...],               // recorded streams with no local device
  "worst_level": 3, "worst_level_name": "L3", "worst_category": "warning",
  "log_file":   "/…/2026-09-08_plasma_session.log",
  "events_file":"/…/events.jsonl",
  "ts": 1725830000.12
}
```

`worst_level` folds in every device source's status, every recorded stream's
health (a silently-stalled or lost recorded stream escalates its device's row),
each MSense source's SQC-snapshot / live-stream transfer state, and
`recorder.state == "unavailable"` during a collection.
