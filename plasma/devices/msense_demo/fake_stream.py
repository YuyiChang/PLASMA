"""Synthetic sensor data for the simulated MSense device.

Two jobs:

* encode realistic ECG / PPG waveforms into the exact packed record byte
  layouts that ``plasma.devices.msense.records`` decodes, so the SQC plot shows
  a real-looking signal;
* assemble the NUS notification frames for an SQC snapshot (via
  ``plasma.devices.msense.nus_sim``), optionally perturbed by an injected fault.
"""
import math
import random
import struct

import numpy as np

from plasma.devices.msense import nus_sim
from plasma.devices.msense.nus_sim import message
from plasma.devices.msense.nus_stream import (
    PROFILE, DEVICE_PPG, DEVICE_ECG, MSG_START_ACK,
)
from plasma.devices.msense.records import crc8_07

# ── live notification payloads (ENMO / battery / IMU-stream) ─────────────────


def enmo_packet(counter):
    """8-byte ENMO notification: <f enmo> <I counter> (see enmo_handler)."""
    enmo = 0.02 + 0.015 * math.sin(counter / 7.0) + random.uniform(0, 0.01)
    return struct.pack("<fI", max(0.0, enmo), counter & 0xFFFFFFFF)


def battery_packet(pct):
    return bytes([max(0, min(100, int(pct)))])


def imu_stream_packet(counter, t):
    """20-byte IMU-stream notification: <hhh accel> <fff quat-vec> <H counter>.

    A slow continuous rotation about a tilted axis so the orientation box in the
    IMU panel visibly moves.
    """
    accel_divisor = 8192  # matches imu_stream_handler
    ax = int(0.03 * accel_divisor * math.sin(t * 0.6))
    ay = int(0.03 * accel_divisor * math.cos(t * 0.6))
    az = int(1.0 * accel_divisor)
    # small per-frame delta rotation (vector part of a near-identity quat)
    q0 = 0.02 * math.sin(t * 0.9)
    q1 = 0.02 * math.cos(t * 0.7)
    q2 = 0.015 * math.sin(t * 0.5)
    return struct.pack("<hhhfffH", ax, ay, az, q0, q1, q2, counter & 0xFFFF)


# ── packed SQC record encoders (inverse of records.decode_ppg / decode_ecg) ──

_PPG_DC = 0x3FFFF          # mid-scale of the 19-bit range
_PPG_AMP = 0x0C000


def ppg_payload(n_records, *, fs=None):
    """``n_records`` × 16-byte PPGv2 records: 4× LE uint24 optical + LE uint32
    tick. Channels are a ~1.1 Hz cardiac sinusoid (+ 2nd harmonic + noise),
    each masked to the 19 valid bits."""
    fs = fs or PROFILE[DEVICE_PPG]["rate_hz"]
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
        rec += struct.pack("<I", i & 0xFFFFFFFF)   # tick
        out += rec
    return bytes(out)


def ecg_payload(n_records, *, fs=None):
    """``n_records`` × 12-byte MAX30001 frames: sync a5 ec 01, reserved 0,
    LE uint32 rtc, big-endian signed int24 sample, CRC-8/0x07 over bytes 2..10."""
    fs = fs or PROFILE[DEVICE_ECG]["rate_hz"]
    hr = 1.05
    beat_period = fs / hr
    out = bytearray()
    for i in range(n_records):
        # sharp QRS: narrow gaussian once per beat, plus a little baseline wander
        phase = (i % beat_period) / beat_period
        qrs = math.exp(-((phase - 0.5) ** 2) / (2 * 0.006)) * 380000
        wander = 12000 * math.sin(2 * math.pi * i / fs * 0.25)
        s = int(qrs + wander + random.uniform(-1500, 1500))
        s &= 0xFFFFFF                                   # two's-complement int24
        frame = bytearray([0xA5, 0xEC, 0x01, 0x00])
        frame += struct.pack("<I", i & 0xFFFFFFFF)      # rtc
        frame += bytes([(s >> 16) & 0xFF, (s >> 8) & 0xFF, s & 0xFF])  # BE int24
        frame.append(crc8_07(bytes(frame[2:11])))
        out += frame
    return bytes(out)


DEVICE_CODE = {"PPG": DEVICE_PPG, "ECG": DEVICE_ECG}


def sqc_frames(sensor, session_id, *, fault=None):
    """Ordered NUS notification frames for one SQC snapshot.

    Returns ``(frames, terminal)`` — ``terminal`` is False when a fault cuts the
    stream short (no END), which is exactly what makes the driver's no-progress
    watchdog fire.
    """
    device_type = DEVICE_CODE.get(str(sensor).upper(), DEVICE_PPG)
    p = PROFILE[device_type]
    n = p["history_records"] + p["forward_records"]
    records = (ppg_payload(n) if device_type == DEVICE_PPG else ecg_payload(n))

    if fault == "sqc_error":
        # START_ACK + a few DATA frames, then silence — no END.
        frames = [message(MSG_START_ACK, nus_sim.start_ack_payload(device_type), session_id)]
        data, _ = nus_sim.data_messages(device_type, 128, session_id=session_id,
                                        records=records)
        frames.extend(data[:5])
        return frames, False

    return nus_sim.full_sequence(device_type, session_id, records=records), True
