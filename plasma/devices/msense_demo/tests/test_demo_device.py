"""The simulated MSense device drives the real driver end to end — no BLE.

Uses the repo conftest's PLASMA_HOME isolation. Each test seeds the
``msense_demo`` plugin blob on the shared ``device_config`` singleton and
restores it afterwards.
"""
import atexit
import time

import pytest

from plasma.config import device_config
from plasma.devices.msense import records
from plasma.devices.msense.device import MotionSenseHRV
from plasma.devices.msense_demo import device as demo_device
from plasma.devices.msense_demo.device import MSenseDemo, FakePeripheral

SESSION_INFO = {"sub_id": "sub-01", "ses_id": "ses-01", "participant_enc": 1234}


def _blob(*rows):
    return {"devices": list(rows)}


def _row(name, sensor="PPG", enabled=True, imu=True, fault="none"):
    return {"Name": name, "Nickname": "", "Sensor": sensor, "Enabled": enabled,
            "IMU Stream": imu, "Fault": fault}


@pytest.fixture
def demo_blob(monkeypatch):
    saved = device_config.plugins.get("msense_demo")
    monkeypatch.setattr(device_config, "demo_mode", True)

    def _set(*rows):
        device_config.plugins["msense_demo"] = _blob(*rows)

    yield _set
    if saved is None:
        device_config.plugins.pop("msense_demo", None)
    else:
        device_config.plugins["msense_demo"] = saved


def _make_device():
    d = MSenseDemo(SESSION_INFO, logger=None, tag="MSense Demo (simulated)")
    return d


def _teardown(d):
    atexit.unregister(d._shutdown_cleanup)   # no exit-time cleanup on a dead loop
    d._sqc_threads_stopped = True
    d.auto_reconnect = False
    for p in list(d.active_devices.values()):
        p._alive = False
        p._sqc_cancel.set()
    # the watchdog may be mid _reconnect_peripheral (a 1.5 s sleep) — wait it out
    # so nothing submits a coroutine after the loop stops
    wd = getattr(d, "_sqc_wd_thread", None)
    if wd is not None:
        wd.join(timeout=5)
    try:
        d.disconnect()
    finally:
        d._stop_ble_loop()


def _wait(pred, timeout=15.0, interval=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


# ── plugin gating ───────────────────────────────────────────────────────────

def test_register_is_gated_on_demo_mode(monkeypatch):
    from plasma.devices import msense_demo

    monkeypatch.setattr(device_config, "demo_mode", False)
    calls = []
    msense_demo.register(calls.append)
    assert calls == []

    monkeypatch.setattr(device_config, "demo_mode", True)
    msense_demo.register(calls.append)
    assert [p.id for p in calls] == ["msense_demo"]
    assert calls[0].enabled_by_default is False


# ── encoders round-trip through the real decoders ───────────────────────────

def test_ppg_payload_decodes_cleanly():
    from plasma.devices.msense_demo import fake_stream
    out = records.decode_ppg(fake_stream.ppg_payload(1024))
    assert len(out["ir1"]) == 1024
    assert out["oob_frac"] == 0.0
    assert out["ir1"].std() > 0            # an actual waveform, not a constant


def test_ecg_payload_decodes_with_good_crc():
    from plasma.devices.msense_demo import fake_stream
    out = records.decode_ecg(fake_stream.ecg_payload(3))   # 3 ECB2 blocks
    assert out["error"] is None
    assert out["blocks"] == 3
    assert len(out["ecg"]) == 3 * 1358
    assert out["ecg"].max() > out["ecg"].mean() + 1000    # QRS spikes present


def test_ecg_payload_leading_zero_slots():
    from plasma.devices.msense_demo import fake_stream
    out = records.decode_ecg(fake_stream.ecg_payload(2, leading_zero_slots=3))
    assert out["error"] is None
    assert out["skipped_history_slots"] == 3
    assert out["blocks"] == 2


# ── connect / live streaming ────────────────────────────────────────────────

def test_connects_and_streams_live(demo_blob):
    demo_blob(_row("DEMO-PPG-01", sensor="PPG", imu=True))
    d = _make_device()
    try:
        assert isinstance(d, MotionSenseHRV)
        assert "DEMO-PPG-01" in d.active_devices
        assert isinstance(d.active_devices["DEMO-PPG-01"], FakePeripheral)
        assert d.get_sqc_devices() == ["DEMO-PPG-01"]

        d.session_dir = None
        d.start()
        memo = d.memo["DEMO-PPG-01"]
        assert _wait(lambda: len(memo.channels.get("ENMO", [])) >= 2, timeout=6)
        assert _wait(lambda: len(memo.channels.get("OrientW", [])) >= 5, timeout=6)
        d.stop()
    finally:
        _teardown(d)


# ── SQC snapshot, happy path ────────────────────────────────────────────────

@pytest.mark.parametrize("sensor,fs", [("PPG", 256.0), ("ECG", 512.0)])
def test_sqc_snapshot_ready(demo_blob, sensor, fs):
    demo_blob(_row(f"DEMO-{sensor}", sensor=sensor, imu=False))
    d = _make_device()
    try:
        name = f"DEMO-{sensor}"
        d.request_sqc_snapshot(name)
        assert _wait(lambda: d.get_sqc_status(name)["status"] in ("ready", "error"),
                     timeout=20)
        assert d.get_sqc_status(name)["status"] == "ready"
        res = d.get_sqc_result(name)
        assert res["device_type"] == sensor
        assert res["fs"] == fs
        first = next(iter(res["channels"].values()))
        assert len(first) > 1000
    finally:
        _teardown(d)


# ── live INFINITY stream ───────────────────────────────────────────────────

@pytest.mark.parametrize("sensor", ["PPG", "ECG"])
def test_live_stream_starts_and_stops(demo_blob, sensor):
    demo_blob(_row(f"DEMO-{sensor}", sensor=sensor, imu=False))
    d = _make_device()
    try:
        name = f"DEMO-{sensor}"
        msg = d.start_live_stream(name)
        assert "live" in msg
        assert _wait(lambda: d.get_live_stream_status(name)["status"] == "streaming",
                     timeout=10)

        def _enough():
            p = d.get_live_stream_preview(name)
            return p is not None and len(next(iter(p["channels"].values()))) > 200
        assert _wait(_enough, timeout=15)
        prev = d.get_live_stream_preview(name)
        assert prev["device_type"] == sensor

        d.stop_live_stream(name)
        assert _wait(lambda: d.get_live_stream_status(name)["status"] == "stopped",
                     timeout=10)
    finally:
        _teardown(d)


def test_live_stream_stall_recovers(demo_blob, monkeypatch):
    monkeypatch.setattr(demo_device, "DEMO_DISCONNECT_AFTER_S", 9e9)
    monkeypatch.setattr("plasma.devices.msense.device.SQC_NOPROGRESS_TIMEOUT_S", 0.8)
    demo_blob(_row("DEMO-ECG", sensor="ECG", imu=False, fault="stream_stall"))
    d = _make_device()
    try:
        name = "DEMO-ECG"
        d.start_live_stream(name)
        assert _wait(lambda: d.get_live_stream_status(name)["status"] == "error",
                     timeout=15)
    finally:
        _teardown(d)


# ── faults ──────────────────────────────────────────────────────────────────

def test_fault_device_not_found(demo_blob):
    demo_blob(_row("GHOST", fault="device_not_found"))
    d = _make_device()
    try:
        assert "GHOST" not in d.active_devices
        assert d.memo["GHOST"].sts == "⛔ device not found"
    finally:
        _teardown(d)


def test_fault_disconnect_then_reconnect(demo_blob):
    demo_blob(_row("DEMO-PPG-01", fault="disconnect_reconnect"))
    d = _make_device()
    try:
        name = "DEMO-PPG-01"
        first_client = d.active_devices[name]
        d.session_dir = None
        d.start()
        d.trigger_disconnect(name)
        assert d.memo[name].sts == "🔌 disconnected"

        d._reconnect_peripheral(name, "test")
        assert d.memo[name].sts == "🔄 reconnected"
        assert d.active_devices[name] is not first_client
        assert isinstance(d.active_devices[name], FakePeripheral)
        d.stop()
    finally:
        _teardown(d)


def test_fault_sqc_stream_stall(demo_blob, monkeypatch):
    monkeypatch.setattr(demo_device, "DEMO_DISCONNECT_AFTER_S", 9e9)
    monkeypatch.setattr("plasma.devices.msense.device.SQC_NOPROGRESS_TIMEOUT_S", 0.8)
    demo_blob(_row("DEMO-ECG", sensor="ECG", imu=False, fault="sqc_error"))
    d = _make_device()
    try:
        name = "DEMO-ECG"
        d.request_sqc_snapshot(name)
        assert _wait(lambda: d.memo[name].sts == "⚠️ stream stalled", timeout=15)
        assert d.get_sqc_status(name)["status"] == "error"
        assert d.sqc_state[name]["diag"]["recoveries"] >= 1
    finally:
        _teardown(d)
