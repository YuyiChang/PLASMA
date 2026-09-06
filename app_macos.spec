# -*- mode: python ; coding: utf-8 -*-
#
# Device plugins are imported dynamically by plasma.plugins (dotted strings), so
# PyInstaller's static analysis can't see them. spec_common.plasma_hiddenimports()
# enumerates the whole `plasma` package from disk — see spec_common.py for why a
# filesystem walk rather than collect_submodules / the pyinstaller40 hook.
import os
import sys

_SPEC_DIR = globals().get('SPECPATH') or os.path.dirname(os.path.abspath(SPEC))
if _SPEC_DIR not in sys.path:
    sys.path.insert(0, _SPEC_DIR)
from spec_common import common_datas, plasma_datas, plasma_hiddenimports

datas = common_datas() + plasma_datas()

# liblsl is not bundled in the macOS pylsl wheel — find it from conda or homebrew
_liblsl_candidates = [
    os.path.join(os.environ.get('CONDA_PREFIX', ''), 'lib', 'liblsl.dylib'),
    '/opt/homebrew/lib/liblsl.dylib',
    '/usr/local/lib/liblsl.dylib',
]
_liblsl = next((p for p in _liblsl_candidates if os.path.exists(p)), None)
if _liblsl is None:
    raise FileNotFoundError(
        "liblsl.dylib not found. Install via conda (`conda install -c conda-forge liblsl`) "
        "or homebrew (`brew install labstreaminglayer/tap/lsl`)."
    )

a = Analysis(
    ['app.py'],
    pathex=[_SPEC_DIR],
    binaries=[(_liblsl, 'pylsl/lib')],
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
    name='PLASMA_MacOS_arm64',
    icon='plasma/resources/icons/plasma.icns',
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
