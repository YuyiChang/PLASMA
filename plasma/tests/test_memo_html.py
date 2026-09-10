"""Memo-panel HTML renderer — pure functions, no gradio / no hardware."""
import pytest

from plasma.integrated_panel import (
    build_memo_html, _status_class, _sts_detail, _rec_stats, _fmt_elapsed,
)
from plasma.status import COLLECTING, STOPPED, SETUP


# ── failure-level classification (see docs/failure-levels.md) ─────────────

@pytest.mark.parametrize("sts,phase,expected", [
    # healthy / idle / cleared
    ("🟢", COLLECTING, "healthy"),
    ("Collection in progress", COLLECTING, "healthy"),
    ("🔄 reconnected", COLLECTING, "healthy"),
    ("✅ Bias saved", COLLECTING, "info"),
    ("🟦", SETUP, "info"),
    ("Ready", SETUP, "info"),
    ("Welcome", SETUP, "info"),
    ("Ready to start", SETUP, "info"),
    ("something nobody wrote", SETUP, "info"),
    ("", SETUP, "info"),
    (None, SETUP, "info"),
    # L3 — warning (red)
    ("🚫 FAULT", SETUP, "warning"),
    ("⛔ connect failed", SETUP, "warning"),
    ("⛔ device not found", SETUP, "warning"),
    ("❌ Fault: no device", SETUP, "warning"),
    ("FAULT: boom", COLLECTING, "warning"),
    ("⚠️ start failed", COLLECTING, "warning"),
    # L2 — caution (amber)
    ("⚠️ still recording — stop unconfirmed", STOPPED, "caution"),
    ("⚠️ stop failed", STOPPED, "caution"),
    ("⚠️ stream stalled", COLLECTING, "caution"),
    ("🔌 disconnected", COLLECTING, "caution"),
    # L1 — advisory (blue) / info
    ("🧨 erased — re-Initialize", STOPPED, "advisory"),
    ("🎯 Calibrating...", COLLECTING, "info"),
    ("🛑", STOPPED, "advisory"),                 # acq-stop unverifiable
    # phase-gated stop: red mid-collection, neutral once the operator has stopped
    ("🛑 stopped", COLLECTING, "warning"),
    ("🛑 stopped", STOPPED, "info"),
    ("🟥", COLLECTING, "warning"),
    ("🟥", STOPPED, "info"),
    ("Collection stopped", STOPPED, "info"),
])
def test_status_class(sts, phase, expected):
    assert _status_class(sts, phase) == expected


def test_device_row_escalates_on_silent_recorded_stream():
    """A device whose own recorded stream goes stale/lost escalates the row
    even though the driver never touched memo.sts."""
    dev = _Dev(_Memo("MSense", sts="🟢"), streams={"MSense [x]": "dev"})
    stale = build_memo_html("Collection in progress", _SI, [dev],
                            _snap([_stream("MSense [x]", health="🟡 stale")]))
    assert f'color:{"#a16207"}">📼 MSense' in stale        # amber (L2)
    lost = build_memo_html("Collection in progress", _SI, [dev],
                           _snap([_stream("MSense [x]", health="🔴 lost")]))
    assert f'color:{"#b91c1c"}">📼 MSense' in lost         # red (L3)


def test_disconnected_row_goes_red_when_recorded_stream_is_lost():
    """`🔌 disconnected` stays amber (L2) on its own — the watchdog is still
    retrying — but the row turns red once the wristband's recorded stream has
    been silent long enough to count as lost. Same status string, colour only."""
    dev = _Dev(_Memo("MSense", sts="🔌 disconnected"), streams={"MSense [x]": "dev"})
    amber = build_memo_html("Collection in progress", _SI, [dev], _snap([]))
    assert f'color:{"#a16207"}">MSense' in amber           # amber (L2), still retrying
    red = build_memo_html("Collection in progress", _SI, [dev],
                          _snap([_stream("MSense [x]", health="🔴 lost")]))
    assert f'color:{"#b91c1c"}">📼 MSense' in red          # red (L3)


def test_sts_detail_strips_leading_glyph():
    assert _sts_detail("⛔ connect failed") == "connect failed"
    assert _sts_detail("🟢") == ""
    assert _sts_detail("🔄 reconnected") == "reconnected"
    assert _sts_detail("Ready") == "Ready"
    assert _sts_detail("🔋 87%") == "87%"


def test_fmt_elapsed():
    assert _fmt_elapsed(0) == "00:00:00"
    assert _fmt_elapsed(5) == "00:00:05"
    assert _fmt_elapsed(65) == "00:01:05"
    assert _fmt_elapsed(3661) == "01:01:01"
    assert _fmt_elapsed(-3) == "00:00:00"
    assert _fmt_elapsed(90000) == "25:00:00"


def test_elapsed_in_header():
    dev = _Dev(_Memo("Shimmer", sts="🟢"))
    h = build_memo_html("Collection in progress", _SI, [dev], None,
                        elapsed="00:04:32")
    assert "00:04:32" in h
    h2 = build_memo_html("Welcome", _SI, [dev], None)
    assert "00:04:32" not in h2       # None -> nothing rendered


def test_rec_stats():
    assert _rec_stats({"n_samples": 12480, "srate": 2.0, "xdf_basename": "r.xdf"}) \
        == "12,480 samp @ 2 Hz → r.xdf"
    assert _rec_stats({"n_samples": 4, "srate": 0.0, "xdf_basename": "r.xdf"}) \
        == "4 samp (irregular) → r.xdf"


# ── build_memo_html ──────────────────────────────────────────────────────

_SI = {"sub_id": "sub-1000", "ses_id": "ses-00", "log_dir": "/data/sub-1000/ses-00"}


class _Memo:
    def __init__(self, label, sts="🟦", latest="12:00:00 init"):
        self.name = label
        self.label = label
        self.sts = sts
        self.latest = latest


class _Dev:
    def __init__(self, memo, streams=None):
        self.memo = memo
        self.tag = "dev"
        self._streams = streams or {}

    def get_sources(self):
        return self.memo if isinstance(self.memo, dict) else {self.tag: self.memo}

    def lsl_streams(self):
        return self._streams


def _stream(name, external=False, health="🟢", n=100, srate=2.0):
    return {"name": name, "external": external, "health": health,
            "n_samples": n, "srate": srate,
            "xdf_basename": "plasma_recording_260905_104945.xdf"}


def _snap(streams, state="recording"):
    return {"state": state, "summary": f"{len(streams)} streams",
            "file": "/x/plasma_recording_260905_104945.xdf", "streams": streams}


def test_recording_off_no_camera_icon():
    dev = _Dev(_Memo("Shimmer", sts="🟢"))
    h = build_memo_html("Collection in progress", _SI, [dev], None)
    assert "📼" not in h
    assert f'color:{"#15803d"}' in h            # green name
    assert "Shimmer" in h


def test_matched_stream_folds_into_device_row_once():
    dev = _Dev(_Memo("MSense (Left)", sts="🟢"),
               streams={"MSense [E4:B0]": "dev"})
    h = build_memo_html("Collection in progress", _SI, [dev],
                        _snap([_stream("MSense [E4:B0]")]))
    assert h.count("📼 MSense (Left)") == 1
    assert "100 samp @ 2 Hz" in h
    assert "m-sub rec" in h


def test_two_bands_same_label_both_render():
    """Two MSense wristbands never renamed from the factory default share a
    memo label but are keyed separately — both rows must show."""
    memo = {"E4:B0:AA": _Memo("MSense4PPG"), "E4:B0:BB": _Memo("MSense4PPG")}
    h = build_memo_html("Collection in progress", _SI, [_Dev(memo)], None)
    assert h.count(">MSense4PPG</span>") == 2


def test_dict_memo_only_one_band_recorded():
    memo = {"A": _Memo("A"), "B": _Memo("B")}

    class _MDev(_Dev):
        def lsl_streams(self):
            return {"A [x]": "A"}          # only band A publishes/records

    dev = _MDev(memo)
    h = build_memo_html("x", _SI, [dev], _snap([_stream("A [x]")]))
    assert "📼 A" in h
    assert ">B</span>" in h and "📼 B" not in h


def test_orphan_external_and_plasma_streams():
    dev = _Dev(_Memo("Shimmer"))
    snap = _snap([_stream("Foo", external=True),
                  _stream("PLASMA", external=True, srate=0.0)])
    h = build_memo_html("x", _SI, [dev], snap)
    assert "📼🛰 Foo" in h
    assert "📼🛰 PLASMA" in h
    assert "(irregular)" in h


def test_exactly_one_recorder_header():
    dev = _Dev(_Memo("Shimmer"))
    h = build_memo_html("x", _SI, [dev], _snap([]))
    assert h.count("📼 0 streams") == 1
    assert "plasma_recording_260905_104945.xdf" in h


def test_recorder_header_colour_by_state():
    dev = _Dev(_Memo("Shimmer"))
    rec = build_memo_html("x", _SI, [dev], _snap([], state="recording"))
    assert f'color:{"#15803d"}">📼 0 streams' in rec           # green while recording
    stop = build_memo_html("x", _SI, [dev], _snap([], state="stopped"))
    assert f'color:{"#4b5563"}">📼 0 streams' in stop           # neutral once stopped — not a failure
    una = build_memo_html("x", _SI, [dev], _snap([], state="unavailable"))
    assert f'color:{"#b91c1c"}">📼 0 streams' in una            # red — no XDF at all


def test_html_is_escaped():
    dev = _Dev(_Memo("X", sts="🟢",
                     latest="12:00 <img src=x onerror=alert(1)>"))
    h = build_memo_html("Welcome", _SI, [dev], None)
    assert "<img" not in h
    assert "&lt;img" in h


def test_lost_orphan_stream_is_red():
    dev = _Dev(_Memo("Shimmer"))
    snap = _snap([_stream("Gone", external=True, health="🔴 lost")])
    h = build_memo_html("x", _SI, [dev], snap)
    assert f'color:{"#b91c1c"}' in h


def test_external_streams_shown_before_recording():
    ext = [{"name": "OtherPC-EEG", "type": "EEG", "srate": 256.0, "host": "lab-pc"},
           {"name": "Tobii", "type": "Gaze", "srate": 0.0, "host": "eye-pc"}]
    h = build_memo_html("Ready to start", _SI, [], None, ext_streams=ext)
    assert "🛰 OtherPC-EEG" in h and "EEG · 256 Hz · lab-pc" in h
    assert "🛰 Tobii" in h and "Gaze · eye-pc" in h        # no srate -> no "0 Hz"
    assert "📼" not in h


def test_external_streams_suppressed_once_recording():
    ext = [{"name": "OtherPC-EEG", "type": "EEG", "srate": 256.0, "host": "lab-pc"}]
    h = build_memo_html("x", _SI, [_Dev(_Memo("Shimmer"))], _snap([]), ext_streams=ext)
    assert "🛰 OtherPC-EEG" not in h
