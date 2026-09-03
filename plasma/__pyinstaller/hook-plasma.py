"""Collect every ``plasma`` submodule + data file.

Device plugins are imported dynamically by ``plasma.plugins`` (a string in
``_STATIC`` / ``_DISCOVERY``), so PyInstaller's static analysis never sees them.
``collect_submodules`` sweeps the whole package instead, which keeps this in sync
with the plugin registry automatically.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("plasma")
datas = collect_data_files("plasma")
