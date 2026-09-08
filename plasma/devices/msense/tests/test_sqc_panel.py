"""Coverage for the ECG/PPG Signal Quality tab's figure builder — specifically
that filtering is computed on the fly for partial (History Only / Custom) and
still-streaming captures, not just full ones.
"""
import numpy as np

from plasma.devices.msense.panels.sqc import _build_sqc_figure


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
