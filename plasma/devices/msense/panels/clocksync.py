"""Clock Sync tab: counter-align uploaded MSense CSVs to a YAMS `.txt`
reference and export timestamped CSVs. Wraps the pure `..extract.clocksync`."""
import io
import os
import tempfile
import time
import zipfile
from contextlib import redirect_stdout
from glob import glob

import gradio as gr

from ..extract import clocksync as _cs


def run_sync(csv_files, txt_file, progress=gr.Progress()):
    if txt_file is None:
        return "Error: no YAMS .txt file uploaded.", [], gr.DownloadButton(interactive=False)
    if not csv_files:
        return "Error: no CSV files uploaded.", [], gr.DownloadButton(interactive=False)

    progress(0, desc="Preparing…")
    with tempfile.TemporaryDirectory() as out_dir:
        txt_path = txt_file
        csv_paths = list(csv_files)
        ac_csvs = [p for p in csv_paths if os.path.basename(p).lower().endswith('ac.csv')]
        other = [p for p in csv_paths if p not in ac_csvs]

        status, processed_as_sibling = [], set()
        total = len(ac_csvs) + len(other)
        done = 0

        for ac_path in ac_csvs:
            tag = os.path.splitext(os.path.basename(ac_path))[0]
            progress(done / total, desc=f"Syncing {tag}…")
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    _, f_interp, _ = _cs.sync_csv_to_yams(ac_path, txt_path, out_dir)
                status.append(f"[OK] {tag}: {buf.getvalue().strip()[-200:]}")
                prefix = os.path.basename(ac_path)[:-len('ac.csv')]
                sibs = [p for p in other
                        if os.path.basename(p).lower().endswith('ppg.csv') and prefix in os.path.basename(p)]
                if sibs:
                    with redirect_stdout(io.StringIO()):
                        _cs.apply_interp_to_csv(sibs[0], f_interp, out_dir)
                    status.append(f"[OK] {os.path.basename(sibs[0])}: propagated from {tag}")
                    processed_as_sibling.add(sibs[0])
            except Exception as e:
                status.append(f"[ERROR] {tag}: {e}")
            done += 1

        for csv_path in other:
            if csv_path in processed_as_sibling:
                continue
            tag = os.path.splitext(os.path.basename(csv_path))[0]
            progress(done / total, desc=f"Syncing {tag}…")
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    _cs.sync_csv_to_yams(csv_path, txt_path, out_dir)
                status.append(f"[OK] {tag}: {buf.getvalue().strip()[-200:]}")
            except Exception as e:
                status.append(f"[ERROR] {tag}: {e}")
            done += 1

        png_paths = sorted(glob(os.path.join(out_dir, '*.png')))
        try:
            from PIL import Image
            images = [Image.open(p).copy() for p in png_paths]
        except Exception:
            images = png_paths

        synced = sorted(glob(os.path.join(out_dir, '*_synced.csv')))
        if not synced:
            return "\n".join(status), images, gr.DownloadButton("No output", interactive=False)

        progress(0.95, desc="Zipping…")
        zip_path = os.path.join(tempfile.gettempdir(), f"{time.strftime('%y%m%d%H%M')}_synced.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in synced + png_paths:
                zf.write(f, os.path.basename(f))
        return "\n".join(status), images, gr.DownloadButton("🎉 Download synced ZIP",
                                                            value=zip_path, interactive=True)


def build_clocksync(ip=None):
    with gr.Column():
        gr.Markdown("## ⏱️ Clock Sync\nMatch MSense CSV files to a YAMS `.txt` reference by "
                    "Counter, fit a CDCT → Unix-time interpolant, and export timestamped CSVs.")
        with gr.Row():
            txt_input = gr.File(label="YAMS .txt file", file_count="single", file_types=[".txt"])
            csv_input = gr.File(label="MSense CSV file(s)", file_count="multiple", file_types=[".csv"])
        run_btn = gr.Button("Run Sync", variant="primary")

        status_box = gr.Textbox(label="Status / stats", lines=8, interactive=False)
        gallery = gr.Gallery(label="QC plots", columns=2, height=400)
        download_btn = gr.DownloadButton(label="Download synced ZIP", interactive=False)

        run_btn.click(run_sync, inputs=[csv_input, txt_input],
                      outputs=[status_box, gallery, download_btn])
