"""The "MSense IMU" tab: the gyro-only 3D orientation indicator plus
Reset-orientation / Calibrate-gyro-bias controls. Takes the IntegratedPanel."""
import gradio as gr
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..quaternion import quat_to_axes
from ._common import msense_device as _msense_device

_ORIENT_CHANNELS = ("OrientX", "OrientY", "OrientZ", "OrientW")

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
