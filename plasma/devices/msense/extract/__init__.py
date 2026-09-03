"""Offline MSense `.bin` field-data toolkit — pure (no Gradio).

- `pipeline`  : `.bin` -> CSV/PKL extraction (`extract_dir`, `extract_zip`, `batch_extract_zips`)
- `clocksync` : counter-align an extracted CSV to a YAMS `.txt` unix-time reference
- `options`   : `ExtractionOptions` dataclass

CLI: `python -m plasma.devices.msense.extract {dir | batch | sync}`
"""
from .options import ExtractionOptions
from .pipeline import (
    ExtractionReport,
    batch_extract_zips,
    extract_dir,
    extract_zip,
)
from .clocksync import apply_interp_to_csv, sync_csv_to_yams, sync_paths

__all__ = [
    "ExtractionOptions",
    "ExtractionReport",
    "extract_dir",
    "extract_zip",
    "batch_extract_zips",
    "sync_csv_to_yams",
    "apply_interp_to_csv",
    "sync_paths",
]
