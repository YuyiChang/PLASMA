"""Synthetic sensor data for the simulated MSense device.

Two jobs:

* encode realistic ECG / PPG waveforms into the exact packed record byte
  layouts that ``plasma.devices.msense.records`` decodes (ECB2 blocks for ECG,
  packed-16 records for PPG), so the SQC / live plots show a real-looking
  signal;
* assemble the sensor-stream **v0** notification frames for a snapshot or a
  live stream (via ``plasma.devices.msense.nus_sim``), optionally perturbed by
  an injected fault.
"""
import math
import random
import struct

import numpy as np

from plasma.devices.msense import nus_sim
from plasma.devices.msense.nus_stream import (
    ECG, PPG, MODE_FINITE, FINITE_TOTAL_BYTES, END_SUCCESS, END_STOPPED,
)
from plasma.devices.msense.records import (
    crc32_iso_hdlc, ECB2_BLOCK_SIZE, ECB2_SAMPLES_PER_BLOCK, ECB2_HEADER_SIZE,
    PPG_RECORD_SIZE,
)

# ── live notification payloads (ENMO / battery / IMU-stream) ─────────────────


def enmo_packet(counter):
    """8-byte ENMO notification: <f enmo> <I counter> (see enmo_handler)."""
    enmo = 0.02 + 0.015 * math.sin(counter / 7.0) + random.uniform(0, 0.01)
    return struct.pack("<fI", max(0.0, enmo), counter & 0xFFFFFFFF)


def battery_packet(pct):
    return bytes([max(0, min(100, int(pct)))])


def imu_stream_packet(counter, t):
    """20-byte IMU-stream notification: <hhh accel> <fff quat-vec> <H counter>."""
    accel_divisor = 8192
    ax = int(0.03 * accel_divisor * math.sin(t * 0.6))
    ay = int(0.03 * accel_divisor * math.cos(t * 0.6))
    az = int(1.0 * accel_divisor)
    q0 = 0.02 * math.sin(t * 0.9)
    q1 = 0.02 * math.cos(t * 0.7)
    q2 = 0.015 * math.sin(t * 0.5)
    return struct.pack("<hhhfffH", ax, ay, az, q0, q1, q2, counter & 0xFFFF)


# ── packed sensor payload encoders ─────────────────────────────────────────

_PPG_DC = 0x3FFFF          # mid-scale of the 19-bit range
_PPG_AMP = 0x0C000
_ECG_QRS = 90_000          # counts


def ppg_payload(n_records, *, start_tick=0):
    """``n_records`` × 16-byte PPGv2 records: 4× LE uint24 optical + LE uint32
    tick. Channels are a ~1.1 Hz cardiac sinusoid (+ 2nd harmonic + noise),
    each masked to the 19 valid bits."""
    fs = 256.0
    t = np.arange(n_records) / fs
    hr = 1.15
    out = bytearray()
    phases = (0.0, 0.4, 2.1, 2.6)
    for i in range(n_records):
        rec = bytearray()
        for ph in phases:
            v = (_PPG_DC
                 + _PPG_AMP * math.sin(2 * math.pi * hr * t[i] + ph)
                 + 0.3 * _PPG_AMP * math.sin(4 * math.pi * hr * t[i] + ph)
                 + random.uniform(-800, 800))
            v = int(v) & 0x7FFFF
            rec += bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])
        rec += struct.pack("<I", (start_tick + i * 2) & 0xFFFFFFFF)   # 512 Hz tick, step 2
        out += rec
    return bytes(out)


def _ecb2_block(first_tick, first_index):
    """One valid 4096-byte ECB2 block with a synthetic ECG waveform
    (ETAG = PTAG = 0 for every sample)."""
    blk = bytearray(ECB2_BLOCK_SIZE)
    blk[0:4] = b"ECB2"
    blk[4:8] = (first_tick & 0xFFFFFFFF).to_bytes(4, "little")
    blk[8:12] = (first_index & 0xFFFFFFFF).to_bytes(4, "little")
    fs = 512.0
    hr = 1.05
    beat = fs / hr
    for i in range(ECB2_SAMPLES_PER_BLOCK):
        n = first_index + i
        phase = (n % beat) / beat
        qrs = math.exp(-((phase - 0.5) ** 2) / (2 * 0.0016)) * _ECG_QRS
        wander = 3_000 * math.sin(2 * math.pi * n / fs * 0.25)
        ecg = int(qrs + wander + random.uniform(-400, 400))
        u18 = ecg & 0x3FFFF
        raw24 = (u18 << 6)             # etag 0, ptag 0
        off = ECB2_HEADER_SIZE + 3 * i
        blk[off] = (raw24 >> 16) & 0xFF
        blk[off + 1] = (raw24 >> 8) & 0xFF
        blk[off + 2] = raw24 & 0xFF
    blk[12:16] = crc32_iso_hdlc(blk, 12, 16).to_bytes(4, "little")
    return bytes(blk)


def ecg_payload(n_blocks, *, start_tick=1000, start_index=0, leading_zero_slots=0):
    """``n_blocks`` contiguous ECB2 blocks. ``leading_zero_slots`` all-zero
    4096-byte slots are prepended (unavailable-history padding); they still
    advance tick/index continuity for the first real block."""
    out = bytearray(bytes(ECB2_BLOCK_SIZE) * leading_zero_slots)
    tick, idx = start_tick, start_index
    for _ in range(n_blocks):
        out += _ecb2_block(tick, idx)
        tick = (tick + ECB2_SAMPLES_PER_BLOCK) & 0xFFFFFFFF
        idx = (idx + ECB2_SAMPLES_PER_BLOCK) & 0xFFFFFFFF
    return bytes(out)


# ── stream frame assembly ──────────────────────────────────────────────────

def _finite_body(sensor):
    """Exactly FINITE_TOTAL_BYTES of sensor bytes for one product."""
    if sensor == ECG:
        n_blocks = FINITE_TOTAL_BYTES // ECB2_BLOCK_SIZE          # 32
        return ecg_payload(n_blocks)
    return ppg_payload(FINITE_TOTAL_BYTES // PPG_RECORD_SIZE)      # 8192 records


def _sensor_code(sensor):
    return ECG if str(sensor).upper() == "ECG" else PPG


def sqc_frames(sensor, stream_id, *, fault=None, mtu=247):
    """Ordered NUS notification frames for one FINITE SQC snapshot.

    Returns ``(frames, terminal)`` — ``terminal`` is False when a fault cuts
    the stream short (no END), which is what makes the driver's no-progress
    watchdog fire.
    """
    product = _sensor_code(sensor)
    body = _finite_body(product)

    if fault in ("sqc_error", "stream_stall"):
        frames = [nus_sim.message(nus_sim.MSG_START_ACK,
                                  nus_sim.start_ack_payload(MODE_FINITE), stream_id)]
        frames.extend(nus_sim.data_stream(body[:4096], stream_id=stream_id, mtu=mtu))
        return frames, False

    return nus_sim.finite_sequence(stream_id, body, mtu=mtu, status=END_SUCCESS), True


_HISTORY_BYTES = 32_768


def infinity_frame_iter(sensor, stream_id, *, mtu=247, stop_event=None):
    """Generator of framed v0 notifications for a live INFINITY stream: one
    START_ACK, the 32 KiB history, then future ~4 KiB chunks until
    ``stop_event`` is set — after which one STOPPED END is yielded."""
    product = _sensor_code(sensor)
    yield nus_sim.message(nus_sim.MSG_START_ACK,
                          nus_sim.start_ack_payload(nus_sim.MODE_INFINITY), stream_id)

    if product == ECG:
        tick = idx = 0

        def next_chunk():          # one 4096-byte ECB2 block
            nonlocal tick, idx
            blk = _ecb2_block(tick, idx)
            tick = (tick + ECB2_SAMPLES_PER_BLOCK) & 0xFFFFFFFF
            idx = (idx + ECB2_SAMPLES_PER_BLOCK) & 0xFFFFFFFF
            return blk
    else:
        pos = [0]

        def next_chunk():          # 256 PPG records == 4096 bytes
            out = ppg_payload(256, start_tick=pos[0] * 2)
            pos[0] += 256
            return out

    off = 0
    carry = bytearray()
    # 32 KiB history: exactly 8 ECB2 blocks / 2048 PPG records
    while len(carry) < _HISTORY_BYTES:
        carry += next_chunk()
    for m in nus_sim.data_stream(bytes(carry[:_HISTORY_BYTES]), stream_id=stream_id,
                                 start_offset=off, mtu=mtu):
        yield m
    off += _HISTORY_BYTES
    future = bytes(carry[_HISTORY_BYTES:])

    while stop_event is None or not stop_event.is_set():
        if not future:
            future = next_chunk()
        for m in nus_sim.data_stream(future, stream_id=stream_id, start_offset=off, mtu=mtu):
            yield m
        off += len(future)
        future = b""

    yield nus_sim.end_message(END_STOPPED, stream_id)
