import gradio as gr
import json
import os
import tempfile
import pandas as pd

__version__ = "0.1.0-beta"
__data_dir__ = "data"

DEVICE_CATALOG = {
    'MSense Wristbands': {
        'module': 'plasma.devices.msense',
        'class': 'MotionSenseHRV'
    },
    'qb2 LiDAR': {
        'module': 'plasma.devices.qb2',
        'class': 'Qb2'
    },
    'Pupil Lab IMU': {
        'module': 'plasma.devices.pupil_labs',
        'class': 'PupilLabsIMU'
    },
    'Pupil Lab Eye Event Blink': {
        'module': 'plasma.devices.pupil_labs',
        'class': 'PupilLabsEyeEventBlink'
    },
    'ShimmerGSR': {
        'module': 'plasma.devices.shimmer',
        'class': 'ShimmerGSR'
    },
    'OBS Recorder': {
        'module': 'plasma.devices.obs',
        'class': 'ObsRecorder'
    },
    'Bitalino': {
        'module': 'plasma.devices.bitalino',
        'class': 'PlasmaBitalino'
    },
}

_DEVICE_CONFIG_FILE = "plasma_device_config.json"

_DEFAULT_DISABLED_DEVICES = {'Bitalino'}  # needs PyBluez, which isn't bundled — opt-in only

_DEFAULTS = {
    "enabled_devices": [d for d in DEVICE_CATALOG.keys() if d not in _DEFAULT_DISABLED_DEVICES],
    "msense_devices": [],
    "ip_qb2_lidar": "",
    "ip_pupil_labs": "",
}

_MSENSE_COLUMNS = ["Name", "Nickname", "UUID / MAC Address", "Enabled", "IMU Stream"]


def _normalize_msense(raw):
    """Accepts either the legacy {name: uuid} mapping or the current
    list of {Name, Nickname, UUID / MAC Address, Enabled, IMU Stream} records,
    and returns the latter — legacy entries default to enabled, Nickname blank,
    IMU Stream off since it's demo firmware not every wristband has."""
    if isinstance(raw, dict):
        return [{"Name": k, "Nickname": "", "UUID / MAC Address": v, "Enabled": True, "IMU Stream": False} for k, v in raw.items()]
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
    ``scanned``  — list of ``{"name", "address"}`` from ``plasma.ble_scan.scan_msense``.

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


class DeviceConfig:
    def __init__(self):
        cfg = self._load()
        self._active = cfg["enabled_devices"]
        self.msense_devices = cfg["msense_devices"]
        self.ip_lidar = cfg["ip_qb2_lidar"]
        self.ip_pupil_labs = cfg["ip_pupil_labs"]
        # label -> {"name", "address"} for the most recent Configuration-tab scan
        self._scan_cache = {}

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        if os.path.exists(_DEVICE_CONFIG_FILE):
            try:
                with open(_DEVICE_CONFIG_FILE, 'r') as f:
                    raw = json.load(f)
                cfg = dict(_DEFAULTS)
                cfg["enabled_devices"] = [d for d in raw.get("enabled_devices", cfg["enabled_devices"]) if d in DEVICE_CATALOG]
                cfg["msense_devices"] = _normalize_msense(raw.get("msense_devices", cfg["msense_devices"]))
                cfg["ip_qb2_lidar"] = raw.get("ip_qb2_lidar", cfg["ip_qb2_lidar"])
                cfg["ip_pupil_labs"] = raw.get("ip_pupil_labs", cfg["ip_pupil_labs"])
                return cfg
            except Exception:
                pass
        # File missing or corrupt — create with defaults
        self._active = list(_DEFAULTS["enabled_devices"])
        self.msense_devices = list(_DEFAULTS["msense_devices"])
        self.ip_lidar = _DEFAULTS["ip_qb2_lidar"]
        self.ip_pupil_labs = _DEFAULTS["ip_pupil_labs"]
        self._save()
        return dict(_DEFAULTS)

    def _save(self):
        with open(_DEVICE_CONFIG_FILE, 'w') as f:
            json.dump({
                "enabled_devices": self._active,
                "msense_devices": self.msense_devices,
                "ip_qb2_lidar": self.ip_lidar,
                "ip_pupil_labs": self.ip_pupil_labs,
            }, f, indent=2)

    # ── public API ────────────────────────────────────────────────────────────

    def get_active_table(self):
        return {k: DEVICE_CATALOG[k] for k in self._active if k in DEVICE_CATALOG}

    def get_active_msense_devices(self):
        """Name -> UUID/MAC of MSense wristbands that are both listed and enabled."""
        return {
            rec["Name"]: rec["UUID / MAC Address"]
            for rec in self.msense_devices
            if rec.get("Enabled", True) and str(rec.get("Name", "")).strip() and str(rec.get("UUID / MAC Address", "")).strip()
        }

    def get_imu_stream_devices(self):
        """Names of MSense wristbands with the demo IMU-stream characteristic enabled."""
        return {
            rec["Name"]
            for rec in self.msense_devices
            if rec.get("Enabled", True) and rec.get("IMU Stream", False) and str(rec.get("Name", "")).strip()
        }

    def get_msense_display_labels(self):
        """Name -> ``"Name (Nickname)"`` for listed+enabled wristbands (bare Name
        when Nickname is blank).

        Display only: consumed by the Session dashboard status panel and the
        Signal visualizer. The BLE Name stays the identifier used for LSL stream
        names, status/memo keys and gyro-bias lookup.
        """
        out = {}
        for rec in self.msense_devices:
            name = str(rec.get("Name", "")).strip()
            if not (rec.get("Enabled", True) and name and str(rec.get("UUID / MAC Address", "")).strip()):
                continue
            nick = str(rec.get("Nickname", "")).strip()
            out[name] = f"{name} ({nick})" if nick else name
        return out

    # ── UI callbacks ──────────────────────────────────────────────────────────

    @staticmethod
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

    def _apply(self, selected, msense_df, ip_lidar, ip_pupil_labs):
        self._active = list(selected)
        self.msense_devices = self._msense_records_from_df(msense_df)
        self.ip_lidar = ip_lidar
        self.ip_pupil_labs = ip_pupil_labs
        self._save()
        n_enabled = sum(1 for rec in self.msense_devices if rec["Enabled"])
        return f"Saved — {len(self._active)} device type(s) enabled, {n_enabled}/{len(self.msense_devices)} MSense wristband(s) enabled"

    def _export_config(self, selected, msense_df, ip_lidar, ip_pupil_labs):
        config = {
            "enabled_devices": selected,
            "msense_devices": self._msense_records_from_df(msense_df),
            "ip_qb2_lidar": ip_lidar,
            "ip_pupil_labs": ip_pupil_labs,
        }
        path = os.path.join(tempfile.mkdtemp(), "plasma_device_config.json")
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
        return path, f"Config exported with {len(selected)} device(s)"

    def _import_config(self, file):
        if file is None:
            return gr.update(), gr.update(), gr.update(), gr.update(), ""
        try:
            with open(file, 'r') as f:
                raw = json.load(f)
            enabled = [d for d in raw.get("enabled_devices", []) if d in DEVICE_CATALOG]
            unknown = [d for d in raw.get("enabled_devices", []) if d not in DEVICE_CATALOG]
            msense_records = _normalize_msense(raw.get("msense_devices", self.msense_devices))
            ip_lidar = raw.get("ip_qb2_lidar", self.ip_lidar)
            ip_pupil = raw.get("ip_pupil_labs", self.ip_pupil_labs)
            msg = f"Loaded — {len(enabled)} device type(s)"
            if unknown:
                msg += f" (skipped unknown: {', '.join(unknown)})"
            msense_df = pd.DataFrame(msense_records, columns=_MSENSE_COLUMNS)
            return (
                gr.update(value=enabled),
                gr.update(value=msense_df),
                gr.update(value=ip_lidar),
                gr.update(value=ip_pupil),
                msg,
            )
        except Exception as e:
            return gr.update(), gr.update(), gr.update(), gr.update(), f"Import error: {e}"

    # ── scan-to-add (Configuration tab) ──────────────────────────────────────

    def _scan_msense(self):
        """Blocking 5 s BLE scan; populate the discovered-wristbands checkbox."""
        from plasma.ble_scan import scan_msense
        try:
            found = scan_msense()
        except Exception as e:
            self._scan_cache = {}
            return gr.update(choices=[], value=[]), f"Scan failed: {e}"

        self._scan_cache = {}
        labels = []
        for dev in found:
            label = f"{dev['name']} — {dev['address']}"
            self._scan_cache[label] = dev
            labels.append(label)

        msg = (f"Found {len(labels)} MSense wristband(s) — select and Add"
               if labels else "No MSense wristbands found (in range and advertising?)")
        return gr.update(choices=labels, value=labels), msg

    def _add_scanned(self, selected_labels, add_mode, msense_df):
        """Merge the checked scan results into the editable table."""
        picked = [self._scan_cache[l] for l in (selected_labels or []) if l in self._scan_cache]
        if not picked:
            return gr.update(), "Nothing selected to add"

        existing = self._msense_records_from_df(msense_df)
        records, msg = merge_msense_records(
            existing, picked, overwrite=str(add_mode).startswith("Overwrite"))
        df = pd.DataFrame(records, columns=_MSENSE_COLUMNS)
        return gr.update(value=df), f"{msg} — press Apply to save"

    # ── Gradio interface ──────────────────────────────────────────────────────

    def interface(self):
        with gr.Column():
            with gr.Accordion("Device catalog", open=True):
                gr.Markdown("Select which sensors appear in the session dashboard.")
                checkbox_group = gr.CheckboxGroup(
                    choices=list(DEVICE_CATALOG.keys()),
                    value=list(self._active),
                    label="Available sensors",
                )

            with gr.Accordion("MSense wristbands", open=True):
                gr.Markdown(
                    "Name–UUID pairs for BLE wristband discovery and connection. Uncheck Enabled to skip "
                    "connecting to a wristband without removing it from the list. IMU Stream is demo firmware "
                    "not every wristband has — only check it for units known to support it. Nickname is an "
                    "optional label: when set, the Session dashboard status panel and Signal visualizer show "
                    "\"Name (Nickname)\"; leave it blank to show just the device name. The BLE Name stays the "
                    "identifier for LSL streams, saved files and gyro-bias.\n\n"
                    "**Scan** for wristbands in range, tick the ones to add, then Append (keep the current "
                    "list) or Overwrite. Scan before initializing devices on the Session dashboard — the Mac "
                    "has a single BLE radio, and edits here only take effect the next time you Initialize."
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
                    value=pd.DataFrame(self.msense_devices, columns=_MSENSE_COLUMNS),
                    headers=_MSENSE_COLUMNS,
                    datatype=["str", "str", "str", "bool", "bool"],
                    row_count=(len(self.msense_devices), "dynamic"),
                    col_count=(5, "fixed"),
                    interactive=True,
                )

            with gr.Accordion("Network settings", open=True):
                ip_lidar_txt = gr.Text(value=self.ip_lidar, label="QB2 LiDAR IP address")
                ip_pupil_txt = gr.Text(value=self.ip_pupil_labs, label="Pupil Labs IP address")

            with gr.Row():
                btn_apply = gr.Button("Apply", variant="primary")
                btn_export = gr.Button("Export config")

            file_import = gr.File(label="Import config (.json)", file_types=[".json"])
            export_file = gr.File(label="Config file", interactive=False)
            status = gr.Textbox(interactive=False, value="", show_label=False, container=False)

            btn_apply.click(
                self._apply,
                inputs=[checkbox_group, msense_df, ip_lidar_txt, ip_pupil_txt],
                outputs=status,
            )
            btn_export.click(
                self._export_config,
                inputs=[checkbox_group, msense_df, ip_lidar_txt, ip_pupil_txt],
                outputs=[export_file, status],
            )
            file_import.change(
                self._import_config,
                inputs=file_import,
                outputs=[checkbox_group, msense_df, ip_lidar_txt, ip_pupil_txt, status],
            )
            btn_scan.click(self._scan_msense, outputs=[scan_results, status])
            btn_add.click(
                self._add_scanned,
                inputs=[scan_results, add_mode, msense_df],
                outputs=[msense_df, status],
            )


device_config = DeviceConfig()

# Aliases kept for existing device-module imports
plasma_config = device_config
device_table = device_config.get_active_table()
