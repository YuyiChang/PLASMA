"""Configuration for the simulated MSense device.

The blob persisted under ``plugins.msense_demo`` in
``plasma_device_config.json`` is::

    {"devices": [{Name, Nickname, Sensor, Subject ID override, Enabled,
                  IMU Stream, Fault}, ...], "participant_id_override_enabled": false}

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
from plasma.devices.msense.config import SUBJECT_OVERRIDE, OVERRIDE_ENABLED, subject_override

_COLUMNS = ["Name", "Nickname", "Sensor", SUBJECT_OVERRIDE, "Enabled", "IMU Stream", "Fault"]
_SENSORS = ("PPG", "ECG")


def _synth_addr(name, nickname=""):
    """A stable, MAC-shaped fake address for a demo wristband. Derived from
    Name **and** Nickname so two rows sharing a Name still get distinct
    addresses (the driver keys per-wristband state by address)."""
    h = hashlib.md5(f"{name}\x00{nickname}".encode("utf-8")).digest()
    return "DE:" + ":".join(f"{b:02X}" for b in h[:5])


def _records(blob):
    # The editor and driver must use the same first-use defaults. An absent
    # list means never configured; an explicitly empty list means no devices.
    if not isinstance(blob, dict) or "devices" not in blob:
        return [_default_row(0), _default_row(1)]
    return blob["devices"]


def _normalize_row(rec):
    sensor = str(rec.get("Sensor", "PPG")).strip().upper()
    return {
        "Name": str(rec.get("Name", "")).strip(),
        "Nickname": str(rec.get("Nickname", "") or "").strip(),
        "Sensor": sensor if sensor in _SENSORS else "PPG",
        SUBJECT_OVERRIDE: subject_override(rec.get(SUBJECT_OVERRIDE)),
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


def participant_overrides(blob):
    if not isinstance(blob, dict) or not blob.get(OVERRIDE_ENABLED, False):
        return {}
    values = {_addr(r): r[SUBJECT_OVERRIDE] for r in _enabled(blob)}
    return {addr: value for addr, value in values.items() if value}


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
            SUBJECT_OVERRIDE: row.get(SUBJECT_OVERRIDE),
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
        gr.Markdown("Enter four-digit **Available participant IDs** once, then select an ID "
                    "in each **Subject ID override** dropdown. **Use dashboard Subject ID** "
                    "inherits the default. Enable overrides and press **Apply demo wristbands**; "
                    "saved overrides apply at the next Start without reconnecting. "
                    "Changes during recording affect the next run.")
        from plasma.devices.msense.wristband_editor import build_wristband_editor
        with gr.Column(elem_id="msense-demo-wristbands-config"):
            build_wristband_editor(
                host, "msense_demo", devices, _COLUMNS,
                normalize=lambda rows: _records_from_df(pd.DataFrame(rows)),
                new_record=_default_row, apply_label="Apply demo wristbands",
                choices={"Sensor": list(_SENSORS), "Fault": list(_faults.FAULTS)})
