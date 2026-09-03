"""MSense wristband configuration: the per-plugin config blob shape, the
selection helpers the driver reads, and the "MSense wristbands" section of the
Configuration tab.

The blob persisted under ``plugins.msense`` in ``plasma_device_config.json`` is
``{"devices": [ {Name, Nickname, UUID / MAC Address, Enabled, IMU Stream}, ... ]}``.

This module must NOT import ``plasma.config`` — ``config_section`` receives the
``DeviceConfig`` instance as ``host`` and reads/writes it through
``host.get_plugin_config`` / ``host.update_plugin_config``.
"""
import json

import gradio as gr
import pandas as pd

_MSENSE_COLUMNS = ["Name", "Nickname", "UUID / MAC Address", "Enabled", "IMU Stream"]

# label -> {"name", "address"} for the most recent Configuration-tab scan
_scan_cache = {}


def _normalize_msense(raw):
    """Coerce a list of wristband records to the canonical 5-field shape —
    Nickname blank when missing, Enabled defaulting on, IMU Stream (demo
    firmware, not every unit has it) defaulting off."""
    if isinstance(raw, list):
        return [
            {
                "Name": rec.get("Name", ""),
                "Nickname": rec.get("Nickname", "") or "",
                "UUID / MAC Address": rec.get("UUID / MAC Address", ""),
                "Enabled": bool(rec.get("Enabled", True)),
                "IMU Stream": bool(rec.get("IMU Stream", False)),
            }
            for rec in raw
        ]
    return []


def merge_msense_records(existing, scanned, overwrite):
    """Fold freshly scanned wristbands into the configured list.

    ``existing`` — list of msense record dicts (as ``_normalize_msense`` emits).
    ``scanned``  — list of ``{"name", "address"}`` from ``ble_scan.scan_msense``.

    ``overwrite=True``  → one fresh record per scanned device, nothing kept.
    ``overwrite=False`` → ``existing`` plus a record for every scanned address
    not already listed (case-insensitive on "UUID / MAC Address").

    Fresh records use the advertised name as Name, blank Nickname, Enabled on,
    IMU Stream off. Returns ``(records, summary_str)``.
    """
    def _fresh(dev):
        return {
            "Name": dev["name"],
            "Nickname": "",
            "UUID / MAC Address": dev["address"],
            "Enabled": True,
            "IMU Stream": False,
        }

    if overwrite:
        records = [_fresh(dev) for dev in scanned]
        return records, f"Overwrote table with {len(records)} scanned wristband(s)"

    have = {str(rec.get("UUID / MAC Address", "")).strip().upper() for rec in existing}
    added = [_fresh(dev) for dev in scanned if dev["address"].strip().upper() not in have]
    skipped = len(scanned) - len(added)
    msg = f"Appended {len(added)} new wristband(s)"
    if skipped:
        msg += f" ({skipped} already listed)"
    return list(existing) + added, msg


# ── device_info.json import (YAMS provisioning format) ───────────────────────

def import_device_info(path):
    """Read a YAMS-style ``device_info.json`` (a flat ``{serial: UUID/MAC}``
    mapping) into ``[{"name": serial, "address": uuid}, ...]`` — the shape
    ``merge_msense_records`` consumes. Returns ``[]`` on any problem.

    Connect-time matching is by **address** (see ``device.py``'s
    ``connect_devices``), so the serial becomes the record Name / LSL label
    only — exactly what YAMS did with its aliases.
    """
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "address": str(v)}
            for k, v in raw.items() if str(v).strip()]


def merge_device_info_into_blob(blob, path, overwrite=False):
    """Merge a ``device_info.json`` into an msense config blob's device list.
    Returns ``(new_blob, summary_str)``."""
    scanned = import_device_info(path)
    if not scanned:
        return blob, f"No usable entries in {path}"
    records, msg = merge_msense_records(_records(blob), scanned, overwrite)
    return {"devices": records}, msg


def _msense_records_from_df(msense_df):
    def _nickname(row):
        v = row.get("Nickname", "")
        return str(v).strip() if pd.notna(v) else ""

    return [
        {
            "Name": row.get("Name", ""),
            "Nickname": _nickname(row),
            "UUID / MAC Address": row.get("UUID / MAC Address", ""),
            "Enabled": bool(row["Enabled"]) if pd.notna(row.get("Enabled", True)) else True,
            "IMU Stream": bool(row["IMU Stream"]) if pd.notna(row.get("IMU Stream", False)) else False,
        }
        for _, row in msense_df.iterrows()
        if str(row.get("Name", "")).strip() and str(row.get("UUID / MAC Address", "")).strip()
    ]


# ── selection helpers — take the plugin blob (``host.get_plugin_config("msense")``) ──

def _records(blob):
    return blob.get("devices", []) if isinstance(blob, dict) else []


def active_devices(blob):
    """Name -> UUID/MAC of wristbands that are both listed and enabled."""
    return {
        rec["Name"]: rec["UUID / MAC Address"]
        for rec in _records(blob)
        if rec.get("Enabled", True) and str(rec.get("Name", "")).strip()
        and str(rec.get("UUID / MAC Address", "")).strip()
    }


def imu_stream_devices(blob):
    """Names of wristbands with the demo IMU-stream characteristic enabled."""
    return {
        rec["Name"]
        for rec in _records(blob)
        if rec.get("Enabled", True) and rec.get("IMU Stream", False)
        and str(rec.get("Name", "")).strip()
    }


def display_labels(blob):
    """Name -> ``"Name (Nickname)"`` for listed+enabled wristbands (bare Name
    when Nickname is blank). Display only — the BLE Name stays the identifier
    for LSL stream names, status/memo keys and gyro-bias lookup."""
    out = {}
    for rec in _records(blob):
        name = str(rec.get("Name", "")).strip()
        if not (rec.get("Enabled", True) and name
                and str(rec.get("UUID / MAC Address", "")).strip()):
            continue
        nick = str(rec.get("Nickname", "")).strip()
        out[name] = f"{name} ({nick})" if nick else name
    return out


# ── Configuration-tab section ──────────────────────────────────────────────────

def config_section(host):
    devices = _records(host.get_plugin_config("msense"))

    with gr.Accordion("MSense wristbands", open=True):
        gr.Markdown(
            "Name–UUID pairs for BLE wristband discovery and connection. Uncheck Enabled to skip "
            "connecting to a wristband without removing it from the list. IMU Stream is demo firmware "
            "not every wristband has — only check it for units known to support it. Nickname is an "
            "optional label: when set, the Session dashboard status panel and Signal visualizer show "
            "\"Name (Nickname)\"; leave it blank to show just the device name. The BLE Name stays the "
            "identifier for LSL streams, saved files and gyro-bias.\n\n"
            "**Scan** for wristbands in range, tick the ones to add, then Append (keep the current "
            "list) or Overwrite, then **Apply MSense wristbands**. Scan before initializing devices "
            "on the Session dashboard — the Mac has a single BLE radio, and edits here only take "
            "effect the next time you Initialize."
        )
        with gr.Row():
            btn_scan = gr.Button("🔍 Scan for MSense wristbands (5 s)")
            add_mode = gr.Radio(
                ["Append (skip addresses already listed)", "Overwrite table"],
                value="Append (skip addresses already listed)",
                label="Add selected as",
            )
            btn_add = gr.Button("➕ Add selected", variant="primary")
        scan_results = gr.CheckboxGroup(choices=[], value=[], label="Discovered wristbands")
        msense_df = gr.Dataframe(
            value=pd.DataFrame(devices, columns=_MSENSE_COLUMNS),
            headers=_MSENSE_COLUMNS,
            datatype=["str", "str", "str", "bool", "bool"],
            row_count=(len(devices), "dynamic"),
            col_count=(5, "fixed"),
            interactive=True,
        )
        btn_apply = gr.Button("Apply MSense wristbands", variant="primary")
        status = gr.Textbox(interactive=False, value="", show_label=False, container=False)

        def _apply(df):
            records = _msense_records_from_df(df)
            host.update_plugin_config("msense", {"devices": records})
            n_enabled = sum(1 for rec in records if rec["Enabled"])
            return f"Saved — {n_enabled}/{len(records)} MSense wristband(s) enabled"

        def _scan():
            from plasma.devices.msense.ble_scan import scan_msense
            _scan_cache.clear()
            try:
                found = scan_msense()
            except Exception as e:
                return gr.update(choices=[], value=[]), f"Scan failed: {e}"
            labels = []
            for dev in found:
                label = f"{dev['name']} — {dev['address']}"
                _scan_cache[label] = dev
                labels.append(label)
            msg = (f"Found {len(labels)} MSense wristband(s) — select and Add"
                   if labels else "No MSense wristbands found (in range and advertising?)")
            return gr.update(choices=labels, value=labels), msg

        def _add(selected_labels, mode, df):
            picked = [_scan_cache[l] for l in (selected_labels or []) if l in _scan_cache]
            if not picked:
                return gr.update(), "Nothing selected to add"
            existing = _msense_records_from_df(df)
            records, msg = merge_msense_records(
                existing, picked, overwrite=str(mode).startswith("Overwrite"))
            return (gr.update(value=pd.DataFrame(records, columns=_MSENSE_COLUMNS)),
                    f"{msg} — press Apply MSense wristbands to save")

        btn_apply.click(_apply, inputs=msense_df, outputs=status)
        btn_scan.click(_scan, outputs=[scan_results, status])
        btn_add.click(_add, inputs=[scan_results, add_mode, msense_df], outputs=[msense_df, status])
