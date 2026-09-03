"""Test-wide isolation: every test gets its own throwaway app-state directory.

Without this, `plasma.app_context` resolves `home` to the current working
directory, so tests that construct `DeviceConfig`, save a gyro bias, or open a
journaler would read/write the developer's real `plasma_device_config.json` /
`plasma_gyro_bias.json` / `data/` in the repo root.
"""
import pytest

from plasma import app_context


@pytest.fixture(autouse=True)
def _isolate_app_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASMA_HOME", str(tmp_path))
    app_context.reset()
    yield
    app_context.reset()
