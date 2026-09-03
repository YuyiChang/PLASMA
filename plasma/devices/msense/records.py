"""Decoders for the packed MSense PPG / ECG record formats.

Pure numeric helpers — they take the reassembled sensor payload from
plasma/nus_stream.py (a bare concatenation of whole records) and return
per-channel numpy arrays. See local_docs/PPG_PACKED_16_BYTE_FORMAT.md and
local_docs/ECG_TEMP_DATA_FORMAT.md for the byte layouts.
"""
import numpy as np

PPG_RECORD_SIZE = 16
PPG_SAMPLE_MASK = 0x7FFFF  # 19 meaningful bits per optical channel
PPG_OVERFLOW_MASK = 0xFFFFFF ^ PPG_SAMPLE_MASK  # bits 19..23 must be zero
PPG_FS = 256.0
PPG_CHANNELS = ("ir1", "ir2", "g1", "g2")

ECG_FRAME_SIZE = 12
ECG_SYNC = b"\xa5\xec"
ECG_FRAME_TYPE = 0x01
ECG_FS = 512.0


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


def _crc8_07_table():
    table = np.zeros(256, dtype=np.uint8)
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        table[i] = crc
    return table


_CRC8_TABLE = _crc8_07_table()


def crc8_07(data):
    """CRC-8, poly 0x07, init 0x00, no reflection / final XOR."""
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return int(crc)


def _crc8_07_columns(block, lo, hi):
    """Vectorized CRC-8 over columns [lo, hi) of an (N, w) uint8 array."""
    crc = np.zeros(len(block), dtype=np.uint8)
    for col in range(lo, hi):
        crc = _CRC8_TABLE[crc ^ block[:, col]]
    return crc


def decode_ecg(payload):
    """MAX30001 12-byte frames -> {ecg: int32, rtc_tick: uint32, fs, crc_ok_frac}.

    Assumes the payload is frame-aligned (the stream layer delivers whole
    records); if sync/CRC checks fail widely it rescans for the sync word.
    Partial trailing frames are dropped.
    """
    buf = np.frombuffer(bytes(payload), dtype=np.uint8)
    n = len(buf) // ECG_FRAME_SIZE
    block = buf[: n * ECG_FRAME_SIZE].reshape(n, ECG_FRAME_SIZE) if n else np.empty((0, ECG_FRAME_SIZE), np.uint8)

    aligned_ok = n > 0 and np.mean(
        (block[:, 0] == ECG_SYNC[0]) & (block[:, 1] == ECG_SYNC[1]) & (block[:, 2] == ECG_FRAME_TYPE)
    ) > 0.5
    if not aligned_ok:
        block = _rescan_ecg(bytes(payload))
        n = len(block)

    if n == 0:
        return {"ecg": np.zeros(0, np.int32), "rtc_tick": np.zeros(0, np.uint32),
                "fs": ECG_FS, "crc_ok_frac": 0.0}

    raw24 = (
        (block[:, 8].astype(np.int64) << 16)
        | (block[:, 9].astype(np.int64) << 8)
        | block[:, 10].astype(np.int64)
    )
    raw24 = np.where(raw24 >= (1 << 23), raw24 - (1 << 24), raw24).astype(np.int32)

    rtc_tick = (
        block[:, 4].astype(np.uint32)
        | (block[:, 5].astype(np.uint32) << 8)
        | (block[:, 6].astype(np.uint32) << 16)
        | (block[:, 7].astype(np.uint32) << 24)
    )

    good = (
        (block[:, 0] == ECG_SYNC[0]) & (block[:, 1] == ECG_SYNC[1])
        & (block[:, 2] == ECG_FRAME_TYPE)
        & (_crc8_07_columns(block, 2, 11) == block[:, 11])
    )

    return {
        "ecg": raw24,
        "rtc_tick": rtc_tick,
        "fs": ECG_FS,
        "crc_ok_frac": float(good.mean()),
    }


def _rescan_ecg(payload):
    """Recover frames from a misaligned byte stream by scanning for the sync
    word and CRC-validating each candidate."""
    frames = []
    i = 0
    end = len(payload) - ECG_FRAME_SIZE
    while i <= end:
        if payload[i] == ECG_SYNC[0] and payload[i + 1] == ECG_SYNC[1] \
                and payload[i + 2] == ECG_FRAME_TYPE \
                and crc8_07(payload[i + 2:i + 11]) == payload[i + 11]:
            frames.append(np.frombuffer(payload[i:i + ECG_FRAME_SIZE], dtype=np.uint8))
            i += ECG_FRAME_SIZE
        else:
            i += 1
    if not frames:
        return np.empty((0, ECG_FRAME_SIZE), np.uint8)
    return np.vstack(frames)
