"""The offline extraction pipeline: end-to-end on synthetic bins, and the
load-bearing invariant that it never imports Gradio.
"""
import os
import struct
import subprocess
import sys
import tempfile

from plasma.devices.msense.extract import ExtractionReport, extract_dir
from plasma.devices.msense.extract.options import ExtractionOptions


def _w_ppg_v2(n, start=0, step=2):
    return b"".join(struct.pack("<5I", 100 + i, 200 + i, 300 + i, 400 + i,
                                start + i * step) for i in range(n))


def _w_ac_v2(n, start=0, step=16):
    return b"".join(struct.pack("<3h4fI", i, -i, i * 2, 0.1, 0.2, 0.3, 0.01,
                                start + i * step) for i in range(n))


def test_pure_pipeline_never_imports_gradio():
    code = (
        "import sys;"
        "import plasma.devices.msense.formats;"
        "import plasma.devices.msense.detect;"
        "import plasma.devices.msense.extract.pipeline;"
        "import plasma.devices.msense.extract.clocksync;"
        "import plasma.devices.msense.extract.options;"
        "assert 'gradio' not in sys.modules, sorted(m for m in sys.modules if 'grad' in m)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_extract_dir_report_shape():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(500))
        with open(os.path.join(src, "ac1700000000.bin"), "wb") as f:
            f.write(_w_ac_v2(500))

        report = extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))

        assert isinstance(report, ExtractionReport)
        names = sorted(os.path.basename(p) for p in report.out_paths)
        assert names == ["ac.csv", "ppg.csv"]
        assert all(os.path.exists(p) for p in report.out_paths)
        assert report.readme_path and os.path.exists(report.readme_path)
        assert report.n_files == 2
        assert "Extracted 2 file(s)" in report.summary()
        assert len(report.resolutions) == 2


def test_extract_dir_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(200))
        extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True, dry_run=True))
        assert os.listdir(out) == []
