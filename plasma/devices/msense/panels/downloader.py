"""File downloader tab: pull recorded `.bin` files off a USB-mounted MSense
drive, zip them, optionally auto-extract. (Was `yams/file_extractor.py`.)"""
import os
import re
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from glob import glob

import gradio as gr
import psutil

from ..extract.pipeline import extract_zip, get_CDCT_init
from .extractor import ExtractionOptionsPanel

_MAC_RE = re.compile(r'(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}')


def get_flash_drives():
    """(dropdown, checkbox_group) of currently mounted removable/USB volumes."""
    drives = [p.device for p in psutil.disk_partitions()
              if "removable" in p.opts.lower() or "usb" in p.device.lower()]
    return (gr.Dropdown(choices=drives, value=drives[0] if drives else None, allow_custom_value=True),
            gr.CheckboxGroup(choices=drives, value=drives))


def _mac_to_name():
    """{MAC-or-UUID (upper) -> configured Name} from the msense config blob."""
    try:
        from plasma.config import device_config
        blob = device_config.get_plugin_config("msense")
    except Exception:
        return {}
    out = {}
    for rec in (blob.get("devices", []) if isinstance(blob, dict) else []):
        addr = str(rec.get("UUID / MAC Address", "")).strip().upper()
        name = str(rec.get("Name", "")).strip()
        if addr and name:
            out[addr] = name
    return out


def _copy_drive(dev_name, file_list, dst_dir):
    dest = os.path.join(dst_dir, dev_name)
    os.makedirs(dest, exist_ok=True)
    for src_path in file_list:
        shutil.copy(src_path, os.path.join(dest, os.path.basename(src_path)))
    return len(file_list)


def build_downloader(ip=None):
    state_files = gr.State({})   # {drive_path: [file, ...]}

    with gr.Column():
        gr.Markdown("Connect the MSense device over USB — it mounts as a drive. Refresh, "
                    "browse the sessions on it, tick the ones you want, and download.")
        with gr.Row():
            drive_grp = gr.CheckboxGroup(label="📁 MSense drive(s)")
            drive_custom = gr.Dropdown(label="📁 Custom path", allow_custom_value=True)
            btn_refresh = gr.Button("🔄 Refresh / Start over")

        btn_browse = gr.Button("Browse sessions")
        with gr.Row():
            enc_table = gr.CheckboxGroup(label="Available sessions", scale=3)
            auto_extract = gr.Checkbox(True, label="Extract data after download")

        opts = ExtractionOptionsPanel()
        opts.gate_on(auto_extract)

        btn_download = gr.Button("Get selected sessions 📂", variant="primary")
        status = gr.Markdown()
        download_btn = gr.DownloadButton("No data to download", interactive=False)

        def _refresh():
            dd, cg = get_flash_drives()
            return dd, cg, {}, gr.CheckboxGroup(choices=[], value=[])

        def _browse(custom, drives, files_state):
            paths = list(drives or [])
            if custom and custom not in paths:
                paths.append(custom)
            files_state = {}
            options = []
            seen = set()
            for p in paths:
                fl = sorted(glob(os.path.join(p, "*.bin"))) + glob(os.path.join(p, "*.txt"))
                files_state[p] = fl
                for f in (os.path.basename(x) for x in fl):
                    if 'ac' not in f and 'ecg' not in f:
                        continue
                    m = re.match(r'(\d*)(?:ac|ecg)', f)
                    if not m:
                        continue
                    enc = m.group(1) or ''
                    t0, _ = get_CDCT_init(f)
                    ts = datetime.fromtimestamp(t0).strftime('%m/%d/%y') if t0 else '?'
                    if enc and enc.isdigit() and int(enc) > 32000:
                        alias = f"sub-{enc[:-2]}, ses-{enc[-2:]} ({ts})"
                    elif enc:
                        alias = f"{enc} ({ts})"
                    else:
                        alias = f"(no id) ({ts})"
                    if enc not in seen:
                        seen.add(enc)
                        options.append((alias, enc))
            return files_state, gr.CheckboxGroup(choices=options, value=[])

        def _download(enc_list, files_state, auto, options=None, progress=gr.Progress()):
            if not files_state:
                return "⛔ Browse a drive first", gr.DownloadButton(interactive=False)
            name_map = _mac_to_name()
            per_drive = {}
            for i, (drive, files) in enumerate(files_state.items()):
                matched, tag = [], f"dev{i}-{time.strftime('%y%m%d%H%M')}"
                for f in files:
                    base = os.path.basename(f)
                    if base.endswith("uuid.txt"):
                        matched.append(f)
                        try:
                            hits = _MAC_RE.findall(open(f).read())
                        except Exception:
                            hits = []
                        if hits:
                            tag = name_map.get(hits[0].upper(), hits[0]).replace(':', '-')
                        continue
                    if base.endswith('.bin') and (not enc_list or any(base.startswith(e) for e in enc_list)):
                        matched.append(f)
                per_drive[tag] = matched

            total = sum(len(v) for v in per_drive.values())
            if total == 0:
                return "Nothing matched the selection", gr.DownloadButton(interactive=False)

            with tempfile.TemporaryDirectory() as dst:
                copied, failed = 0, []
                with ThreadPoolExecutor(max_workers=min(len(per_drive), 4)) as ex:
                    futs = {ex.submit(_copy_drive, n, fl, dst): n for n, fl in per_drive.items()}
                    for fut in as_completed(futs):
                        try:
                            copied += fut.result()
                            progress(copied / total, desc=f"Copied {copied}/{total}")
                        except Exception as e:
                            failed.append(futs[fut])
                            gr.Warning(f"Drive {futs[fut]} failed: {e}")

                zip_path = os.path.join(tempfile.gettempdir(),
                                        f"{time.strftime('%y%m%d%H%M')}_msense.zip")
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for root, _d, files in os.walk(dst):
                        for f in files:
                            fp = os.path.join(root, f)
                            zf.write(fp, os.path.relpath(fp, dst))

            msg = f"Copied {copied} file(s) from {len(per_drive) - len(failed)}/{len(per_drive)} drive(s)."
            if failed:
                msg += f" Failed: {', '.join(failed)}."
            if auto:
                progress(0.95, desc="Extracting…")
                out = extract_zip(zip_path, out_dir=tempfile.gettempdir(), options=options)
                return msg + " Extracted.", gr.DownloadButton("🎉 Download extracted data",
                                                              value=out, interactive=True)
            return msg, gr.DownloadButton("🎉 Download data", value=zip_path, interactive=True)

        btn_refresh.click(_refresh, outputs=[drive_custom, drive_grp, state_files, enc_table])
        btn_browse.click(_browse, inputs=[drive_custom, drive_grp, state_files],
                         outputs=[state_files, enc_table])
        btn_download.click(opts.bind(_download),
                           inputs=[enc_table, state_files, auto_extract] + opts.inputs,
                           outputs=[status, download_btn])
