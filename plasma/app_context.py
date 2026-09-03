"""Per-app identity + resolved paths for writable state.

PLASMA writes several things to disk with fixed names, all relative to the
process's working directory: the device-config JSON, the gyro-bias JSON, the
``data/`` session tree, the journaler's ``task.txt``, the session log. That is
fine for a repo checkout but wrong for a pip-installed app, and it leaves no way
for a wrapper app (YAMS) to rebrand the LSL journaler stream or keep its state
under its own directory without editing PLASMA source.

This module is the single indirection point. It is **stdlib-only** so it can be
imported from anywhere in the package, including the import-cycle-sensitive
``plasma.plugins`` and ``plasma.devices.msense.config``.

Resolution order for the state directory:
    1. ``$PLASMA_HOME`` if set
    2. the current working directory

A wrapper app calls :func:`configure` once, before ``plasma.__main__.main()``::

    from plasma.app_context import configure
    configure(app_name="YAMS", journal_stream="YAMS",
              config_filename="yams_device_config.json")
"""
import os
from dataclasses import dataclass, replace

__all__ = ["AppContext", "app_context", "configure", "reset"]


@dataclass(frozen=True)
class AppContext:
    app_name: str = "PLASMA"
    home: str = "."
    journal_stream: str = "PLASMA"
    data_dir_name: str = "data"
    config_filename: str = "plasma_device_config.json"
    gyro_bias_filename: str = "plasma_gyro_bias.json"
    task_filename: str = "task.txt"

    @property
    def data_dir(self) -> str:
        return os.path.join(self.home, self.data_dir_name)

    @property
    def config_path(self) -> str:
        return os.path.join(self.home, self.config_filename)

    @property
    def gyro_bias_path(self) -> str:
        return os.path.join(self.home, self.gyro_bias_filename)

    @property
    def task_path(self) -> str:
        return os.path.join(self.home, self.task_filename)


_ctx: AppContext | None = None


def _from_env() -> AppContext:
    home = os.environ.get("PLASMA_HOME") or os.getcwd()
    return AppContext(home=home)


def app_context() -> AppContext:
    """The active context, derived from the environment on first use and cached."""
    global _ctx
    if _ctx is None:
        _ctx = _from_env()
    return _ctx


def configure(**overrides) -> AppContext:
    """Override fields on the active context. Call once, before ``main()``."""
    global _ctx
    base = _ctx if _ctx is not None else _from_env()
    _ctx = replace(base, **overrides)
    return _ctx


def reset() -> None:
    """Drop the cached context so the next :func:`app_context` re-derives it (tests)."""
    global _ctx
    _ctx = None
