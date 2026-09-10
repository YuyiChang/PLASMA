"""Restart / shut-down plumbing: the relaunch argv, the process-exit primitive,
and IntegratedPanel's exit hook that flushes a running recording."""
import os
import sys

import pytest

from plasma import __main__ as m
from plasma.integrated_panel import IntegratedPanel


# ── build_blocks ───────────────────────────────────────────────────────────

def test_build_blocks_assembles_the_ui(monkeypatch):
    """build_blocks() (shared by main() and scripts/capture_screenshots.py)
    returns a gr.Blocks with every core tab + the headless API registered."""
    import gradio as gr
    from plasma import plugins
    from plasma.config import device_config

    plugins.load_plugins()
    device_config.refresh_defaults()
    ip = IntegratedPanel()
    app = m.build_blocks(ip)
    assert isinstance(app, gr.Blocks)
    assert hasattr(ip, "_event_log")          # plasma_api.register(ip) ran


# ── _relaunch_argv ──────────────────────────────────────────────────────────

def test_relaunch_argv_from_source(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/plasma/__main__.py", "--foo"])
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert m._relaunch_argv() == [sys.executable, "-m", "plasma", "--foo"]


def test_relaunch_argv_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/PLASMA_MacOS_arm64")
    monkeypatch.setattr(sys, "argv", ["/opt/PLASMA_MacOS_arm64", "--bar"])
    assert m._relaunch_argv() == ["/opt/PLASMA_MacOS_arm64", "--bar"]


# ── shutdown() ─────────────────────────────────────────────────────────────

class _Halt(Exception):
    pass


class _FakeApp:
    def __init__(self, calls):
        self._calls = calls

    def close(self):
        self._calls.append(("app.close",))


@pytest.fixture
def traced_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(m.atexit, "_run_exitfuncs", lambda: calls.append(("hooks",)))
    monkeypatch.setattr(m, "app", _FakeApp(calls))

    def _execv(path, argv):
        calls.append(("execv", path, tuple(argv)))
        raise _Halt

    def _exit(code):
        calls.append(("_exit", code))
        raise _Halt

    monkeypatch.setattr(m.os, "execv", _execv)
    monkeypatch.setattr(m.os, "_exit", _exit)
    monkeypatch.setattr(m.os, "name", "posix")
    return calls


def test_shutdown_runs_hooks_closes_server_then_exits(traced_exit):
    with pytest.raises(_Halt):
        m.shutdown(restart=False)
    assert traced_exit == [("hooks",), ("app.close",), ("_exit", 0)]


def test_shutdown_restart_runs_hooks_then_execs(traced_exit, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/plasma/__main__.py"])
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(_Halt):
        m.shutdown(restart=True)
    assert traced_exit[0] == ("hooks",)
    assert traced_exit[1] == ("app.close",)
    kind, path, argv = traced_exit[2]
    assert kind == "execv"
    assert argv == (sys.executable, "-m", "plasma")


# ── IntegratedPanel._atexit_cleanup ────────────────────────────────────────

class _Spy:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def disconnect(self):
        self.calls.append("disconnect")


def _bare_panel():
    return IntegratedPanel.__new__(IntegratedPanel)


def test_atexit_cleanup_flushes_recorder_and_devices():
    p = _bare_panel()
    rec, dev = _Spy(), _Spy()
    p.lsl_recorder = rec
    p.available_devices = [dev]

    p._atexit_cleanup()

    assert rec.calls == ["stop"]
    assert dev.calls == ["stop", "disconnect"]


def test_atexit_cleanup_noop_when_idle():
    p = _bare_panel()
    p.lsl_recorder = None
    p.available_devices = []
    p._atexit_cleanup()          # must not raise


def test_atexit_cleanup_swallows_errors():
    p = _bare_panel()

    class _Boom:
        def stop(self):
            raise RuntimeError("boom")

        def disconnect(self):
            raise RuntimeError("boom")

    p.lsl_recorder = _Boom()
    p.available_devices = [_Boom()]
    p._atexit_cleanup()          # must not raise


# ── get_visual_sources: two sub-sources may share a display label ───────────

def test_get_visual_sources_disambiguates_repeated_labels():
    class _Memo:
        def __init__(self, label):
            self.label = label
            self.channels = {"ENMO": [1, 2, 3]}

    class _Dev:
        tag = "msense"
        memo = {"E4:B0:AA": _Memo("MSense4PPG"), "E4:B0:BB": _Memo("MSense4PPG")}

        def get_sources(self):
            return self.memo

    p = _bare_panel()
    p.available_devices = [_Dev()]
    sources = p.get_visual_sources()
    assert len(sources) == 2                       # neither wristband dropped
    assert "MSense4PPG" in sources
    assert any(k != "MSense4PPG" and k.startswith("MSense4PPG") for k in sources)
