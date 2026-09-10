"""Headless API layer — pure, no gradio server, no hardware.

Covers plasma/api.py: session_status() aggregation + worst-level rollup, the
SessionEventLog snapshot-diff → events.jsonl, and the start_session outcome
report.
"""
import json
import types

import pytest

from plasma import api
from plasma.status import COLLECTING, SETUP, STOPPED, Level


# ── fakes ────────────────────────────────────────────────────────────────

class _Memo:
    def __init__(self, name, sts="🟢", latest="12:00:00 ok"):
        self.name = name
        self.label = name
        self.sts = sts
        self.latest = latest


class _Dev:
    def __init__(self, tag, memo=None, streams=None, sqc=None, live=None):
        self.tag = tag
        self.memo = memo or _Memo(tag)
        self._streams = streams or {}
        self._sqc = sqc            # {key: {"status","error",...}}
        self._live = live

    def get_sources(self):
        return self.memo if isinstance(self.memo, dict) else {self.tag: self.memo}

    def lsl_streams(self):
        return self._streams

    def get_sqc_status(self, name):
        return (self._sqc or {}).get(name, {"status": "unavailable", "error": None})

    def get_live_stream_status(self, name):
        return (self._live or {}).get(name, {"status": "idle", "error": None})


class _Rec:
    def __init__(self, state="recording", streams=()):
        self._state = state
        self._streams = list(streams)

    def status(self):
        return {"state": self._state,
                "summary": f"{len(self._streams)} stream(s)",
                "file": "/x/plasma_recording_260908_120000.xdf",
                "streams": self._streams}


def _stream(name, external=False, health="🟢", n=100, srate=2.0):
    return {"name": name, "type": "EEG", "external": external, "health": health,
            "n_samples": n, "srate": srate,
            "xdf_basename": "plasma_recording_260908_120000.xdf"}


class _Panel:
    """Minimal stand-in for IntegratedPanel — only the attributes
    session_status() / SessionEventLog touch."""

    def __init__(self, tmp_path, devices=(), recorder=None, phase=COLLECTING):
        self.log_root = str(tmp_path)
        self.sts = "Collection in progress"
        self.available_devices = list(devices)
        self.lsl_recorder = recorder
        self.session_info = {"sub_id": "sub-1001", "ses_id": "ses-02",
                             "participant_enc": 100102}
        self.session_dir = str(tmp_path / "sess")
        self._collection_started = 1000.0 if phase != SETUP else None
        self._collection_stopped = 1050.0 if phase == STOPPED else None
        self.journal_calls = []

    def _external_lsl_streams(self):
        return []

    def journal(self, msg):
        self.journal_calls.append(msg)

    @property
    def phase(self):
        if self._collection_started is None:
            return SETUP
        return STOPPED if self._collection_stopped is not None else COLLECTING


# ── session_status ──────────────────────────────────────────────────────

def test_status_healthy(tmp_path):
    p = _Panel(tmp_path, devices=[_Dev("Shimmer", _Memo("Shimmer"))])
    st = api.session_status(p)
    assert st["phase"] == "COLLECTING"
    assert st["worst_level"] == 0
    assert st["session"]["sub_id"] == "sub-1001"
    assert st["devices"][0]["sources"][0]["level_name"] == "NONE"
    assert st["events_file"].endswith("events.jsonl")


def test_status_worst_level_from_lost_recorded_stream(tmp_path):
    dev = _Dev("MSense", _Memo("MSense", sts="🟢"), streams={"MSense [x]": "MSense"})
    rec = _Rec(streams=[_stream("MSense [x]", health="🔴 lost")])
    p = _Panel(tmp_path, devices=[dev], recorder=rec)
    st = api.session_status(p)
    assert st["worst_level"] == int(Level.L3)
    assert st["worst_category"] == "warning"
    src = st["devices"][0]["sources"][0]
    assert src["level"] == int(Level.L3)
    assert src["stream"]["health"] == "🔴 lost"


def test_status_stopped_device_is_warning_midcollection(tmp_path):
    dev = _Dev("MSense", _Memo("MSense", sts="🛑 stopped"))
    p = _Panel(tmp_path, devices=[dev], phase=COLLECTING)
    assert api.session_status(p)["worst_level"] == int(Level.L3)
    # …but expected once the operator has stopped
    p2 = _Panel(tmp_path, devices=[_Dev("MSense", _Memo("MSense", sts="🛑 stopped"))],
                phase=STOPPED)
    assert api.session_status(p2)["worst_level"] == 0


def test_status_recorder_unavailable_is_warning(tmp_path):
    p = _Panel(tmp_path, devices=[_Dev("Shimmer")], recorder=_Rec(state="unavailable"))
    st = api.session_status(p)
    assert st["worst_level"] == int(Level.L3)
    assert st["recorder"]["state"] == "unavailable"


def test_status_orphan_stream_reported(tmp_path):
    rec = _Rec(streams=[_stream("OtherPC-EEG", external=True)])
    p = _Panel(tmp_path, devices=[_Dev("Shimmer")], recorder=rec)
    st = api.session_status(p)
    assert st["other_recorded_streams"][0]["name"] == "OtherPC-EEG"


def test_status_folds_sqc_rejection_into_worst_level(tmp_path):
    dev = _Dev("MSense", _Memo("MSense", sts="🟢"),
               sqc={"MSense": {"status": "rejected", "error": "BUSY"}})
    p = _Panel(tmp_path, devices=[dev], phase=SETUP)
    st = api.session_status(p)
    src = st["devices"][0]["sources"][0]
    assert src["level"] == int(Level.L2)
    assert src["category"] == "caution"
    assert "BUSY" in src["level_reason"]
    assert st["worst_level"] == int(Level.L2)


def test_status_folds_live_stream_error(tmp_path):
    dev = _Dev("MSense", _Memo("MSense", sts="🟢"),
               live={"MSense": {"status": "error", "error": "stalled"}})
    p = _Panel(tmp_path, devices=[dev])
    st = api.session_status(p)
    assert st["worst_level"] == int(Level.L2)
    assert "stalled" in st["devices"][0]["sources"][0]["level_reason"]


def test_status_sqc_not_recording_is_advisory_only(tmp_path):
    dev = _Dev("MSense", _Memo("MSense", sts="🟢"),
               sqc={"MSense": {"status": "rejected", "error": "NOT_RECORDING"}})
    p = _Panel(tmp_path, devices=[dev], phase=SETUP)
    st = api.session_status(p)
    assert st["worst_level"] == int(Level.L1)
    assert st["devices"][0]["sources"][0]["category"] == "advisory"


def test_eventlog_emits_fault_for_sqc_error(tmp_path):
    sqc = {"MSense": {"status": "receiving", "error": None}}
    dev = _Dev("MSense", _Memo("MSense", sts="🟢"), sqc=sqc)
    p = _Panel(tmp_path, devices=[dev])
    log = api.SessionEventLog(p, data_dir=str(tmp_path))
    log.tick()                                             # baseline
    sqc["MSense"] = {"status": "error", "error": "no START_ACK within 8s"}
    log.tick()
    faults = [e for e in _load(tmp_path / "events.jsonl") if e["event"] == "fault"]
    assert faults and faults[0]["level"] == int(Level.L2)
    assert "START_ACK" in faults[0]["sts"]


def test_status_survives_missing_session_info(tmp_path):
    p = _Panel(tmp_path)
    del p.session_info
    st = api.session_status(p)
    assert st["session"]["sub_id"] is None


# ── SessionEventLog ────────────────────────────────────────────────────

def _load(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_eventlog_fault_then_recover(tmp_path):
    memo = _Memo("MSense", sts="🟢")
    dev = _Dev("MSense", memo)
    p = _Panel(tmp_path, devices=[dev])
    (tmp_path / "sess").mkdir()
    log = api.SessionEventLog(p, data_dir=str(tmp_path))

    log.tick()                                   # baseline — healthy
    memo.sts = "⚠️ start failed"                  # -> L3
    log.tick()
    memo.sts = "🔄 reconnected"                  # -> healthy
    log.tick()

    rolling = _load(tmp_path / "events.jsonl")
    kinds = [e["event"] for e in rolling]
    assert "fault" in kinds and "recover" in kinds
    fault = next(e for e in rolling if e["event"] == "fault")
    assert fault["device"] == "MSense" and fault["level"] == int(Level.L3)

    # mirrored to the per-session copy + the journaler stream
    assert (tmp_path / "sess" / "events.jsonl").exists()
    assert any(m.startswith("[FAULT]") for m in p.journal_calls)
    assert any(m.startswith("[RECOVER]") for m in p.journal_calls)


def test_eventlog_session_start_stop(tmp_path):
    p = _Panel(tmp_path, devices=[_Dev("Shimmer")], phase=SETUP)
    log = api.SessionEventLog(p, data_dir=str(tmp_path))
    log.tick()
    p._collection_started = 1000.0                # -> COLLECTING
    log.tick()
    p._collection_stopped = 1050.0               # -> STOPPED
    log.tick()
    kinds = [e["event"] for e in _load(tmp_path / "events.jsonl")]
    assert "session_start" in kinds and "session_stop" in kinds


def test_eventlog_read_filters_by_level_and_ts(tmp_path):
    memo = _Memo("MSense", sts="🟢")
    p = _Panel(tmp_path, devices=[_Dev("MSense", memo)])
    log = api.SessionEventLog(p, data_dir=str(tmp_path))
    log.tick()
    memo.sts = "🔌 disconnected"                  # -> L2
    log.tick()
    all_ev = log.read(0.0, 0)
    assert all_ev
    hi = log.read(0.0, int(Level.L3))
    assert all(e["level"] >= int(Level.L3) for e in hi)
    future = log.read(all_ev[-1]["ts"] + 1, 0)
    assert future == []


# ── control wrappers ──────────────────────────────────────────────────

def test_start_session_reports_device_init_failure(tmp_path):
    p = _Panel(tmp_path, phase=SETUP)
    calls = []
    p.get_participant_encoding = lambda s, e: calls.append(("enc", s, e))
    p._set_record_lsl = lambda v: calls.append(("rec", v))

    def _boom(devices):
        raise RuntimeError("device XYZ not in catalog")
    p.init_devices = _boom
    p.start_collection = lambda: calls.append(("start",))
    p._event_log = api.SessionEventLog(p, data_dir=str(tmp_path))

    res = api.start_session(p, "sub-1", "ses-0", ["XYZ"], record=False)
    assert res["ok"] is False
    assert any("device XYZ" in e for e in res["errors"])
    assert ("enc", "sub-1", "ses-0") in calls


def test_mark_rejects_empty(tmp_path):
    p = _Panel(tmp_path)
    assert api.mark(p, "   ")["ok"] is False
    assert api.mark(p, "hello")["ok"] is True
    assert "hello" in p.journal_calls
