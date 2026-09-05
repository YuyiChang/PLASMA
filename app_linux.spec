# -*- mode: python ; coding: utf-8 -*-
#
# Device plugins are imported dynamically by plasma.plugins, so they are pulled
# in by plasma/__pyinstaller/hook-plasma.py (auto-discovered via the `pyinstaller40`
# entry point once `pip install -e .` has run) — no hand hiddenimports list here.
import glob
import os
import shutil
import tempfile

from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('gradio_client')
datas += collect_data_files('gradio')
datas += collect_data_files('safehttpx')
datas += collect_data_files('groovy')

# liblsl is not bundled in the Linux pylsl wheel — get it from conda
# (`conda install -c conda-forge liblsl`) or a distro package. pylsl's
# package-scope loader only checks for `liblsl.so` / `lsl.so` (no versioned
# soname), so the file we bundle into pylsl/lib must have exactly that name.
_roots = [
    os.path.join(os.environ.get('CONDA_PREFIX', ''), 'lib'),
    '/usr/lib/x86_64-linux-gnu',
    '/usr/local/lib',
    '/usr/lib',
    '/lib/x86_64-linux-gnu',
]
_liblsl = None
if os.environ.get('PYLSL_LIB') and os.path.isfile(os.environ['PYLSL_LIB']):
    _liblsl = os.environ['PYLSL_LIB']
else:
    _plain = next((os.path.join(r, 'liblsl.so') for r in _roots
                   if os.path.isfile(os.path.join(r, 'liblsl.so'))), None)
    if _plain:
        _liblsl = _plain
    else:
        _versioned = sorted(g for r in _roots
                            for g in glob.glob(os.path.join(r, 'liblsl.so.*')))
        if _versioned:
            # copy the real object out under the plain `liblsl.so` name so the
            # loader picks it up from pylsl/lib
            _liblsl = os.path.join(tempfile.mkdtemp(), 'liblsl.so')
            shutil.copy2(os.path.realpath(_versioned[-1]), _liblsl)

if _liblsl is None:
    raise FileNotFoundError(
        "liblsl.so not found. Install it with conda "
        "(`conda install -c conda-forge liblsl`), a distro package, or download "
        "it from the liblsl releases page: https://github.com/sccn/liblsl/releases"
    )

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(_liblsl, 'pylsl/lib')],
    datas=datas,
    hiddenimports=["pylsl", "pupil_labs", "plasma.lsl_recorder",
                   "plasma.xdf_writer", "plasma.lsl_util"],
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
    name='PLASMA_Linux_x64',
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
