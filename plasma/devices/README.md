# Developing a sensor plugin

Every sensor in PLASMA is a **plugin**: a `PlasmaDevice` subclass plus a
registration entry. The orchestrator (`plasma/integrated_panel.py`) never imports
a concrete device — it discovers them through `plasma/plugins.py` and drives them
through the `PlasmaDevice` contract. Adding a sensor never requires touching
`config.py`, `integrated_panel.py`, `template.py` or `__main__.py`.

This guide covers two cases:

1. **Simple sensor** — one file, a start/stop capture loop, no custom UI.
2. **Rich sensor** — its own package with a Configuration-tab section and/or
   extra Gradio tabs (see `plasma/devices/msense/` for the reference
   implementation).

---

## 1. The `PlasmaDevice` contract

`plasma/devices/template.py` defines the base class. Your subclass is constructed
once per Initialize as:

```python
Device(session_info, logger, tag="<catalog display name>")
```

| member | who provides | meaning |
|---|---|---|
| `session_info` | orchestrator | `{"sub_id", "ses_id", "participant_enc", "log_dir"}` — `log_dir` is `data/<sub>/<ses>` |
| `logger` | orchestrator | stdlib logger; use `self.info(msg)` which prefixes `[tag]` |
| `tag` | orchestrator | the catalog display name; also the default LSL stream name / memo key |
| `self.memo` | you | a `PlasmaMemo` (or a `{sub_name: PlasmaMemo}` dict for multi-unit devices) |
| `self._stop_event` | base | `threading.Event`; your loop checks `self._stop_event.is_set()` |

Lifecycle methods the orchestrator calls (all wrapped in try/except by the caller):

| method | default | override when |
|---|---|---|
| `start()` | spawns a daemon thread running `self.streaming()` | you need custom start logic (see OBS/Shimmer) |
| `streaming()` | placeholder loop | **always** — this is your capture loop |
| `stop()` | sets `_stop_event`, marks memo stopped | you hold vendor handles to close |
| `disconnect()` | no-op | you hold a connection (BLE/socket/serial) that must be torn down before re-Initialize |
| `get_sources()` | `{tag: self.memo}` | leave alone unless multi-unit (then just make `self.memo` a dict) |

### `PlasmaMemo` — status + live buffers

```python
PlasmaMemo(name, channels=None, window_s=30.0, label=None, channel_groups=None)
```

- `memo.sts` — a short status string shown on the Session dashboard. Convention:
  `🟦` init · `🟢` running · `🟥` stopped · `⛔`/`❌`/`🚫 FAULT` error.
- `memo.set_latest("...")` — a timestamped one-liner shown under the status.
- `memo.set_data(channel, value, t)` — append a sample to a rolling buffer.
  `t` is **your** time reference in seconds (e.g. `time.time() - self.t_start`),
  not wall clock. Buffers auto-prune to `window_s`.
- `channels=[...]` — declare the channel names you'll push. The Signal visualizer
  offers exactly these.
- `channel_groups={"Accel (g)": ["AccX","AccY","AccZ"], ...}` — optional; puts
  related channels on one shared subplot instead of one row each.
- `label` — a cosmetic display name (e.g. `"MSense4ECG-Z5G4A (GrayGuy)"`); the
  identifier stays `name`.

### LSL

Each device creates its own `pylsl.StreamInfo` / `StreamOutlet` in `__init__` and
`push_sample` / `push_chunk` from `streaming()`. There is no shared LSL helper.

---

## 2. Simple sensor — walkthrough

Create `plasma/devices/mysensor.py`:

```python
import time
from pylsl import StreamInfo, StreamOutlet
from plasma.devices.template import PlasmaDevice, PlasmaMemo


class MySensor(PlasmaDevice):
    def __init__(self, session_info, logger=None, tag=None):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo(tag, channels=["value"])

        self.outlet = StreamOutlet(StreamInfo("MySensor", "misc", 1, 50, "float32"))
        try:
            self.dev = open_my_device()          # vendor SDK
            self.memo.sts = "Ready"
        except Exception as e:
            self.memo.sts = f"⛔ {e}"

    def streaming(self):
        self.t_start = time.time()
        while not self._stop_event.is_set():
            x = self.dev.read()
            self.outlet.push_sample([x])
            t = time.time() - self.t_start
            self.memo.set_data("value", x, t)
            self.memo.set_latest(f"value={x:.3f}")
            time.sleep(0.02)

    def disconnect(self):
        if getattr(self, "dev", None):
            self.dev.close()
```

Register it in **`plasma/plugins.py`** — add to `_STATIC`:

```python
PlasmaPlugin("mysensor", "My Sensor", "plasma.devices.mysensor", "MySensor"),
```

`_STATIC` entries are listed in the catalog even if their vendor SDK fails to
import (the class is imported lazily, only on Initialize). Set
`enabled_by_default=False` for sensors that need special hardware/drivers.

Add the module to **both** PyInstaller specs' `hiddenimports`
(`app_macos.spec`, `app_windows.spec`) — PyInstaller can't see the dynamic import.

That's it. The sensor now appears in the Configuration catalog, the Session
dashboard device list, and (once it pushes channels) the Signal visualizer.

### Custom start/stop (no capture thread)

If the vendor SDK has its own callback/streaming model, override `start()` and
`stop()` directly instead of `streaming()` — see `plasma/devices/obs.py`
(loops over websocket clients) and `plasma/devices/shimmer.py`.

### Multiple physical units

Make `self.memo` a `{unit_name: PlasmaMemo}` dict. `get_sources()` returns it as
is, so each unit shows up as its own visualizer source. See
`plasma/devices/msense/device.py`.

---

## 3. Rich sensor — its own package

When a sensor needs configuration beyond an IP address, or its own
visualization/analysis tab, give it a package: `plasma/devices/mysensor/` with an
`__init__.py` exposing `register(register_fn)`, and list the **package** in
`plasma/plugins.py:_DISCOVERY` (not `_STATIC`).

```python
# plasma/devices/mysensor/__init__.py
def register(register_fn):
    from plasma.plugins import PlasmaPlugin
    from plasma.devices.mysensor import config as _config, panels as _panels
    register_fn(PlasmaPlugin(
        id="mysensor",
        display_name="My Sensor",
        module="plasma.devices.mysensor.device",
        class_name="MySensor",
        config_section=_config.config_section,
        tabs=(("My Sensor Analysis", _panels.build_tab),),
    ))
```

`register()` runs at app start (`plasma.plugins.load_plugins()`), so keep it
cheap — do **not** import heavy vendor SDKs at module top level of anything
`register()` touches; import them inside methods or inside `device.py` (which is
loaded lazily on Initialize).

### `config_section(host)` — a Configuration-tab accordion

```python
import gradio as gr

def config_section(host):
    blob = host.get_plugin_config("mysensor")          # {} on first run
    with gr.Accordion("My Sensor", open=True):
        addr = gr.Text(value=blob.get("address", ""), label="Address")
        btn = gr.Button("Apply My Sensor", variant="primary")
        status = gr.Textbox(show_label=False, container=False, interactive=False)

        def _apply(address):
            host.update_plugin_config("mysensor", {"address": address})
            return "Saved"

        btn.click(_apply, inputs=addr, outputs=status)
```

- `host` is the `DeviceConfig` singleton. Read with
  `host.get_plugin_config("<id>")`, write with
  `host.update_plugin_config("<id>", blob)` — the blob is persisted under
  `plugins.<id>` in `plasma_device_config.json`.
- Your section owns its **own Apply button**. The global Apply only writes
  `enabled_devices` and the built-in IP fields.
- **Do not import `plasma.config`** from your package — always take `host` as an
  argument (avoids an import cycle).
- `device.py` reads the blob at construction:
  `blob = device_config.get_plugin_config("mysensor")`.

### `tabs` — extra Gradio tabs

Each entry is `(title, builder)` where `builder(ip)` takes the `IntegratedPanel`
instance. Reach your live driver with `ip.find_device(MySensor)` (returns `None`
until Initialize). Tabs only render while the device is **enabled** in the
catalog. See `plasma/devices/msense/panels.py` for a full example (a status
table + Plotly figures on a `gr.Timer`).

---

## 4. Checklist

- [ ] `PlasmaDevice` subclass; `streaming()` (or `start`/`stop`) implemented
- [ ] `self.memo` set with declared `channels`; `memo.sts` reflects real state
- [ ] `disconnect()` overridden if you hold a connection
- [ ] own `StreamOutlet`; `push_sample`/`push_chunk` from the loop
- [ ] registered — `_STATIC` entry (simple) or package + `_DISCOVERY` (rich)
- [ ] added to `app_macos.spec` **and** `app_windows.spec` `hiddenimports`
- [ ] new dependency added to `requirements.txt` (annotate `# <id> plugin only`)
- [ ] offline check: `python -m plasma` launches, sensor shows in the catalog
- [ ] tests under `plasma/devices/<id>/tests/` if the sensor has a package
