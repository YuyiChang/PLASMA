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


# ── ACF3 (ac:v3) container writer — header + ACB1 blocks + ACT2 terminal ──────
AC_V3_REGION = 4 * 1024 * 1024 - 2 * 4096


def _acf3_crc(buf, off, length):
    import zlib
    patched = bytearray(buf)
    patched[off:off + length] = bytes(length)
    return zlib.crc32(bytes(patched)) & 0xFFFFFFFF


def _acf3_header():
    h = bytearray(4096)
    struct.pack_into("<4sHH", h, 0, b"ACF3", 3, 2)
    struct.pack_into("<II", h, 8, 1125, 2)       # ODR 1125/2 = 562.5 Hz
    struct.pack_into("<HH", h, 16, 2, 16384)
    struct.pack_into("<I", h, 24, 512)           # anchor clock Hz
    struct.pack_into("<I", h, 28, 32)
    struct.pack_into("<BBBB", h, 32, 0, 0, 1, 32)
    struct.pack_into("<I", h, 20, _acf3_crc(bytes(h), 20, 4))
    return bytes(h)


def _acf3_block(first_seq, n=680, anchor_tick=0):
    body = bytearray(16)
    struct.pack_into("<4sII", body, 0, b"ACB1", anchor_tick, first_seq)
    body += b"".join(struct.pack("<3h", (i * 2) | (i & 1), 1000 + i, -1000 - i)
                     for i in range(n))
    struct.pack_into("<I", body, 12, _acf3_crc(bytes(body), 12, 4))
    return bytes(body)


def _acf3_terminal(valid_len):
    t = bytearray(4096)
    struct.pack_into("<4sI", t, 0, b"ACT2", valid_len)
    struct.pack_into("<I", t, 8, _acf3_crc(bytes(t), 8, 4))
    return bytes(t)


def _w_acf3(first_seq, n_blocks=2):
    blocks = [_acf3_block(first_seq + 680 * k) for k in range(n_blocks)]
    region = b"".join(blocks)
    region += bytes(AC_V3_REGION - len(region))
    return _acf3_header() + region + _acf3_terminal(sum(len(b) for b in blocks))


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

        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))

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
        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))

        assert [os.path.basename(p) for p in report.out_paths] == ["ecg.csv"]
        df = pd.read_csv(report.out_paths[0])
        # output columns are locked to ECG_EXTRACTION_HANDOFF.md's schema —
        # no CDCT/init_CDCT/Datetime/Counter
        assert list(df.columns) == ["SampleIndex", "RtcTick", "ECG", "ETAG", "PTAG"]
        assert len(df) == 3 * SPB
        assert df["SampleIndex"].iloc[0] == 0 and df["SampleIndex"].iloc[-1] == 3 * SPB - 1
        assert df["RtcTick"].iloc[0] == 1000 and df["RtcTick"].iloc[-1] == 1000 + 3 * SPB - 1
        assert [r.spec.name for r in report.resolutions] == ["block_v2"]


def test_extract_ecf2_multi_chunk_is_time_continuous():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        # two chunks of one recording, sample indices contiguous across the join
        with open(os.path.join(src, "ecg17000000000_0.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=0, start_index=0))
        with open(os.path.join(src, "ecg17000000000_1.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=1, start_index=2 * SPB, start_tick=1000 + 2 * SPB))

        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))
        df = pd.read_csv(report.out_paths[0])

        assert len(df) == 4 * SPB
        # SampleIndex runs 0..4*1358-1 with no restart at the chunk boundary
        assert df["SampleIndex"].tolist() == list(range(4 * SPB))
        # RtcTick is one continuous clock — monotonic across the chunk join,
        # exactly 1 tick/sample throughout (tick rate == sample rate)
        tick = df["RtcTick"].to_numpy()
        assert (np.diff(tick) == 1).all()
        assert tick[0] == 1000 and tick[-1] == 1000 + 4 * SPB - 1


def test_extract_ecf2_chunk_gap_is_reported(capsys):
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ecg17000000000_0.bin"), "wb") as f:
            f.write(_w_ecf2(2, chunk_index=0, start_index=0))
        with open(os.path.join(src, "ecg17000000000_2.bin"), "wb") as f:  # chunk 1 missing
            f.write(_w_ecf2(2, chunk_index=2, start_index=4 * SPB, start_tick=1000 + 4 * SPB))

        extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))
        assert "missing chunk" in capsys.readouterr().out


def test_extract_ac_v3_single_file():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        blocks = [_acf3_block(680 * k, anchor_tick=1000 + 680 * k) for k in range(2)]
        with open(os.path.join(src, "ac1700000000.bin"), "wb") as f:
            region = b"".join(blocks) + bytes(AC_V3_REGION - sum(len(b) for b in blocks))
            f.write(_acf3_header() + region + _acf3_terminal(sum(len(b) for b in blocks)))

        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))
        df = pd.read_csv(report.out_paths[0])
        # output columns are locked to ECG_EXTRACTION_HANDOFF.md's schema —
        # no CDCT/init_CDCT/Datetime/Counter
        assert list(df.columns) == ["SampleSequence", "RtcTickEstBlock", "AccX", "AccY", "AccZ",
                                    "RtcTickEst"]
        assert len(df) == 2 * 680
        assert df["SampleSequence"].iloc[0] == 0 and df["SampleSequence"].iloc[-1] == 2 * 680 - 1
        # AccX/Y/Z are already in g (raw count / 16384)
        assert abs(df["AccX"].iloc[0] - 0 / 16384.0) < 1e-12
        assert abs(df["AccY"].iloc[0] - 1000 / 16384.0) < 1e-12
        # RtcTickEstBlock is the block's own anchor, shared by every sample in it
        assert (df["RtcTickEstBlock"].iloc[:680] == 1000).all()
        assert (df["RtcTickEstBlock"].iloc[680:] == 1680).all()
        # RtcTickEst ramps linearly from block 0's anchor to block 1's, at
        # exactly (1680-1000)/680 = 1.0 ticks/sample, landing exactly on
        # 1680.0 at the first sample of block 1
        assert abs(df["RtcTickEst"].iloc[0] - 1000.0) < 1e-9
        assert abs(df["RtcTickEst"].iloc[680] - 1680.0) < 1e-9
        assert (np.diff(df["RtcTickEst"]) >= 0).all()


def test_extract_ac_v3_multichunk_rtc_tick_est_spans_chunks():
    """RtcTickEst must ramp across a chunk boundary — the last block of chunk
    0 ramps toward the first block of chunk 1, not toward nothing."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        b0 = _acf3_block(0, anchor_tick=1000)
        b1 = _acf3_block(680, anchor_tick=1680)          # last block of chunk 0
        b2 = _acf3_block(1360, anchor_tick=2360)         # first block of chunk 1
        region0 = b0 + b1 + bytes(AC_V3_REGION - len(b0) - len(b1))
        region1 = b2 + bytes(AC_V3_REGION - len(b2))
        with open(os.path.join(src, "ac17000000000_0.bin"), "wb") as f:
            f.write(_acf3_header() + region0 + _acf3_terminal(len(b0) + len(b1)))
        with open(os.path.join(src, "ac17000000000_1.bin"), "wb") as f:
            f.write(_acf3_header() + region1 + _acf3_terminal(len(b2)))

        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))
        df = pd.read_csv(report.out_paths[0])

        assert len(df) == 3 * 680
        tick = df["RtcTickEst"].to_numpy()
        assert abs(tick[680] - 1680.0) < 1e-9    # start of the last block of chunk 0
        assert abs(tick[1360] - 2360.0) < 1e-9   # start of chunk 1's block — ramp continues
        assert (np.diff(tick) >= 0).all()


def test_ac_v3_multichunk_boundary_gap_reported(capsys):
    """A first_sample_sequence jump across an ACF3 chunk boundary is a firmware
    drop — _stitch_ac_v3_chunks reports it and still joins the chunks."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        # chunk 0: seq 0..1359 ; chunk 1 resumes at 1400 — 40 samples dropped
        with open(os.path.join(src, "ac17000000000_0.bin"), "wb") as f:
            f.write(_w_acf3(0, n_blocks=2))
        with open(os.path.join(src, "ac17000000000_1.bin"), "wb") as f:
            f.write(_w_acf3(1400, n_blocks=2))

        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))
        assert "40 sample(s) dropped at the chunk 1 boundary" in capsys.readouterr().out

        df = pd.read_csv(report.out_paths[0])
        assert len(df) == 4 * 680            # chunks joined, nothing fabricated
        assert df["SampleSequence"].tolist() == (list(range(0, 1360)) + list(range(1400, 2760)))


def test_datetime_column_matches_vectorized_and_scalar_conversion():
    """The vectorized pd.to_datetime path (replacing a per-row
    datetime.fromtimestamp()/.strftime() loop) must produce the exact same
    string format and values."""
    from datetime import datetime, timezone
    t0 = 1700000000
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, f"ppg{t0}.bin"), "wb") as f:
            f.write(_w_ppg_v2(50))
        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv",
                                                       include_cdct=True))
        df = pd.read_csv(report.out_paths[0])

        expected_first = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
        assert df["Datetime"].iloc[0] == expected_first
        # every row's Datetime matches a fresh scalar conversion of its own CDCT
        for cdct, dt in zip(df["CDCT"].iloc[::7], df["Datetime"].iloc[::7]):
            assert dt == datetime.fromtimestamp(int(cdct), timezone.utc).strftime("%Y/%m/%d %H:%M:%S")


def test_ppg_ac_omit_cdct_by_default():
    """`include_cdct` defaults to False: PPG/AC (legacy/v2, the wristband
    formats) output no longer carries CDCT/init_CDCT/Datetime unless asked."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(50))
        with open(os.path.join(src, "ac1700000000.bin"), "wb") as f:
            f.write(_w_ac_v2(50))
        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv"))

        ppg_df = pd.read_csv([p for p in report.out_paths if p.endswith("ppg.csv")][0])
        ac_df = pd.read_csv([p for p in report.out_paths if p.endswith("ac.csv")][0])
        for df in (ppg_df, ac_df):
            assert "CDCT" not in df.columns
            assert "init_CDCT" not in df.columns
            assert "Datetime" not in df.columns
        # AC unit conversion still happens regardless of include_cdct
        assert ac_df["AccX"].abs().max() < 8.0001


def test_readme_records_ppg_device_file_start_times():
    """A new README table records each PPG-device file's (ppg/ac legacy/v2)
    start time in UTC, independent of `include_cdct` — it's the only place
    that survives once CDCT/Datetime are dropped from the CSV by default.
    ac:v3/ecg:block_v2 (the MSense4ECG chest device) are excluded."""
    from datetime import datetime, timezone
    t0 = 1700000000
    expected_dt = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, f"ppg{t0}.bin"), "wb") as f:
            f.write(_w_ppg_v2(50))
        with open(os.path.join(src, f"ac{t0}.bin"), "wb") as f:
            f.write(_w_ac_v2(50))
        with open(os.path.join(src, f"ecg{t0}.bin"), "wb") as f:
            f.write(_w_ecf2(1))
        report = extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))

        readme = open(report.readme_path).read()
        assert "--- PPG-device file start times (UTC) ---" in readme
        assert f"ppg{t0}.bin: {expected_dt}" in readme
        assert f"ac{t0}.bin: {expected_dt}" in readme
        assert f"ecg{t0}.bin" not in readme.split("--- PPG-device file start times (UTC) ---")[1]


def test_multiple_sessions_under_one_prefix_are_chunk_written():
    """Two distinct recordings (different filename t0, same sensor prefix) go
    through generate_csv_for_pattern's per-session write loop — the combined
    CSV must read back exactly as if it had been written in one shot."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(30))
        with open(os.path.join(src, "ppg1800000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(40))
        report = extract_dir(src, out,
                             options=ExtractionOptions(ignore_id_parsing=True, save_format="csv",
                                                       include_cdct=True))
        df = pd.read_csv(report.out_paths[0])

        assert len(df) == 30 + 40
        # no stray header row leaked into the data from the second append —
        # every row of a genuinely numeric column parsed as a number
        assert pd.api.types.is_numeric_dtype(df["Counter"])
        assert df["CDCT"].iloc[0] == 1700000000.0
        # second session starts its own CDCT clock at its own t0, not
        # continuing the first session's
        assert df["CDCT"].iloc[30] == 1800000000.0


def test_feather_is_the_default_save_format():
    assert ExtractionOptions().save_format == "feather"


def test_extract_dir_writes_feather_by_default():
    """Same data as test_extract_dir_report_shape, but exercising the actual
    default (no save_format override) — .feather files, read back correctly."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(500))
        with open(os.path.join(src, "ac1700000000.bin"), "wb") as f:
            f.write(_w_ac_v2(500))

        report = extract_dir(src, out, options=ExtractionOptions(ignore_id_parsing=True))

        names = sorted(os.path.basename(p) for p in report.out_paths)
        assert names == ["ac.feather", "ppg.feather"]
        dfs = {os.path.basename(p): pd.read_feather(p) for p in report.out_paths}
        assert len(dfs["ac.feather"]) == 500
        assert len(dfs["ppg.feather"]) == 500


def test_multiple_sessions_under_one_prefix_feather_matches_csv():
    """The whole-frame feather path (concat once, write once) must produce the
    same rows as the incremental CSV path for the same multi-session input."""
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out_csv, \
         tempfile.TemporaryDirectory() as out_feather:
        with open(os.path.join(src, "ppg1700000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(30))
        with open(os.path.join(src, "ppg1800000000.bin"), "wb") as f:
            f.write(_w_ppg_v2(40))

        r_csv = extract_dir(src, out_csv,
                            options=ExtractionOptions(ignore_id_parsing=True, save_format="csv",
                                                      include_cdct=True))
        r_feather = extract_dir(src, out_feather,
                                options=ExtractionOptions(ignore_id_parsing=True, include_cdct=True))

        df_csv = pd.read_csv(r_csv.out_paths[0])
        df_feather = pd.read_feather(r_feather.out_paths[0])
        assert len(df_feather) == 30 + 40
        assert df_feather["Counter"].tolist() == df_csv["Counter"].tolist()
        assert df_feather["Datetime"].tolist() == df_csv["Datetime"].tolist()
        assert np.allclose(df_feather["CDCT"], df_csv["CDCT"])


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
