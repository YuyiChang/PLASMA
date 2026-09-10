"""Configuration for the simulated MSense device.

The blob persisted under ``plugins.msense_demo`` in
``plasma_device_config.json`` is::

    {"devices": [ {Name, Nickname, Sensor, Enabled, IMU Stream, Fault}, ... ]}

``Sensor`` is ``"PPG"`` or ``"ECG"`` (a real unit is one or the other). ``Fault``
is one of :data:`plasma.devices.msense_demo.faults.FAULT_IDS`.

Like ``plasma.devices.msense.config`` this module must NOT import
``plasma.config`` — ``config_section`` gets the ``DeviceConfig`` instance as
``host``.
"""
import hashlib

import gradio as gr
import pandas as pd

from plasma.devices.msense_demo import faults as _faults

_COLUMNS = ["Name", "Nickname", "Sensor", "Enabled", "IMU Stream", "Fault"]
_SENSORS = ("PPG", "ECG")


def _synth_addr(name, nickname=""):
    """A stable, MAC-shaped fake address for a demo wristband. Derived from
    Name **and** Nickname so two rows sharing a Name still get distinct
    addresses (the driver keys per-wristband state by address)."""
    h = hashlib.md5(f"{name}\x00{nickname}".encode("utf-8")).digest()
    return "DE:" + ":".join(f"{b:02X}" for b in h[:5])


def _records(blob):
    return blob.get("devices", []) if isinstance(blob, dict) else []


def _normalize_row(rec):
    sensor = str(rec.get("Sensor", "PPG")).strip().upper()
    return {
        "Name": str(rec.get("Name", "")).strip(),
        "Nickname": str(rec.get("Nickname", "") or "").strip(),
        "Sensor": sensor if sensor in _SENSORS else "PPG",
        "Enabled": bool(rec.get("Enabled", True)),
        "IMU Stream": bool(rec.get("IMU Stream", False)),
        "Fault": _faults.normalize(rec.get("Fault")),
    }


def _enabled(blob):
    return [r for r in (_normalize_row(x) for x in _records(blob))
            if r["Name"] and r["Enabled"]]


# ── selection helpers (take host.get_plugin_config("msense_demo")) ────────────

def _addr(r):
    return _synth_addr(r["Name"], r["Nickname"])


def active_devices(blob):
    """address -> Name for enabled demo wristbands (keyed by address to match
    the real driver — see plasma.devices.msense.config.active_devices)."""
    return {_addr(r): r["Name"] for r in _enabled(blob)}


def imu_stream_devices(blob):
    return {_addr(r) for r in _enabled(blob) if r["IMU Stream"]}


def display_labels(blob):
    out = {}
    for r in _enabled(blob):
        out[_addr(r)] = f"{r['Name']} ({r['Nickname']})" if r["Nickname"] else r["Name"]
    return out


def sensor_types(blob):
    """address -> "PPG" | "ECG"."""
    return {_addr(r): r["Sensor"] for r in _enabled(blob)}


def device_faults(blob):
    """address -> fault id."""
    return {_addr(r): r["Fault"] for r in _enabled(blob)}


# ── Configuration-tab section ────────────────────────────────────────────────

def _records_from_df(df):
    rows = []
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        rows.append(_normalize_row({
            "Name": name,
            "Nickname": row.get("Nickname", ""),
            "Sensor": row.get("Sensor", "PPG"),
            "Enabled": row.get("Enabled", True) if pd.notna(row.get("Enabled", True)) else True,
            "IMU Stream": row.get("IMU Stream", False) if pd.notna(row.get("IMU Stream", False)) else False,
            "Fault": row.get("Fault", _faults.NO_FAULT),
        }))
    return rows


def _default_row(n):
    kind = "PPG" if n % 2 == 0 else "ECG"
    return {"Name": f"DEMO-{kind}-{n + 1:02d}", "Nickname": "", "Sensor": kind,
            "Enabled": True, "IMU Stream": kind == "PPG", "Fault": _faults.NO_FAULT}


def config_section(host):
    devices = [_normalize_row(r) for r in _records(host.get_plugin_config("msense_demo"))]
    if not devices:
        devices = [_default_row(0), _default_row(1)]

    fault_lines = "\n".join(
        f"- `{fid}` — {meta['label']}: {meta['help']}"
        for fid, meta in _faults.FAULTS.items()
    )

    with gr.Accordion("MSense Demo (simulated)", open=True):
        gr.Markdown(
            "Simulated wristbands for development and demos — no hardware, no "
            "Bluetooth. They connect, stream live ENMO / battery / IMU, and serve "
            "ECG/PPG signal-quality snapshots through the real driver. Edits here "
            "take effect the next time you **Initialize** on the Session "
            "dashboard. (Demo mode itself needs a PLASMA restart.)\n\n"
            "**Sensor** is `PPG` or `ECG`. **Fault** injects a failure:\n\n"
            f"{fault_lines}"
        )
        df = gr.Dataframe(
            value=pd.DataFrame(devices, columns=_COLUMNS),
            headers=_COLUMNS,
            datatype=["str", "str", "str", "bool", "bool", "str"],
            row_count=(len(devices), "dynamic"),
            col_count=(len(_COLUMNS), "fixed"),
            interactive=True,
        )
        with gr.Row():
            btn_add = gr.Button("➕ Add demo wristband")
            btn_apply = gr.Button("Apply demo wristbands", variant="primary")
        status = gr.Textbox(interactive=False, value="", show_label=False, container=False)

        def _add(cur_df):
            rows = _records_from_df(cur_df)
            rows.append(_default_row(len(rows)))
            return (gr.update(value=pd.DataFrame(rows, columns=_COLUMNS)),
                    "Added — press Apply to save")

        def _apply(cur_df):
            rows = _records_from_df(cur_df)
            host.update_plugin_config("msense_demo", {"devices": rows})
            n_on = sum(1 for r in rows if r["Enabled"])
            return f"Saved — {n_on}/{len(rows)} demo wristband(s) enabled"

        btn_add.click(_add, inputs=df, outputs=[df, status])
        btn_apply.click(_apply, inputs=df, outputs=status)
