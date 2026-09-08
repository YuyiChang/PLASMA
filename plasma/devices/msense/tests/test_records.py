"""Offline coverage for the live sensor-stream record decoders.

PPG packed-16 decode + the incremental record/block reassemblers. ECB2 ECG
block decode has its own module, :mod:`test_ecb2`.
"""
import numpy as np

from plasma.devices.msense.records import (
    decode_ppg, Packed16Reassembler, EcgBlockReassembler,
)

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
    assert out["ir1"][0] == (0xFF0001 & 0x7FFFF)
    assert decode_ppg(PPG_VECTOR)["oob_frac"] == 0.0


# ── incremental reassembly ─────────────────────────────────────────────────

def test_packed16_reassembler_splits_and_carries():
    r = Packed16Reassembler()
    body = PPG_VECTOR * 4
    r.feed(0, body[:10])          # partial record
    assert r.take() == b""
    r.feed(10, body[10:37])       # completes 2 records, 5 carry bytes
    got = r.take()
    assert len(got) == 32 and r.records == 2
    r.feed(37, body[37:])
    assert len(r.take()) == 32    # remaining 2 records
    assert r.records == 4


def test_ecg_reassembler_bad_first_block_sets_error():
    r = EcgBlockReassembler()
    r.feed(0, bytes(b"\xffnot a real block" + bytes(4096 - 17)))
    assert r.error is not None
    assert r.blocks == 0
