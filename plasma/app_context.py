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
    2. when running as a frozen (PyInstaller) bundle — a per-user
       application-data directory:
         * Windows: ``%LOCALAPPDATA%\\PLASMA``
         * macOS:   ``~/Library/Application Support/PLASMA``
         * Linux:   ``$XDG_DATA_HOME/plasma`` (or ``~/.local/share/plasma``)
    3. otherwise (repo checkout / ``pip install -e .`` / running from source) —
       the current working directory

Resolution happens once, lazily, on the first :func:`app_context` call and is
then cached — so ``$PLASMA_HOME`` / the working directory must be right before
anything in ``plasma`` is imported (``plasma.config`` builds its module-level
singleton at import time). A wrapper app calls :func:`configure` once, before
``plasma.__main__.main()`` — that path re-derives the cache so it works even
after import::

    from plasma.app_context import configure
    configure(app_name="YAMS", journal_stream="YAMS",
              config_filename="yams_device_config.json")
"""
import os
import sys
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

_DEFAULT_APP_DIR = "PLASMA"


def _user_data_dir(app: str = _DEFAULT_APP_DIR) -> str:
    """Per-user, per-OS application-data directory (stdlib, no platformdirs)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, app)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", app)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, app.lower())


def _is_frozen() -> bool:
    """True when running inside a PyInstaller / py2app / cx_Freeze bundle."""
    return getattr(sys, "frozen", False)


def _from_env() -> AppContext:
    env = os.environ.get("PLASMA_HOME")
    if env:
        return AppContext(home=env)
    if _is_frozen():
        return AppContext(home=_user_data_dir())
    return AppContext(home=os.getcwd())


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
