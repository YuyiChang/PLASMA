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

_DEFAULTS = {
    "enabled_devices": list(DEVICE_CATALOG.keys()),
    "msense_devices": [],
    "ip_qb2_lidar": "",
    "ip_pupil_labs": "",
}

_MSENSE_COLUMNS = ["Name", "UUID / MAC Address", "Enabled"]


def _normalize_msense(raw):
    """Accepts either the legacy {name: uuid} mapping or the current
    list of {Name, UUID / MAC Address, Enabled} records, and returns
    the latter — legacy entries default to enabled."""
    if isinstance(raw, dict):
        return [{"Name": k, "UUID / MAC Address": v, "Enabled": True} for k, v in raw.items()]
    if isinstance(raw, list):
        return [
            {
                "Name": rec.get("Name", ""),
                "UUID / MAC Address": rec.get("UUID / MAC Address", ""),
                "Enabled": bool(rec.get("Enabled", True)),
            }
            for rec in raw
        ]
    return []


class DeviceConfig:
    def __init__(self):
        cfg = self._load()
        self._active = cfg["enabled_devices"]
        self.msense_devices = cfg["msense_devices"]
        self.ip_lidar = cfg["ip_qb2_lidar"]
        self.ip_pupil_labs = cfg["ip_pupil_labs"]

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

    # ── UI callbacks ──────────────────────────────────────────────────────────

    @staticmethod
    def _msense_records_from_df(msense_df):
        return [
            {
                "Name": row["Name"],
                "UUID / MAC Address": row["UUID / MAC Address"],
                "Enabled": bool(row["Enabled"]) if pd.notna(row.get("Enabled", True)) else True,
            }
            for _, row in msense_df.iterrows()
            if str(row["Name"]).strip() and str(row["UUID / MAC Address"]).strip()
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
                gr.Markdown("Name–UUID pairs for BLE wristband discovery and connection. Uncheck Enabled to skip connecting to a wristband without removing it from the list.")
                msense_df = gr.Dataframe(
                    value=pd.DataFrame(self.msense_devices, columns=_MSENSE_COLUMNS),
                    headers=_MSENSE_COLUMNS,
                    datatype=["str", "str", "bool"],
                    row_count=(len(self.msense_devices), "dynamic"),
                    col_count=(3, "fixed"),
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


device_config = DeviceConfig()

# Aliases kept for existing device-module imports
plasma_config = device_config
device_table = device_config.get_active_table()
