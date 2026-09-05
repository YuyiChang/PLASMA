"""The offline extraction pipeline: end-to-end on synthetic bins, and the
load-bearing invariant that it never imports Gradio.
"""
import os
import struct
import subprocess
import sys
import tempfile
import zipfile

from plasma.devices.msense.extract import ExtractionReport, extract_dir, extract_zip
from plasma.devices.msense.extract.options import ExtractionOptions
from plasma.devices.msense.extract.pipeline import get_device_name


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


# ── uuid.txt device Name → per-device folder ────────────────────────────────

_UUID_V5 = (
    "EA:94:11:E5:D4:34 (random)\n"
    "Name: MSense4ECG-EX4BT\n"
    "Device ID: 74A3A7257D5D5F0F\n"
    "Version: 5.0.2 NAND\n"
)


def test_get_device_name_parses_name_field(tmp_path):
    (tmp_path / "uuid.txt").write_text(_UUID_V5)
    assert get_device_name(str(tmp_path)) == "MSense4ECG-EX4BT"
    assert get_device_name(str(tmp_path / "uuid.txt")) == "MSense4ECG-EX4BT"


def test_get_device_name_none_without_name_line(tmp_path):
    (tmp_path / "uuid.txt").write_text("EA:94:11:E5:D4:34 (random)\nVersion: 4.6.3\n")
    assert get_device_name(str(tmp_path)) is None
    assert get_device_name(str(tmp_path / "missing")) is None


def test_get_device_name_sanitises(tmp_path):
    (tmp_path / "uuid.txt").write_text("NAME = weird / name!!\n")
    assert get_device_name(str(tmp_path)) == "weird_name"


def test_extract_zip_renames_device_folders_to_uuid_name(tmp_path):
    src = tmp_path / "src"
    for mac, name in [("D8-92-0E-46-16-0D", "MSense4PPG-KA5SA"),
                      ("EA-94-11-E5-D4-34", "MSense4ECG-EX4BT")]:
        d = src / mac
        d.mkdir(parents=True)
        (d / "ppg1700000000.bin").write_bytes(_w_ppg_v2(200))
        (d / "ac1700000000.bin").write_bytes(_w_ac_v2(200))
        (d / "uuid.txt").write_text(f"{mac.replace('-', ':')} (random)\nName: {name}\nVersion: 5.1.4\n")

    zip_path = tmp_path / "2609051705_msense.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for root, _d, files in os.walk(src):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, src))

    out_zip = extract_zip(str(zip_path), out_dir=str(tmp_path / "out"),
                          options=ExtractionOptions(ignore_id_parsing=True))
    with zipfile.ZipFile(out_zip) as z:
        tops = sorted({n.split("/")[0] for n in z.namelist() if "/" in n})
    assert tops == ["MSense4ECG-EX4BT", "MSense4PPG-KA5SA"]
