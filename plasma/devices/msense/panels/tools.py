"""The "MSense" tab — every MSense panel under one plugin tab with sub-tabs, so
PLASMA's top-level tab bar stays lean.

Live sub-tabs (need a wristband, reached via `ip.find_device`): Signal Quality,
IMU / Orientation. Offline sub-tabs (USB / CSV files, no BLE): Downloader,
Extractor, Clock Sync, Data viewer, Devices.
"""
import gradio as gr

from .clocksync import build_clocksync
from .control import build_control_tab
from .downloader import build_downloader
from .extractor import build_extractor, build_extractor_pro
from .imu import build_imu_tab
from .sqc import build_sqc_tab
from .uuid_tools import build_device_manager, build_uuid_extractor
from .viewer import build_viewer


#: sub-tab labels whose live Plotly plot the timer-gate cares about
_SQC_SUBTAB = "📡 Signal Quality"
_IMU_SUBTAB = "🧭 IMU / Orientation"


def build_msense_tab(ip=None):
    with gr.Tabs() as yams_tabs:
        with gr.Tab(_SQC_SUBTAB):
            build_sqc_tab(ip)
        with gr.Tab(_IMU_SUBTAB):
            build_imu_tab(ip)
        with gr.Tab("🎛️ Control"):
            build_control_tab(ip)
        with gr.Tab("📂 Downloader"):
            build_downloader(ip)
        with gr.Tab("🛠️ Extractor"):
            build_extractor(ip)
        with gr.Tab("🛠️ Extractor (zip)"):
            build_extractor_pro(ip)
        with gr.Tab("⏱️ Clock Sync"):
            build_clocksync(ip)
        with gr.Tab("📊 Data viewer"):
            build_viewer(ip)
        with gr.Tab("📋 Devices"):
            with gr.Accordion("UUID extractor", open=True):
                build_uuid_extractor(ip)
            with gr.Accordion("Import device_info.json", open=False):
                build_device_manager(ip)

    # Gate the live-plot refresh timers on sub-tab visibility — gr.Plot leaks
    # its Plotly <div> on every tick (gradio#10252), so a timer running behind
    # an unseen sub-tab is pure memory churn. build_blocks() adds the matching
    # top-level-tab gate; `ip._yams_sub` lets it re-assert the right sub-tab
    # timer when the user returns to this tab.
    if ip is not None and getattr(ip, "_sqc_timer", None) is not None:
        ip._yams_sub = gr.State(_SQC_SUBTAB)

        def _on_subtab(evt: gr.SelectData):
            sub = evt.value or ""
            return (sub,
                    gr.Timer(active=(sub == _SQC_SUBTAB)),
                    gr.Timer(active=(sub == _IMU_SUBTAB)))

        yams_tabs.select(_on_subtab,
                         outputs=[ip._yams_sub, ip._sqc_timer, ip._imu_timer])
