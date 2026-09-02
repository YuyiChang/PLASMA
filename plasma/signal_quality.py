"""Offline filtering / quality-check helpers for MSense ECG & PPG snapshots.

Pure numeric functions only — no BLE/device state lives here. See
plasma/devices/msense.py for the NUS request/receive pipeline and
data/ECG_PPG_SIGNAL_QUALITY_BLE_NUS.md for the (still-provisional) wire
protocol these operate on.
"""
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks, iirnotch


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


def estimate_heart_rate(filtered, fs, min_bpm=35, max_bpm=220):
    """Peak-count HR estimate. Returns (bpm_or_None, peak_indices)."""
    filtered = np.asarray(filtered, dtype=float)
    min_distance = int(fs * 60.0 / max_bpm)
    peaks, _ = find_peaks(filtered, distance=max(min_distance, 1), prominence=np.std(filtered) * 0.5)
    if len(peaks) < 2:
        return None, peaks
    intervals_s = np.diff(peaks) / fs
    bpm = 60.0 / np.median(intervals_s)
    if not (min_bpm <= bpm <= max_bpm):
        return None, peaks
    return float(bpm), peaks


def signal_quality_index(raw, filtered, clip_value=None):
    """Coarse good/marginal/poor label from three cheap checks: clipping
    (raw sample pinned at/near the ADC rail), flatlining (near-zero sample-
    to-sample change — likely no skin contact), and in-band SNR (filtered
    signal power vs. what filtering removed).

    This is a fast sanity check for "is this contact/fit good enough to
    start a real recording", not a validated clinical SQI.
    """
    raw = np.asarray(raw, dtype=float)
    filtered = np.asarray(filtered, dtype=float)

    flags = []
    if clip_value is not None:
        clip_frac = float(np.mean(np.abs(raw) >= 0.98 * clip_value))
        if clip_frac > 0.01:
            flags.append(f"clipping {clip_frac:.1%}")

    flat_frac = float(np.mean(np.abs(np.diff(raw)) < 1e-9))
    if flat_frac > 0.5:
        flags.append(f"flat {flat_frac:.1%}")

    residual = (raw - np.mean(raw)) - filtered
    sig_power = np.var(filtered)
    noise_power = np.var(residual) + 1e-9
    snr_db = 10 * np.log10(sig_power / noise_power) if sig_power > 0 else -np.inf

    if flags or snr_db < 0:
        label = "🔴 poor"
    elif snr_db < 6:
        label = "🟡 marginal"
    else:
        label = "🟢 good"

    return {"label": label, "snr_db": snr_db, "flags": flags}
