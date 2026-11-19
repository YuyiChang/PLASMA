"""Release gates and bounded process cleanup, using a fake frozen server."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import time

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".github" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ref,expected", [
    ("refs/tags/v2.3.2", 0), ("refs/tags/v2.3.1", 1),
    ("refs/heads/dev", 0), ("refs/tags/nightly", 0)])
def test_release_guard_rejects_version_mismatch_before_build(ref, expected, tmp_path):
    (tmp_path / "plasma").mkdir()
    (tmp_path / "plasma/__init__.py").write_text('__version__ = "2.3.2"\n')
    assert load_script("check_release_version").check(ref, tmp_path) == expected


def test_pypi_tests_and_artifacts_are_gated_by_early_version_check():
    jobs = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())["jobs"]
    assert jobs["test"]["needs"] == "check-version"
    assert "needs" not in jobs["check-version"]
    assert jobs["build"]["needs"] == "test"
    assert "needs.check-version.result == 'success'" in jobs["publish"]["if"]
    assert "skipped" not in jobs["publish"]["if"]


@pytest.fixture
def fake_app(tmp_path):
    """An executable starts a child which ignores TERM, like a stuck bootstrap."""
    if os.name == "nt":
        pytest.skip("POSIX executable fixture; Windows cleanup command is tested separately")
    app = tmp_path / "fake-plasma"
    app.write_text(f"#!{sys.executable}\n" + '''
import http.server, os, pathlib, signal, subprocess, sys, time
assert pathlib.Path.cwd() == pathlib.Path(os.environ['PLASMA_HOME'])
assert os.environ['PLASMA_NO_BROWSER'] == '1'
assert os.environ['GRADIO_ANALYTICS_ENABLED'] == 'False'
if os.environ.get('SMOKE_SCENARIO') == 'never-ready':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(.1)
subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if os.environ.get('SMOKE_SCENARIO') == 'stalled-http': time.sleep(60)
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args): pass
if os.environ.get('SMOKE_SCENARIO') == 'import-error':
    print('No module named missing_device', flush=True)
http.server.HTTPServer(('127.0.0.1', int(os.environ['GRADIO_SERVER_PORT'])), Handler).serve_forever()
''')
    app.chmod(app.stat().st_mode | stat.S_IXUSR)
    return app


@pytest.mark.parametrize("scenario,expected", [
    ("ready", 0), ("never-ready", 1), ("stalled-http", 1), ("import-error", 1)])
def test_startup_http_and_ignored_shutdown_are_all_bounded(fake_app, monkeypatch, scenario, expected):
    monkeypatch.setenv("SMOKE_SCENARIO", scenario)
    start = time.monotonic()
    assert load_script("smoke_test").smoke(fake_app, startup_timeout=1, shutdown_timeout=.1) == expected
    assert time.monotonic() - start < 6


def test_windows_cleanup_uses_native_pid_and_child_tree(monkeypatch):
    from unittest.mock import Mock
    from types import SimpleNamespace
    module = load_script("smoke_test")
    # Replace the module's OS facade, leaving pytest's real platform intact.
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    process = Mock(pid=12345)
    assert module.stop_tree(process)
    assert run.call_args.args[0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert run.call_args.kwargs["timeout"] == 10


def test_release_uses_one_mac_allocation_without_dropping_lsl_regression():
    jobs = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())["jobs"]
    assert jobs["test-lsl-macos"]["if"] == "github.event_name == 'pull_request'"
    assert jobs["build-macos"]["needs"] == "test"
    assert "MACOS_ARM64_RUNNER" in jobs["build-macos"]["runs-on"]
    smoke_steps = [step for name, job in jobs.items() for step in job.get("steps", [])
                   if "Smoke test" in step.get("name", "")]
    assert all(step["timeout-minutes"] == 5 for step in smoke_steps)
