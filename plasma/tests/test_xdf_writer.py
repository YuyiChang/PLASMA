"""XDFWriter round-trips: write chunks, read them back with pyxdf, assert
fidelity. No LSL involved — pure encoder verification."""
import struct

import numpy as np
import pytest

from plasma.xdf_writer import XDFWriter, _varlen

pyxdf = pytest.importorskip("pyxdf")


def _info_xml(name, ch_count, ch_format, srate=0.0, stype="test"):
    return (
        '<?xml version="1.0"?><info>'
        f"<name>{name}</name><type>{stype}</type>"
        f"<channel_count>{ch_count}</channel_count>"
        f"<nominal_srate>{srate}</nominal_srate>"
        f"<channel_format>{ch_format}</channel_format>"
        "<source_id>test-src</source_id>"
        "<desc><plasma_origin>1</plasma_origin></desc>"
        "</info>"
    )


def _load(path):
    # dejitter off so we can assert raw timestamps exactly
    streams, header = pyxdf.load_xdf(str(path), dejitter_timestamps=False,
                                    synchronize_clocks=False)
    return {s["info"]["name"][0]: s for s in streams}, header


# ── varlen ─────────────────────────────────────────────────────────────────

def test_varlen_selector_and_bytes():
    assert _varlen(5) == b"\x01\x05"
    assert _varlen(255) == b"\x01\xff"
    assert _varlen(256) == b"\x04" + (256).to_bytes(4, "little")
    assert _varlen(2 ** 32 - 1) == b"\x04" + (2 ** 32 - 1).to_bytes(4, "little")
    assert _varlen(2 ** 32) == b"\x08" + (2 ** 32).to_bytes(8, "little")


def test_varlen_rejects_negative():
    with pytest.raises(ValueError):
        _varlen(-1)


# ── file structure ─────────────────────────────────────────────────────────

def test_magic_and_fileheader(tmp_path):
    p = tmp_path / "a.xdf"
    w = XDFWriter()
    w.open(str(p))
    w.close()
    raw = p.read_bytes()
    assert raw.startswith(b"XDF:")
    # first chunk after magic: 1-byte len selector, len, tag=1
    assert raw[4] == 0x01
    length = raw[5]
    tag = struct.unpack("<H", raw[6:8])[0]
    assert tag == 1
    assert b"<version>1.0</version>" in raw[8:8 + length]


# ── numeric round-trip ─────────────────────────────────────────────────────

def test_numeric_roundtrip_double64(tmp_path):
    p = tmp_path / "num.xdf"
    n, nchan = 100, 3
    ts = [1000.0 + i * 0.01 for i in range(n)]
    samples = [[float(i), float(i) * 2, float(i) * -0.5] for i in range(n)]

    w = XDFWriter()
    w.open(str(p))
    w.write_stream_header(1, _info_xml("Num", nchan, "double64", srate=100.0), "double64", nchan)
    w.write_clock_offset(1, 1000.0, 0.001)
    w.write_samples(1, ts[:50], samples[:50])
    w.write_boundary()
    w.write_samples(1, ts[50:], samples[50:])
    w.write_clock_offset(1, 1000.5, 0.0011)
    w.write_stream_footer(1)
    w.close()

    streams, _ = _load(p)
    s = streams["Num"]
    assert s["time_series"].shape == (n, nchan)
    assert np.allclose(s["time_series"], np.array(samples))
    assert np.allclose(s["time_stamps"], np.array(ts))
    assert int(s["footer"]["info"]["sample_count"][0]) == n


@pytest.mark.parametrize("fmt,char", [("int32", "i"), ("int64", "q"), ("int16", "h")])
def test_integer_formats_exact(tmp_path, fmt, char):
    p = tmp_path / f"{fmt}.xdf"
    vals = [[7, -3], [2 ** 14, 0], [-1, 5]]
    ts = [10.0, 10.1, 10.2]
    w = XDFWriter()
    w.open(str(p))
    w.write_stream_header(1, _info_xml("I", 2, fmt), fmt, 2)
    w.write_samples(1, ts, vals)
    w.write_stream_footer(1)
    w.close()

    streams, _ = _load(p)
    assert np.array_equal(streams["I"]["time_series"], np.array(vals))


# ── string / marker round-trip ─────────────────────────────────────────────

def test_string_marker_roundtrip(tmp_path):
    p = tmp_path / "markers.xdf"
    msgs = ["Task start [A]", "Flag [] noisy", "Task end [A]", "unicode → ✓", ""]
    ts = [5.0, 12.5, 20.0, 25.0, 30.0]
    w = XDFWriter()
    w.open(str(p))
    w.write_stream_header(1, _info_xml("PLASMA", 1, "string", stype="string"), "string", 1)
    w.write_samples(1, ts, [[m] for m in msgs])
    w.write_stream_footer(1)
    w.close()

    streams, _ = _load(p)
    got = [row[0] for row in streams["PLASMA"]["time_series"]]
    assert got == msgs
    assert np.allclose(streams["PLASMA"]["time_stamps"], np.array(ts))


# ── multiple interleaved streams ───────────────────────────────────────────

def test_two_streams_interleaved(tmp_path):
    p = tmp_path / "two.xdf"
    w = XDFWriter()
    w.open(str(p))
    w.write_stream_header(1, _info_xml("A", 1, "float32", srate=1.0), "float32", 1)
    w.write_stream_header(2, _info_xml("B", 2, "int32"), "int32", 2)
    w.write_samples(1, [1.0], [[1.5]])
    w.write_samples(2, [1.1], [[10, 11]])
    w.write_samples(1, [2.0], [[2.5]])
    w.write_samples(2, [2.1, 2.2], [[20, 21], [30, 31]])
    w.write_stream_footer(1)
    w.write_stream_footer(2)
    w.close()

    streams, _ = _load(p)
    assert np.allclose(streams["A"]["time_series"].ravel(), [1.5, 2.5])
    assert np.array_equal(streams["B"]["time_series"], np.array([[10, 11], [20, 21], [30, 31]]))


def test_clock_offsets_recorded(tmp_path):
    p = tmp_path / "clk.xdf"
    w = XDFWriter()
    w.open(str(p))
    w.write_stream_header(1, _info_xml("C", 1, "double64", srate=10.0), "double64", 1)
    w.write_clock_offset(1, 100.0, 0.25)
    w.write_samples(1, [100.0, 100.1], [[1.0], [2.0]])
    w.write_clock_offset(1, 101.0, 0.26)
    w.write_stream_footer(1)
    w.close()

    # synchronize_clocks (pyxdf default) reads the tag-4 chunks back
    streams, _ = pyxdf.load_xdf(str(p), dejitter_timestamps=False)
    s = streams[0]
    assert list(s["clock_times"]) == [100.0, 101.0]
    assert s["clock_values"][0] == pytest.approx(0.25)
    assert s["clock_values"][1] == pytest.approx(0.26)
