"""ECB2 block / ECF2 file decode — checked against the byte-layout vectors in
``docs/ECG_BLOCK_FORMAT.md`` §13.
"""
import numpy as np
import pytest

from plasma.devices.msense.records import (
    crc32_iso_hdlc, decode_ecb2_block, decode_ecg, EcbValidationError,
    EcgBlockReassembler, ECB2_BLOCK_SIZE, ECB2_SAMPLES_PER_BLOCK,
)

DOC_BLOCK_CRC = 0xB27DDE56
DOC_HEADER_CRC = 0x3FDB88B5


def _make_block(first_tick, first_index):
    blk = bytearray(ECB2_BLOCK_SIZE)
    blk[0:4] = b"ECB2"
    blk[4:8] = int(first_tick).to_bytes(4, "little")
    blk[8:12] = int(first_index).to_bytes(4, "little")
    for i in range(ECB2_SAMPLES_PER_BLOCK):
        raw24 = ((i & 0x3FFFF) << 6) | ((i % 4) << 3) | (i % 8)
        off = 16 + 3 * i
        blk[off] = (raw24 >> 16) & 0xFF
        blk[off + 1] = (raw24 >> 8) & 0xFF
        blk[off + 2] = raw24 & 0xFF
    blk[12:16] = crc32_iso_hdlc(blk, 12, 16).to_bytes(4, "little")
    return bytes(blk)


def _make_header(chunk_index, recording_id):
    hdr = bytearray(ECB2_BLOCK_SIZE)
    hdr[0:4] = b"ECF2"
    hdr[4:8] = int(chunk_index).to_bytes(4, "little")
    hdr[8:16] = int(recording_id).to_bytes(8, "little")
    hdr[16:20] = crc32_iso_hdlc(hdr, 16, 20).to_bytes(4, "little")
    return bytes(hdr)


# ── doc vectors ────────────────────────────────────────────────────────────

def test_crc_check_value():
    assert crc32_iso_hdlc(b"123456789") == 0xCBF43926


def test_doc_block_crc():
    blk = bytearray(_make_block(100000, 0))
    blk[12:16] = b"\x00\x00\x00\x00"
    assert crc32_iso_hdlc(blk, 12, 16) == DOC_BLOCK_CRC


def test_doc_header_crc():
    hdr = bytearray(_make_header(7, 0x0123456789ABCDEF))
    hdr[16:20] = b"\x00\x00\x00\x00"
    assert crc32_iso_hdlc(hdr, 16, 20) == DOC_HEADER_CRC


# ── block decode ───────────────────────────────────────────────────────────

def test_decode_block_fields():
    d = decode_ecb2_block(_make_block(100000, 0))
    assert d["first_rtc_tick"] == 100000
    assert d["first_sample_index"] == 0
    # etag = i % 4, ptag = i % 8, ecg = i (u18 part is i)
    np.testing.assert_array_equal(d["etag"][:8], [0, 1, 2, 3, 0, 1, 2, 3])
    np.testing.assert_array_equal(d["ptag"][:8], [0, 1, 2, 3, 4, 5, 6, 7])
    np.testing.assert_array_equal(d["ecg"][:5], [0, 1, 2, 3, 4])


def test_decode_block_bad_magic():
    blk = bytearray(_make_block(0, 0))
    blk[0] = ord("X")
    with pytest.raises(EcbValidationError):
        decode_ecb2_block(bytes(blk))


def test_decode_block_bad_crc():
    blk = bytearray(_make_block(0, 0))
    blk[12] ^= 0xFF
    with pytest.raises(EcbValidationError):
        decode_ecb2_block(bytes(blk))


def test_decode_block_nonzero_reserved():
    blk = bytearray(_make_block(0, 0))
    blk[4095] = 1
    blk[12:16] = crc32_iso_hdlc(blk, 12, 16).to_bytes(4, "little")
    with pytest.raises(EcbValidationError):
        decode_ecb2_block(bytes(blk))


def test_decode_block_bad_etag():
    blk = bytearray(ECB2_BLOCK_SIZE)
    blk[0:4] = b"ECB2"
    # sample 0 raw24 with etag bits = 0b111 (7)
    blk[16], blk[17], blk[18] = 0x00, 0x00, 0x38
    blk[12:16] = crc32_iso_hdlc(blk, 12, 16).to_bytes(4, "little")
    with pytest.raises(EcbValidationError):
        decode_ecb2_block(bytes(blk))


# ── stream reassembly ──────────────────────────────────────────────────────

def test_continuity_ok():
    b0 = _make_block(100000, 0)
    b1 = _make_block(100000 + 1358, 1358)
    out = decode_ecg(b0 + b1)
    assert out["error"] is None
    assert out["blocks"] == 2
    assert out["ecg"].size == 2 * ECB2_SAMPLES_PER_BLOCK
    np.testing.assert_array_equal(out["sample_index"][:2], [0, 1])
    assert out["sample_index"][ECB2_SAMPLES_PER_BLOCK] == 1358


def test_continuity_break_detected():
    b0 = _make_block(100000, 0)
    b_bad = _make_block(100000 + 1358, 9999)   # wrong sample index
    out = decode_ecg(b0 + b_bad)
    assert out["error"] == "ECB2 continuity break"
    assert out["blocks"] == 1               # prefix retained


def test_leading_zero_history_slots_skipped():
    zero = bytes(ECB2_BLOCK_SIZE)
    real = _make_block(500, 0)
    out = decode_ecg(zero + zero + real)
    assert out["error"] is None
    assert out["skipped_history_slots"] == 2
    assert out["blocks"] == 1
    assert out["history_boundary_sample"] == ECB2_SAMPLES_PER_BLOCK


def test_zero_block_after_real_is_error():
    real = _make_block(500, 0)
    zero = bytes(ECB2_BLOCK_SIZE)
    out = decode_ecg(real + zero)
    assert out["error"] == "zero block outside leading history padding"
    assert out["blocks"] == 1


def test_zero_block_past_history_region_is_error():
    # 8 real history blocks fill [0, 32768); a 9th zero block is future data
    blocks = b"".join(_make_block(1000 + 1358 * k, 1358 * k) for k in range(8))
    out = decode_ecg(blocks + bytes(ECB2_BLOCK_SIZE))
    assert out["error"] == "zero block outside leading history padding"
    assert out["blocks"] == 8


def test_reassembler_take_incremental():
    r = EcgBlockReassembler()
    b0 = _make_block(0, 0)
    r.feed(0, b0[:1000])
    assert r.take() == []
    r.feed(1000, b0[1000:])
    got = r.take()
    assert len(got) == 1 and got[0]["first_sample_index"] == 0
    assert r.take() == []
