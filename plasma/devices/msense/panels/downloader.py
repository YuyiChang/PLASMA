"""File downloader tab: pull recorded `.bin` files off a USB-mounted MSense
drive, zip them, optionally auto-extract. (Was `yams/file_extractor.py`.)

The transfer runs as a Gradio generator so the UI streams live feedback: an
overall progress bar, a per-device status table (one row per drive, plus a
synthetic row for the zip/extract stage) and an append-only activity log. The
same events go to ``logging`` so they also land in the app console.
"""
import logging
import os
import queue
import re
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from glob import glob

import gradio as gr
import psutil

from ..extract.options import PANEL_FIELDS, ExtractionOptions
from ..extract.pipeline import extract_folder, get_CDCT_init, get_device_name
from .extractor import ExtractionOptionsPanel

logger = logging.getLogger(__name__)

_MAC_RE = re.compile(r'(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}')

_DF_HEADERS = ["Device", "Phase", "Files", "Size", "Elapsed"]
_PHASE_ICON = {
    "queued": "•", "copying": "⏳", "done": "✅",
    "zipping": "📦", "zipped": "📦", "extracting": "⚙️", "failed": "❌",
}
_COPY_CHUNK = 4 * 1024 * 1024


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


def _fmt_size(n):
    if not n:
        return "—"
    if n < 1e9:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e9:.2f} GB"


def _fmt_elapsed(secs):
    secs = max(int(secs), 0)
    return f"{secs // 60}:{secs % 60:02d}"


def _bar(frac, width=24):
    filled = int(round(min(max(frac, 0.0), 1.0) * width))
    return "▓" * filled + "░" * (width - filled)


def _copy_file(src, dst, on_bytes, chunk=_COPY_CHUNK):
    """shutil.copy, but streams `on_bytes(delta)` as it goes so a single large
    `.bin` still moves the progress bar."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            on_bytes(len(buf))
    try:
        shutil.copymode(src, dst)
    except OSError:
        pass


def _copy_drive(dev_name, file_list, dst_dir, report):
    """Copy one drive's files into `dst_dir/<dev_name>/`, calling
    `report(dev, phase, files_done, files_total, bytes_done, filename)` after
    every chunk and file. Runs in a worker thread."""
    dest = os.path.join(dst_dir, dev_name)
    os.makedirs(dest, exist_ok=True)
    total = len(file_list)
    state = {"bytes": 0}
    for i, src in enumerate(file_list, 1):
        base = os.path.basename(src)

        def _tick(delta, _i=i, _base=base):
            state["bytes"] += delta
            report(dev_name, "copying", _i - 1, total, state["bytes"], _base)

        _copy_file(src, os.path.join(dest, base), _tick)
        report(dev_name, "copying", i, total, state["bytes"], base)
    report(dev_name, "done", total, total, state["bytes"], None)
    return total


def _resolve_per_drive(enc_list, files_state):
    """{device tag -> [matched file paths]} for the current session selection.

    The tag — which becomes the per-device data folder — is, in order of
    preference: the ``Name:`` field written in ``uuid.txt`` by v5+ firmware
    (e.g. ``MSense4ECG-EX4BT``); the configured name for that BLE address from
    the legacy MSense-config lookup table (deprecated, kept for old firmware);
    the raw BLE address; a synthetic ``devN-<timestamp>``.
    """
    name_map = _mac_to_name()
    per_drive = {}
    for i, (_drive, files) in enumerate(files_state.items()):
        matched = []
        name_tag = mac_tag = None
        for f in files:
            base = os.path.basename(f)
            if base.endswith("uuid.txt"):
                matched.append(f)
                name_tag = get_device_name(f)
                try:
                    hits = _MAC_RE.findall(open(f).read())
                except Exception:
                    hits = []
                if hits:
                    mac = hits[0].upper()
                    mac_tag = name_map.get(mac, mac).replace(':', '-')
                continue
            if base.endswith('.bin') and (not enc_list or any(base.startswith(e) for e in enc_list)):
                matched.append(f)
        tag = name_tag or mac_tag or f"dev{i}-{time.strftime('%y%m%d%H%M')}"
        while tag in per_drive:      # two drives resolving to one name
            tag += "·"
        per_drive[tag] = matched
    return per_drive


def _download(enc_list, files_state, auto, progress=gr.Progress(), *opt_values):
    """Gradio generator behind "Get selected sessions". Copies the selected
    sessions off every browsed drive in parallel, zips, optionally extracts,
    streaming (progress_md, status_df, log_box, download_btn) on every step.

    `progress` sits before *opt_values so Gradio's special-args scan (which
    stops at the first VAR_POSITIONAL) still detects it and injects the tracker
    at this position.
    """
    options = ExtractionOptions(**dict(zip(PANEL_FIELDS, opt_values)))
    log = []

    def note(msg):
        log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        logger.info("downloader: %s", msg)

    def out(head, *, working=True, value=None, label="Working…"):
        table = [[t,
                  f'{_PHASE_ICON.get(r["phase"], "")} {r["phase"]}'.strip(),
                  f'{r["done"]}/{r["total"]}' if r["total"] else "—",
                  _fmt_size(r["bytes"]),
                  _fmt_elapsed(time.time() - r["t0"]) if r["t0"] else "—"]
                 for t, r in rows.items()]
        return (head,
                gr.Dataframe(value=table or None, visible=bool(table)),
                gr.Textbox(value="\n".join(log[-300:]), visible=True),
                gr.DownloadButton(label, value=value,
                                  interactive=not working and value is not None))

    rows = {}
    if not files_state:
        yield ("⛔ Browse a drive first", gr.Dataframe(visible=False),
               gr.Textbox(visible=False), gr.DownloadButton(interactive=False))
        return

    per_drive = _resolve_per_drive(enc_list, files_state)
    total = sum(len(v) for v in per_drive.values())
    total_bytes = sum(os.path.getsize(f) for v in per_drive.values()
                      for f in v if os.path.exists(f))
    if total == 0:
        yield ("Nothing matched the selection", gr.Dataframe(visible=False),
               gr.Textbox(visible=False), gr.DownloadButton(interactive=False))
        return

    t0 = time.time()
    rows = {t: {"phase": "queued", "done": 0, "total": len(fl), "bytes": 0, "t0": None}
            for t, fl in per_drive.items()}
    arch = "archive"
    while arch in rows:
        arch = "·" + arch

    def head(done_bytes, prefix=""):
        frac = done_bytes / total_bytes if total_bytes else 0.0
        line = (f"{_bar(frac)}  {frac * 100:3.0f}%  ·  "
                f"{_fmt_size(done_bytes)} / {_fmt_size(total_bytes)}  ·  "
                f"{_fmt_elapsed(time.time() - t0)}")
        return f"{prefix}\n\n{line}" if prefix else line

    note(f"Starting: {total} file(s), {_fmt_size(total_bytes)} from {len(per_drive)} device(s)")
    progress(0.0, desc="Starting…")
    yield out(head(0))

    evq = queue.Queue()

    def report(*a):
        evq.put(dict(zip(("dev", "phase", "done", "total", "bytes", "file"), a)))

    bytes_by_dev = {t: 0 for t in per_drive}
    failed = []

    with tempfile.TemporaryDirectory() as dst:
        with ThreadPoolExecutor(max_workers=min(len(per_drive), 4)) as ex:
            futs = {ex.submit(_copy_drive, n, fl, dst, report): n
                    for n, fl in per_drive.items()}
            pending = set(futs)
            while pending or not evq.empty():
                try:
                    ev = evq.get(timeout=0.2)
                except queue.Empty:
                    ev = None
                if ev is not None:
                    r = rows[ev["dev"]]
                    if r["t0"] is None:
                        r["t0"] = time.time()
                    r.update(phase=ev["phase"], done=ev["done"], bytes=ev["bytes"])
                    bytes_by_dev[ev["dev"]] = ev["bytes"]
                    tot = sum(bytes_by_dev.values())
                    progress(tot / total_bytes if total_bytes else 0.0,
                             desc=f"{_fmt_size(tot)} / {_fmt_size(total_bytes)}")
                    if ev["file"]:
                        note(f'{ev["dev"]}: {ev["file"]} ({ev["done"]}/{ev["total"]})')
                    yield out(head(tot))
                for f in {f for f in pending if f.done()}:
                    n = futs[f]
                    try:
                        f.result()
                        rows[n]["phase"] = "done"
                        note(f'{n}: done — {rows[n]["done"]}/{rows[n]["total"]} '
                             f'files, {_fmt_size(rows[n]["bytes"])}')
                    except Exception as e:
                        rows[n]["phase"] = "failed"
                        failed.append(n)
                        note(f"{n}: FAILED — {e}")
                        gr.Warning(f"Drive {n} failed: {e}")
                    pending.discard(f)

        copied = sum(r["done"] for t, r in rows.items() if t in per_drive)
        done_bytes = sum(bytes_by_dev.values())

        # Extract straight out of `dst` — the raw .bin files copied above —
        # instead of zipping them and immediately unzipping that same zip for
        # extraction. That round trip bought nothing (the data never left
        # local disk) and raw sensor binaries don't compress well anyway; the
        # extracted zip below is the only zip built on this path. A raw zip
        # is still built, but only if extraction wasn't requested or produced
        # nothing (see extract_folder's docstring).
        result_path = label = extra = None
        extracted, crashed = None, False
        if auto:
            rows[arch] = {"phase": "extracting", "done": 0, "total": 0, "bytes": 0, "t0": time.time()}
            note("extracting…")
            progress(0.95, desc="Extracting…")
            yield out(head(done_bytes, prefix="Copy complete — extracting…"))
            try:
                extracted = extract_folder(dst, out_dir=tempfile.gettempdir(), options=options)
            except Exception as e:
                crashed = True
                logger.exception("downloader: extraction failed")
                gr.Warning(f"Extraction failed ({e}) — falling back to a raw zip.")
                note(f"extraction failed: {e} — falling back to a raw zip")
            if extracted:
                rows[arch].update(phase="done", bytes=os.path.getsize(extracted))
                result_path, label, extra = extracted, "🎉 Download extracted data", " · extracted"
            elif not crashed:
                note("extraction produced no output — falling back to a raw zip")

        if result_path is None:
            rows[arch] = {"phase": "zipping", "done": 0, "total": 0, "bytes": 0, "t0": time.time()}
            note("zipping…")
            progress(0.97, desc="Zipping…")
            yield out(head(done_bytes, prefix="Copy complete — building archive…"))

            zip_path = os.path.join(tempfile.gettempdir(),
                                    f"{time.strftime('%y%m%d%H%M')}_msense.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(dst):
                    for f in files:
                        fp = os.path.join(root, f)
                        zf.write(fp, os.path.relpath(fp, dst))
            rows[arch].update(phase="failed" if crashed else "done",
                              bytes=os.path.getsize(zip_path))
            result_path, label, extra = zip_path, "🎉 Download data", ""

    summary = (f"✅ Copied {copied}/{total} file(s) from "
               f"{len(per_drive) - len(failed)}/{len(per_drive)} device(s) "
               f"in {_fmt_elapsed(time.time() - t0)}{extra}.")
    if failed:
        summary += f"  Failed: {', '.join(failed)}."
    note(summary)
    progress(1.0, desc="Done")
    gr.Info(summary)
    yield out(head(total_bytes, prefix=summary), working=False, value=result_path, label=label)


def build_downloader(ip=None):
    state_files = gr.State({})   # {drive_path: [file, ...]}

    with gr.Column():
        gr.Markdown("Connect the MSense device over USB — it mounts as a drive. Refresh, "
                    "browse the sessions on it, tick the ones you want, and download.")
        with gr.Row():
            drive_grp = gr.CheckboxGroup(label="📁 MSense drive(s)")
            drive_custom = gr.Dropdown(label="📁 Custom path(s)", allow_custom_value=True,
                                       info="one or more paths, separated by ;")
            btn_refresh = gr.Button("🔄 Refresh / Start over")

        btn_browse = gr.Button("Browse sessions")
        with gr.Row():
            enc_table = gr.CheckboxGroup(label="Available sessions", scale=3)
            auto_extract = gr.Checkbox(True, label="Extract data after download")

        opts = ExtractionOptionsPanel()
        opts.gate_on(auto_extract)

        btn_download = gr.Button("Get selected sessions 📂", variant="primary")

        progress_md = gr.Markdown()
        status_df = gr.Dataframe(headers=_DF_HEADERS, datatype=["str"] * 5,
                                 col_count=(5, "fixed"), interactive=False, wrap=True,
                                 label="Transfer status", visible=False)
        log_box = gr.Textbox(label="Activity log", lines=8, max_lines=8,
                             interactive=False, visible=False, autoscroll=True)
        download_btn = gr.DownloadButton("No data to download", interactive=False)

        def _reset_feedback():
            return (gr.Markdown(), gr.Dataframe(value=None, visible=False),
                    gr.Textbox(value="", visible=False),
                    gr.DownloadButton("No data to download", value=None, interactive=False))

        def _refresh():
            dd, cg = get_flash_drives()
            return (dd, cg, {}, gr.CheckboxGroup(choices=[], value=[]), *_reset_feedback())

        def _browse(custom, drives, files_state):
            paths = list(drives or [])
            for p in (custom or "").split(";"):
                p = p.strip()
                if p and p not in paths:
                    paths.append(p)
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

        btn_refresh.click(_refresh,
                          outputs=[drive_custom, drive_grp, state_files, enc_table,
                                   progress_md, status_df, log_box, download_btn])
        btn_browse.click(_browse, inputs=[drive_custom, drive_grp, state_files],
                         outputs=[state_files, enc_table])
        btn_download.click(_download,
                           inputs=[enc_table, state_files, auto_extract] + opts.inputs,
                           outputs=[progress_md, status_df, log_box, download_btn])
