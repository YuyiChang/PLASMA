"""`plasma.app_context` — env resolution, `configure()` overrides, path composition."""
import os

from plasma import app_context
from plasma.app_context import AppContext, configure


def test_defaults_derive_from_plasma_home(tmp_path, monkeypatch):
    # the autouse fixture already points PLASMA_HOME at a tmp dir; override it
    monkeypatch.setenv("PLASMA_HOME", str(tmp_path))
    app_context.reset()
    ctx = app_context.app_context()
    assert ctx.app_name == "PLASMA"
    assert ctx.journal_stream == "PLASMA"
    assert ctx.home == str(tmp_path)
    assert ctx.config_path == os.path.join(str(tmp_path), "plasma_device_config.json")
    assert ctx.data_dir == os.path.join(str(tmp_path), "data")
    assert ctx.gyro_bias_path == os.path.join(str(tmp_path), "plasma_gyro_bias.json")
    assert ctx.task_path == os.path.join(str(tmp_path), "task.txt")


def test_falls_back_to_cwd_without_plasma_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PLASMA_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    app_context.reset()
    assert app_context.app_context().home == str(tmp_path)


def test_configure_overrides_and_is_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASMA_HOME", str(tmp_path))
    app_context.reset()
    configure(app_name="YAMS", journal_stream="YAMS",
              config_filename="yams_device_config.json")
    ctx = app_context.app_context()
    assert ctx.app_name == "YAMS"
    assert ctx.journal_stream == "YAMS"
    assert ctx.config_path.endswith("yams_device_config.json")
    # home still came from the environment
    assert ctx.home == str(tmp_path)


def test_configure_rebrands_the_journaler_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASMA_HOME", str(tmp_path))
    app_context.reset()
    configure(journal_stream="YAMS")
    from plasma import journal
    # open_journal_outlet reads the name at call time; assert it's what we set
    # (it may still return None if liblsl is unavailable — that's fine)
    assert app_context.app_context().journal_stream == "YAMS"
    _ = journal.open_journal_outlet()


def test_reset_clears_the_cache(monkeypatch):
    configure(app_name="X")
    assert app_context.app_context().app_name == "X"
    app_context.reset()
    assert app_context.app_context().app_name == "PLASMA"


def test_appcontext_is_frozen():
    ctx = AppContext()
    try:
        ctx.home = "/elsewhere"
    except Exception as e:
        assert e.__class__.__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("AppContext should be immutable")
