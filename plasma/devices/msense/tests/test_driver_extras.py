"""Ported YAMS driver extras — journaler helpers, flash-erase passcode gate,
capability-flag SQC filtering, battery packet parse. No real BLE — fakes are
bleak-shaped (see _FakePeripheral) and run against a real asyncio event loop
via _bare_driver's _start_ble_loop(), exercising the actual _run_async bridge."""
import asyncio
import struct
import threading
import time

import pytest

from plasma.devices.template import PlasmaMemo
from plasma import journal
from plasma.app_context import app_context
from plasma.devices.msense.device import (
    MotionSenseHRV, ERASE_CODE, AcquisitionStopNotConfirmed,
)
from plasma.devices.msense import nus_sim
from plasma.devices.msense.nus_stream import (
    StreamSession, PROFILE, ECG, MODE_FINITE, MODE_INFINITY,
    MSG_START_ACK, MSG_END, END_STOPPED, FINITE_TOTAL_BYTES,
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
    """Bleak-shaped fake: is_connected/address are plain attributes (bleak
    itself exposes is_connected as a property — reading it works the same
    either way); connect/disconnect/write_gatt_char/read_gatt_char/
    start_notify are coroutines, matching BleakClient's real API. Used
    against a real asyncio loop (via _bare_driver's _start_ble_loop), so
    tests exercise the actual _run_async bridge, not a mock of it."""
    def __init__(self, address="AA:BB:CC:DD:EE:FF", connected=True):
        self.address = address
        self.is_connected = connected
        self.writes = []
        # da39c931 readback: 0/1, or a callable raising to simulate a read error
        self.acq_enabled = 0

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def write_gatt_char(self, char_uuid, data, response=True):
        self.writes.append((char_uuid, bytes(data)))

    async def read_gatt_char(self, char_uuid):
        if char_uuid.startswith("da39c931"):
            if callable(self.acq_enabled):
                return self.acq_enabled()          # e.g. lambda: (_ for _ in ()).throw(...)
            return bytes([int(self.acq_enabled)])
        return b"\x00"

    async def start_notify(self, char_uuid, callback):
        pass


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
    d._connect_rssi = {}
    d.sqc_state = {}
    d.live_state = {}
    d._acq_stop_status = {}
    d._start_ble_loop()
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
    _ch, data = p.writes[0]
    assert len(data) == 1  # single unsigned byte, not a 4-byte word
    assert struct.unpack("<B", data)[0] == ERASE_CODE
    assert d.active_devices == {} and d.active_outlets == {}


def test_finish_gyro_calibration_is_keyed_by_address(monkeypatch):
    saved = []
    monkeypatch.setattr("plasma.devices.msense.device.save_gyro_bias",
                        lambda addr, bias, n: saved.append((addr, bias, n)))
    d = _bare_driver()
    addr = "E4:B0:AA:BB:CC:DD"
    d._state_lock = threading.Lock()
    d.gyro_bias = {}
    d.gyro_calib = {addr: {"until": 0.0, "sum": [3.0, 6.0, 9.0], "n": 3}}
    d.memo = {addr: PlasmaMemo(addr, label="MSense4ECG (left)")}
    d.display_labels = {addr: "MSense4ECG (left)"}

    d._finish_gyro_calibration(addr)

    assert d.gyro_bias[addr] == (1.0, 2.0, 3.0)
    assert saved == [(addr, (1.0, 2.0, 3.0), 3)]
    assert d.memo[addr].sts == "✅ Bias saved"


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


def test_ensure_mtu_noop_without_bluez_backend():
    # macOS/Windows real backends + the test fake have no _backend._acquire_mtu.
    d = _bare_driver()
    d._ensure_mtu(_FakePeripheral(), "w1")  # must not raise


def test_ensure_mtu_forces_bluez_exchange():
    d = _bare_driver()

    class _BlueZBackend:
        _mtu_size = None  # bleak's default — no negotiation happened yet

        async def _acquire_mtu(self):
            self._mtu_size = 247  # AcquireWrite → BlueZ reports the real MTU

    class _BlueZPeripheral(_FakePeripheral):
        def __init__(self):
            super().__init__()
            self._backend = _BlueZBackend()

    p = _BlueZPeripheral()
    d._ensure_mtu(p, "w1")
    assert p._backend._mtu_size == 247

    # idempotent: a second call doesn't re-acquire
    p._backend._acquire_mtu = None
    d._ensure_mtu(p, "w1")  # must not raise


def test_battery_handler_parses_and_stores():
    d = _bare_driver()
    d.memo = {"w1": PlasmaMemo("w1", channels=["battery"])}
    d.caps = {"w1": {"nus": True, "imu": False, "battery": False}}

    d.battery_handler(bytes([90]), "w1")

    assert d.battery["w1"] == 90
    assert d.caps["w1"]["battery"] is True
    assert d.memo["w1"].get_latest("battery")[1] == 90


# ── acquisition-stop confirmation (da39c931 readback) ──────────────────────

def _acq_driver(monkeypatch, acq_enabled):
    monkeypatch.setattr("plasma.devices.msense.device.ACQ_STOP_READBACK_INTERVAL_S", 0.0)
    d = _bare_driver()
    p = _FakePeripheral()
    p.acq_enabled = acq_enabled
    d.active_devices = {"w1": p}
    d.caps = {"w1": {}}
    d.memo = {"w1": PlasmaMemo("w1")}
    d.journal_marks = []
    d.journal_hook = d.journal_marks.append
    d.session_info = {"participant_enc": 1}
    d.imu_stream_devices = set()
    return d, p


def test_collection_stop_confirmed_when_readback_zero(monkeypatch):
    d, p = _acq_driver(monkeypatch, acq_enabled=0)
    d.collection_ctl("w1", start=False)               # no raise
    assert d.get_acq_stop_status("w1")["status"] == "confirmed"
    # exactly one enable write (the stop), no retry
    assert [w for w in p.writes if w[0].startswith("da39c931")] == \
        [("da39c931-1d81-48e2-9c68-d0ae4bbd351f", b"\x00")]


def test_collection_stop_unconfirmed_retries_then_raises(monkeypatch):
    d, p = _acq_driver(monkeypatch, acq_enabled=1)     # device never stops
    with pytest.raises(AcquisitionStopNotConfirmed):
        d.collection_ctl("w1", start=False)
    assert d.get_acq_stop_status("w1")["status"] == "unconfirmed"
    # the stop write was issued twice (initial + one retry)
    stop_writes = [w for w in p.writes if w[0].startswith("da39c931") and w[1] == b"\x00"]
    assert len(stop_writes) == 2


def test_collection_stop_unverifiable_when_read_raises(monkeypatch):
    def _boom():
        raise RuntimeError("char not readable")
    d, p = _acq_driver(monkeypatch, acq_enabled=_boom)
    d.collection_ctl("w1", start=False)               # no raise — degrades safely
    assert d.get_acq_stop_status("w1")["status"] == "unverifiable"


def test_collection_start_clears_prior_stop_status(monkeypatch):
    d, p = _acq_driver(monkeypatch, acq_enabled=1)
    with pytest.raises(AcquisitionStopNotConfirmed):
        d.collection_ctl("w1", start=False)
    assert d.get_acq_stop_status("w1")["status"] == "unconfirmed"
    p.acq_enabled = 0
    d.register_enmo = lambda *a: None
    d.register_battery = lambda *a: None
    d.collection_ctl("w1", start=True)
    assert d.get_acq_stop_status("w1")["status"] == "unknown"


@pytest.mark.parametrize("acq,sts_contains,mark_contains", [
    (0, "stopped", "stop confirmed"),
    (1, "still recording", "UNCONFIRMED"),
])
def test_stop_maps_outcome_to_memo_and_journal(monkeypatch, acq, sts_contains, mark_contains):
    d, p = _acq_driver(monkeypatch, acq_enabled=acq)
    d.active_outlets = {}
    d.live_state = {}
    d.stop()
    assert sts_contains in d.memo["w1"].sts
    assert any(mark_contains in m for m in d.journal_marks)


def test_stop_unverifiable_keeps_plain_glyph(monkeypatch):
    d, p = _acq_driver(monkeypatch, acq_enabled=lambda: (_ for _ in ()).throw(RuntimeError()))
    d.active_outlets = {}
    d.live_state = {}
    d.stop()
    assert d.memo["w1"].sts == "🛑"
    assert any("not verifiable" in m for m in d.journal_marks)


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
    d.sqc_state[name].update(
        session=StreamSession(_tns.SID, product=ECG, expect_mode=MODE_FINITE),
        status="requesting", product=ECG)
    return d


def test_nus_data_handler_finalizes_early_stop_as_partial(monkeypatch):
    """A STOPPED end (quick mode, a manual "Cancel all", or a device-initiated
    stop) must still be decoded/plotted, not discarded as a hard error."""
    name = "w1"
    d = _driver_with_sqc_session(name)
    finish_calls = []

    def _fake_finish(name_, partial=False, warning=None):
        finish_calls.append((name_, partial, warning))
        d.sqc_state[name_]["status"] = "ready"
    monkeypatch.setattr(d, "_finish_sqc_snapshot", _fake_finish)

    d._nus_data_handler(_tns._msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)), name)
    for m in nus_sim.data_stream(bytes(20_000), stream_id=_tns.SID):
        d._nus_data_handler(m, name)
    d._nus_data_handler(nus_sim.end_message(END_STOPPED, _tns.SID), name)

    assert finish_calls == [(name, True, None)]   # partial=True, no warning
    assert d.sqc_state[name]["status"] == "ready"  # not "error"


def test_nus_data_handler_finalizes_protocol_violation_as_warned_partial(monkeypatch):
    """A mid-stream framing violation with bytes already accumulated still gets
    decoded/plotted (not discarded), but carries a warning."""
    name = "w1"
    d = _driver_with_sqc_session(name)
    finish_calls = []

    def _fake_finish(name_, partial=False, warning=None):
        finish_calls.append((name_, partial, warning))
        d.sqc_state[name_]["status"] = "ready"
    monkeypatch.setattr(d, "_finish_sqc_snapshot", _fake_finish)

    d._nus_data_handler(_tns._msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)), name)
    d._nus_data_handler(nus_sim.data_message(0, b"\x11" * 200, _tns.SID), name)
    d._nus_data_handler(nus_sim.data_message(9999, b"\x22" * 200, _tns.SID), name)  # gap

    assert len(finish_calls) == 1
    _, partial, warning = finish_calls[0]
    assert partial is True
    assert warning is not None and "protocol violation" in warning
    assert d.sqc_state[name]["status"] == "ready"


# ── SQC ↔ session journaler auto-markers ───────────────────────────────────

def _driver_with_journal():
    d = _bare_driver()
    d.sqc_state = {}
    d._sqc_journal_open = set()
    d.journal_marks = []
    d.journal_hook = d.journal_marks.append  # what IntegratedPanel wires in
    return d


def test_plasmadevice_journal_is_a_noop_without_a_hook():
    d = _bare_driver()
    d.journal_hook = None
    d.journal("nothing wired")  # must not raise


def test_sqc_request_pushes_start_marker(monkeypatch):
    d = _driver_with_journal()
    monkeypatch.setattr(d, "_ensure_sqc_threads", lambda: None)
    p = _FakePeripheral()
    p.mtu_size = 247
    d.active_devices = {"w1": p}
    d.caps = {"w1": {"nus": True, "product": ECG}}

    msg = d.request_sqc_snapshot("w1")

    assert "waiting for START_ACK" in msg
    assert d.journal_marks == ["[SQC] w1 start (full)"]
    assert "w1" in d._sqc_journal_open
    assert p.writes and p.writes[0][0].startswith("6e400002")  # OP_START written


def test_sqc_request_marker_reflects_capture_mode(monkeypatch):
    d = _driver_with_journal()
    monkeypatch.setattr(d, "_ensure_sqc_threads", lambda: None)
    p = _FakePeripheral()
    p.mtu_size = 247
    d.active_devices = {"w1": p}
    d.caps = {"w1": {"nus": True, "product": ECG}}

    d.request_sqc_snapshot("w1", history_only=True)
    assert d.journal_marks == ["[SQC] w1 start (history-only)"]


def test_journal_finished_sqc_closes_marker_on_terminal_status():
    d = _driver_with_journal()
    d._sqc_journal_open = {"w1"}
    d.sqc_state = {"w1": {"status": "ready"}}

    d._journal_finished_sqc()
    assert d.journal_marks == ["[SQC] w1 end (ready)"]
    assert "w1" not in d._sqc_journal_open

    d._journal_finished_sqc()  # idempotent — no duplicate "end"
    assert d.journal_marks == ["[SQC] w1 end (ready)"]


def test_journal_finished_sqc_leaves_marker_open_until_terminal():
    d = _driver_with_journal()
    d._sqc_journal_open = {"w1"}
    d.sqc_state = {"w1": {"status": "receiving"}}

    d._journal_finished_sqc()
    assert d.journal_marks == []
    assert "w1" in d._sqc_journal_open


def test_disconnect_flushes_open_sqc_markers():
    d = _driver_with_journal()
    d._sqc_journal_open = {"w1"}
    d.sqc_state = {"w1": {"status": "receiving"}}

    d.disconnect()
    assert d.journal_marks == ["[SQC] w1 end (disconnected)"]


# ── bounded BLE calls (auto-reconnect freeze fix) ───────────────────────────
#
# We used to run on simplepyble, whose connect()/disconnect() are blocking C
# calls with no timeout of their own — confirmed via a macOS thread dump to
# hold the GIL hostage inside native code, freezing the whole app with no
# way for a Python-level timeout to recover. Replaced with bleak (asyncio-
# native); _run_async submits a coroutine to a dedicated event-loop thread
# and waits with a real, working timeout. These tests use a REAL asyncio
# loop (via _bare_driver's _start_ble_loop) with fake coroutines that never
# complete, proving the actual bridge mechanism recovers promptly.

def test_run_async_success():
    d = _bare_driver()
    async def _coro():
        return "ok"
    assert d._run_async(_coro()) == "ok"


def test_run_async_propagates_exception():
    d = _bare_driver()
    async def _boom():
        raise RuntimeError("nope")
    with pytest.raises(RuntimeError, match="nope"):
        d._run_async(_boom())


def test_run_async_times_out_without_waiting_for_the_blocked_coroutine():
    d = _bare_driver()
    async def _hangs():
        await asyncio.sleep(999)  # simulates a stuck/slow BLE op — never returns in time

    start = time.time()
    with pytest.raises(TimeoutError):
        d._run_async(_hangs(), timeout_s=0.2)
    elapsed = time.time() - start

    assert elapsed < 1.0  # returned promptly at ~timeout_s, not stuck for 999s


def test_register_nus_notify_bounded_when_notify_hangs(monkeypatch):
    """start_notify() is just as capable of being slow/stuck as connect()/
    disconnect() — a hang here (e.g. subscribing right after a reconnect, on
    a still-marginal link) must not stall the caller forever either. All
    four register_* methods share this same _run_async-wrapped shape; NUS
    stands in for the group."""
    monkeypatch.setattr("plasma.devices.msense.device.BLE_OP_TIMEOUT_S", 0.2)
    d = _bare_driver()

    class _HangingPeripheral(_FakePeripheral):
        async def start_notify(self, char_uuid, callback):
            await asyncio.sleep(999)

    p = _HangingPeripheral()

    start = time.time()
    with pytest.raises(TimeoutError):
        d.register_nus_notify(p, "w1")
    elapsed = time.time() - start

    assert elapsed < 1.0


def test_ensure_notify_idempotent_across_start_stop_start():
    """collection_ctl(False) doesn't unsubscribe, so a second Start re-calls
    register_* on the same client — CoreBluetooth rejects a double
    start_notify. _ensure_notify must no-op the repeat, but still re-subscribe
    on a fresh client (post-reconnect)."""
    d = _bare_driver()
    d._notify_state = {}

    class _CountingPeripheral(_FakePeripheral):
        def __init__(self):
            super().__init__()
            self.notify_calls = 0

        async def start_notify(self, char_uuid, callback):
            self.notify_calls += 1

    p1 = _CountingPeripheral()
    d.register_enmo(p1, "w1")
    d.register_enmo(p1, "w1")            # Start / Stop / Start on same client
    d.register_battery(p1, "w1")
    assert p1.notify_calls == 2          # ENMO once, battery once — not 3

    p2 = _CountingPeripheral()           # fresh client after a reconnect
    d.register_enmo(p2, "w1")
    assert p2.notify_calls == 1

    d._notify_state = {}                 # connect_devices / disconnect / erase
    d.register_enmo(p2, "w1")
    assert p2.notify_calls == 2


def test_reconnect_peripheral_bounded_when_connect_hangs(monkeypatch):
    """A connect() that never returns must not stall _reconnect_peripheral
    forever — it should give up after the bounded timeout and mark the
    device as failed instead of hanging. _reconnect_peripheral builds a
    fresh BleakClient for the reconnect attempt (not reusing the old one —
    reconnecting on the same client isn't reliable on macOS's CoreBluetooth
    backend), so the hang is injected via a patched BleakClient constructor."""
    monkeypatch.setattr("plasma.devices.msense.device.BLE_OP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # skip the real 1.5s pause

    class _HangingPeripheral(_FakePeripheral):
        async def connect(self):
            await asyncio.sleep(999)

    monkeypatch.setattr("plasma.devices.msense.device.BleakClient",
                        lambda address, **kwargs: _HangingPeripheral(address=address))

    d = _bare_driver()
    p = _FakePeripheral()  # the "old" (already connected) client being replaced
    d.active_devices = {"w1": p}
    d.caps = {"w1": {}}
    d.memo = {"w1": PlasmaMemo("w1")}
    d.imu_stream_devices = set()

    start = time.time()
    d._reconnect_peripheral("w1", "test")
    elapsed = time.time() - start

    assert elapsed < 1.0
    assert d.memo["w1"].sts == "🔌 reconnect failed"
