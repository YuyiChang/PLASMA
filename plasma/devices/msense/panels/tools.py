"""The "MSense" tab — every MSense panel under one plugin tab with sub-tabs, so
PLASMA's top-level tab bar stays lean.

Live sub-tabs (need a wristband, reached via `ip.find_device`): Signal Quality,
IMU / Orientation. Offline sub-tabs (USB / CSV files, no BLE): Downloader,
Extractor, Clock Sync, Data viewer, Devices.
"""
import gradio as gr

from .clocksync import build_clocksync
from .downloader import build_downloader
from .extractor import build_extractor, build_extractor_pro
from .imu import build_imu_tab
from .sqc import build_sqc_tab
from .uuid_tools import build_device_manager, build_uuid_extractor
from .viewer import build_viewer


def build_msense_tab(ip=None):
    with gr.Tabs():
        with gr.Tab("📡 Signal Quality"):
            build_sqc_tab(ip)
        with gr.Tab("🧭 IMU / Orientation"):
            build_imu_tab(ip)
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
