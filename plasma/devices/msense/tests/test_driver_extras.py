"""Ported YAMS driver extras — journaler helpers, flash-erase passcode gate,
capability-flag SQC filtering, battery packet parse. No BLE."""
import struct

from plasma.devices.template import PlasmaMemo
from plasma import journal
from plasma.devices.msense.device import MotionSenseHRV, ERASE_CODE


# ── journaler (core leaf) ───────────────────────────────────────────────────

def test_journal_stream_name_and_types():
    assert journal.JOURNAL_STREAM == "PLASMA"
    assert journal.MSG_TYPES == ["Task start", "Task end", "Flag"]


def test_format_journal_msg():
    assert journal.format_journal_msg("Task start", "walk", "") == "Task start [walk]"
    assert journal.format_journal_msg("Flag", "", "odd noise") == "Flag [] odd noise"


def test_task_labels_default_and_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
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
    assert struct.unpack("<I", data)[0] == ERASE_CODE
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
