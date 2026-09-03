"""MSense-owned Gradio tabs:

* ``build_sqc_tab`` — the "ECG/PPG Signal Quality" tab: on-demand raw ECG/PPG
  snapshots over the NUS bounded-stream protocol.
* ``build_imu_tab`` — the "MSense IMU" tab: the gyro-only 3D orientation
  indicator plus Reset-orientation / Calibrate-gyro-bias controls.

Both take the :class:`~plasma.integrated_panel.IntegratedPanel` instance and use
``_msense_device(ip)`` to reach the live driver.
"""
import gradio as gr
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .quaternion import quat_to_axes
from .signal_quality import filter_ecg, filter_ppg

_ORIENT_CHANNELS = ("OrientX", "OrientY", "OrientZ", "OrientW")


def _msense_device(ip):
    # imported lazily so registering the plugin doesn't pull simplepyble
    from .device import MotionSenseHRV
    return ip.find_device(MotionSenseHRV)

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


# ── ECG/PPG Signal Quality tab ────────────────────────────────────────────────

def build_sqc_tab(ip):
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

        def _request(max_seconds=None, history_only=False):
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first"
            try:
                ms = float(max_seconds) if max_seconds not in (None, "", 0) else None
            except (TypeError, ValueError):
                ms = None
            if ms is not None and ms <= 0:
                ms = None
            return dev.request_all_sqc_snapshots(max_seconds=ms, history_only=bool(history_only))

        def _cancel():
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized"
            return dev.cancel_all_sqc_snapshots()

        btn_refresh_sqc.click(lambda: _update_sqc(ip), outputs=[sqc_status, sqc_plot])
        btn_request_sqc.click(_request, inputs=[sqc_max_s, sqc_hist_only], outputs=sqc_status)
        btn_cancel_sqc.click(_cancel, outputs=sqc_status)
        sqc_timer.tick(fn=lambda: _update_sqc(ip), outputs=[sqc_status, sqc_plot])


def _update_sqc(ip):
    dev = _msense_device(ip)
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

    fig = _build_sqc_figure(results) if results else gr.update()
    return "\n".join(lines), fig


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


# ── MSense IMU (orientation) tab ──────────────────────────────────────────────

def _orientation_sources(ip):
    """{label: memo} for live MSense sources carrying composed-orientation channels."""
    out = {}
    for name, memo in ip.get_visual_sources().items():
        if all(ch in memo.channels for ch in _ORIENT_CHANNELS):
            out[name] = memo
    return out


def build_imu_tab(ip):
    with gr.Column():
        gr.Markdown(
            "Gyro-only dead-reckoning **orientation** for MSense wristbands streaming the demo IMU "
            "characteristic. Initialize the MSense device on the **Session dashboard** tab and press "
            "Start, then Refresh here.\n\n"
            "*Orientation is composed from per-frame deltas since the last Start/Reset — there's no "
            "accelerometer/magnetometer correction, so it drifts over time. Use Reset orientation to "
            "re-zero it. Calibrate gyro bias with the wristband(s) held still — the bias is saved to "
            "disk and reapplied automatically on future launches.*"
        )
        with gr.Row():
            source_select = gr.Dropdown(choices=[], multiselect=True, label="Wristband(s)")
            btn_refresh = gr.Button("🔄 Refresh")
        with gr.Row():
            btn_reset = gr.Button("↺ Reset orientation")
            calib_duration = gr.Number(value=3, minimum=1, precision=0,
                                       label="Calibration duration (s)", scale=0)
            btn_calibrate = gr.Button("🎯 Calibrate gyro bias")

        orientation_plot = gr.Plot(show_label=False)
        timer = gr.Timer(value=0.2, active=True)

        def _refresh():
            names = list(_orientation_sources(ip).keys())
            return gr.Dropdown(choices=names, value=names)

        def _reset():
            dev = _msense_device(ip)
            if dev is not None:
                dev.reset_orientation()

        def _calibrate(duration):
            dev = _msense_device(ip)
            if dev is not None:
                dev.start_gyro_calibration(duration)
                gr.Info(f"Calibrating gyro bias for {duration}s — keep wristband(s) still")

        btn_refresh.click(_refresh, outputs=source_select)
        btn_reset.click(_reset)
        btn_calibrate.click(_calibrate, inputs=calib_duration)
        timer.tick(fn=lambda s: _update_orientation(ip, s), inputs=source_select,
                   outputs=orientation_plot)


def _update_orientation(ip, selected_sources):
    sources = _orientation_sources(ip)
    quat_sources = []
    for src_name in selected_sources or []:
        memo = sources.get(src_name)
        if memo is None:
            continue
        latest = [memo.get_latest(ch) for ch in _ORIENT_CHANNELS]
        if all(v is not None for v in latest):
            quat_sources.append((src_name, tuple(v[1] for v in latest)))

    if not quat_sources:
        return go.Figure(layout=dict(title="No orientation data — Start a session with IMU Stream enabled",
                                     height=220, uirevision="plasma-orientation"))

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
    return fig
