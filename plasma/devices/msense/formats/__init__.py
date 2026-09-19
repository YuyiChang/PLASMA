"""On-disk record layouts, declared once.

Every fact about a layout lives in one `RecordSpec`: how many bytes a record
takes, how to turn those bytes into columns, where the counter sits, how wide it
is and how fast it advances. The same fields drive three consumers — decoding
(`read_bin` here), content detection (`plasma.devices.msense.detect`) and counter
validation — so they cannot drift apart the way the old arrangement did, where
`DataExtractor.ppg_labels` described one layout while `read_ppg_bin` implemented
another.

Adding a future variant means adding one entry to `REGISTRY`. Nothing else in
the codebase enumerates formats.

This module is a leaf: numpy and pandas only, no Gradio, no options, no I/O
policy beyond reading the file it is handed.

Note: this is the **offline on-disk** decoder. The **live sensor-stream** payload
decoders (`decode_ppg` / `decode_ecg`, and the ECB2 block validation) live in
`plasma/devices/msense/records.py`; this module reuses its `crc32_iso_hdlc` for
the `ecg:block_v2` (`ECF2`) container's CRCs, but decodes ECB2 data blocks with
its own lenient block decoder rather than `records.decode_ecb2_block` — the
offline extractor treats a reserved ETAG (>3) as a warning, not a hard stop
(see `_decode_ecb2_block_lenient` and `ECG_EXTRACTION_HANDOFF.md`), which
differs from the live-stream decoder's stricter validation.
The wristband's legacy `.bin` layouts (`ppg:legacy/v2/packed16`, `ac:legacy/v2`)
are unchanged. The MSense4ECG-Z5G4A chest device's two streams are each locked
to a single container format per ECG_EXTRACTION_HANDOFF.md — `ecg` to
`block_v2` (`ECF2`), `ac`'s `v3` layout to `ACF3` — with output columns locked
to that document's schema; no other on-disk layout is assumed for either.
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

import numpy as np
import pandas as pd

from plasma.devices.msense.records import (
    crc32_iso_hdlc,
    ECB2_BLOCK_SIZE, ECB2_HEADER_SIZE, ECB2_RESERVED_OFFSET, ECB2_SAMPLES_PER_BLOCK,
)

# ECF2 ECG block-container file — see docs/ECG_BLOCK_FORMAT.md §5
ECF2_MAGIC = b"ECF2"
ECF2_FILE_SIZE = 4 * 1024 * 1024
ECF2_DATA_PAGES = 1023

# Packed 16-byte PPG channel packing — see docs/PPG_PACKED_16_BYTE_FORMAT.md
PPG_PACKED_RECORD_SIZE = 16
PPG_PACKED_SAMPLE_MASK = 0x7FFFF
PPG_PACKED_RESERVED_MASK = 0xFFF80000   # bits 19..31 must be clear in every channel

# Firmware at or above this version writes the "v2" layouts.
V2_VERSION = (4, 7, 0)

# ACF3 accelerometer container — see docs/ACCELEROMETER_BINARY_FORMAT.md
AC_V3_MAGIC = b"ACF3"
AC_V3_BLOCK_MAGIC = b"ACB1"
AC_V3_TERMINAL_MAGIC = b"ACT2"
AC_V3_FILE_SIZE = 4 * 1024 * 1024
AC_V3_HEADER_SIZE = 4096
AC_V3_TERMINAL_SIZE = 4096
AC_V3_TERMINAL_OFFSET = AC_V3_FILE_SIZE - AC_V3_TERMINAL_SIZE
AC_V3_REGION_SIZE = AC_V3_FILE_SIZE - AC_V3_HEADER_SIZE - AC_V3_TERMINAL_SIZE
AC_V3_BLOCK_SIZE = 4096
AC_V3_BLOCK_HEADER_SIZE = 16
AC_V3_SAMPLE_SIZE = 6
AC_V3_SAMPLES_PER_BLOCK = (AC_V3_BLOCK_SIZE - AC_V3_BLOCK_HEADER_SIZE) // AC_V3_SAMPLE_SIZE
AC_V3_MAX_DATA_BLOCKS = AC_V3_REGION_SIZE // AC_V3_BLOCK_SIZE   # 1022 full blocks fit exactly
AC_V3_COUNTS_PER_G = 16384.0    # raw_count / 16384.0 = g, at the documented +/-2 g full scale
_U32 = 1 << 32
# A forward jump in first_sample_sequence larger than a whole chunk could hold
# cannot be a real firmware drop (the drop buffer is a handful of blocks) — it
# is corruption / a torn file. Anything above this, or any backwards jump, is a
# sequence-validation failure per the format doc's decoder procedure step 3.
_AC_V3_MAX_CREDIBLE_DROP = AC_V3_MAX_DATA_BLOCKS * AC_V3_SAMPLES_PER_BLOCK


# ---------------------------------------------------------------------------
# byte helpers
# ---------------------------------------------------------------------------

def _le_uint(b, off, n):
    """Little-endian unsigned integer of `n` bytes from an (N, size) uint8 array."""
    out = np.zeros(b.shape[0], dtype=np.uint32)
    for i in range(n):
        out |= b[:, off + i].astype(np.uint32) << (8 * i)
    return out


# ---------------------------------------------------------------------------
# decoders: (N, size) uint8 -> ({column: array}, malformed mask, info dict)
# ---------------------------------------------------------------------------

def _dtype_decoder(dtype):
    """Decoder for any layout expressible as a packed numpy structured dtype.

    Equivalent to struct.iter_unpack with the matching format string, but
    vectorized and with the column names attached to the layout itself.
    """
    def decode(b):
        rec = np.ascontiguousarray(b).view(dtype).reshape(-1)
        cols = {name: rec[name] for name in dtype.names}
        return cols, np.zeros(len(rec), dtype=bool), {}
    return decode


def _decode_packed16(b):
    """4x uint24 channel (19 meaningful bits) + uint32 512 Hz tick."""
    ir1, ir2, g1, g2 = (_le_uint(b, i, 3) for i in (0, 3, 6, 9))
    tick = _le_uint(b, 12, 4)
    malformed = ((ir1 | ir2 | g1 | g2) & PPG_PACKED_RESERVED_MASK) != 0
    cols = {"ir1": ir1, "ir2": ir2, "g1": g1, "g2": g2, "Counter": tick}
    return cols, malformed, {"reserved_bits": int(malformed.sum())}


def _ac_v3_crc32_ok(buf, crc_field_off, crc_field_len, expected):
    """CRC-32/ISO-HDLC (zlib's) over `buf` with the stored CRC field zeroed."""
    patched = bytearray(buf)
    patched[crc_field_off:crc_field_off + crc_field_len] = bytes(crc_field_len)
    return (zlib.crc32(bytes(patched)) & 0xFFFFFFFF) == expected


def _sniff_ac_v3(data: bytes) -> float:
    """Content score for the ac:v3 container.

    Keyed off the `ACB1` block magic at the start of the data region, not the
    header's own `magic` field: firmware is known to leave a stale header
    (`ACF2`, format_version 1, sample_format 0, crc/odr/anchor fields zeroed)
    on a session's first chunk even though the data region and terminal
    record are the same v3 layout as every other chunk. Only v3 is ever
    written in production/test, so this is the one ac container format.
    """
    return 1.0 if (len(data) >= AC_V3_HEADER_SIZE + 4
                   and data[AC_V3_HEADER_SIZE:AC_V3_HEADER_SIZE + 4] == AC_V3_BLOCK_MAGIC) else 0.0


def _chunk_index_from_filename(basename):
    """Zero-based chunk index from a `<id><sensor><session_id>_<chunk>.bin`
    filename; 0 for a non-chunked name (`first_sample_sequence` starts at 0 for
    chunk `0000` — the format doc, "Session filenames and chunking"). Shared by
    the ac:v3 (ACF3) and ecg:block_v2 (ECF2) container readers."""
    m = re.search(r"_(\d+)\.bin$", basename)
    return int(m.group(1)) if m else 0


def _read_ac_v3(filepath, strict=False):
    """Decode one ac:v3 accelerometer chunk: 4 KiB header, ACB1 data blocks, ACT2 terminal.

    Unlike the flat per-record layouts this is a real container: block count is
    variable, the last block may be short, and per-sample timing is projected
    from one RTC anchor per block rather than stored per sample. None of that
    fits `whole_records`/`RecordSpec.decode`, so this owns the full file->df path.

    The header's own magic/format_version/sample_format/odr/anchor fields are
    read for logging only, never to decide *whether* or *how* to decode — see
    `_sniff_ac_v3` and docs/ACCELEROMETER_BINARY_FORMAT.md, "Stale first-chunk
    header".

    Output columns are locked to ECG_EXTRACTION_HANDOFF.md's ``extract_ecg_ac_v3``
    schema — ``SampleSequence``, ``RtcTickEstBlock``, ``AccX``, ``AccY``,
    ``AccZ`` — and nothing else: no ``CDCT``/``init_CDCT``/``Datetime``/
    ``Counter``, which that reference decoder never produces. ``RtcTickEst``
    (the piecewise-linear per-sample tick ramp) is not built here since it can
    span a chunk boundary — see ``pipeline._stitch_ac_v3_chunks``.
    """
    basename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) != AC_V3_FILE_SIZE:
        raise ValueError(f"{basename}: {len(data)} bytes, expected {AC_V3_FILE_SIZE} for an ACF3 chunk")

    header = data[:AC_V3_HEADER_SIZE]
    magic, fmt_version, sample_format = struct.unpack_from("<4sHH", header, 0)
    if magic != AC_V3_MAGIC or fmt_version != 3 or sample_format != 2:
        # `_sniff_ac_v3` already routed this file here on the ACB1 block magic,
        # not on these fields — a session's first chunk is known to carry a
        # stale header (observed: magic=ACF2, format_version=1,
        # sample_format=0) even though the data region and terminal record are
        # ordinary v3. Only v3 is ever produced in production/test, so decode
        # it as v3 regardless of what the header claims; just say so.
        print(f"{basename}: header says magic={magic!r} format_version={fmt_version} "
              f"sample_format={sample_format} (expected {AC_V3_MAGIC!r}/3/2) — "
              f"decoding as ACF3 anyway (known stale-header quirk)")
    header_crc, = struct.unpack_from("<I", header, 20)
    if not _ac_v3_crc32_ok(header, 20, 4, header_crc):
        msg = f"{basename}: ACF3 header CRC mismatch"
        if strict:
            raise ValueError(msg)
        print(msg)

    region = data[AC_V3_HEADER_SIZE:AC_V3_HEADER_SIZE + AC_V3_REGION_SIZE]
    terminal = data[AC_V3_TERMINAL_OFFSET:AC_V3_TERMINAL_OFFSET + AC_V3_TERMINAL_SIZE]
    tmagic, valid_len, tcrc = struct.unpack_from("<4sII", terminal, 0)

    interrupted = tmagic != AC_V3_TERMINAL_MAGIC or not _ac_v3_crc32_ok(terminal, 8, 4, tcrc)
    if interrupted:
        valid_len = len(region)
    elif valid_len > len(region):
        msg = f"{basename}: terminal valid_data_length {valid_len} exceeds capacity {len(region)}"
        if strict:
            raise ValueError(msg)
        print(msg + " — clamping")
        valid_len = len(region)

    x_parts, y_parts, z_parts, seq_parts, tick_col_parts = [], [], [], [], []
    block_ticks, block_n_samples = [], []   # one entry per ok block, README + RtcTickEst input
    off = 0
    n_bad_blocks = 0
    n_blocks_ok = 0
    n_bad_samples = 0
    first_seq_seen = None
    expected_seq = None      # seq the next block should open at, if nothing was dropped
    dropped_total = 0
    seq_gaps = []            # (prev_block_end_seq, this_block_first_seq, n_dropped)
    anchor_first = anchor_last = None   # AnchorCounter (reserved_timer_output) range, README-only
    while off + AC_V3_BLOCK_HEADER_SIZE <= valid_len:
        remaining = valid_len - off
        block_len = AC_V3_BLOCK_SIZE if remaining >= AC_V3_BLOCK_SIZE else remaining
        block = region[off:off + block_len]
        bmagic, anchor_tick, first_seq, bcrc = struct.unpack_from("<4sIII", block, 0)
        n_samples = (block_len - AC_V3_BLOCK_HEADER_SIZE) // AC_V3_SAMPLE_SIZE

        ok = (bmagic == AC_V3_BLOCK_MAGIC and n_samples > 0
              and _ac_v3_crc32_ok(block, 12, 4, bcrc))
        # step 3 of the decoder procedure: "Validate each magic, sequence, and
        # CRC". A firmware drop advances first_sample_sequence but skips samples,
        # so a forward jump is a dropped-sample count; a backwards jump (or a
        # jump too large to be a real drop) fails validation like a bad CRC.
        drop = 0
        if ok and expected_seq is not None:
            delta = (first_seq - expected_seq) & (_U32 - 1)
            if 0 < delta <= _AC_V3_MAX_CREDIBLE_DROP:
                drop = delta
            elif delta != 0:
                ok = False       # backwards / implausible — sequence invalid

        if not ok:
            n_bad_blocks += 1
            n_bad_samples += n_samples if 0 < n_samples < AC_V3_SAMPLES_PER_BLOCK \
                else AC_V3_SAMPLES_PER_BLOCK
            # A short/garbled tail (interrupted collection, or a mid-file tear)
            # cannot be trusted to resync cleanly: stop rather than guess.
            if interrupted or block_len < AC_V3_BLOCK_SIZE:
                break
            off += AC_V3_BLOCK_SIZE
            continue

        if drop:
            dropped_total += drop
            seq_gaps.append((int(expected_seq), int(first_seq), int(drop)))

        raw = np.frombuffer(block, dtype="<u2", count=n_samples * 3,
                            offset=AC_V3_BLOCK_HEADER_SIZE).reshape(-1, 3)
        x_parts.append((raw[:, 0] & np.uint16(0xFFFE)).view(np.int16))
        y_parts.append(raw[:, 1].view(np.int16))
        z_parts.append(raw[:, 2].view(np.int16))
        seq_parts.append(first_seq + np.arange(n_samples, dtype=np.uint32))
        # RtcTickEstBlock: this block's own RTC0 anchor tick, shared by every
        # sample in it (the *estimate* — see RtcTickEst, computed once the
        # whole session's blocks are known: `pipeline._stitch_ac_v3_chunks`).
        tick_col_parts.append(np.full(n_samples, anchor_tick, dtype=np.int64))
        block_ticks.append(int(anchor_tick))
        block_n_samples.append(n_samples)

        if first_seq_seen is None:
            first_seq_seen = int(first_seq)
            anchor_first = int(anchor_tick)
        anchor_last = int(anchor_tick)
        n_blocks_ok += 1
        expected_seq = (int(first_seq) + n_samples) & (_U32 - 1)
        off += block_len

    if not seq_parts:
        raise ValueError(f"{basename}: no valid ACF3 data blocks decoded")

    orig_counter = np.concatenate(seq_parts)
    df = pd.DataFrame({
        # SampleSequence: first_sample_sequence + i, the device's own
        # modulo-2^32 hardware sample sequence (session-continuous; a gap
        # means firmware dropped samples, not missing time — see
        # `dropped_samples`/`seq_gaps` below).
        "SampleSequence": orig_counter,
        "RtcTickEstBlock": np.concatenate(tick_col_parts),
        # Acceleration in g (raw count / 16384), at the documented +/-2 g
        # full scale. AccX's sampled-FSYNC marker bit is already cleared
        # above, before this conversion.
        "AccX": np.concatenate(x_parts) / AC_V3_COUNTS_PER_G,
        "AccY": np.concatenate(y_parts) / AC_V3_COUNTS_PER_G,
        "AccZ": np.concatenate(z_parts) / AC_V3_COUNTS_PER_G,
    })

    _, dt = get_CDCT_init(filepath)

    if n_bad_blocks:
        msg = (f"AC {basename}: {n_bad_blocks} ACF3 block(s) failed validation "
               f"(~{n_bad_samples} samples)" + (" — collection ended mid-block" if interrupted else ""))
        if strict:
            raise ValueError(msg)
        print(msg + " — dropped")

    chunk_index = _chunk_index_from_filename(basename)
    if dropped_total:
        msg = (f"AC {basename}: {dropped_total} sample(s) dropped by firmware "
               f"across {len(seq_gaps)} block boundary(ies)")
        if strict:
            raise ValueError(msg)
        print(msg)
    if chunk_index == 0 and first_seq_seen not in (None, 0):
        print(f"AC {basename}: first chunk does not start at sequence 0 "
              f"(first_sample_sequence={first_seq_seen}) — earlier data may be missing")

    df.attrs["malformed_records"] = n_bad_samples
    df.attrs["dropped_samples"] = int(dropped_total)
    df.attrs["seq_gaps"] = seq_gaps
    df.attrs["first_seq"] = first_seq_seen
    df.attrs["last_seq"] = int(orig_counter[-1])
    df.attrs["chunk_index"] = chunk_index
    df.attrs["trailing_bytes"] = 0
    df.attrs["spec"] = "ac:v3"
    # AnchorCounter (block header offset 4, "reserved_timer_output" in the
    # format doc): a per-block RTC0 tick, already a column (`RtcTickEstBlock`).
    # Its first/last range per file also goes to the README (`write_provenance`).
    df.attrs["anchor_counter_first"] = anchor_first
    df.attrs["anchor_counter_last"] = anchor_last
    df.attrs["n_blocks_ok"] = n_blocks_ok
    df.attrs["n_blocks_bad"] = n_bad_blocks
    # Per-block (tick, n_samples) pairs, in this chunk's block order — not a
    # column: `pipeline._stitch_ac_v3_chunks` concatenates these across every
    # chunk of a session (in `chunk_index` order) to build one session-wide
    # `RtcTickEst` ramp, since the last block of this chunk may need to ramp
    # toward the first block of the *next* chunk.
    df.attrs["block_ticks"] = block_ticks
    df.attrs["block_n_samples"] = block_n_samples
    return df, dt


# ---------------------------------------------------------------------------
# ECF2 — the ECG block-container file (v0 firmware; see docs/ECG_BLOCK_FORMAT.md)
# ---------------------------------------------------------------------------

def _sniff_ecf2(data: bytes) -> float:
    """Content score for the ECF2 container: it is entirely self-identifying."""
    return 1.0 if data[:4] == ECF2_MAGIC else 0.0


def _decode_ecb2_block_lenient(block, basename, page):
    """One 4096-byte ``ECB2`` data block -> (first_rtc_tick, first_sample_index,
    ecg, etag, ptag). MSB-first 24-bit MAX30001 FIFO words, CRC-32/ISO-HDLC
    over the block with the stored CRC field zeroed (ECG_BLOCK_FORMAT.md secs
    2-3) — the opposite sample byte order from the AC v3 format's
    little-endian samples.

    Deliberately more lenient than ``records.decode_ecb2_block`` (used by the
    live sensor stream): the spec says a reserved ETAG (4-7) should have ended
    the recording, but the block's own CRC already confirms byte-level
    integrity, so a reserved tag is logged and kept rather than raised — per
    the verified reference decoder in ECG_EXTRACTION_HANDOFF.md.
    """
    if block[0:4] != b"ECB2":
        raise ValueError(f"bad block magic {bytes(block[0:4])!r} at page {page}")
    if any(block[ECB2_RESERVED_OFFSET:ECB2_BLOCK_SIZE]):
        raise ValueError(f"non-zero reserved tail bytes at page {page}")

    first_rtc_tick = int.from_bytes(bytes(block[4:8]), "little")
    first_sample_index = int.from_bytes(bytes(block[8:12]), "little")
    stored_crc = int.from_bytes(bytes(block[12:16]), "little")
    if crc32_iso_hdlc(block, 12, 16) != stored_crc:
        raise ValueError(f"block CRC mismatch at page {page}")

    raw = (np.frombuffer(bytes(block), dtype=np.uint8,
                         count=ECB2_SAMPLES_PER_BLOCK * 3, offset=ECB2_HEADER_SIZE)
           .reshape(-1, 3).astype(np.uint32))
    raw24 = (raw[:, 0] << 16) | (raw[:, 1] << 8) | raw[:, 2]
    etag = ((raw24 >> 3) & 0x7).astype(np.uint8)
    ptag = (raw24 & 0x7).astype(np.uint8)
    n_reserved = int((etag > 3).sum())
    if n_reserved:
        print(f"ECG {basename}: page {page}: {n_reserved} sample(s) with reserved ETAG "
              f"(>3) — spec says this should end the recording, so treat this data with caution")
    u18 = (raw24 >> 6).astype(np.int64)
    ecg = np.where(u18 & 0x20000, u18 - 0x40000, u18).astype(np.int32)

    return first_rtc_tick, first_sample_index, ecg, etag, ptag


def _read_ecf2(filepath, strict=False):
    """Decode one ECF2 chunk: 4 KiB header page, then up to 1023 ECB2 data
    pages, then erased (all-``0xFF``) pages. Stops at the first erased page or
    the first invalid block. Returns (DataFrame, datetime string).

    Output columns are locked to ECG_EXTRACTION_HANDOFF.md's ``extract_ecg_v2``
    schema — ``SampleIndex``, ``RtcTick``, ``ECG``, ``ETAG``, ``PTAG`` — and
    nothing else: no ``CDCT``/``init_CDCT``/``Datetime``/``Counter``, which
    that reference decoder never produces.
    """
    basename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) != ECF2_FILE_SIZE:
        raise ValueError(f"{basename}: {len(data)} bytes, expected {ECF2_FILE_SIZE} for an ECF2 chunk")

    # A bad/unparseable file header is non-fatal — block offsets are fixed
    # regardless of header content and every block is independently
    # CRC-checked (same pattern as the AC v3 reader's stale-header handling).
    header = data[:ECB2_BLOCK_SIZE]
    # chunk_index always falls back to the filename's own chunk suffix (like
    # the AC v3 reader's `_chunk_index_from_filename`) since a bad header must
    # not block stitching multi-chunk sessions in order.
    chunk_index = _chunk_index_from_filename(basename)
    recording_id = None
    if header[0:4] != ECF2_MAGIC:
        msg = f"{basename}: not an ECF2 file (magic {bytes(header[0:4])!r})"
        if strict:
            raise ValueError(msg)
        print(f"{msg} — ignoring header, decoding data pages directly (their own CRCs still apply)")
    else:
        header_chunk_index = int.from_bytes(header[4:8], "little")
        if header_chunk_index != chunk_index:
            print(f"{basename}: header chunk_index={header_chunk_index} != filename chunk {chunk_index}")
        recording_id = int.from_bytes(header[8:16], "little")
        stored_hdr_crc = int.from_bytes(header[16:20], "little")
        if crc32_iso_hdlc(header, 16, 20) != stored_hdr_crc:
            msg = f"{basename}: ECF2 header CRC mismatch"
            if strict:
                raise ValueError(msg)
            print(f"{msg} — ignoring header, decoding data pages directly (their own CRCs still apply)")

    ecg_parts, etag_parts, ptag_parts, idx_parts, tick_parts = [], [], [], [], []
    prev_index = prev_tick = None
    n_blocks = 0
    anchor_first = anchor_last = None   # first_rtc_tick range, README-only
    error = None
    for page in range(1, ECF2_DATA_PAGES + 1):
        block = data[page * ECB2_BLOCK_SIZE:(page + 1) * ECB2_BLOCK_SIZE]
        if block[0:4] == b"\xff\xff\xff\xff":
            break  # erased sentinel — end of recorded data
        try:
            first_rtc_tick, first_sample_index, ecg, etag, ptag = \
                _decode_ecb2_block_lenient(block, basename, page)
        except ValueError as e:
            error = f"page {page}: {e}"
            break
        if prev_index is not None and (
                first_sample_index != (prev_index + ECB2_SAMPLES_PER_BLOCK) % (1 << 32)
                or first_rtc_tick != (prev_tick + ECB2_SAMPLES_PER_BLOCK) % (1 << 32)):
            error = f"page {page}: ECB2 continuity break"
            break
        prev_index, prev_tick = first_sample_index, first_rtc_tick
        ecg_parts.append(ecg)
        etag_parts.append(etag)
        ptag_parts.append(ptag)
        idx_parts.append(first_sample_index + np.arange(ECB2_SAMPLES_PER_BLOCK, dtype=np.int64))
        tick_parts.append(first_rtc_tick + np.arange(ECB2_SAMPLES_PER_BLOCK, dtype=np.int64))
        if anchor_first is None:
            anchor_first = int(first_rtc_tick)
        anchor_last = int(first_rtc_tick)
        n_blocks += 1

    if not ecg_parts:
        raise ValueError(f"{basename}: no valid ECB2 data blocks decoded"
                         + (f" ({error})" if error else ""))
    if error:
        msg = f"ECG {basename}: decoding stopped early — {error}"
        if strict:
            raise ValueError(msg)
        print(msg)

    sample_index = np.concatenate(idx_parts)
    df = pd.DataFrame({
        # SampleIndex: first_sample_index + i, the recording-local sample
        # ordinal (zero at the recording's first sample), monotonically
        # advancing (+1358/block) through every chunk of the same
        # recording_id (ECG_BLOCK_FORMAT.md §3/§5) — gap-free by construction,
        # since only a complete, CRC-clean block is ever persisted.
        "SampleIndex": sample_index,
        # RtcTick: first_rtc_tick + i — an exact per-sample RTC0 tick (this
        # format's sample rate equals the tick rate), not an estimate.
        "RtcTick": np.concatenate(tick_parts),
        "ECG": np.concatenate(ecg_parts),
        "ETAG": np.concatenate(etag_parts),
        "PTAG": np.concatenate(ptag_parts),
    })
    _, dt = get_CDCT_init(filepath)
    df.attrs["malformed_records"] = 0
    df.attrs["trailing_bytes"] = 0
    df.attrs["spec"] = "ecg:block_v2"
    df.attrs["recording_id"] = recording_id
    df.attrs["chunk_index"] = chunk_index
    df.attrs["first_sample_index"] = int(sample_index[0])
    df.attrs["last_sample_index"] = int(sample_index[-1])
    df.attrs["decode_error"] = error
    # first_rtc_tick range per file, README-only (write_provenance) — not a
    # global counter, since SampleIndex/RtcTick are already session-continuous
    # by construction and need no per-session offset added.
    df.attrs["anchor_counter_first"] = anchor_first
    df.attrs["anchor_counter_last"] = anchor_last
    df.attrs["n_blocks_ok"] = n_blocks
    df.attrs["n_blocks_bad"] = 0
    return df, dt


_PPG_LEGACY_DT = np.dtype([(n, "<i4") for n in
                           ("ir1", "ir2", "g1", "g2", "Timestamp", "Counter")])
_PPG_V2_DT = np.dtype([(n, "<u4") for n in ("ir1", "ir2", "g1", "g2", "Counter")])
_AC_LEGACY_DT = np.dtype([("AccX", "<i2"), ("AccY", "<i2"), ("AccZ", "<i2"),
                          ("QuatX", "<f4"), ("QuatY", "<f4"), ("QuatZ", "<f4"),
                          ("ENMO", "<f4"), ("Timestamp", "<i4"), ("Counter", "<i4")])
_AC_V2_DT = np.dtype([("AccX", "<i2"), ("AccY", "<i2"), ("AccZ", "<i2"),
                      ("QuatX", "<f4"), ("QuatY", "<f4"), ("QuatZ", "<f4"),
                      ("ENMO", "<f4"), ("Counter", "<u4")])


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordSpec:
    """One on-disk layout for one sensor.

    `since`/`until` bound the firmware range that writes it. `since=None` means
    the layout carries no version tie at all — that is the case for packed16,
    and it is why content detection exists rather than being an optimisation.
    """
    name: str
    sensor: str                 # "ppg" | "ac" | "ecg"
    size: int                   # bytes per record
    decode: Callable            # (N, size) uint8 -> (cols, malformed, info)
    tick_offset: int            # byte offset of the counter, for detection
    tick_rate: int              # Hz the counter advances at
    tick_step: int              # expected delta between consecutive records
    tick_bits: int = 32         # counter width; sets the rollover modulus
    erased: str = "counter_max"  # "counter_max" | "minus_one"
    trim_erased_tail: bool = False
    validated: bool = False     # has an independent integrity check beyond the tick
    since: tuple | None = None
    until: tuple | None = None
    read_file: Callable | None = None   # container formats: (filepath, strict) -> (df, dt);
                                         # bypasses whole_records/decode/the tick-diff CDCT math
    sniff: Callable | None = None       # container formats: bytes -> score in [0, 1];
                                         # bypasses the tick-diff content scorer in detect

    @property
    def key(self):
        return f"{self.sensor}:{self.name}"

    @property
    def sample_rate(self):
        return self.tick_rate / self.tick_step

    @property
    def wrap(self):
        return 2 ** self.tick_bits

    def matches_version(self, version):
        """True when firmware `version` is documented to write this layout."""
        if self.since is None and self.until is None:
            return False        # not version-tagged; only content can find it
        if self.since is not None and version < self.since:
            return False
        if self.until is not None and version >= self.until:
            return False
        return True


REGISTRY = (
    RecordSpec("legacy", "ppg", 24, _dtype_decoder(_PPG_LEGACY_DT),
               tick_offset=20, tick_rate=320, tick_step=5, tick_bits=16,
               erased="minus_one", until=V2_VERSION),
    RecordSpec("v2", "ppg", 20, _dtype_decoder(_PPG_V2_DT),
               tick_offset=16, tick_rate=512, tick_step=2, since=V2_VERSION),
    RecordSpec("packed16", "ppg", PPG_PACKED_RECORD_SIZE, _decode_packed16,
               tick_offset=12, tick_rate=512, tick_step=2,
               trim_erased_tail=True, validated=True),

    RecordSpec("legacy", "ac", 30, _dtype_decoder(_AC_LEGACY_DT),
               tick_offset=26, tick_rate=320, tick_step=10, tick_bits=16,
               erased="minus_one", until=V2_VERSION),
    RecordSpec("v2", "ac", 26, _dtype_decoder(_AC_V2_DT),
               tick_offset=22, tick_rate=512, tick_step=16, since=V2_VERSION),

    # v3's per-record fields below are placeholders: read_file/sniff bypass every
    # generic per-record code path (whole_records, decode, the tick-diff CDCT
    # cumsum, and the tick-diff content scorer), so decode/tick_offset/tick_rate
    # are never consulted. tick_step/tick_bits ARE used by the generic
    # counter_validity_check on the 'SampleSequence' column read_file produces,
    # so they describe that column's real semantics (a modulo-2^32 sample
    # sequence) — output is otherwise locked to ECG_EXTRACTION_HANDOFF.md's
    # schema: no other on-disk AC format is assumed for this device.
    RecordSpec("v3", "ac", AC_V3_BLOCK_SIZE, lambda b: (_ for _ in ()).throw(
                   NotImplementedError("ac:v3 is a container format; see read_file")),
               tick_offset=0, tick_rate=1125 / 2, tick_step=1,
               read_file=_read_ac_v3, sniff=_sniff_ac_v3),

    # ECF2 block-container (v0 firmware) — the only ecg layout: no other
    # on-disk ECG format is assumed (ECG_EXTRACTION_HANDOFF.md). read_file/
    # sniff bypass every generic per-record path; the placeholder per-record
    # fields below are never consulted, except tick_step/tick_bits, which
    # describe the 'SampleIndex' column's real semantics (a recording-local,
    # modulo-2^32 sample ordinal) for the generic counter_validity_check.
    RecordSpec("block_v2", "ecg", ECB2_BLOCK_SIZE, lambda b: (_ for _ in ()).throw(
                   NotImplementedError("ecg:block_v2 is a container format; see read_file")),
               tick_offset=0, tick_rate=512, tick_step=1,
               validated=True, read_file=_read_ecf2, sniff=_sniff_ecf2),
)

SENSORS = ("ppg", "ac", "ecg")


def specs_for(sensor):
    return tuple(s for s in REGISTRY if s.sensor == sensor)


def spec_names(sensor):
    return tuple(s.name for s in specs_for(sensor))


def get_spec(sensor, name):
    for s in REGISTRY:
        if s.sensor == sensor and s.name == name:
            return s
    raise ValueError(f"no {sensor} record format named {name!r}; "
                     f"known: {', '.join(spec_names(sensor))}")


def spec_for_version(sensor, version):
    """The layout `version` firmware is documented to write, or None."""
    for s in specs_for(sensor):
        if s.matches_version(version):
            return s
    return None


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def get_CDCT_init(file_path):
    """Reference timestamp encoded in the filename.

    Legacy form `<id><sensor><t0>.bin`: t0 is already unix-seconds.

    Chunked v3 form `<id><sensor><session_id>_<chunk>.bin` (see
    docs/ACCELEROMETER_BINARY_FORMAT.md, "Session filenames and chunking"):
    `session_id = unix_time_seconds*1000 + (uptime_ms modulo 1000)` is
    millisecond-scale, not a timestamp itself, and the `_<chunk>` suffix must
    not be captured as t0 — either mistake reads out a nonsense date.
    """
    filename = os.path.basename(file_path)
    match = re.search(r'[A-Za-z]+(\d+)(_\d+)?\.bin$', filename)
    if not match:
        return 0, datetime.fromtimestamp(0, UTC).strftime("%Y/%m/%d %H:%M:%S")
    value = int(match.group(1))
    t0 = value // 1000 if match.group(2) else value
    return t0, datetime.fromtimestamp(t0, UTC).strftime("%Y/%m/%d %H:%M:%S")


def recompute_cdct(df, spec, t0):
    """(Re)compute CDCT/init_CDCT from `df['Counter']` and a single anchor `t0`.

    Used both for a single file (by `read_bin`) and for an already-concatenated
    multi-chunk session (by `DataExtractor.collect_all_data_by_prefix`): every
    chunk of one session shares the same filename-derived t0 (the chunk suffix
    doesn't change it, see `get_CDCT_init`), but `Counter` keeps advancing
    across chunk boundaries — so CDCT must be computed once per session, over
    the full concatenated counter, not once per chunk. Computing it per chunk
    instead restarts the clock at every chunk boundary, since every chunk
    would otherwise reuse the same t0 as if it were the start of the recording.
    Do not call this across a *session* boundary: the counter (RTC tick /
    sample sequence) is documented as collection-local, not a device-wide
    free-running clock, so two different sessions need their own t0 anchors.
    """
    counter = df['Counter'].to_numpy()
    if not np.issubdtype(counter.dtype, np.floating):
        counter = counter.astype(np.int64)
    counter_diff = np.diff(counter) % spec.wrap
    counter_diff = np.insert(counter_diff, 0, 0)
    df = df.copy()
    df['CDCT'] = t0 + np.cumsum(counter_diff) / spec.tick_rate
    df['init_CDCT'] = t0
    return df


def whole_records(data, spec):
    """Reshape to whole records, dropping a trailing erased block if the spec says to.

    Only *complete trailing* erased records are trimmed — interior ones are kept
    so they surface in the malformed count instead of silently shifting every
    later record.
    """
    n = len(data) // spec.size
    b = np.frombuffer(data[: n * spec.size], dtype=np.uint8).reshape(-1, spec.size)
    if not spec.trim_erased_tail or b.shape[0] == 0:
        return b
    written = np.flatnonzero(~(b == 0xFF).all(axis=1))
    if written.size == 0:
        return b[:0]
    return b[: written[-1] + 1]


def read_bin(filepath, spec, strict=False, include_cdct=True):
    """Decode one binary file with `spec`. Returns (DataFrame, datetime string).

    Malformed records are dropped and counted; `strict` raises instead.

    `include_cdct` (flat per-record formats only — container formats never
    had CDCT and ignore this): when `False`, skip `recompute_cdct` and don't
    attach `CDCT`/`init_CDCT` at all. The pipeline defaults this to `False`
    for actual extraction (see `ExtractionOptions.include_cdct`); this
    function's own default stays `True` so direct callers (tests, notebooks)
    keep seeing the historical columns unless they ask otherwise.
    """
    if spec.read_file is not None:
        return spec.read_file(filepath, strict)

    with open(filepath, "rb") as f:
        data = f.read()

    remainder = len(data) % spec.size
    if remainder:
        msg = (f"{os.path.basename(filepath)}: {len(data)} bytes is not divisible by "
               f"{spec.size}; {remainder} trailing byte(s) ignored")
        if strict:
            raise ValueError(msg)
        print(msg)

    b = whole_records(data, spec)
    if b.shape[0] == 0:
        raise ValueError("No valid records found in file.")

    cols, malformed, info = spec.decode(b)

    n_bad = int(malformed.sum())
    if n_bad:
        detail = ", ".join(f"{k}={v}" for k, v in info.items() if v)
        msg = (f"{spec.sensor.upper()} {os.path.basename(filepath)}: "
               f"{n_bad}/{len(malformed)} records failed validation ({detail})")
        if strict:
            raise ValueError(msg)
        print(msg + " — dropped")

    if n_bad:
        keep = ~malformed
        cols = {k: v[keep] for k, v in cols.items()}

    df = pd.DataFrame(cols)

    if spec.erased == "minus_one":
        # Pre-v4.7.0 firmware writes -1 into unused fields. Kept verbatim: it
        # also nulls genuine samples that happen to equal -1, but changing that
        # would alter every legacy CSV ever produced.
        df = df.replace(-1, np.nan).dropna(how='all')
    else:
        df = df[df['Counter'] != np.iinfo(np.uint32).max]

    if df.empty:
        raise ValueError(
            f"{os.path.basename(filepath)}: no usable records after decoding "
            f"{len(malformed)} {spec.size}-byte records ({n_bad} malformed). "
            f"Is this file really in the {spec.sensor}/{spec.name} format?")

    t0, dt = get_CDCT_init(filepath)
    if include_cdct:
        df = recompute_cdct(df, spec, t0)

    df.attrs['malformed_records'] = n_bad
    df.attrs['trailing_bytes'] = remainder
    df.attrs['spec'] = spec.key

    return df, dt


# ---------------------------------------------------------------------------
# back-compat shims — the packed16 unit tests and older callers import these
# ---------------------------------------------------------------------------

PPG_PACKED16 = get_spec("ppg", "packed16")


def decode_ppg_packed16(data):
    """(ir1, ir2, g1, g2, tick, malformed) from raw packed-16 bytes."""
    b = whole_records(data, PPG_PACKED16)
    cols, malformed, _ = _decode_packed16(b)
    return (cols["ir1"], cols["ir2"], cols["g1"], cols["g2"],
            cols["Counter"], malformed)


def read_ppg_bin_packed16(filepath, strict=False):
    return read_bin(filepath, PPG_PACKED16, strict=strict)
