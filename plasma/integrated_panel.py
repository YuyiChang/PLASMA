import gradio as gr
import struct
import os
from plasma.lsl_session import encode_participant
import importlib
import logging, datetime
from logging import Logger
from plasma.config import device_config, __data_dir__, __version__
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from plasma.quaternion import quat_to_axes
from plasma.signal_quality import filter_ecg, filter_ppg
from plasma.devices.msense import MotionSenseHRV

VISUALIZER_PLOT_ELEM_ID = "plasma-visualizer-plot"

# Related scalar channels sharing one subplot instead of one row each.
# Channels not listed here fall back to being their own singleton group.
CHANNEL_GROUPS = {
    "Accel (g)": ["AccX", "AccY", "AccZ"],
    "Quaternion Δ (per-frame)": ["Q0", "Q1", "Q2", "Q3"],
    "Orientation (composed)": ["OrientX", "OrientY", "OrientZ", "OrientW"],
}
_CHANNEL_TO_GROUP = {ch: label for label, chs in CHANNEL_GROUPS.items() for ch in chs}

# Unit box (half-extents in local/body frame) drawn inside the orientation
# indicator so rotation about any axis is visually obvious, not just the
# 3 axis lines. Flattened/elongated like a wristband rather than a cube.
_BOX_HALF_EXTENTS = (0.4, 0.22, 0.1)
_BOX_X_FLAGS = [0, 0, 1, 1, 0, 0, 1, 1]
_BOX_Y_FLAGS = [0, 1, 1, 0, 0, 1, 1, 0]
_BOX_Z_FLAGS = [0, 0, 0, 0, 1, 1, 1, 1]
_BOX_I = [7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2]
_BOX_J = [3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 3]
_BOX_K = [0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 7, 6]
_BOX_LOCAL_VERTS = [
    (
        _BOX_HALF_EXTENTS[0] if xf else -_BOX_HALF_EXTENTS[0],
        _BOX_HALF_EXTENTS[1] if yf else -_BOX_HALF_EXTENTS[1],
        _BOX_HALF_EXTENTS[2] if zf else -_BOX_HALF_EXTENTS[2],
    )
    for xf, yf, zf in zip(_BOX_X_FLAGS, _BOX_Y_FLAGS, _BOX_Z_FLAGS)
]


def _group_for_channel(ch):
    return _CHANNEL_TO_GROUP.get(ch, ch)


def _rotate_point(p, axes):
    """Rotate local-frame point p=(px,py,pz) using the 3 rotated basis
    vectors returned by quat_to_axes (its columns are the rotation matrix)."""
    px, py, pz = p
    ax, ay, az = axes
    return (
        px * ax[0] + py * ay[0] + pz * az[0],
        px * ax[1] + py * ay[1] + pz * az[1],
        px * ax[2] + py * ay[2] + pz * az[2],
    )

class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_config.get_active_table().keys())
        self.log_root = __data_dir__

        self.available_devices = []

        self.sts = "Welcome"

        self.logger = get_logger(__data_dir__)
        self.logger.info(f"Begin PLASMA v{__version__} session log")

    def visualizer_interface(self):
        with gr.Column():
            gr.Markdown(
                "Live device signal viewer. Initialize device(s) on the **Session dashboard** tab first, "
                "then refresh sources here. X-axis uses each device's own sample counter, not computer clock time."
            )
            with gr.Row():
                source_select = gr.Dropdown(choices=[], multiselect=True, label="Data source(s)")
                channel_select = gr.CheckboxGroup(choices=[], label="Channel(s)")

            with gr.Row():
                btn_refresh_sources = gr.Button("🔄 Refresh sources")
                btn_reset_orientation = gr.Button("↺ Reset orientation")
                calib_duration = gr.Number(value=3, minimum=1, precision=0, label="Calibration duration (s)", scale=0)
                btn_calibrate_gyro = gr.Button("🎯 Calibrate gyro bias")
                btn_fullscreen = gr.Button("⛶ Fullscreen")

            with gr.Column(elem_id=VISUALIZER_PLOT_ELEM_ID):
                plot = gr.Plot(show_label=False)
                orientation_plot = gr.Plot(show_label=False, visible=False)
                gr.Markdown(
                    "*Orientation is a gyro-only dead-reckoning estimate composed from per-frame deltas since the "
                    "last Start/Reset — there's no accelerometer/magnetometer correction, so it will drift over "
                    "time. Use Reset orientation to re-zero it. Calibrate gyro bias with the wristband(s) held "
                    "still — the bias is saved to disk and reapplied automatically on future launches.*"
                )

            timer = gr.Timer(value=0.2, active=True)

            btn_refresh_sources.click(
                self.refresh_visual_sources, outputs=source_select
            ).then(
                self.refresh_channels, inputs=source_select, outputs=channel_select
            )
            source_select.change(self.refresh_channels, inputs=source_select, outputs=channel_select)
            btn_reset_orientation.click(self.reset_orientation_all)
            btn_calibrate_gyro.click(self.calibrate_gyro_all, inputs=calib_duration)
            timer.tick(fn=self.update_plot, inputs=[source_select, channel_select], outputs=plot)
            timer.tick(fn=self.update_orientation, inputs=[source_select, channel_select], outputs=orientation_plot)
            btn_fullscreen.click(
                None, None, None,
                js=f"""() => {{
                    const el = document.getElementById('{VISUALIZER_PLOT_ELEM_ID}');
                    if (el && el.requestFullscreen) {{ el.requestFullscreen(); }}
                }}""",
            )

    def signal_quality_interface(self):
        with gr.Column():
            gr.Markdown(
                "On-demand raw ECG/PPG snapshot for every connected MSense wristband so you can "
                "eyeball electrode / optical contact before starting a full session. Initialize the "
                "MSense device on the **Session dashboard** tab first, then Refresh here. Wristbands "
                "are snapshotted **one at a time** (the Mac has a single BLE radio); each capture is "
                "saved to disk (session log dir if a recording is running, else "
                "`data/sqc_snapshots/`).\n\n"
                "The device always streams a fixed 96 KiB (a pre-buffered *history* window then a "
                "*forward* window it acquires live — ~16 s total ECG / ~24 s PPG). **Quick mode** "
                "(default) sends CANCEL once *N* seconds have arrived and keeps the partial; with "
                "*N* ≤ the history window (~5 s ECG / ~8 s PPG) the whole live-acquisition wait is "
                "skipped. Clear the field (and leave *History only* off) for a full capture.\n"
            )
            with gr.Row():
                btn_refresh_sqc = gr.Button("🔄 Refresh")
                btn_request_sqc = gr.Button("📡 Snapshot all wristbands (sequential)", variant="primary")
                btn_cancel_sqc = gr.Button("✖ Cancel all")
            with gr.Row():
                sqc_max_s = gr.Number(
                    value=5, precision=1, minimum=0,
                    label="Quick mode — stop after N s (clear for full capture)")
                sqc_hist_only = gr.Checkbox(
                    value=False, label="History only (stop at the history→forward boundary)")

            sqc_status = gr.Markdown()
            sqc_plot = gr.Plot(show_label=False)

            # coarse refresh — the BLE transfer is slow, so ~1 Hz is plenty and
            # keeps re-decoding / re-plotting the growing signal cheap
            sqc_timer = gr.Timer(value=1.0, active=True)

            btn_refresh_sqc.click(self.update_sqc, outputs=[sqc_status, sqc_plot])
            btn_request_sqc.click(self.request_sqc, inputs=[sqc_max_s, sqc_hist_only],
                                  outputs=sqc_status)
            btn_cancel_sqc.click(self.cancel_sqc, outputs=sqc_status)
            sqc_timer.tick(fn=self.update_sqc, outputs=[sqc_status, sqc_plot])

    def _get_msense_device(self):
        for dev in self.available_devices:
            if isinstance(dev, MotionSenseHRV):
                return dev
        return None

    def request_sqc(self, max_seconds=None, history_only=False):
        dev = self._get_msense_device()
        if dev is None:
            return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first"
        try:
            ms = float(max_seconds) if max_seconds not in (None, "", 0) else None
        except (TypeError, ValueError):
            ms = None
        if ms is not None and ms <= 0:
            ms = None
        return dev.request_all_sqc_snapshots(max_seconds=ms, history_only=bool(history_only))

    def cancel_sqc(self):
        dev = self._get_msense_device()
        if dev is None:
            return "⛔ MSense device not initialized"
        return dev.cancel_all_sqc_snapshots()

    def update_sqc(self):
        dev = self._get_msense_device()
        if dev is None:
            return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first", gr.update()

        names = dev.get_sqc_devices()
        if not names:
            return "No MSense wristbands connected.", gr.update()

        def _diag_str(status):
            d = status.get("diag") or {}
            if not d.get("notifs"):
                return ""
            s = (f" · {d['kib_s']} KiB/s ({d['notif_s']}/s × {d['mean_notif_bytes']}B), "
                 f"gap {d['last_gap_s']}s (max {d['max_gap_s']}s), proc≤{d['max_proc_ms']}ms")
            if d.get("mtu") is not None or d.get("rssi") is not None:
                s += f", mtu {d.get('mtu')}, rssi {d.get('rssi')}"
            if d.get("seq_gaps"):
                s += f", ⚠️{d['seq_gaps']} seq gaps"
            if d.get("recoveries"):
                s += f", 🔄{d['recoveries']}× reconnect"
            return s

        lines, results = [], []
        for name in names:
            disp = dev.display_name(name)
            status = dev.get_sqc_status(name)
            st = status["status"]
            if st in ("idle", "unavailable"):
                lines.append(f"- **{disp}** — idle")
                continue
            if st in ("requesting", "receiving"):
                total = status["records_total"] or "?"
                pct = ""
                if isinstance(total, int) and total:
                    pct = f" ({100 * status['records_received'] // total}%)"
                lines.append(
                    f"- **{disp}** — 📡 {st} ({status['phase']}) "
                    f"{status['records_received']}/{total} records{pct}{_diag_str(status)}"
                )
                preview = dev.get_sqc_preview(name)
                if preview is not None:
                    results.append((disp, preview))
                continue
            if st == "finishing":
                lines.append(f"- **{disp}** — 💾 finalizing…{_diag_str(status)}")
                continue
            if st == "rejected":
                lines.append(f"- **{disp}** — 🚫 rejected: {status['error']}")
                continue
            if st == "error":
                lines.append(f"- **{disp}** — ❌ {status['error']}{_diag_str(status)}")
                continue

            result = dev.get_sqc_result(name)
            if result is None:
                lines.append(f"- **{disp}** — ❌ ready but no data")
                continue
            results.append((disp, result))
            prov = result.get("provenance") or {}
            dirty = " ⚠️dirty" if prov.get("git_tree_state") == "dirty" else ""
            quick = ""
            if result.get("quick_seconds"):
                ph = prov.get("phase_at_cancel", "")
                quick = f" · ✂ quick {result['quick_seconds']:g}s{' (' + ph + ')' if ph else ''}"
            lines.append(
                f"- **{disp}** — ✅ {result['device_type']}{quick} · id `{prov.get('device_id', '?')}` · "
                f"fw `{str(prov.get('git_commit', '?'))[:10]}`{dirty} · saved `{status['saved_path']}`"
            )

        fig = self._build_sqc_figure(results) if results else gr.update()
        return "\n".join(lines), fig

    @staticmethod
    def _build_sqc_figure(results):
        # one row per (unit x channel); raw on the primary y-axis, a light
        # bandpass overlay on a secondary y-axis so the pulsatile component
        # stays visible next to the large DC term. While a capture is still
        # streaming in ("partial") we plot the raw trace only — filtering a
        # still-growing signal just adds edge artifacts.
        rows = []
        for name, result in results:
            partial = result.get("partial")            # live preview, still streaming
            quick_s = result.get("quick_seconds")      # finished but cut short on purpose
            for ch_name, y in result["channels"].items():
                title = f"{name} · {ch_name}"
                if partial:
                    title += " — receiving…"
                elif quick_s:
                    title += f" — {quick_s:g}s quick"
                rows.append((title, np.asarray(y, dtype=float), result["fs"],
                             result["device_type"] == "ECG", partial))

        fig = make_subplots(
            rows=len(rows), cols=1, shared_xaxes=False, vertical_spacing=0.06 / max(len(rows), 1),
            subplot_titles=[title for title, *_ in rows],
            specs=[[{"secondary_y": True}] for _ in rows],
        )
        for i, (title, y, fs, is_ecg, partial) in enumerate(rows, start=1):
            t = np.arange(len(y)) / fs
            fig.add_trace(go.Scatter(x=t, y=y, mode="lines", name="raw",
                                     line=dict(color="#1f77b4")), row=i, col=1, secondary_y=False)
            if not partial and len(y) > 64:
                try:
                    filt = filter_ecg(y, fs) if is_ecg else filter_ppg(y, fs)
                    fig.add_trace(go.Scatter(x=t, y=filt, mode="lines", name="filtered",
                                             line=dict(color="#d62728", width=1), opacity=0.7),
                                  row=i, col=1, secondary_y=True)
                except Exception:
                    pass
            fig.update_xaxes(title_text="Time (s)", row=i, col=1)

        fig.update_layout(
            height=200 * max(len(rows), 1),
            margin=dict(l=50, r=20, t=30, b=30),
            showlegend=False,
            uirevision="plasma-sqc",
        )
        return fig

    def get_visual_sources(self):
        """Flat {"device tag [· sub-source]": PlasmaMemo} map of every live
        source that currently has at least one data channel to plot."""
        sources = {}
        for dev in self.available_devices:
            for name, memo in dev.get_sources().items():
                if memo.channels:
                    # label = name if name == dev.tag else f"{dev.tag} · {name}"
                    label = getattr(memo, "label", name)
                    sources[label] = memo
        return sources

    def refresh_visual_sources(self):
        names = list(self.get_visual_sources().keys())
        return gr.Dropdown(choices=names, value=names)

    def refresh_channels(self, selected_sources):
        sources = self.get_visual_sources()
        groups = []
        for name in selected_sources or []:
            memo = sources.get(name)
            if memo is None:
                continue
            for ch in memo.channels:
                g = _group_for_channel(ch)
                if g not in groups:
                    groups.append(g)
        return gr.CheckboxGroup(choices=groups, value=groups)

    def update_plot(self, selected_sources, selected_groups):
        sources = self.get_visual_sources()
        groups = selected_groups or []

        if not groups or not selected_sources:
            fig = go.Figure()
            fig.update_layout(
                title="Select a data source and channel(s) to visualize",
                height=300,
                uirevision="plasma-visualizer",
            )
            return fig

        fig = make_subplots(rows=len(groups), cols=1, shared_xaxes=True, vertical_spacing=0.015)

        for row, group in enumerate(groups, start=1):
            for ch in CHANNEL_GROUPS.get(group, [group]):
                for src_name in selected_sources:
                    memo = sources.get(src_name)
                    if memo is None or ch not in memo.channels:
                        continue
                    x, y = memo.get_series(ch)
                    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=f"{src_name} · {ch}"), row=row, col=1)

            fig.update_yaxes(title_text=group, title_standoff=4, row=row, col=1)
            # only the bottom-most subplot needs an x-axis title/tick labels;
            # the rest just waste vertical space repeating the same axis
            if row == len(groups):
                fig.update_xaxes(title_text="Time since session start (s)", row=row, col=1)
            else:
                fig.update_xaxes(showticklabels=False, row=row, col=1)

        fig.update_layout(
            height=max(150 * len(groups), 200),
            margin=dict(l=50, r=20, t=10, b=30),
            showlegend=True,
            uirevision="plasma-visualizer",
        )
        return fig

    def update_orientation(self, selected_sources, selected_groups):
        sources = self.get_visual_sources()
        quat_sources = []
        if "Orientation (composed)" in (selected_groups or []):
            for src_name in selected_sources or []:
                memo = sources.get(src_name)
                if memo is None:
                    continue
                latest = [memo.get_latest(ch) for ch in ("OrientX", "OrientY", "OrientZ", "OrientW")]
                if all(v is not None for v in latest):
                    quat_sources.append((src_name, tuple(v[1] for v in latest)))

        if not quat_sources:
            return gr.update(visible=False)

        n = len(quat_sources)
        fig = make_subplots(
            rows=1, cols=n,
            specs=[[{"type": "scene"}] * n],
            subplot_titles=[name for name, _ in quat_sources],
            horizontal_spacing=0.02,
        )

        axis_colors = {"X": "red", "Y": "green", "Z": "blue"}
        for col, (src_name, q) in enumerate(quat_sources, start=1):
            axes = quat_to_axes(q)
            for label, vec in zip(("X", "Y", "Z"), axes):
                fig.add_trace(
                    go.Scatter3d(
                        x=[0, vec[0]], y=[0, vec[1]], z=[0, vec[2]],
                        mode="lines",
                        line=dict(color=axis_colors[label], width=6),
                        name=label,
                        legendgroup=label,
                        showlegend=(col == 1),
                    ),
                    row=1, col=col,
                )

            box_verts = [_rotate_point(p, axes) for p in _BOX_LOCAL_VERTS]
            fig.add_trace(
                go.Mesh3d(
                    x=[v[0] for v in box_verts],
                    y=[v[1] for v in box_verts],
                    z=[v[2] for v in box_verts],
                    i=_BOX_I, j=_BOX_J, k=_BOX_K,
                    color="lightslategray",
                    opacity=0.5,
                    flatshading=True,
                    name="orientation",
                    showlegend=False,
                    hoverinfo="skip",
                ),
                row=1, col=col,
            )

            scene_key = "scene" if col == 1 else f"scene{col}"
            fig.update_layout(**{
                scene_key: dict(
                    xaxis=dict(range=[-1, 1], visible=False),
                    yaxis=dict(range=[-1, 1], visible=False),
                    zaxis=dict(range=[-1, 1], visible=False),
                    aspectmode="cube",
                )
            })

        fig.update_layout(
            height=220,
            margin=dict(l=0, r=0, t=20, b=0),
            showlegend=True,
            uirevision="plasma-orientation",
        )
        return gr.update(value=fig, visible=True)

    def reset_orientation_all(self):
        for dev in self.available_devices:
            dev.reset_orientation()

    def calibrate_gyro_all(self, duration):
        for dev in self.available_devices:
            dev.start_gyro_calibration(duration)
        gr.Info(f"Calibrating gyro bias for {duration}s — keep wristband(s) still")

    def interface(self):
        with gr.Row():
            with gr.Column():
                with gr.Accordion(label="Session info", open=True):
                    default_sub = "sub-1000"
                    default_ses = "ses-00"

                    with gr.Row():
                        sub_name = gr.Text(default_sub, label="Subject ID", info="Format: sub-XXXX, X is integer")
                        ses_name = gr.Text(default_ses, label="Session ID", info="Format: ses-YY, Y is integer")
                        subject_enc = gr.Number(self.get_participant_encoding(default_sub, default_ses), label='Participant encoding (Read-only)', interactive=False,
                                                info="Format: XXXXYY")
                        sub_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        ses_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        _ = self.get_participant_encoding(default_sub, default_ses)


                with gr.Accordion(label="Device initialization", open=True):
                    device_grp = gr.CheckboxGroup(choices=self.device_list, value=self.device_list, label="Select sensor(s)")
                    with gr.Row():
                        btn_init = gr.Button("🚦Initialize selected device(s)")
                        btn_refresh = gr.Button("Refresh list")

                    btn_init.click(self.init_devices, inputs=device_grp)
                    btn_refresh.click(self._refresh_devices, outputs=device_grp)

            with gr.Column():
                with gr.Accordion(label="Device control", open=True):
                
                    with gr.Row():
                        self.btn_start = gr.Button("Start▶️")
                        self.btn_stop = gr.Button("Stop🛑")
                                        
                    self.btn_start.click(self.start_collection)
                    self.btn_stop.click(self.stop_collection)

                self.params = {"Memo": {"type": "welcome!"}}
                params = gr.ParamViewer(self.params)
                timer = gr.Timer(value=1)
                timer.tick(fn=self.update_params, outputs=params)

        # with gr.Accordion("Help", open=False):
        #     with open("./plasma/help.md") as f:
        #         help_txt = f.read()
        #         # print(help_txt)
        #     md = gr.Markdown(help_txt)

    def _refresh_devices(self):
        active = list(device_config.get_active_table().keys())
        return gr.CheckboxGroup(choices=active, value=active)

    def init_devices(self, selected_devices):
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.info(f"Error stopping previous device before reinit: {e}")
            try:
                dev.disconnect()
            except Exception as e:
                self.logger.info(f"Error disconnecting previous device before reinit: {e}")

        self.available_devices = []
        active_table = device_config.get_active_table()
        for dev in selected_devices:
            cls = active_table[dev]
            print(dev, cls)
            module = importlib.import_module(cls['module'])
            Device = getattr(module, cls['class'])
            device_instance = Device(self.session_info, self.logger, tag=dev)
            self.available_devices.append(device_instance)

        self.sts = "Ready to start"


    def start_collection(self):
        for dev in self.available_devices:
            try:
                dev.start()
            except Exception as e:
                self.logger.info(f"Error starting device {dev.tag}: {e}")
        self.sts = "Collection in progress"

    def stop_collection(self):
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.info(f"Error stopping device {dev.tag}: {e}")
        self.sts = "Collection stopped"

    def update_params(self):
        params = {
            self.sts: {"type": f"{self.session_info['sub_id']} {self.session_info['ses_id']}",
                     "description": self.session_info['log_dir']}
        }

        for dev in self.available_devices:
            if isinstance(dev.memo, dict):
                for k, v in dev.memo.items():
                    params[f"- {getattr(v, 'label', k)}"] = v.get_sts()
            else:
                params[f"- {getattr(dev.memo, 'label', dev.memo.name)}"] = dev.memo.get_sts()

        # print(params)
        return params


    def get_participant_encoding(self, sub, ses):
        integer_representation = encode_participant(sub, ses)

        # print(name, integer_representation)
        self.participant_byte = struct.pack("<I", integer_representation)
        self.session_info = {
            'sub_id': sub,
            'ses_id': ses,
            'participant_enc': integer_representation,
            'log_dir': os.path.join(self.log_root, sub, ses)
        }
        return integer_representation


def get_logger(yams_dir="data"):
    # current YYMMDD
    now = datetime.datetime.now()
    date = now.strftime("%Y-%m-%d")

    # init logger
    logger = logging.getLogger(__name__)
    os.makedirs(yams_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, 
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(yams_dir, f"{date}_plasma_session.log")),
                            logging.StreamHandler()
                        ])
    return logger