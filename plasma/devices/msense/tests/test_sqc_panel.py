"""Coverage for the ECG/PPG Signal Quality tab's figure builder — specifically
that filtering is computed on the fly for partial (History Only / Custom) and
still-streaming captures, not just full ones.
"""
import types

import gradio as gr
import numpy as np

from plasma.devices.msense.panels import sqc as _sqc
from plasma.devices.msense.panels.sqc import _build_sqc_figure, _update_sqc


def _ppg_result(n=2048, **extra):
    fs = 256.0
    t = np.arange(n) / fs
    pulse = 5000 * np.sin(2 * np.pi * 1.2 * t) + 200_000        # DC-heavy raw
    chans = {c: pulse + i * 30 for i, c in enumerate(("ir1", "ir2", "g1", "g2"))}
    return {"device_type": "PPG", "channels": chans, "fs": fs,
            "history_boundary_sample": None, **extra}


def _filtered_trace_names(fig):
    return {tr.name for tr in fig.data if tr.name and "filtered" in tr.name}


def test_full_capture_has_filtered_ppg():
    fig = _build_sqc_figure([("W", _ppg_result())], ppg_mode="Filtered")
    assert _filtered_trace_names(fig)


def test_history_only_partial_capture_still_filters():
    # a finished quick-mode capture: partial + quick_seconds set, no `streaming`
    res = _ppg_result(n=2048, partial=True, quick_seconds=8.0)
    fig = _build_sqc_figure([("W", res)], ppg_mode="Filtered")
    assert _filtered_trace_names(fig), "History Only / Custom capture must show filtered PPG"


def test_custom_partial_both_mode_filters():
    res = _ppg_result(n=1200, partial=True, quick_seconds=5.0)
    fig = _build_sqc_figure([("W", res)], ppg_mode="Both")
    names = {tr.name for tr in fig.data if tr.name}
    assert any("filtered" in n for n in names) and any("raw" in n for n in names)


def test_still_streaming_preview_also_filters_when_long_enough():
    res = _ppg_result(n=1024, partial=True, streaming=True)
    fig = _build_sqc_figure([("W", res)], ppg_mode="Filtered")
    assert _filtered_trace_names(fig)
    assert fig.layout.annotations[0].text.endswith("receiving…")


def test_too_short_capture_falls_back_to_raw():
    res = _ppg_result(n=32, partial=True, quick_seconds=0.1)
    fig = _build_sqc_figure([("W", res)], ppg_mode="Filtered")
    assert not _filtered_trace_names(fig)
    assert any(tr.name and "raw" in tr.name for tr in fig.data)


# ── on-screen decimation (browser-memory fix) ───────────────────────────────

def _ecg_result(n, **extra):
    fs = 512.0
    y = np.sin(np.arange(n) / 20.0) * 300
    return {"device_type": "ECG", "channels": {"ecg": y}, "fs": fs,
            "history_boundary_sample": None, **extra}


def test_decimation_caps_on_screen_points():
    res = _ecg_result(40_000)
    small = _build_sqc_figure([("W", res)], decimate=True)
    full = _build_sqc_figure([("W", res)], decimate=False)
    assert max(len(tr.x) for tr in small.data) <= 3200
    assert max(len(tr.x) for tr in full.data) >= 39_000


# ── _update_sqc gr.skip() cache ───────────────────────────────────────────

class _FakeDev:
    def __init__(self):
        self._decoded = _ecg_result(4000)
        self.status = "ready"

    def get_sqc_devices(self): return ["W1"]
    def caps_summary(self): return ""
    def display_name(self, n): return n
    def get_sqc_status(self, n): return {"status": self.status, "diag": {},
                                         "saved_path": "/x/w.bin", "error": None,
                                         "phase": "forward", "bytes_total": None}
    def get_sqc_preview(self, n): return None
    def get_sqc_result(self, n): return self._decoded
    def get_live_stream_status(self, n): return {"status": "idle", "diag": {}}
    def get_live_stream_preview(self, n): return None


def test_update_sqc_skips_unchanged_plot(monkeypatch):
    dev = _FakeDev()
    ip = types.SimpleNamespace()
    monkeypatch.setattr(_sqc, "_msense_device", lambda _ip: dev)

    _txt, fig1 = _update_sqc(ip)
    assert hasattr(fig1, "data")                              # first render is a real figure

    _txt, fig2 = _update_sqc(ip)
    assert isinstance(fig2, dict) and fig2 == gr.skip()       # nothing changed -> skip

    dev._decoded = _ecg_result(9000)                          # new snapshot data
    _txt, fig3 = _update_sqc(ip)
    assert hasattr(fig3, "data")
