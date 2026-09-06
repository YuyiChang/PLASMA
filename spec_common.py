"""Shared helpers for ``app_linux.spec`` / ``app_macos.spec`` / ``app_windows.spec``.

Why a filesystem walk instead of ``PyInstaller.utils.hooks.collect_submodules`` /
``collect_data_files`` / the ``pyinstaller40`` entry-point hook: those resolve the
``plasma`` package through the setuptools (>=64) editable-install finder
(``__editable__.plasma_app-*.finder``). Under newer setuptools that finder does
not enumerate sub-packages the way ``pkgutil.iter_modules`` expects, so
``plasma.devices.*`` — the device plugins, which ``plasma.plugins`` imports by
dotted string — silently dropped out of the frozen build and MSense went missing.

The walk below is independent of how (or whether) ``plasma-app`` is pip-installed.

This module MUST NOT import PyInstaller at top level: ``plasma/tests`` imports it
in the CI ``test`` job, which has no ``pyinstaller`` installed.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.join(HERE, "plasma")

# Directory names never collected as part of the frozen package.
_SKIP_DIRS = {"__pycache__", "tests", "__pyinstaller", ".git", ".pytest_cache"}
# Module stems (``<stem>.py``) never added as hidden imports: dunder entry
# points and the standalone dev/probe scripts that are "NOT wired into the app".
_SKIP_STEMS = {"__main__", "bleak_probe", "bleak_reconnect_test"}


def _is_skipped_stem(stem):
    return (stem in _SKIP_STEMS
            or stem.startswith("test_") or stem.endswith("_test"))


def _iter_package_dirs():
    """Yield (dirpath, filenames) for every real ``plasma`` sub-package on disk.

    A directory counts only if it and every ancestor up to ``plasma`` has an
    ``__init__.py`` (so data dirs like ``plasma/resources`` are skipped)."""
    for dirpath, dirnames, filenames in os.walk(_PKG_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "__init__.py" not in filenames:
            # not a package — don't descend into it
            dirnames[:] = []
            continue
        yield dirpath, filenames


def _dotted(dirpath):
    return ".".join(os.path.relpath(dirpath, HERE).split(os.sep))


def plasma_hiddenimports():
    """Every importable module under ``./plasma``, as sorted dotted names."""
    mods = set()
    for dirpath, filenames in _iter_package_dirs():
        pkg = _dotted(dirpath)
        if not all(p.isidentifier() for p in pkg.split(".")):
            continue
        mods.add(pkg)
        for fn in filenames:
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            stem = fn[:-3]
            if _is_skipped_stem(stem) or not stem.isidentifier():
                continue
            mods.add(f"{pkg}.{stem}")
    return sorted(mods)


def plasma_datas():
    """Non-``.py`` files under ``./plasma`` (docs, icons) as (src, dest_dir)."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(_PKG_ROOT):
        _dirnames[:] = [d for d in _dirnames if d not in _SKIP_DIRS]
        dest = os.path.relpath(dirpath, HERE)
        for fn in filenames:
            if fn.endswith((".py", ".pyc")) or fn == ".DS_Store":
                continue
            out.append((os.path.join(dirpath, fn), dest))
    return out


def common_datas():
    """gradio/… data files shared by every spec. PyInstaller-only."""
    from PyInstaller.utils.hooks import collect_data_files

    d = []
    for pkg in ("gradio_client", "gradio", "safehttpx", "groovy"):
        d += collect_data_files(pkg)
    return d
