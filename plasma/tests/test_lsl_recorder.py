"""SessionRecorder end-to-end: run real in-process LSL outlets, record them
with the recorder, read the XDF back with pyxdf, assert fidelity + the
origin/health bookkeeping.

These use the loopback LSL transport, so they are inherently timing-y — the
tolerances are deliberately loose, stream names are randomised so a real LSL
stream on the developer's network can't collide, and each test asserts only
about streams it created (the recorder faithfully grabs everything else too).
"""
import gc
import threading
import time
import uuid

import numpy as np
import pytest

pylsl = pytest.importorskip("pylsl")
pyxdf = pytest.importorskip("pyxdf")

from plasma.lsl_recorder import SessionRecorder, _stale_after
from plasma.lsl_util import mark_plasma_origin
from plasma.status import STALE_S, IRREGULAR_STALE_S


def _name(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_stale_after_is_rate_aware():
    # a periodic stream goes stale in seconds; an irregular / event stream
    # (journaler, Pupil eye events, srate == 0) only after minutes — no false
    # 'stale' alarm on a stream that's silent by nature
    assert _stale_after(50.0) == STALE_S
    assert _stale_after(2.0) == STALE_S
    assert _stale_after(0.0) == IRREGULAR_STALE_S
    assert _stale_after(None) == IRREGULAR_STALE_S
    assert IRREGULAR_STALE_S > STALE_S * 10


def _outlet(name, stype, nchan, srate, fmt, plasma_origin=False):
    info = pylsl.StreamInfo(name, stype, nchan, srate, fmt, name)
    if plasma_origin:
        mark_plasma_origin(info)
    return pylsl.StreamOutlet(info)


class _Pusher:
    """Background thread pushing rows into registered outlets ~100 Hz."""

    def __init__(self):
        self._entries = []          # (outlet, row_fn)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def add(self, outlet, row_fn):
        self._entries.append((outlet, row_fn))

    def _run(self):
        i = 0
        while not self._stop.is_set():
            for outlet, row_fn in list(self._entries):
                try:
                    outlet.push_sample(row_fn(i))
                except Exception:
                    pass
            i += 1
            time.sleep(0.01)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2.0)


def _recorder():
    return SessionRecorder(clock_sync_interval=1.0, boundary_interval=1.0,
                           resolve_interval=1.0)


def _load(rec):
    streams, _ = pyxdf.load_xdf(rec.status()["file"], dejitter_timestamps=False,
                                synchronize_clocks=False)
    return {s["info"]["name"][0]: s for s in streams}


# ── numeric capture + origin classification ───────────────────────────────

def test_records_external_and_own_streams(tmp_path):
    ext_name, own_name = _name("Ext"), _name("Own")
    ext = _outlet(ext_name, "EEG", 2, 100.0, "float32")
    own = _outlet(own_name, "PPG", 3, 100.0, "float32", plasma_origin=True)

    rec = _recorder()
    assert rec.start(str(tmp_path)) is True
    with _Pusher() as p:
        p.add(ext, lambda i: [float(i), float(-i)])
        p.add(own, lambda i: [float(i), 1.0, 2.0])
        time.sleep(4.0)
    rec.stop()

    streams = _load(rec)
    assert ext_name in streams and own_name in streams
    assert len(streams[ext_name]["time_series"]) >= 100
    assert len(streams[own_name]["time_series"]) >= 100

    ext_seen = streams[ext_name]["time_series"]
    assert np.array_equal(ext_seen[:, 0], -ext_seen[:, 1])   # values intact
    assert np.all(np.diff(ext_seen[:, 0]) >= 0)              # in order

    by_name = {s["name"]: s for s in rec.status()["streams"]}
    assert by_name[ext_name]["external"] is True
    assert by_name[own_name]["external"] is False
    assert rec.status()["state"] == "stopped"


# ── string / marker stream ────────────────────────────────────────────────

def test_records_string_markers(tmp_path):
    mname = _name("Markers")
    markers = _outlet(mname, "Markers", 1, pylsl.IRREGULAR_RATE, "string",
                      plasma_origin=True)
    msgs = ["session start", "event [x]", "unicode → ✓", "session end"]

    rec = _recorder()
    assert rec.start(str(tmp_path)) is True
    time.sleep(2.0)                       # let the inlet bind
    for m in msgs:
        markers.push_sample([m])
        time.sleep(0.3)
    time.sleep(1.0)
    rec.stop()

    streams = _load(rec)
    got = [row[0] for row in streams[mname]["time_series"]]
    assert got == msgs
    by_name = {s["name"]: s for s in rec.status()["streams"]}
    assert by_name[mname]["external"] is False


# ── late joiner ───────────────────────────────────────────────────────────

def test_late_joiner_is_captured(tmp_path):
    early_name, late_name = _name("Early"), _name("Late")
    early = _outlet(early_name, "EEG", 1, 100.0, "float32")

    rec = _recorder()
    assert rec.start(str(tmp_path)) is True
    with _Pusher() as p:
        p.add(early, lambda i: [float(i)])
        time.sleep(3.0)
        late = _outlet(late_name, "EEG", 1, 100.0, "float32")
        p.add(late, lambda i: [float(i)])
        time.sleep(5.0)                   # resolver + bind + flow
    rec.stop()

    streams = _load(rec)
    assert early_name in streams and late_name in streams
    assert len(streams[late_name]["time_series"]) > 0
    names = {s["name"] for s in rec.status()["streams"]}
    assert {early_name, late_name} <= names


# ── mid-recording drop ────────────────────────────────────────────────────

def test_dropped_stream_gets_footer_and_lost_health(tmp_path):
    keep_name, drop_name = _name("Keep"), _name("Drop")
    keep = _outlet(keep_name, "EEG", 1, 100.0, "float32")
    drop = _outlet(drop_name, "EEG", 1, 100.0, "float32")

    rec = _recorder()
    assert rec.start(str(tmp_path)) is True
    with _Pusher() as p:
        p.add(keep, lambda i: [float(i)])
        p.add(drop, lambda i: [float(i)])
        time.sleep(3.0)
        p._entries = [(keep, p._entries[0][1])]   # stop feeding 'drop'
        del drop
        gc.collect()
        time.sleep(8.0)                            # let LSL notice it's gone
    rec.stop()

    by_name = {s["name"]: s for s in rec.status()["streams"]}
    assert by_name[drop_name]["health"] == "🔴 lost"

    streams = _load(rec)
    assert drop_name in streams                    # header + samples + footer
    assert len(streams[drop_name]["time_series"]) > 0


# ── graceful degradation ──────────────────────────────────────────────────

def test_start_returns_false_when_pylsl_missing(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_pylsl(name, *a, **k):
        if name == "pylsl" or name.startswith("pylsl."):
            raise ImportError("simulated: liblsl not found")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_pylsl)

    rec = SessionRecorder()
    assert rec.start(str(tmp_path)) is False
    assert rec.status()["state"] == "unavailable"
    rec.stop()                                     # must not raise
    assert rec.status()["state"] == "unavailable"
