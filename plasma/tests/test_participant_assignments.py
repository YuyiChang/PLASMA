"""Multi-participant sessions exercise the real driver against simulated BLE."""
import csv
import json
import logging
import os
from unittest.mock import Mock

import pytest

from plasma.config import device_config
from plasma.devices.msense.device import CTL_ENC_CHAR_UUID
from plasma.devices.msense_demo.device import MSenseDemo, CTL_STARTSTOP_CHAR_UUID
from plasma.integrated_panel import IntegratedPanel
from plasma.lsl_session import SessionInfo
from plasma.participant_assignments import identity
from plasma.devices.msense.config import SUBJECT_OVERRIDE, OVERRIDE_ENABLED


def _assign(dev, address, value):
    from plasma.devices.msense_demo.config import _synth_addr
    blob = device_config.plugins[dev.CONFIG_KEY]
    for row in blob["devices"]:
        if _synth_addr(row["Name"], row.get("Nickname", "")) == address:
            row[SUBJECT_OVERRIDE] = value


@pytest.fixture
def group(tmp_path, monkeypatch):
    monkeypatch.setitem(device_config.plugins, "msense_demo", {"devices": [
        {"Name": "Band A", "Sensor": "PPG", "Enabled": True, "IMU Stream": False, SUBJECT_OVERRIDE: "1001"},
        {"Name": "Band B", "Sensor": "ECG", "Enabled": True, "IMU Stream": False}], OVERRIDE_ENABLED: True})
    p = IntegratedPanel.__new__(IntegratedPanel)
    p.log_root = str(tmp_path / "data")
    p.logger = logging.getLogger("participant-test")
    p.session_info = SessionInfo("sub-1001", "ses-02", 100102, p.log_root)
    p.record_lsl, p.lsl_recorder = False, None
    p.journal_outlet, p.task_sequence = Mock(), Mock()
    d = MSenseDemo(p.session_info, logger=None, tag="Demo")
    p.available_devices = [d]
    yield p, d, list(d.device_list)
    d.stop()
    d.dispose()


@pytest.mark.parametrize("sub,ses,expected", [
    ("sub-1001", "ses-02", ("sub-1001", "ses-02", 100102)),
    (" sub-1 ", "ses-0", ("sub-0001", "ses-00", 100)),
    ("sub-42949672", "ses-95", ("sub-42949672", "ses-95", 0xFFFFFFFF))])
def test_identity(sub, ses, expected):
    assert identity(sub, ses) == expected


@pytest.mark.parametrize("sub,ses", [("abc", "ses-02"), ("sub-1", "ses-100"),
    ("sub-42949672", "ses-96"), ("sub-../1", "ses-02"), ("sub--1", "ses-02")])
def test_invalid_identity(sub, ses):
    with pytest.raises(ValueError):
        identity(sub, ses)


def test_start_freezes_verified_ids_before_acquisition_and_routes_files(group, monkeypatch):
    p, d, (a, b) = group
    _assign(d, b, "1002")
    # Editing after Initialize must require no new connection or outlet.
    clients, outlets = dict(d.active_devices), dict(d.active_outlets)
    seen = []
    for addr, client in clients.items():
        original = client.read_gatt_char

        async def read(char, addr=addr, original=original):
            if char == CTL_ENC_CHAR_UUID:
                seen.append(addr)
            return await original(char)
        monkeypatch.setattr(client, "read_gatt_char", read)
    enable = d._write_acq_enable

    def assert_preflight(client, start):
        if start:
            assert {a, b} <= set(seen)
        return enable(client, start)
    monkeypatch.setattr(d, "_write_acq_enable", assert_preflight)
    p.start_collection()
    assert [clients[x]._last_enc for x in (a, b)] == [100102, 100202]
    assert d.active_devices == clients and d.active_outlets == outlets
    assert p.sts == "Collection in progress"
    manifest = json.load(open(os.path.join(p.session_dir, "participant_assignments.json")))
    assert manifest["status"] == "recording"
    assert [x["encoding"] for x in manifest["assignments"]] == [100102, 100202]
    assert len(list(csv.DictReader(open(os.path.join(p.session_dir, "session_encoding.csv"))))) == 2
    assert p.journal_outlet.push_sample.call_args_list[0].args[0][0].startswith("[PARTICIPANT_MAP] ")
    for addr, subject in [(a, "sub-1001"), (b, "sub-1002")]:
        folder = os.path.join(p.session_dir, "msense", subject, "msense_demo")
        assert outlets[addr].log_dir == folder
        assert subject in d.memo[addr].label
        path = d._save_sqc_capture(addr, 2, b"raw", {"test": True})
        assert os.path.dirname(path) == folder
        sidecar = json.load(open(os.path.splitext(path)[0] + ".json"))
        assert sidecar["participant"]["subject_id"] == subject
    # Saving configuration during a run must leave the frozen mapping untouched.
    _assign(d, b, "2000")
    device_config.plugins[d.CONFIG_KEY][OVERRIDE_ENABLED] = False
    assert d.participant_assignments[b].subject_id == "sub-1002"
    for action in [lambda: p.get_participant_encoding("sub-1003", "ses-03"),
                   lambda: p.start_collection()]:
        with pytest.raises(ValueError):
            action()
    assert "⛔" in d.write_enc(999)
    p.stop_collection()
    assert json.load(open(os.path.join(p.session_dir, "participant_assignments.json")))["status"] == "stopped"
    # Draft edits do not relabel the preceding run's captures.
    assert d.participant_assignments[b].subject_id == "sub-1002"
    p.start_collection()
    assert not d.participant_assignments
    assert [clients[x]._last_enc for x in (a, b)] == [100102, 100102]
    assert "sub-1002" not in d.memo[b].label
    p.stop_collection()


@pytest.mark.parametrize("fault", ["readback", "missing"])
def test_preflight_failure_never_enables_any_wristband(group, monkeypatch, fault):
    p, d, (a, b) = group
    if fault == "missing":
        d.active_devices[b].is_connected = False
    else:
        original = d.active_devices[b].read_gatt_char

        async def wrong(char):
            return b"\0\0\0\0" if char == CTL_ENC_CHAR_UUID else await original(char)
        monkeypatch.setattr(d.active_devices[b], "read_gatt_char", wrong)
    with pytest.raises(RuntimeError, match=b):
        p.start_collection()
    for client in d.active_devices.values():
        assert not any(char == CTL_STARTSTOP_CHAR_UUID and data == b"\x01"
                       for char, data in client.writes)
    assert not p._assignments_running()
    assert p.sts == "Collection start failed"


def test_start_failure_rolls_back_started_acquisition(group, monkeypatch):
    p, d, (a, b) = group
    original = d.collection_ctl

    def fail_second(addr, start=True):
        if addr == b and start:
            raise RuntimeError("acquisition unavailable")
        return original(addr, start)
    monkeypatch.setattr(d, "collection_ctl", fail_second)
    with pytest.raises(RuntimeError, match="acquisition unavailable"):
        p.start_collection()
    assert not any(client._recording for client in d.active_devices.values())
    assert not p._assignments_running()
    assert json.load(open(os.path.join(p.session_dir, "participant_assignments.json")))["status"] == "failed"


def test_config_overrides_persist_leading_zeroes_and_inherit_default(group):
    from plasma.config import DeviceConfig
    p, d, (a, b) = group
    _assign(d, a, "0007")
    device_config.update_plugin_config(d.CONFIG_KEY, device_config.get_plugin_config(d.CONFIG_KEY))
    reloaded = DeviceConfig()
    assert reloaded.get_plugin_config(d.CONFIG_KEY)["devices"][0][SUBJECT_OVERRIDE] == "0007"
    p.get_participant_encoding("sub-1005", "ses-06")
    assignments = {addr: assignment for _, addr, assignment in p._compile_assignments()}
    assert assignments[a].subject_id == "sub-0007"
    assert assignments[a].encoding == 706
    assert assignments[b].subject_id == "sub-1005"
    assert not assignments[b].overridden
    p.start_collection()
    assert "Subject ID override: sub-0007" in d.memo[a].label
    assert d.memo[b].label == "Band B"
    p.stop_collection()
    device_config.plugins[d.CONFIG_KEY][OVERRIDE_ENABLED] = False
    assert not p._load_participant_overrides()


@pytest.mark.parametrize("enabled", [False, True])
def test_no_override_has_no_extra_memo_message_or_group_directory(group, enabled):
    p, d, _ = group
    device_config.plugins[d.CONFIG_KEY][OVERRIDE_ENABLED] = enabled
    if enabled:
        for row in device_config.plugins[d.CONFIG_KEY]["devices"]:
            row[SUBJECT_OVERRIDE] = ""
    p.start_collection()
    assert not d.participant_assignments
    assert "group-sessions" not in p.session_dir
    assert all("override" not in m.label.lower() for m in d.memo.values())
    p.stop_collection()


def test_recording_failure_aborts_before_acquisition(group, monkeypatch):
    p, d, _ = group
    p.record_lsl = True
    monkeypatch.setattr(p, "_start_recorder", lambda: None)
    with pytest.raises(RuntimeError, match="XDF recorder"):
        p.start_collection()
    assert not any(client._recording for client in d.active_devices.values())


def test_reconnect_reapplies_frozen_encoding_and_stop_does_not_resume(group):
    p, d, (_, b) = group
    _assign(d, b, "2002")
    p.start_collection()
    old = d.active_devices[b]
    d._reconnect_peripheral(b, "participant-test")
    new = d.active_devices[b]
    assert new is not old
    assert new._last_enc == 200202 and new._recording
    p.stop_collection()
    d._reconnect_peripheral(b, "stopped-test")
    assert not d.active_devices[b]._recording


def test_real_xdf_contains_participant_map_and_stable_device_ids(group):
    import uuid
    import time
    pyxdf = pytest.importorskip("pyxdf")
    from plasma.app_context import configure, app_context
    from plasma.journal import open_journal_outlet

    p, d, (a, b) = group
    configure(journal_stream="participant-map-test-" + uuid.uuid4().hex)
    p.journal_outlet = open_journal_outlet()
    p.record_lsl = True
    _assign(d, b, "1002")
    try:
        p.start_collection()
        path = p.lsl_recorder.status()["file"]
        time.sleep(0.7)
    finally:
        p.stop_collection()
    streams, _ = pyxdf.load_xdf(path, dejitter_timestamps=False, synchronize_clocks=False)
    journal = next(s for s in streams if s["info"]["name"][0] == app_context().journal_stream)
    maps = [json.loads(row[0].split(" ", 1)[1]) for row in journal["time_series"]
            if row[0].startswith("[PARTICIPANT_MAP] ")]
    assert len(maps) == 1
    assert {r["encoding"] for r in maps[0]["assignments"]} == {100102, 100202}
    for addr in (a, b):
        sensor = next(s for s in streams if s["info"]["source_id"][0] == addr)
        record = next(r for r in maps[0]["assignments"] if r["address"] == addr)
        assert record["stream_name"] == sensor["info"]["name"][0]
        assert record["source_id"] == addr
