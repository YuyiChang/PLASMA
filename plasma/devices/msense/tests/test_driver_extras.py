"""Ported YAMS driver extras — journaler helpers, flash-erase passcode gate,
capability-flag SQC filtering, battery packet parse. No BLE."""
import struct
import threading
import time

import pytest

from plasma.devices.template import PlasmaMemo
from plasma import journal
from plasma.app_context import app_context
from plasma.devices.msense.device import MotionSenseHRV, ERASE_CODE
from plasma.devices.msense.nus_stream import (
    StreamSession, PROFILE, DEVICE_ECG, MSG_START_ACK, MSG_END,
)
from . import test_nus_stream as _tns


# ── journaler (core leaf) ───────────────────────────────────────────────────

def test_journal_stream_name_and_types():
    assert app_context().journal_stream == "PLASMA"
    assert journal.MSG_TYPES == ["Task start", "Task end", "Flag"]


def test_format_journal_msg():
    assert journal.format_journal_msg("Task start", "walk", "") == "Task start [walk]"
    assert journal.format_journal_msg("Flag", "", "odd noise") == "Flag [] odd noise"


def test_task_labels_default_and_file(tmp_path):
    # tmp_path is the app home (see the autouse _isolate_app_state fixture)
    assert journal.task_labels() == ["A", "B", "C", "D", "E"]
    (tmp_path / "task.txt").write_text("walk\nsit\n\nstairs\n")
    assert journal.task_labels() == ["walk", "sit", "stairs"]


# ── driver helpers (constructed without going through __init__) ──────────────

class _FakePeripheral:
    def __init__(self, connected=True):
        self._connected = connected
        self.writes = []

    def is_connected(self):
        return self._connected

    def write_request(self, svc, ch, data):
        self.writes.append((svc, ch, data))

    def disconnect(self):
        self._connected = False


def _bare_driver():
    d = MotionSenseHRV.__new__(MotionSenseHRV)
    d.logger = None
    d.active_devices = {}
    d.active_outlets = {}
    d.caps = {}
    d.battery = {}
    d.memo = {}
    d.t_start = 0.0
    d._sqc_threads_stopped = False
    return d


def test_erase_wrong_code_is_a_noop():
    d = _bare_driver()
    p = _FakePeripheral()
    d.active_devices = {"w1": p}
    d.active_outlets = {"w1": object()}
    d.memo = {"w1": PlasmaMemo("w1")}

    assert d.erase_flash_data(67) == "⛔ wrong erase code"
    assert d.erase_flash_data("nope") == "⛔ wrong erase code"
    assert p.writes == []
    assert d.active_devices == {"w1": p}   # not disconnected


def test_erase_right_code_writes_68_and_disconnects():
    d = _bare_driver()
    p = _FakePeripheral()
    d.active_devices = {"w1": p}
    d.active_outlets = {"w1": object()}
    d.memo = {"w1": PlasmaMemo("w1")}

    msg = d.erase_flash_data(ERASE_CODE)

    assert "erase issued to 1" in msg
    assert len(p.writes) == 1
    _svc, _ch, data = p.writes[0]
    assert len(data) == 1  # single unsigned byte, not a 4-byte word
    assert struct.unpack("<B", data)[0] == ERASE_CODE
    assert d.active_devices == {} and d.active_outlets == {}


def test_get_sqc_devices_filters_on_nus_capability():
    d = _bare_driver()
    d.active_devices = {"w1": object(), "w2": object(), "w3": object()}
    d.caps = {
        "w1": {"nus": True, "imu": False, "battery": True},
        "w2": {"nus": False, "imu": False, "battery": True},
        "w3": {"nus": True, "imu": True, "battery": True},
    }
    assert d.get_sqc_devices() == ["w1", "w3"]
    assert "NUS unavailable on: w2" in d.caps_summary()


def test_battery_handler_parses_and_stores():
    d = _bare_driver()
    d.memo = {"w1": PlasmaMemo("w1", channels=["battery"])}
    d.caps = {"w1": {"nus": True, "imu": False, "battery": False}}

    d.battery_handler(bytes([90]), None, "w1")

    assert d.battery["w1"] == 90
    assert d.caps["w1"]["battery"] is True
    assert d.memo["w1"].get_latest("battery")[1] == 90


# ── SQC streaming mode dispatch ─────────────────────────────────────────────

def test_sqc_history_phase_done():
    done = MotionSenseHRV._sqc_history_phase_done
    # forward phase -> always done, regardless of status
    assert done({"phase": "forward", "status": "receiving"}) is True
    # still in history and still going -> not done
    assert done({"phase": "history", "status": "receiving"}) is False
    assert done({"phase": "history", "status": "requesting"}) is False
    # terminal without ever reaching forward (history-only quick mode, an
    # early error, a rejection) -> done, nothing more to wait for
    assert done({"phase": "history", "status": "ready"}) is True
    for status in ("error", "rejected", "idle", "unavailable"):
        assert done({"phase": None, "status": status}) is True


def _stub_driver_for_dispatch():
    d = _bare_driver()
    d.sqc_state = {}
    d.caps = {"w1": {"nus": True}, "w2": {"nus": True}}
    d.active_devices = {"w1": object(), "w2": object()}
    d._ensure_sqc_threads = lambda: None  # skip real debug/watchdog threads
    return d


def test_request_all_sqc_snapshots_rejects_unknown_mode():
    d = _stub_driver_for_dispatch()
    assert d.request_all_sqc_snapshots(stream_mode="ludicrous") == \
        "⛔ unknown streaming mode: 'ludicrous'"
    assert getattr(d, "_sqc_run_thread", None) is None


def test_request_all_sqc_snapshots_dispatches_to_matching_runner():
    calls = []
    for mode, runner_attr in (("sequential", "_run_sqc_sequential"),
                               ("parallel", "_run_sqc_parallel"),
                               ("hybrid", "_run_sqc_hybrid")):
        d = _stub_driver_for_dispatch()
        setattr(d, runner_attr,
                lambda names, ms, ho, calls=calls: calls.append((names, ms, ho)))
        msg = d.request_all_sqc_snapshots(max_seconds=3, history_only=False,
                                          stream_mode=mode)
        d._sqc_run_thread.join(timeout=1.0)
        assert not d._sqc_run_thread.is_alive()
        assert calls[-1] == (["w1", "w2"], 3, False)
        assert mode in msg or (mode == "sequential" and "one at a time" in msg)


def test_request_all_sqc_snapshots_rejects_overlapping_run():
    d = _stub_driver_for_dispatch()
    started = threading.Event()
    release = threading.Event()

    def _blocking_runner(names, ms, ho):
        started.set()
        release.wait(timeout=2.0)

    d._run_sqc_sequential = _blocking_runner
    d.request_all_sqc_snapshots(stream_mode="sequential")
    assert started.wait(timeout=1.0)

    assert d.request_all_sqc_snapshots(stream_mode="parallel") == \
        "⏳ A snapshot run is already in progress"

    release.set()
    d._sqc_run_thread.join(timeout=1.0)


# ── _nus_data_handler: unsolicited/clean cancels still get plotted ─────────

def _driver_with_sqc_session(name="w1"):
    d = _bare_driver()
    d.sqc_state = {name: MotionSenseHRV._new_sqc_state()}
    d.sqc_state[name].update(session=StreamSession(_tns.SID), status="requesting")
    return d


def test_nus_data_handler_finalizes_unsolicited_clean_cancel_as_partial(monkeypatch):
    """A CANCELLED end this driver did NOT itself request (early_cancel_sent
    unset — e.g. a manual "Cancel all" click, or a device-initiated cancel)
    must still be decoded/plotted, not discarded as a hard error."""
    name = "w1"
    d = _driver_with_sqc_session(name)
    finish_calls = []

    def _fake_finish(name_, partial=False, warning=None):
        finish_calls.append((name_, partial, warning))
        d.sqc_state[name_]["status"] = "ready"
    monkeypatch.setattr(d, "_finish_sqc_snapshot", _fake_finish)

    d._nus_data_handler(_tns._msg(MSG_START_ACK, _tns._start_ack_payload(DEVICE_ECG)), name)
    data, _ = _tns._data_msgs(DEVICE_ECG, 100)
    for m in data[:5]:
        d._nus_data_handler(m, name)

    rs = PROFILE[DEVICE_ECG]["record_size"]
    local_bytes = 5 * 100 * rs
    end = _tns._end_payload(DEVICE_ECG, 5, status=0x0008,
                            override={"history": 500, "forward": 0, "total": local_bytes})
    d._nus_data_handler(_tns._msg(MSG_END, end), name)

    assert finish_calls == [(name, True, None)]   # partial=True, no warning
    assert d.sqc_state[name]["status"] == "ready"  # not "error"


def test_nus_data_handler_finalizes_cancel_with_data_loss_as_warned_partial(monkeypatch):
    """A CANCELLED end whose counts don't match what was locally received
    still gets decoded/plotted (not discarded), but carries a warning."""
    name = "w1"
    d = _driver_with_sqc_session(name)
    finish_calls = []

    def _fake_finish(name_, partial=False, warning=None):
        finish_calls.append((name_, partial, warning))
        d.sqc_state[name_]["status"] = "ready"
    monkeypatch.setattr(d, "_finish_sqc_snapshot", _fake_finish)

    d._nus_data_handler(_tns._msg(MSG_START_ACK, _tns._start_ack_payload(DEVICE_ECG)), name)
    data, _ = _tns._data_msgs(DEVICE_ECG, 100)
    for m in data[:5]:
        d._nus_data_handler(m, name)

    rs = PROFILE[DEVICE_ECG]["record_size"]
    local_bytes = 5 * 100 * rs
    end = _tns._end_payload(DEVICE_ECG, 5, status=0x0008,
                            override={"history": 500, "forward": 0, "total": local_bytes - rs})
    d._nus_data_handler(_tns._msg(MSG_END, end), name)

    assert len(finish_calls) == 1
    _, partial, warning = finish_calls[0]
    assert partial is True
    assert warning is not None and "data loss during cancel" in warning
    assert d.sqc_state[name]["status"] == "ready"  # still plotted, not "error"


# ── bounded BLE calls (auto-reconnect freeze fix) ───────────────────────────
#
# simplepyble's connect()/disconnect() are blocking C calls with no timeout
# of their own; observed in production hanging the shared watchdog thread
# (and possibly the whole app) forever against an unreachable peripheral.
# _run_ble_op bounds that; these tests never let a "hung" fn actually run
# unbounded — they prove the caller gets control back promptly regardless.

def test_run_ble_op_success():
    ok, result, err = MotionSenseHRV._run_ble_op(lambda: None, 1.0, "noop")
    assert ok is True
    assert result is None
    assert err is None


def test_run_ble_op_returns_fns_result():
    ok, result, err = MotionSenseHRV._run_ble_op(lambda: b"\x5a", 1.0, "read")
    assert ok is True
    assert result == b"\x5a"
    assert err is None


def test_run_ble_op_propagates_exception():
    def _boom():
        raise RuntimeError("nope")
    ok, result, err = MotionSenseHRV._run_ble_op(_boom, 1.0, "boom")
    assert ok is False
    assert result is None
    assert isinstance(err, RuntimeError)
    assert str(err) == "nope"


def test_run_ble_op_times_out_without_waiting_for_the_blocked_call():
    release = threading.Event()

    def _hangs():
        release.wait()  # simulates a stuck simplepyble call — never returns

    start = time.time()
    ok, result, err = MotionSenseHRV._run_ble_op(_hangs, 0.2, "hangs")
    elapsed = time.time() - start

    assert ok is False
    assert result is None
    assert isinstance(err, TimeoutError)
    assert elapsed < 1.0  # returned promptly at ~timeout_s, not stuck forever
    release.set()  # let the leaked daemon thread finish, don't outlive the test


def test_register_nus_notify_bounded_when_notify_hangs(monkeypatch):
    """peripheral.notify() is just as unbounded as connect()/disconnect() in
    simplepyble — a hang here (e.g. subscribing right after a reconnect, on a
    still-marginal link) must not stall the caller forever either. All four
    register_* methods share this same _run_ble_op-wrapped shape; NUS stands
    in for the group."""
    monkeypatch.setattr("plasma.devices.msense.device.RECONNECT_OP_TIMEOUT_S", 0.2)
    release = threading.Event()

    class _HangingPeripheral(_FakePeripheral):
        def notify(self, svc, ch, callback):
            release.wait()  # never returns within the test

    d = _bare_driver()
    p = _HangingPeripheral()

    start = time.time()
    with pytest.raises(TimeoutError):
        d.register_nus_notify(p, "w1")
    elapsed = time.time() - start

    assert elapsed < 1.0
    release.set()  # let the leaked daemon thread finish


def test_reconnect_peripheral_bounded_when_connect_hangs(monkeypatch):
    """A peripheral.connect() that never returns must not stall
    _reconnect_peripheral forever — it should give up after the bounded
    timeout and mark the device as failed instead of hanging."""
    monkeypatch.setattr("plasma.devices.msense.device.RECONNECT_OP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # skip the real 1.5s pause

    release = threading.Event()

    class _HangingPeripheral(_FakePeripheral):
        def connect(self):
            release.wait()  # never returns within the test

    d = _bare_driver()
    p = _HangingPeripheral()
    d.active_devices = {"w1": p}
    d.caps = {"w1": {}}
    d.memo = {"w1": PlasmaMemo("w1")}
    d.imu_stream_devices = set()

    start = time.time()
    d._reconnect_peripheral("w1", "test")
    elapsed = time.time() - start

    assert elapsed < 1.0
    assert d.memo["w1"].sts == "🔌 reconnect failed"
    release.set()  # let the leaked daemon thread finish
