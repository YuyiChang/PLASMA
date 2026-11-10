"""QB2 lifecycle and recording integrity without a physical sensor or liblsl sockets."""
import asyncio
import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from plasma.config import DeviceConfig
from plasma import plugins


@pytest.fixture
def qb2(monkeypatch):
    pytest.importorskip("blickfeld_qb2")
    from plasma.devices import qb2 as module

    class Info:
        def __init__(self, *args):
            self.args = args

        def desc(self):
            return self

        def append_child_value(self, *args):
            return self

    class Outlet:
        def __init__(self, info):
            self.samples = []
            self.info = info

        def push_sample(self, sample, timestamp):
            self.samples.append((sample, timestamp))

    monkeypatch.setattr(module, "StreamInfo", Info)
    monkeypatch.setattr(module, "StreamOutlet", Outlet)
    monkeypatch.setattr(module, "plasma_config", SimpleNamespace(
        ip_lidar="192.0.2.1", qb2_api_key="test-secret"))
    return module


class Source:
    def __init__(self, module, monkeypatch, ids=(), fail=None):
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.frames_sent = threading.Event()
        self.args = []
        self.tokens = []
        source = self

        class Channel:
            def __init__(self, **kwargs):
                source.args.append(kwargs)

            def __enter__(self):
                # Channel must be constructed inside a running event loop.
                asyncio.get_running_loop()
                source.entered.set()
                return self

            def __exit__(self, *args):
                source.closed.set()

        class PointCloud:
            def __init__(self, channel):
                pass

            async def async_stream(self):
                from blickfeld_qb2.core_processing.data import Frame, FrameBinary
                for fid in ids:
                    yield SimpleNamespace(frame=Frame(
                        id=fid, timestamp=123456, binary=FrameBinary(
                            length=2, cartesian=np.array([[1, 2, 3], [4, 5, 6]], dtype="<f4"),
                            photon_count=np.array([10, 20], dtype="<u2"))))
                    await asyncio.sleep(0)
                source.frames_sent.set()
                if fail:
                    raise fail
                await asyncio.Event().wait()

        def token_factory(**kwargs):
            source.tokens.append(kwargs)
            return "token-factory"

        monkeypatch.setattr(module.blickfeld_qb2, "Channel", Channel)
        monkeypatch.setattr(module.blickfeld_qb2, "TokenFactory", token_factory)
        monkeypatch.setattr(module.blickfeld_qb2.core_processing.services, "PointCloud", PointCloud)


def device(module, tmp_path):
    return module.Qb2Unit({"log_dir": str(tmp_path / "old-sub" / "old-ses"),
                       "participant_enc": 100}, logging.getLogger("test-qb2"), "qb2 LiDAR")


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true")


def test_shared_folder_payload_auth_and_uint64(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch, ids=[2**63 + 1, 2**63 + 2])
    dev = device(qb2, tmp_path)
    assert not (tmp_path / "old-sub").exists()  # Initialize creates no recording folder
    dev.session_dir = str(tmp_path / "new-sub" / "ses-02" / "shared-with-xdf")
    try:
        dev.start()
        wait_for(lambda: dev.frames_saved == 2)
    finally:
        dev.stop()
    assert source.closed.is_set()
    assert source.args == [{"fqdn_or_ip": "192.0.2.1", "token": "token-factory", "secure": True}]
    assert source.tokens == [{"application_key_secret": "test-secret"}]
    paths = sorted(dev.out_dir.glob("*.npy"))
    assert len(paths) == 2
    assert dev.out_dir.parent == tmp_path / "new-sub" / "ses-02" / "shared-with-xdf"
    saved = np.load(paths[0], allow_pickle=True).item()
    assert saved["frame"].id == 2**63 + 1
    assert saved["frame"].timestamp == 123456
    np.testing.assert_array_equal(saved["frame"].binary.cartesian, [[1, 2, 3], [4, 5, 6]])
    np.testing.assert_array_equal(saved["frame"].binary.photon_count, [10, 20])
    assert saved["unix_time"] > 0
    assert saved["datetime"].endswith("+00:00")
    assert dev.outlet.samples[0] == ([str(2**63 + 1)], saved["lsl_time"])
    assert dev.memo.sts == "🟥 stopped"


def test_stop_interrupts_stalled_read_and_disconnect_is_idempotent(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch)
    dev = device(qb2, tmp_path)
    dev.start()
    assert source.frames_sent.wait(2)
    before = time.monotonic()
    dev.disconnect()
    dev.disconnect()
    assert time.monotonic() - before < 1
    assert source.closed.is_set()
    assert not dev._thread.is_alive()
    assert dev.outlet is None


def test_no_key_omits_token_and_restart_does_not_overwrite(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch, ids=[7, 7])
    qb2.plasma_config.qb2_api_key = ""
    dev = device(qb2, tmp_path)
    dev.session_dir = str(tmp_path / "session")
    for _ in range(2):
        try:
            dev.start()
            wait_for(lambda: dev.frames_saved == 2)
        finally:
            dev.stop()
    assert len(list(dev.out_dir.glob("*.npy"))) == 4
    assert all("token" not in args for args in source.args)
    assert source.tokens == []


def test_timeout_is_visible_without_xdf_recorder(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch)
    dev = device(qb2, tmp_path)
    dev.FRAME_TIMEOUT_S = 0.03
    dev.start()
    dev._thread.join(2)
    assert not dev._thread.is_alive()
    assert "No QB2 frame" in dev.memo.sts
    dev.stop()
    assert dev.memo.sts.startswith("⛔")  # Stop must not erase a capture failure
    assert source.closed.is_set()


def test_network_failure_drains_received_frames_and_redacts_secret(qb2, monkeypatch, tmp_path, caplog):
    source = Source(qb2, monkeypatch, ids=[1, 2], fail=RuntimeError("invalid test-secret"))
    dev = device(qb2, tmp_path)
    with caplog.at_level(logging.INFO):
        dev.start()
        dev._thread.join(2)
    assert not dev._thread.is_alive()
    assert dev.frames_saved == 2
    assert source.closed.is_set()
    assert "[redacted]" in dev.memo.sts
    assert "test-secret" not in caplog.text


def test_write_failure_interrupts_reader_and_publishes_no_marker(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch, ids=[1])
    dev = device(qb2, tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(qb2.np, "save", fail)
    dev.start()
    dev._thread.join(2)
    assert not dev._thread.is_alive()
    assert source.closed.is_set()
    assert "disk full" in dev.memo.sts
    assert dev.outlet.samples == []
    assert list(dev.out_dir.iterdir()) == []  # no incomplete .npy or partial left behind


def test_queue_overflow_reports_loss_and_drains_accepted_frames(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch, ids=range(30))
    dev = device(qb2, tmp_path)
    dev.WRITE_QUEUE_SIZE = 1
    save = dev._save_frame

    def slow_save(payload):
        time.sleep(0.03)
        return save(payload)

    monkeypatch.setattr(dev, "_save_frame", slow_save)
    dev.start()
    dev._thread.join(3)
    assert not dev._thread.is_alive()
    assert "queue full" in dev.memo.sts
    assert 0 < dev.frames_saved < 30
    assert len(list(dev.out_dir.glob("*.npy"))) == dev.frames_saved
    assert source.closed.is_set()


def test_stop_drains_pending_disk_write_and_refuses_parallel_start(qb2, monkeypatch, tmp_path):
    Source(qb2, monkeypatch, ids=[1])
    dev = device(qb2, tmp_path)
    writing, release = threading.Event(), threading.Event()
    save = dev._save_frame

    def delayed(payload):
        writing.set()
        assert release.wait(3)
        return save(payload)

    monkeypatch.setattr(dev, "_save_frame", delayed)
    dev.STOP_TIMEOUT_S = 0.02
    try:
        dev.start()
        assert writing.wait(2)
        with pytest.raises(RuntimeError, match="still active"):
            dev.start()
        with pytest.raises(RuntimeError, match="timed out"):
            dev.stop()
        assert "stop unconfirmed" in dev.memo.sts
        with pytest.raises(RuntimeError, match="still active"):
            dev.start()
    finally:
        release.set()
        dev._thread.join(3)
        dev.stop()
    assert dev.frames_saved == 1
    assert not dev._thread.is_alive()


def test_unconfigured_device_fails_before_creating_outlet(qb2, tmp_path):
    qb2.plasma_config.ip_lidar = ""
    dev = device(qb2, tmp_path)
    assert dev.memo.sts.startswith("⛔")
    with pytest.raises(RuntimeError, match="Configuration"):
        dev.start()


def test_settings_persist_export_and_import(tmp_path):
    plugins.load_plugins()
    cfg = DeviceConfig()
    cfg.ip_lidar, cfg.qb2_api_key = "192.0.2.1", "demo-key"
    cfg._apply(["qb2 LiDAR"], "", False)
    loaded = DeviceConfig()
    assert loaded.ip_lidar == "192.0.2.1"
    assert loaded.qb2_api_key == "demo-key"
    exported, _ = cfg._export_config(cfg._active, "")
    try:
        result = cfg._import_config(exported)
        assert "Loaded" in result[2]
        assert DeviceConfig().get_plugin_config("qb2")["devices"][0]["api_key"] == "demo-key"
    finally:
        os.unlink(exported)
        os.rmdir(os.path.dirname(exported))
    # Legacy files without plugin config must also persist and clear old keys.
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"enabled_devices": ["qb2 LiDAR"], "ip_qb2_lidar": "192.0.2.2"}))
    cfg._import_config(str(legacy))
    assert DeviceConfig().ip_lidar == "192.0.2.2"
    assert DeviceConfig().qb2_api_key == ""


def test_settings_failed_replace_keeps_previous_file(tmp_path, monkeypatch):
    cfg = DeviceConfig()
    cfg._save()
    from plasma.app_context import app_context
    path = app_context().config_path
    previous = open(path).read()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    cfg.qb2_api_key = "must-not-truncate-config"
    with pytest.raises(OSError):
        cfg._save()
    assert open(path).read() == previous
    assert not list(tmp_path.glob(".plasma-config-*"))
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_missing_sdk_is_visible_without_blocking_other_devices(qb2, monkeypatch, tmp_path):
    monkeypatch.setattr(qb2, "blickfeld_qb2", None)
    dev = device(qb2, tmp_path)
    assert 'plasma-app[qb2]' in dev.memo.sts
    assert dev.outlet is None


def test_outlet_failure_is_visible(qb2, monkeypatch, tmp_path):
    def fail(info):
        raise RuntimeError("could not create stream outlet")

    monkeypatch.setattr(qb2, "StreamOutlet", fail)
    dev = device(qb2, tmp_path)
    assert "could not create stream outlet" in dev.memo.sts
    assert dev.outlet is None


def test_immediate_stop_leaves_no_worker(qb2, monkeypatch, tmp_path):
    Source(qb2, monkeypatch)
    dev = device(qb2, tmp_path)
    dev.start()
    dev.stop()
    assert not dev._thread.is_alive()
    assert dev._fault is None


def test_unexpected_stream_end_is_a_fault(qb2, monkeypatch, tmp_path):
    class Ended:
        def __init__(self, channel):
            pass

        async def async_stream(self):
            if False:
                yield

    source = Source(qb2, monkeypatch)
    monkeypatch.setattr(qb2.blickfeld_qb2.core_processing.services, "PointCloud", Ended)
    dev = device(qb2, tmp_path)
    dev.start()
    dev._thread.join(2)
    assert not dev._thread.is_alive()
    assert "ended unexpectedly" in dev.memo.sts
    assert source.closed.is_set()


def multi_device(module, tmp_path, monkeypatch, rows):
    cfg = DeviceConfig()
    cfg.update_plugin_config("qb2", {"devices": rows})
    monkeypatch.setattr(module, "plasma_config", cfg)
    dev = module.Qb2({"log_dir": str(tmp_path / "data"), "participant_enc": 100},
                     logging.getLogger("test-qb2-multi"))
    dev.session_dir = str(tmp_path / "shared-session")
    return dev


def test_many_devices_mixed_keys_and_disabled(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch, ids=[42])
    rows = [{"id": f"unit-{i}", "address": f"192.0.2.{i+1}", "name": f"LiDAR {i}",
             "api_key": f"key-{i}" if i % 2 else "", "enabled": i != 3} for i in range(12)]
    dev = multi_device(qb2, tmp_path, monkeypatch, rows)
    assert len(dev.units) == 11
    assert "unit-3" not in dev.memo
    assert set(dev.get_sources()) == set(dev.units)
    assert len(dev.lsl_streams()) == 11
    try:
        dev.start()
        wait_for(lambda: all(getattr(u, "frames_saved", 0) == 1 for u in dev.units.values()))
    finally:
        dev.stop()
    assert len(source.args) == 11
    assert sum("token" in a for a in source.args) == 5
    assert {a["fqdn_or_ip"] for a in source.args} == {r["address"] for r in rows if r["enabled"]}
    for identity, unit in dev.units.items():
        assert unit.out_dir == tmp_path / "shared-session" / "qb2" / identity
        assert len(list(unit.out_dir.glob("*.npy"))) == 1
        assert not unit._thread.is_alive()
        assert dev.lsl_streams()[unit.stream_name] == identity
    dev.disconnect()
    assert all(u.outlet is None for u in dev.units.values())


def test_one_lidar_failure_does_not_stop_peers(qb2, monkeypatch, tmp_path):
    Source(qb2, monkeypatch, ids=[10])
    original_channel = qb2.blickfeld_qb2.Channel

    def channel(**kwargs):
        if kwargs["fqdn_or_ip"] == "bad.example":
            raise RuntimeError("bad-key authentication failed")
        return original_channel(**kwargs)

    monkeypatch.setattr(qb2.blickfeld_qb2, "Channel", channel)
    dev = multi_device(qb2, tmp_path, monkeypatch, [
        {"id": "bad", "address": "bad.example", "api_key": "bad-key"},
        {"id": "good", "address": "good.example"},
    ])
    try:
        dev.start()
        wait_for(lambda: dev.units["bad"]._fault is not None and dev.units["good"].frames_saved == 1)
        assert "bad-key" not in dev.memo["bad"].sts
        assert dev.memo["good"].sts == "🟢"
        assert dev.units["good"]._thread.is_alive()
    finally:
        dev.stop()
    assert dev.memo["bad"].sts.startswith("⛔")


def test_token_factory_failure_terminates_worker(qb2, monkeypatch, tmp_path):
    Source(qb2, monkeypatch)

    def invalid(**kwargs):
        raise ValueError("invalid test-secret")

    monkeypatch.setattr(qb2.blickfeld_qb2, "TokenFactory", invalid)
    dev = device(qb2, tmp_path)
    dev.start()
    dev._thread.join(2)
    assert not dev._thread.is_alive()
    assert "[redacted]" in dev.memo.sts


def test_all_disabled_creates_no_workers_outlets_or_folders(qb2, monkeypatch, tmp_path):
    source = Source(qb2, monkeypatch)
    dev = multi_device(qb2, tmp_path, monkeypatch, [
        {"id": "disabled", "address": "192.0.2.1", "enabled": False}])
    dev.start()
    dev.stop()
    assert dev.units == {}
    assert dev.lsl_streams() == {}
    assert source.args == []
    assert not (tmp_path / "shared-session").exists()


def test_device_editor_save_disable_remove_and_export(tmp_path):
    from plasma.devices.qb2_config import configured_devices, save_device, remove_device
    cfg = DeviceConfig()
    cfg.ip_lidar, cfg.qb2_api_key = "192.0.2.1", "legacy-key"
    legacy = configured_devices(cfg)[0]
    assert legacy["api_key"] == "legacy-key"
    saved = save_device(cfg, legacy["id"], "Left", "192.0.2.1", "", False)
    other = save_device(cfg, None, "Right", "qb2-example", "new-key", True)
    loaded = configured_devices(DeviceConfig())
    assert len(loaded) == 2
    assert loaded[0]["id"] == saved
    assert loaded[0]["enabled"] is False
    assert loaded[0]["api_key"] == ""
    assert loaded[1]["id"] == other
    exported, _ = cfg._export_config([], "")
    try:
        imported = DeviceConfig()
        assert "Loaded" in imported._import_config(exported)[2]
        assert configured_devices(imported) == loaded
    finally:
        os.unlink(exported)
        os.rmdir(os.path.dirname(exported))
    remove_device(cfg, saved)
    remove_device(cfg, other)
    assert configured_devices(cfg) == []  # legacy values cannot resurrect a removed device
    assert configured_devices(DeviceConfig()) == []


@pytest.mark.parametrize("rows", [
    [{"id": "a", "address": "qb2-host"}, {"id": "b", "address": "QB2-HOST."}],
    [{"id": "a", "address": "192.0.2.1"}, {"id": "a", "address": "192.0.2.2"}],
    [{"id": "../escape", "address": "192.0.2.1"}],
    [{"id": "a", "address": "http://192.0.2.1"}],
    [{"id": "a", "address": ""}],
    [{"id": "a", "address": "192.0.2.1", "enabled": "false"}],
])
def test_invalid_device_list_is_rejected_without_overwriting(rows):
    from plasma.devices.qb2_config import normalize_devices
    with pytest.raises(ValueError):
        normalize_devices(rows)


def test_invalid_import_preserves_saved_device_list(tmp_path):
    from plasma.devices.qb2_config import configured_devices, save_device
    cfg = DeviceConfig()
    save_device(cfg, None, "Good", "192.0.2.1", "", True)
    previous = configured_devices(cfg)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"plugins": {"qb2": {"devices": [{"address": "http://bad"}]}}}))
    assert "Import error" in cfg._import_config(str(invalid))[2]
    assert configured_devices(DeviceConfig()) == previous


def test_multi_stop_signals_all_and_releases_finished_outlets(qb2, monkeypatch, tmp_path):
    Source(qb2, monkeypatch, ids=[1])
    dev = multi_device(qb2, tmp_path, monkeypatch, [
        {"id": "slow", "address": "slow.example"},
        {"id": "fast", "address": "fast.example"},
    ])
    slow = dev.units["slow"]
    writing, release = threading.Event(), threading.Event()
    original = slow._save_frame

    def delayed(payload):
        writing.set()
        assert release.wait(3)
        return original(payload)

    monkeypatch.setattr(slow, "_save_frame", delayed)
    # Allow the idle writer's 100-ms wake-up while holding only one disk write.
    monkeypatch.setattr(qb2.Qb2Unit, "STOP_TIMEOUT_S", 0.3)
    try:
        dev.start()
        assert writing.wait(2)
        wait_for(lambda: dev.units["fast"].frames_saved == 1)
        with pytest.raises(RuntimeError, match="Stop unconfirmed"):
            dev.disconnect()
        assert all(u._stop_event.is_set() for u in dev.units.values())
        assert not dev.units["fast"]._thread.is_alive()
        assert dev.units["fast"].outlet is None
        assert slow.outlet is not None  # never release a worker's active outlet
    finally:
        release.set()
        slow._thread.join(3)
        dev.disconnect()
    assert slow.frames_saved == 1


def test_failed_device_save_restores_in_memory_settings(monkeypatch):
    from plasma.devices.qb2_config import configured_devices, save_device
    cfg = DeviceConfig()
    save_device(cfg, None, "First", "192.0.2.1", "", True)
    previous = configured_devices(cfg)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "_save", fail)
    with pytest.raises(OSError):
        save_device(cfg, None, "Second", "192.0.2.2", "", True)
    assert configured_devices(cfg) == previous
    assert configured_devices(DeviceConfig()) == previous
