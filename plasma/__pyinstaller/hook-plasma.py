"""Best-effort collection of every ``plasma`` submodule + data file.

Device plugins are imported dynamically by ``plasma.plugins`` (dotted strings in
``_STATIC`` / ``_DISCOVERY``), so PyInstaller's static analysis never sees them.

This hook is a **fallback** for downstream apps that freeze ``plasma`` without
using our spec files. The project's own builds do not rely on it: ``app_*.spec``
call ``spec_common.plasma_hiddenimports()`` (a filesystem walk), because
``collect_submodules`` resolves ``plasma`` through the setuptools editable-install
finder, which under newer setuptools fails to enumerate the ``plasma.devices.*``
sub-packages.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("plasma")
datas = collect_data_files("plasma")
