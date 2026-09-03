"""MSense Gradio panels.

The plugin contributes a single "MSense" tab (`tools.build_msense_tab`) that
nests every panel as a sub-tab:

Live (need the driver via `ip.find_device`):
  - `sqc.build_sqc_tab`   — ECG/PPG Signal Quality
  - `imu.build_imu_tab`   — IMU orientation

Offline field-data toolkit:
  - Downloader / Extractor / Clock Sync / Data viewer / UUID tools

`build_sqc_tab` / `build_imu_tab` stay individually exported for the tests and
for a future front-end that wants them as standalone tabs.
"""
from . import clocksync, control, downloader, extractor, imu, sqc, tools, uuid_tools, viewer
from .imu import build_imu_tab
from .sqc import build_sqc_tab
from .tools import build_msense_tab

__all__ = [
    "sqc", "imu", "control", "tools", "downloader", "extractor", "clocksync", "viewer", "uuid_tools",
    "build_sqc_tab", "build_imu_tab", "build_msense_tab",
]
