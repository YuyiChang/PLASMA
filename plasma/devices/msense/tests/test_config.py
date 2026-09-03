"""Offline coverage for MSense record handling + ble_scan.

No BLE, no Gradio: these exercise the pure normalize / merge / scan-filter logic.
"""
import numpy as np
import pandas as pd
import pytest

import json

from plasma.devices.msense import ble_scan
from plasma.devices.msense.config import (
    _MSENSE_COLUMNS,
    _normalize_msense,
    _msense_records_from_df,
    merge_msense_records,
    merge_device_info_into_blob,
    import_device_info,
    display_labels,
)


# ── _normalize_msense ───────────────────────────────────────────────────────

def test_normalize_list_without_nickname_key():
    out = _normalize_msense([
        {"Name": "w1", "UUID / MAC Address": "u1", "Enabled": False, "IMU Stream": True},
    ])
    assert out[0]["Nickname"] == ""
    assert out[0]["Enabled"] is False
    assert out[0]["IMU Stream"] is True


def test_normalize_list_preserves_nickname():
    out = _normalize_msense([
        {"Name": "w1", "Nickname": "left wrist", "UUID / MAC Address": "u1"},
    ])
    assert out[0]["Nickname"] == "left wrist"


# ── _msense_records_from_df ─────────────────────────────────────────────────

def test_records_from_df_handles_nan_nickname_and_drops_blank_rows():
    df = pd.DataFrame(
        [
            {"Name": "w1", "Nickname": np.nan, "UUID / MAC Address": "u1", "Enabled": True, "IMU Stream": False},
            {"Name": "w2", "Nickname": "right", "UUID / MAC Address": "u2", "Enabled": False, "IMU Stream": True},
            {"Name": "", "Nickname": "orphan", "UUID / MAC Address": "", "Enabled": True, "IMU Stream": False},
        ],
        columns=_MSENSE_COLUMNS,
    )
    recs = _msense_records_from_df(df)
    assert [r["Name"] for r in recs] == ["w1", "w2"]
    assert recs[0]["Nickname"] == ""
    assert recs[1]["Nickname"] == "right"
    assert recs[1]["Enabled"] is False and recs[1]["IMU Stream"] is True


# ── display_labels ─────────────────────────────────────────────────────────

def test_display_labels_name_paren_nickname():
    blob = {"devices": [
        {"Name": "MSense4ECG-Z5G4A", "Nickname": "left wrist", "UUID / MAC Address": "u1", "Enabled": True},
        {"Name": "MSense4ECG-EX4BT", "Nickname": "", "UUID / MAC Address": "u2", "Enabled": True},
        {"Name": "MSense4ECG-OFF", "Nickname": "unused", "UUID / MAC Address": "u3", "Enabled": False},
        {"Name": "", "Nickname": "orphan", "UUID / MAC Address": "u4", "Enabled": True},
    ]}
    out = display_labels(blob)
    assert out == {
        "MSense4ECG-Z5G4A": "MSense4ECG-Z5G4A (left wrist)",
        "MSense4ECG-EX4BT": "MSense4ECG-EX4BT",
    }


# ── merge_msense_records ────────────────────────────────────────────────────

_EXISTING = [
    {"Name": "w1", "Nickname": "keep me", "UUID / MAC Address": "aa-bb", "Enabled": False, "IMU Stream": True},
]


def test_merge_append_skips_existing_address_case_insensitive():
    scanned = [{"name": "w1-new", "address": "AA-BB"}, {"name": "w2", "address": "CC-DD"}]
    recs, msg = merge_msense_records(_EXISTING, scanned, overwrite=False)
    assert [r["UUID / MAC Address"] for r in recs] == ["aa-bb", "CC-DD"]
    # existing row untouched (toggles + nickname preserved)
    assert recs[0] == _EXISTING[0]
    assert recs[1] == {
        "Name": "w2", "Nickname": "", "UUID / MAC Address": "CC-DD",
        "Enabled": True, "IMU Stream": False,
    }
    assert "1 already listed" in msg


def test_merge_overwrite_replaces_and_resets_toggles():
    scanned = [{"name": "w9", "address": "EE-FF"}]
    recs, msg = merge_msense_records(_EXISTING, scanned, overwrite=True)
    assert recs == [{
        "Name": "w9", "Nickname": "", "UUID / MAC Address": "EE-FF",
        "Enabled": True, "IMU Stream": False,
    }]
    assert "Overwrote" in msg


# ── ble_scan.scan_msense ───────────────────────────────────────────────────

class _FakePeripheral:
    def __init__(self, name, addr, connectable=True):
        self._name, self._addr, self._connectable = name, addr, connectable

    def identifier(self):
        return self._name

    def address(self):
        return self._addr

    def is_connectable(self):
        return self._connectable


class _FakeAdapter:
    def __init__(self, results):
        self._results = results
        self.scanned_ms = None

    def scan_for(self, ms):
        self.scanned_ms = ms

    def scan_get_results(self):
        return self._results


def test_scan_msense_filters_dedupes_uppercases():
    adapter = _FakeAdapter([
        _FakePeripheral("MSense4PPG-AAA", "uuid-aaa"),
        _FakePeripheral("SomeOtherDevice", "uuid-xxx"),
        _FakePeripheral("MSense4ECG-BBB", "uuid-bbb"),
        _FakePeripheral("MSense4PPG-AAA", "UUID-AAA"),  # dup address, different case
        _FakePeripheral("", "uuid-empty"),
    ])
    out = ble_scan.scan_msense(timeout_ms=1234, adapter=adapter)
    assert adapter.scanned_ms == 1234
    assert [d["address"] for d in out] == ["UUID-AAA", "UUID-BBB"]
    assert out[0]["name"] == "MSense4PPG-AAA"
    assert all(d["connectable"] is True for d in out)


# ── device_info.json import (YAMS provisioning) ─────────────────────────────

def test_import_device_info(tmp_path):
    p = tmp_path / "device_info.json"
    p.write_text(json.dumps({"4BF01S": "2104F8E3-D94A-1DF7", "Left 74N": "D3:54:EB:A4:9B:82", "blank": ""}))
    out = import_device_info(str(p))
    assert out == [
        {"name": "4BF01S", "address": "2104F8E3-D94A-1DF7"},
        {"name": "Left 74N", "address": "D3:54:EB:A4:9B:82"},
    ]


def test_import_device_info_missing_or_bad(tmp_path):
    assert import_device_info(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]")
    assert import_device_info(str(bad)) == []


def test_merge_device_info_into_blob_append(tmp_path):
    p = tmp_path / "device_info.json"
    p.write_text(json.dumps({"newdev": "AA-BB-CC"}))
    blob = {"devices": [{"Name": "old", "Nickname": "", "UUID / MAC Address": "11-22",
                         "Enabled": True, "IMU Stream": False}]}
    new_blob, msg = merge_device_info_into_blob(blob, str(p), overwrite=False)
    names = [r["Name"] for r in new_blob["devices"]]
    assert names == ["old", "newdev"]
    assert "1 new" in msg


def test_merge_device_info_into_blob_overwrite(tmp_path):
    p = tmp_path / "device_info.json"
    p.write_text(json.dumps({"a": "1", "b": "2"}))
    new_blob, _ = merge_device_info_into_blob({"devices": [{"Name": "x", "UUID / MAC Address": "9"}]},
                                              str(p), overwrite=True)
    assert [r["Name"] for r in new_blob["devices"]] == ["a", "b"]
