"""Offline coverage for the packed PPG / ECG record decoders."""
import struct

import numpy as np
import pytest

from plasma.ppg_ecg_records import decode_ppg, decode_ecg, crc8_07, ECG_SYNC


# ── PPG ─────────────────────────────────────────────────────────────────────

PPG_VECTOR = bytes.fromhex("010000452301ffff0700010078563412")  # from PPG_PACKED_16_BYTE_FORMAT.md


def test_ppg_interop_vector():
    out = decode_ppg(PPG_VECTOR)
    assert out["ir1"][0] == 0x000001
    assert out["ir2"][0] == 0x012345
    assert out["g1"][0] == 0x07FFFF
    assert out["g2"][0] == 0x000100
    assert out["tick"][0] == 0x12345678
    assert out["fs"] == 256.0


def test_ppg_multiple_records_and_partial_tail():
    payload = PPG_VECTOR * 3 + b"\x01\x02\x03"  # 3 whole + partial
    out = decode_ppg(payload)
    assert len(out["ir1"]) == 3
    assert np.all(out["g1"] == 0x07FFFF)


def test_ppg_out_of_range_channel_masked_and_reported():
    bad = bytearray(PPG_VECTOR)
    bad[2] = 0xFF  # push ir1 above bit 18
    out = decode_ppg(bytes(bad))
    assert out["oob_frac"] == 1.0
    assert out["ir1"][0] == (0xFF0001 & 0x7FFFF)  # masked to 19 bits
    assert decode_ppg(PPG_VECTOR)["oob_frac"] == 0.0


# ── ECG ─────────────────────────────────────────────────────────────────────

def _ecg_frame(raw24, rtc_tick, flags=0):
    body = bytes([0x01, flags]) + struct.pack("<I", rtc_tick) + bytes(
        [(raw24 >> 16) & 0xFF, (raw24 >> 8) & 0xFF, raw24 & 0xFF]
    )
    return ECG_SYNC + body + bytes([crc8_07(body)])


def test_crc8_07_known_value():
    # CRC-8/ITU-style poly 0x07, init 0x00: "123456789" -> 0xF4
    assert crc8_07(b"123456789") == 0xF4


def test_ecg_roundtrip_and_sign_extension():
    frames = _ecg_frame(0x0000FF, 16) + _ecg_frame(0xFFFFFE, 17)
    out = decode_ecg(frames)
    assert out["fs"] == 512.0
    assert out["crc_ok_frac"] == 1.0
    assert out["ecg"][0] == 255
    assert out["ecg"][1] == -2
    assert list(out["rtc_tick"]) == [16, 17]


def test_ecg_partial_trailing_frame_dropped():
    out = decode_ecg(_ecg_frame(1, 1) * 2 + b"\xa5\xec\x01")
    assert len(out["ecg"]) == 2


def test_ecg_resync_on_misaligned_stream():
    good = _ecg_frame(0x001234, 100) + _ecg_frame(0x005678, 101)
    out = decode_ecg(b"\x00\x11\x22" + good)  # 3 junk bytes up front
    assert 0x001234 in list(out["ecg"])
    assert 0x005678 in list(out["ecg"])
