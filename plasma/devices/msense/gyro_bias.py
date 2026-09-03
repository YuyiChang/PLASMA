"""Persistent per-wristband gyro bias, keyed by UUID/MAC (hardware identity,
robust to renaming the alias in plasma_device_config.json)."""

import json
import os
import time

_GYRO_BIAS_FILE = "plasma_gyro_bias.json"


def load_gyro_bias():
    """Returns {uuid_or_mac: (bx, by, bz)}, or {} if no calibration exists yet."""
    if not os.path.exists(_GYRO_BIAS_FILE):
        return {}
    try:
        with open(_GYRO_BIAS_FILE, 'r') as f:
            raw = json.load(f)
        return {addr: tuple(rec["bias"]) for addr, rec in raw.items()}
    except Exception:
        return {}


def save_gyro_bias(addr, bias, n_samples):
    raw = {}
    if os.path.exists(_GYRO_BIAS_FILE):
        try:
            with open(_GYRO_BIAS_FILE, 'r') as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    raw[addr] = {
        "bias": list(bias),
        "n_samples": n_samples,
        "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(_GYRO_BIAS_FILE, 'w') as f:
        json.dump(raw, f, indent=2)
