# -*- mode: python ; coding: utf-8 -*-
#
# Device plugins are imported dynamically by plasma.plugins (dotted strings), so
# PyInstaller's static analysis can't see them. spec_common.plasma_hiddenimports()
# enumerates the whole `plasma` package from disk — see spec_common.py for why a
# filesystem walk rather than collect_submodules / the pyinstaller40 hook.
import os, sys, pylsl

_SPEC_DIR = globals().get('SPECPATH') or os.path.dirname(os.path.abspath(SPEC))
if _SPEC_DIR not in sys.path:
    sys.path.insert(0, _SPEC_DIR)
from spec_common import common_datas, plasma_datas, plasma_hiddenimports

datas = common_datas() + plasma_datas()


a = Analysis(
    ['app.py'],
    pathex=[_SPEC_DIR],
    binaries=[(os.path.join(os.path.dirname(pylsl.__file__), 'lib'), 'pylsl/lib')],
    datas=datas,
    hiddenimports=["pylsl", "pupil_labs"] + plasma_hiddenimports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cv2'],
    noarchive=False,
    optimize=0,
    module_collection_mode={
        'gradio': 'py',  # Collect gradio package as source .py files
    },
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PLASMA_Windows_x64',
    icon='plasma/resources/icons/plasma.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
