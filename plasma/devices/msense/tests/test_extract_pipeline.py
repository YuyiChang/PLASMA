"""The offline extraction pipeline: end-to-end on synthetic bins, and the
load-bearing invariant that it never imports Gradio.
"""
import os
import struct
import subprocess
import sys
import tempfile
import zipfile

import numpy as np
import pandas as pd

from plasma.devices.msense.extract import ExtractionReport, extract_dir, extract_zip
from plasma.devices.msense.extract.options import ExtractionOptions
from plasma.devices.msense.extract.pipeline import get_device_name
from plasma.devices.msense.records import crc32_iso_hdlc


def _ecb2_block(first_tick, first_index):
    b = bytearray(4096)
    b[0:4] = b"ECB2"
    b[4:8] = (first_tick & 0xFFFFFFFF).to_bytes(4, "little")
    b[8:12] = (first_index & 0xFFFFFFFF).to_bytes(4, "little")
    for s in range(1358):
        raw = ((s & 0x3FFFF) << 6)
        o = 16 + 3 * s
        b[o], b[o + 1], b[o + 2] = (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF
    b[12:16] = crc32_iso_hdlc(b, 12, 16).to_bytes(4, "little")
    return bytes(b)


def _w_ecf2(n_blocks, *, chunk_index=0, recording_id=0xABCD, start_index=0, start_tick=1000):
    hdr = bytearray(4096)
    hdr[0:4] = b"ECF2"
    hdr[4:8] = chunk_index.to_bytes(4, "little")
    hdr[8:16] = recording_id.to_bytes(8, "little")
    hdr[16:20] = crc32_iso_hdlc(hdr, 16, 20).to_bytes(4, "little")
    out = bytearray(hdr)
    for k in range(n_blocks):
        out += _ecb2_block(start_tick + 1358 * k, start_index + 1358 * k)
    out += b"\xff" * 4096
    return bytes(out) + b"\x00" * (4 * 1024 * 1024 - len(out))


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


SPB = 1358  # ECB2 samples per block


def test_extract_ecf2_single_file():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ecg1700000000.bin"), "wb") as f:
            f.write(_w_ecf2(3))
        report = extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))

        assert [os.path.basename(p) for p in report.out_paths] == ["ecg.csv"]
        df = pd.read_csv(report.out_paths[0])
        assert list(df.columns[:4]) == ["ECG", "ETAG", "PTAG", "Counter"]
        assert len(df) == 3 * SPB
        assert df["Counter"].iloc[0] == 0 and df["Counter"].iloc[-1] == 3 * SPB - 1
        assert [r.spec.name for r in report.resolutions] == ["block_v2"]
        # CDCT is the filename t0 + Counter/512: starts at t0, monotonic,
        # spans ~n_samples/512 s
        assert df["CDCT"].iloc[0] == 1700000000.0
        assert (np.diff(df["CDCT"]) >= 0).all()
        assert abs((df["CDCT"].iloc[-1] - df["CDCT"].iloc[0]) - (3 * SPB - 1) / 512) < 1e-3


def test_extract_ecf2_multi_chunk_is_time_continuous():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        # two chunks of one recording, sample indices contiguous across the join
        with open(os.path.join(src, "ecg17000000000_0.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=0, start_index=0))
        with open(os.path.join(src, "ecg17000000000_1.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=1, start_index=2 * SPB, start_tick=1000 + 2 * SPB))

        report = extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))
        df = pd.read_csv(report.out_paths[0])

        assert len(df) == 4 * SPB
        # Counter runs 0..4*1358-1 with no restart at the chunk boundary
        assert df["Counter"].tolist() == list(range(4 * SPB))
        # CDCT is one continuous clock — monotonic across the chunk join, no
        # jump back to t0, total span ~= 4*1358/512 s
        cdct = df["CDCT"].to_numpy()
        assert (np.diff(cdct) >= 0).all()
        assert abs((cdct[-1] - cdct[0]) - (4 * SPB - 1) / 512) < 1e-3
        # the boundary row (index 2*1358) is one sample-period after the row before
        b = 2 * SPB
        assert abs((cdct[b] - cdct[b - 1]) - 1 / 512) < 1e-4


def test_extract_ecf2_chunk_gap_is_reported(capsys):
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ecg17000000000_0.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=0, start_index=0))
        with open(os.path.join(src, "ecg17000000000_2.bin"), "wb") as f:  # chunk 1 missing
            f.write(_w_ecf2(2, chunk_index=2, start_index=4 * SPB, start_tick=1000 + 4 * SPB))

        extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))
        assert "missing chunk" in capsys.readouterr().out


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
