# -*- mode: python ; coding: utf-8 -*-
import os, pylsl
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files('gradio_client')
datas += collect_data_files('gradio')
datas += collect_data_files('safehttpx')
datas += collect_data_files('groovy')


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(os.path.join(os.path.dirname(pylsl.__file__), 'lib'), 'pylsl/lib')],
    datas=datas,
    hiddenimports=[
        "pylsl",
        "pupil_labs",
        # device plugins are imported dynamically — keep in sync with
        # plasma.plugins._STATIC / ._DISCOVERY
        "plasma.plugins",
        "plasma.devices.qb2",
        "plasma.devices.pupil_labs",
        "plasma.devices.shimmer",
        "plasma.devices.obs",
        "plasma.devices.bitalino",
    ] + collect_submodules("plasma.devices.msense"),
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
