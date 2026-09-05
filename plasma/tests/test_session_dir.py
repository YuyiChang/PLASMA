"""The one unified per-session directory: IntegratedPanel.start_collection()
computes it once and injects it into every device; MotionSenseHRV routes its
.txt dump into <session>/msense/, with a fallback when run outside the panel."""
import logging
import os
import re
from types import SimpleNamespace

import pytest

from plasma.integrated_panel import IntegratedPanel
from plasma.lsl_session import SessionInfo


# ── MotionSenseHRV._resolve_log_dir ──────────────────────────────────────

def test_resolve_log_dir_prefers_injected_session_dir():
    from plasma.devices.msense.device import MotionSenseHRV
    fake = SimpleNamespace(session_dir="/tmp/sess",
                           session_info={"sub_id": "sub-1", "ses_id": "ses-0",
                                         "participant_enc": 100})
    assert MotionSenseHRV._resolve_log_dir(fake) == os.path.join("/tmp/sess", "msense")


def test_resolve_log_dir_fallback_when_no_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASMA_HOME", str(tmp_path))
    from plasma import app_context
    app_context.reset()
    from plasma.devices.msense.device import MotionSenseHRV
    fake = SimpleNamespace(session_info={"sub_id": "sub-1", "ses_id": "ses-0",
                                         "participant_enc": 100})
    got = MotionSenseHRV._resolve_log_dir(fake)
    assert re.fullmatch(
        re.escape(os.path.join(str(tmp_path), "data", "sub-1", "ses-0", "100_"))
        + r"\d{6}_\d{4}", got)
    app_context.reset()


# ── start_collection() threads one dir to every device ───────────────────

class _FakeDev:
    def __init__(self, tag):
        self.tag = tag
        self.session_dir = None
        self.session_info = None
        self.started = False

    def start(self):
        # capture what the panel injected, at the moment start() runs
        self.started = self.session_info


def _panel(tmp_path):
    p = IntegratedPanel.__new__(IntegratedPanel)
    p.logger = logging.getLogger("test-session-dir")
    p.log_root = str(tmp_path / "data")
    p.session_info = SessionInfo("sub-1000", "ses-00", 100000, p.log_root)
    p.available_devices = []
    p.record_lsl = False
    p.lsl_recorder = None
    return p


def test_start_collection_injects_one_session_dir(tmp_path):
    p = _panel(tmp_path)
    d1, d2 = _FakeDev("a"), _FakeDev("b")
    p.available_devices = [d1, d2]

    p.start_collection()

    assert os.path.isdir(p.session_dir)
    assert d1.session_dir == d2.session_dir == p.session_dir
    assert d1.started and d2.started
    assert p.session_dir.startswith(p.session_info["log_dir"])
    assert re.search(r"100000_\d{6}_\d{6}$", p.session_dir)
    assert p.sts == "Collection in progress"


def test_start_collection_repoints_stale_session_info(tmp_path):
    """A device Initialized with one encoding must pick up a later ID change
    at Start (the participant-encoding-not-written bug)."""
    p = _panel(tmp_path)
    dev = _FakeDev("msense")
    dev.session_info = SessionInfo("sub-1000", "ses-00", 100000, str(tmp_path / "data"))
    p.available_devices = [dev]

    # operator edits the IDs after Initialize -> panel rebuilds session_info
    p.get_participant_encoding("sub-2345", "ses-06")
    assert p.session_info["participant_enc"] == 234506

    p.start_collection()

    # what the device saw when start() ran, not the Initialize-time value
    assert dev.session_info["participant_enc"] == 234506
    assert dev.started["participant_enc"] == 234506


def test_recorder_gets_the_same_session_dir(tmp_path, monkeypatch):
    p = _panel(tmp_path)
    p.record_lsl = True
    seen = {}

    class _StubRecorder:
        def __init__(self, logger=None):
            pass

        def start(self, session_dir):
            seen["dir"] = session_dir
            return True

        def status(self):
            return {"file": os.path.join(seen["dir"], "x.xdf")}

    monkeypatch.setattr("plasma.lsl_recorder.SessionRecorder", _StubRecorder)

    p.start_collection()

    assert seen["dir"] == p.session_dir
