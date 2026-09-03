"""PyInstaller hook directory for the ``plasma`` package.

Registered via the ``pyinstaller`` entry point in ``pyproject.toml`` so any
``pyinstaller`` build of an app that imports ``plasma`` picks up
``hook-plasma.py`` automatically — no hand-maintained ``hiddenimports`` list in
the spec files.
"""
import os


def get_hook_dirs():
    return [os.path.dirname(__file__)]
