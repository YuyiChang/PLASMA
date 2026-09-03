"""Offline filtering helpers for MSense ECG & PPG snapshots.

Pure numeric functions only — no BLE/device state. The "ECG/PPG Signal
Quality" tab uses these purely for a light filtered overlay on top of the raw
waveform; contact/SQI scoring is intentionally out of scope (the operator
eyeballs the raw signal). See:

- plasma/nus_stream.py — NUS bounded-stream request/receive protocol
- plasma/ppg_ecg_records.py — packed PPG/ECG record decoders
- local_docs/NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md,
  local_docs/PPG_PACKED_16_BYTE_FORMAT.md,
  local_docs/ECG_TEMP_DATA_FORMAT.md — the wire/record contracts
"""
import numpy as np
from scipy.signal import butter, filtfilt, iirnotch


def _bandpass(x, fs, low, high, order=3):
    nyq = fs / 2.0
    b, a = butter(order, [low / nyq, high / nyq], btype="band")
    return filtfilt(b, a, x)


def filter_ecg(x, fs, notch_hz=60.0, notch_q=30.0):
    """0.5-40 Hz bandpass + mains notch — standard ECG cleanup band."""
    x = np.asarray(x, dtype=float)
    if notch_hz and notch_hz < fs / 2:
        b, a = iirnotch(notch_hz, notch_q, fs)
        x = filtfilt(b, a, x)
    return _bandpass(x, fs, 0.5, min(40.0, fs / 2 - 0.5))


def filter_ppg(x, fs):
    """0.5-8 Hz bandpass — covers resting through vigorous-exercise heart rate."""
    x = np.asarray(x, dtype=float)
    return _bandpass(x, fs, 0.5, min(8.0, fs / 2 - 0.5))
