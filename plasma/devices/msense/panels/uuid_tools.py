"""UUID extractor + device manager: read `uuid.txt` off a mounted MSense drive,
pair it with a serial, and merge into the MSense device list.
(Was `yams/uuid_extractor.py`.)"""
import gradio as gr
import pandas as pd

from ..ble_scan import read_uuid_from_drive
from ..config import _MSENSE_COLUMNS, _normalize_msense, merge_msense_records
from .downloader import get_flash_drives


def _blob_devices():
    from plasma.config import device_config
    blob = device_config.get_plugin_config("msense")
    return blob.get("devices", []) if isinstance(blob, dict) else []


def _save_devices(records):
    from plasma.config import device_config
    device_config.update_plugin_config("msense", {"devices": _normalize_msense(records)})


def build_uuid_extractor(ip=None):
    with gr.Column():
        gr.Markdown("Connect an MSense device over USB, read the MAC from its `uuid.txt`, "
                    "type its serial, and add it to the wristband list.")
        with gr.Row():
            drive = gr.Dropdown(label="📁 MSense path", allow_custom_value=True)
            btn_refresh = gr.Button("🔄 Refresh")
        btn_read = gr.Button("🔧 Read UUID")
        uuid_field = gr.Text(label="MAC / UUID", interactive=False)
        serial_field = gr.Text(label="Serial / name")
        btn_add = gr.Button("📝 Add to wristband list", variant="primary")
        table = gr.Dataframe(value=pd.DataFrame(_blob_devices(), columns=_MSENSE_COLUMNS),
                             headers=_MSENSE_COLUMNS, interactive=False, label="Configured wristbands")
        status = gr.Markdown()

        def _refresh():
            dd, _ = get_flash_drives()
            return dd

        def _add(serial, uuid):
            if not serial or not uuid or ":" not in uuid and "-" not in uuid:
                return gr.update(), "⛔ read a UUID and enter a serial first"
            recs, msg = merge_msense_records(_blob_devices(), [{"name": serial, "address": uuid}],
                                             overwrite=False)
            _save_devices(recs)
            return pd.DataFrame(recs, columns=_MSENSE_COLUMNS), msg

        btn_refresh.click(_refresh, outputs=drive)
        btn_read.click(read_uuid_from_drive, inputs=drive, outputs=uuid_field)
        btn_add.click(_add, inputs=[serial_field, uuid_field], outputs=[table, status])


def build_device_manager(ip=None):
    with gr.Column():
        gr.Markdown("Import a legacy `device_info.json` (`{serial: MAC/UUID}`) into the "
                    "wristband list.")
        f = gr.File(file_types=[".json"], label="device_info.json")
        overwrite = gr.Checkbox(False, label="Overwrite the current list (else append new)")
        btn = gr.Button("Import", variant="primary")
        table = gr.Dataframe(value=pd.DataFrame(_blob_devices(), columns=_MSENSE_COLUMNS),
                             headers=_MSENSE_COLUMNS, interactive=False)
        status = gr.Markdown()

        def _import(path, overwrite):
            if not path:
                return gr.update(), "⛔ choose a device_info.json"
            from ..config import merge_device_info_into_blob
            new_blob, msg = merge_device_info_into_blob({"devices": _blob_devices()}, path,
                                                        overwrite=bool(overwrite))
            _save_devices(new_blob["devices"])
            return pd.DataFrame(new_blob["devices"], columns=_MSENSE_COLUMNS), msg

        btn.click(_import, inputs=[f, overwrite], outputs=[table, status])
