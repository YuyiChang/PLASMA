"""Decoders for the MSense sensor-stream **v0** payloads.

Pure numeric helpers — they take the byte-offset-reassembled sensor payload
from :mod:`plasma.devices.msense.nus_stream` and return per-channel numpy
arrays.

* **PPG** — packed 16-byte records, unchanged by the v0 transport change
  (``ir1/ir2/g1/g2`` uint24-LE + ``global_tick_512hz`` uint32). See
  ``docs/PPG_PACKED_16_BYTE_FORMAT.md``.
* **ECG** — a stream of 4096-byte ``ECB2`` blocks (16-byte header + 1358
  three-byte samples + 6 zero bytes), CRC-32/ISO-HDLC per block. See
  ``docs/ECG_BLOCK_FORMAT.md``.

The **offline on-disk** decoders for downloaded NAND ``.bin`` files live in
:mod:`plasma.devices.msense.formats` and are kept separate on purpose (a
reassembled BLE stream vs a file on a USB drive, different call sites). The
CRC-32 helper here is shared with that module.
"""
import zlib

import numpy as np

# ── PPG packed-16 ───────────────────────────────────────────────────────────

PPG_RECORD_SIZE = 16
PPG_SAMPLE_MASK = 0x7FFFF  # 19 meaningful bits per optical channel
PPG_OVERFLOW_MASK = 0xFFFFFF ^ PPG_SAMPLE_MASK  # bits 19..23 must be zero
PPG_FS = 256.0
PPG_CHANNELS = ("ir1", "ir2", "g1", "g2")

# ── ECG ECB2 blocks ────────────────────────────────────────────────────────

ECB2_MAGIC = b"ECB2"
ECF2_MAGIC = b"ECF2"
ECB2_BLOCK_SIZE = 4096
ECB2_HEADER_SIZE = 16
ECB2_SAMPLES_PER_BLOCK = 1358
ECB2_RESERVED_OFFSET = 4090        # 6 zero bytes to end of block
ECG_FS = 512.0
ECG_HISTORY_BYTES = 32_768         # leading all-zero slots inside [0, this) skip
_U32 = 0x1_0000_0000


# ── CRC-32/ISO-HDLC (shared with plasma.devices.msense.formats) ─────────────

def crc32_iso_hdlc(buf, zero_lo=None, zero_hi=None):
    """CRC-32/ISO-HDLC (zlib's ``crc32``: reflected poly 0xEDB88320, init and
    final-XOR 0xFFFFFFFF; check value 0xCBF43926 for ASCII ``123456789``).

    When ``zero_lo``/``zero_hi`` are given, the bytes in that half-open range
    are treated as zero for the computation (the stored-CRC field)."""
    if zero_lo is not None:
        b = bytearray(buf)
        b[zero_lo:zero_hi] = bytes(zero_hi - zero_lo)
        buf = bytes(b)
    return zlib.crc32(bytes(buf)) & 0xFFFFFFFF


# ── PPG decode ─────────────────────────────────────────────────────────────

def _u24_le(block, offset):
    """Vectorized little-endian uint24 from column `offset` of an (N, 16) view."""
    return (
        block[:, offset].astype(np.uint32)
        | (block[:, offset + 1].astype(np.uint32) << 8)
        | (block[:, offset + 2].astype(np.uint32) << 16)
    )


def decode_ppg(payload):
    """PPGv2 16-byte records -> {ir1, ir2, g1, g2: int32, tick: uint32, fs,
    oob_frac}.

    Trailing bytes that don't complete a 16-byte record are dropped. Per the
    spec bits above bit 18 must be zero; rather than reject the whole capture
    (this is a quality-check preview) such samples are masked to 19 bits and
    the fraction affected is reported as ``oob_frac``.
    """
    buf = np.frombuffer(bytes(payload), dtype=np.uint8)
    n = len(buf) // PPG_RECORD_SIZE
    block = buf[: n * PPG_RECORD_SIZE].reshape(n, PPG_RECORD_SIZE)

    channels = {
        "ir1": _u24_le(block, 0),
        "ir2": _u24_le(block, 3),
        "g1": _u24_le(block, 6),
        "g2": _u24_le(block, 9),
    }
    oob = np.zeros(n, dtype=bool)
    for name, values in channels.items():
        bad = (values & np.uint32(PPG_OVERFLOW_MASK)) != 0
        oob |= bad
        channels[name] = values & np.uint32(PPG_SAMPLE_MASK)

    tick = (
        block[:, 12].astype(np.uint32)
        | (block[:, 13].astype(np.uint32) << 8)
        | (block[:, 14].astype(np.uint32) << 16)
        | (block[:, 15].astype(np.uint32) << 24)
    )

    out = {name: values.astype(np.int32) for name, values in channels.items()}
    out["tick"] = tick
    out["fs"] = PPG_FS
    out["oob_frac"] = float(oob.mean()) if n else 0.0
    return out


class Packed16Reassembler:
    """Incremental 16-byte-record boundary buffer for the live INFINITY PPG
    path. Feed contiguous byte spans, call :meth:`take` for the bytes that
    have since completed whole records."""

    def __init__(self):
        self._carry = bytearray()
        self._ready = bytearray()
        self.records = 0

    def feed(self, offset, data):
        self._carry.extend(data)
        n = len(self._carry) // PPG_RECORD_SIZE
        if n:
            whole = n * PPG_RECORD_SIZE
            self._ready.extend(self._carry[:whole])
            del self._carry[:whole]
            self.records += n

    def take(self):
        out = bytes(self._ready)
        self._ready.clear()
        return out


# ── ECG ECB2 decode ────────────────────────────────────────────────────────

class EcbValidationError(ValueError):
    """A single ECB2 block failed magic / reserved / CRC / tag validation."""


def decode_ecb2_block(block):
    """One 4096-byte ``ECB2`` block -> dict with ``ecg`` (int32[1358]),
    ``etag`` / ``ptag`` (uint8[1358]), ``first_rtc_tick``,
    ``first_sample_index``. Raises :class:`EcbValidationError` on any
    structural failure."""
    if len(block) != ECB2_BLOCK_SIZE:
        raise EcbValidationError(f"block is {len(block)} bytes, expected {ECB2_BLOCK_SIZE}")
    if bytes(block[0:4]) != ECB2_MAGIC:
        raise EcbValidationError("bad ECB2 magic")
    if any(block[ECB2_RESERVED_OFFSET:ECB2_BLOCK_SIZE]):
        raise EcbValidationError("reserved bytes nonzero")

    first_rtc_tick = int.from_bytes(bytes(block[4:8]), "little")
    first_sample_index = int.from_bytes(bytes(block[8:12]), "little")
    stored_crc = int.from_bytes(bytes(block[12:16]), "little")
    if crc32_iso_hdlc(block, 12, 16) != stored_crc:
        raise EcbValidationError("CRC-32 mismatch")

    raw = (np.frombuffer(bytes(block), dtype=np.uint8,
                         count=ECB2_SAMPLES_PER_BLOCK * 3, offset=ECB2_HEADER_SIZE)
           .reshape(-1, 3).astype(np.uint32))
    raw24 = (raw[:, 0] << 16) | (raw[:, 1] << 8) | raw[:, 2]
    etag = ((raw24 >> 3) & 0x7).astype(np.uint8)
    ptag = (raw24 & 0x7).astype(np.uint8)
    if np.any(etag > 3):
        raise EcbValidationError("ETAG > 3 in block")
    u18 = (raw24 >> 6).astype(np.int64)
    ecg = np.where(u18 & 0x20000, u18 - 0x40000, u18).astype(np.int32)

    return {
        "ecg": ecg,
        "etag": etag,
        "ptag": ptag,
        "first_rtc_tick": first_rtc_tick,
        "first_sample_index": first_sample_index,
    }


class EcgBlockReassembler:
    """Byte-offset-addressed ECB2 stream reassembler.

    Feed contiguous byte spans with :meth:`feed(offset, data)`; complete
    4096-byte blocks are validated and continuity-checked as they land. A
    fully-zero slot wholly inside ``[0, 32768)`` and before the first real
    block is *leading history padding* — skipped, counted, no samples emitted.
    A zero slot anywhere else, or any structural / continuity failure, sets
    :attr:`error` and stops further block acceptance."""

    def __init__(self):
        self._carry = bytearray()
        self._slot_offset = 0          # transport offset of _carry[0]
        self._blocks = []             # decoded dicts, in order
        self._new = []                # decoded dicts not yet handed out by take()
        self.skipped_history_slots = 0
        self.history_blocks = 0        # real blocks whose slot started in [0, 32768)
        self.error = None
        self._seen_real = False
        self._prev_index = None
        self._prev_tick = None

    def feed(self, offset, data):
        if self.error is not None:
            return
        self._carry.extend(data)
        while len(self._carry) >= ECB2_BLOCK_SIZE and self.error is None:
            block = bytes(self._carry[:ECB2_BLOCK_SIZE])
            slot_offset = self._slot_offset
            del self._carry[:ECB2_BLOCK_SIZE]
            self._slot_offset += ECB2_BLOCK_SIZE
            self._handle_block(block, slot_offset)

    def _handle_block(self, block, slot_offset):
        if not any(block):
            if (not self._seen_real
                    and slot_offset + ECB2_BLOCK_SIZE <= ECG_HISTORY_BYTES):
                self.skipped_history_slots += 1
                return
            self.error = "zero block outside leading history padding"
            return
        try:
            dec = decode_ecb2_block(block)
        except EcbValidationError as e:
            self.error = str(e)
            return
        if self._prev_index is not None:
            if (dec["first_sample_index"] != (self._prev_index + ECB2_SAMPLES_PER_BLOCK) % _U32
                    or dec["first_rtc_tick"] != (self._prev_tick + ECB2_SAMPLES_PER_BLOCK) % _U32):
                self.error = "ECB2 continuity break"
                return
        self._seen_real = True
        self._prev_index = dec["first_sample_index"]
        self._prev_tick = dec["first_rtc_tick"]
        if slot_offset < ECG_HISTORY_BYTES:
            self.history_blocks += 1
        self._blocks.append(dec)
        self._new.append(dec)

    @property
    def blocks(self):
        return len(self._blocks)

    def take(self):
        """Decoded dicts accepted since the last :meth:`take` (for the live
        ring buffer)."""
        out, self._new = self._new, []
        return out

    def decoded(self):
        """All samples accepted so far, concatenated."""
        return _concat_ecb2(self._blocks, self.history_blocks,
                            self.skipped_history_slots)


def _concat_ecb2(blocks, history_blocks, skipped_history_slots):
    if not blocks:
        empty_i = np.zeros(0, np.int32)
        return {"ecg": empty_i, "etag": np.zeros(0, np.uint8),
                "ptag": np.zeros(0, np.uint8),
                "sample_index": np.zeros(0, np.int64),
                "rtc_tick": np.zeros(0, np.int64), "fs": ECG_FS,
                "blocks": 0, "history_boundary_sample": 0,
                "skipped_history_slots": skipped_history_slots}
    ecg = np.concatenate([b["ecg"] for b in blocks])
    etag = np.concatenate([b["etag"] for b in blocks])
    ptag = np.concatenate([b["ptag"] for b in blocks])
    idx = np.concatenate([b["first_sample_index"] + np.arange(len(b["ecg"]), dtype=np.int64)
                          for b in blocks])
    tick = np.concatenate([b["first_rtc_tick"] + np.arange(len(b["ecg"]), dtype=np.int64)
                           for b in blocks])
    return {
        "ecg": ecg, "etag": etag, "ptag": ptag,
        "sample_index": idx, "rtc_tick": tick, "fs": ECG_FS,
        "blocks": len(blocks),
        "history_boundary_sample": history_blocks * ECB2_SAMPLES_PER_BLOCK,
        "skipped_history_slots": skipped_history_slots,
    }


def decode_ecg(payload):
    """Batch ECB2 decode of a whole reassembled FINITE ECG payload
    (concatenated 4096-byte blocks, starting at transport offset 0).

    Returns ``{ecg, etag, ptag, sample_index, rtc_tick, fs, blocks,
    history_boundary_sample, skipped_history_slots, error}``. A structural or
    continuity failure stops decoding at that block; everything validated
    before it is still returned, with ``error`` set."""
    r = EcgBlockReassembler()
    r.feed(0, bytes(payload))
    out = r.decoded()
    out["error"] = r.error
    return out


# back-compat alias
decode_ecb2_blocks = decode_ecg
