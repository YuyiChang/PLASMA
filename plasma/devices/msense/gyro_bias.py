"""Persistent per-wristband gyro bias, keyed by UUID/MAC (hardware identity,
robust to renaming the alias in plasma_device_config.json)."""

import json
import os
import time

from plasma.app_context import app_context


def load_gyro_bias():
    """Returns {uuid_or_mac: (bx, by, bz)}, or {} if no calibration exists yet."""
    path = app_context().gyro_bias_path
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        return {addr: tuple(rec["bias"]) for addr, rec in raw.items()}
    except Exception:
        return {}


def save_gyro_bias(addr, bias, n_samples):
    path = app_context().gyro_bias_path
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    raw[addr] = {
        "bias": list(bias),
        "n_samples": n_samples,
        "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, 'w') as f:
        json.dump(raw, f, indent=2)
