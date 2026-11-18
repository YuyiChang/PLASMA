"""Multi-wristband initialization with deterministic BLE discovery/clients."""
import asyncio
import atexit
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plasma.config import device_config
from plasma.devices.msense import device as module
from plasma.devices.msense.ble_scan import discover_targets
from plasma.devices.msense.device import MotionSenseHRV, BATTERY_CHAR_UUID
from plasma.integrated_panel import IntegratedPanel


def advertisement(address, name="MSense4PPG"):
    return (SimpleNamespace(address=address, name=name),
            SimpleNamespace(local_name=name, rssi=-55))


def test_scan_accumulates_six_addresses_and_stops_early():
    packets = [advertisement(f"device-{i}") for i in range(6)]
    stopped = []

    class Scanner:
        def __init__(self, detection_callback):
            self.callback = detection_callback

        async def __aenter__(self):
            self.callback(*advertisement("unconfigured"))
            for packet in packets:
                self.callback(*packet)
            return self

        async def __aexit__(self, *args):
            stopped.append(True)

    async def scan():
        # Much less than the requested window: success is event driven.
        return await asyncio.wait_for(discover_targets(
            [f"DEVICE-{i}" for i in range(6)], timeout=5, scanner_factory=Scanner), timeout=0.2)

    found = asyncio.run(scan())
    assert len(found) == 6 and stopped == [True]
    assert found["DEVICE-5"][0] is packets[5][0]


def test_nameless_advertisement_is_not_discarded():
    packet = advertisement("known-device", name=None)

    class Scanner:
        def __init__(self, detection_callback):
            self.callback = detection_callback

        async def __aenter__(self):
            self.callback(*packet)

        async def __aexit__(self, *args):
            pass

    found = asyncio.run(discover_targets(["KNOWN-DEVICE"], timeout=0.01, scanner_factory=Scanner))
    assert found["KNOWN-DEVICE"][0] is packet[0]


@pytest.fixture
def six_wristbands(monkeypatch):
    rows = [{"Name": "MSense4PPG", "Nickname": str(i), "UUID / MAC Address": f"DEV-{i}",
             "Enabled": True, "IMU Stream": False} for i in range(6)]
    monkeypatch.setitem(device_config.plugins, "msense", {"devices": rows})
    scans, attempts, retired, clients, outlets = [], Counter(), [], {}, []
    state = {"visible": {f"DEV-{i}" for i in range(6)}, "fail_once": set(),
             "permanent_fail": set(), "late": set(), "scan_error": False}

    async def discover(targets, timeout):
        targets = set(targets)
        scans.append(targets)
        if state["scan_error"]:
            raise RuntimeError("Bluetooth permission denied")
        visible = state["visible"] | (state["late"] if len(scans) > 1 else set())
        return {address: advertisement(address) for address in targets & visible}

    class Client:
        def __init__(self, native, **kwargs):
            # Passing an address would trigger Bleak's implicit scan on macOS.
            assert not isinstance(native, str)
            self.native = native
            self.address = native.address
            self.is_connected = False
            self.mtu_size = 247
            clients.setdefault(self.address, []).append(self)

        async def connect(self):
            attempts[self.address] += 1
            if (self.address in state["permanent_fail"] or
                    self.address in state["fail_once"] and attempts[self.address] == 1):
                raise RuntimeError("Transient CoreBluetooth failure")
            self.is_connected = True

        async def disconnect(self):
            retired.append(self)
            self.is_connected = False

        async def start_notify(self, *args):
            pass

        async def read_gatt_char(self, char):
            return b"\x5a" if char == BATTERY_CHAR_UUID else b"\x00"

    def outlet(*args):
        value = object()
        outlets.append(value)
        return value

    monkeypatch.setattr("plasma.devices.msense.ble_scan.discover_targets", discover)
    monkeypatch.setattr(module, "BleakClient", Client)
    monkeypatch.setattr(module, "MsenseOutlet", outlet)
    monkeypatch.setattr(MotionSenseHRV, "_ensure_sqc_threads", lambda self: None)
    drivers = []

    def make():
        driver = MotionSenseHRV({"participant_enc": 1234}, None, "MSense Wristbands")
        drivers.append(driver)
        return driver

    yield SimpleNamespace(make=make, scans=scans, attempts=attempts, retired=retired,
                          clients=clients, outlets=outlets, state=state)
    for driver in drivers:
        atexit.unregister(driver._shutdown_cleanup)
        driver.disconnect()
        driver._stop_ble_loop()


def test_sixth_wristband_found_on_retry_keeps_first_five(six_wristbands):
    env = six_wristbands
    env.state["visible"].remove("DEV-5")
    env.state["late"].add("DEV-5")
    driver = env.make()
    assert len(driver.active_devices) == 6
    assert env.scans == [{f"DEV-{i}" for i in range(6)}, {"DEV-5"}]
    assert env.attempts == {f"DEV-{i}": 1 for i in range(6)}
    assert env.retired == [] and len(env.outlets) == 6


def test_transient_connection_failure_retries_only_that_peer(six_wristbands):
    env = six_wristbands
    env.state["fail_once"].add("DEV-3")
    driver = env.make()
    assert len(driver.active_devices) == 6
    assert env.attempts["DEV-3"] == 2
    assert all(env.attempts[f"DEV-{i}"] == 1 for i in range(6) if i != 3)
    assert all(not c.is_connected for c in env.retired)
    assert len(env.outlets) == 6


def test_manual_initialize_retry_keeps_healthy_clients_and_outlets(six_wristbands):
    env = six_wristbands
    env.state["visible"].remove("DEV-5")
    driver = env.make()
    clients, outlets = dict(driver.active_devices), dict(driver.active_outlets)
    assert len(clients) == 5
    env.state["visible"].add("DEV-5")
    driver.retry_initialization()
    assert len(driver.active_devices) == 6
    assert all(driver.active_devices[key] is value for key, value in clients.items())
    assert all(driver.active_outlets[key] is value for key, value in outlets.items())
    assert env.retired == []
    count = len(env.scans)
    driver.retry_initialization()
    assert len(env.scans) == count  # six healthy peers: no scan or reconnect


def test_scan_error_is_not_mislabeled_as_missing(six_wristbands):
    env = six_wristbands
    env.state["scan_error"] = True
    driver = env.make()
    assert len(env.scans) == 2  # bounded, never an endless retry
    assert not driver.active_devices
    assert all(memo.sts == "⛔ scan failed" and "permission denied" in memo.latest
               for memo in driver.memo.values())


def test_saved_configuration_change_requires_full_initialization(six_wristbands, monkeypatch):
    driver = six_wristbands.make()
    assert driver.can_retry_initialization()
    monkeypatch.setitem(device_config.plugins, "msense", {"devices": []})
    assert not driver.can_retry_initialization()


def test_panel_initialize_reuses_unchanged_real_msense(six_wristbands, monkeypatch):
    from plasma.config import device_config

    driver = six_wristbands.make()
    panel = IntegratedPanel.__new__(IntegratedPanel)
    panel.available_devices = [driver]
    panel.session_info = {"participant_enc": 5678}
    panel.logger = Mock()
    monkeypatch.setattr(device_config, "get_active_table", lambda: {"MSense Wristbands": None})
    panel.init_devices(["MSense Wristbands"])
    assert panel.available_devices == [driver]
    assert driver.session_info["participant_enc"] == 5678
    assert not six_wristbands.retired


def test_panel_blocks_initialize_during_recording():
    panel = IntegratedPanel.__new__(IntegratedPanel)
    panel._collection_started = 1.0
    panel._collection_stopped = None
    panel._initialize_devices = Mock()
    panel.init_devices(["MSense Wristbands"])
    panel._initialize_devices.assert_not_called()


def test_late_callback_after_retirement_does_not_report_spurious_disconnect(six_wristbands):
    driver = six_wristbands.make()
    old = driver._live_client.pop("DEV-0")
    status = driver.memo["DEV-0"].sts
    driver._on_unexpected_disconnect("DEV-0", old)
    assert driver.memo["DEV-0"].sts == status


def test_reconnect_control_reports_never_initialized_peers(six_wristbands):
    env = six_wristbands
    env.state["visible"].remove("DEV-5")
    driver = env.make()
    assert "1 configured wristband(s) never connected" in driver.reconnect_all()


def test_dispose_releases_driver_loop_and_clients(six_wristbands):
    driver = six_wristbands.make()
    peers = list(driver.active_devices.values())
    driver.dispose()
    assert not driver._ble_loop_thread.is_alive()
    assert all(not peer.is_connected for peer in peers)
    assert driver._sqc_threads_stopped


def test_disconnect_during_reconnect_pause_does_not_create_another_client(six_wristbands, monkeypatch):
    env = six_wristbands
    driver = env.make()
    count = sum(len(peers) for peers in env.clients.values())
    monkeypatch.setattr(module.time, "sleep", lambda _: setattr(driver, "_sqc_threads_stopped", True))
    driver._reconnect_peripheral("DEV-0", "teardown race regression")
    assert sum(len(peers) for peers in env.clients.values()) == count
    assert not driver.active_devices["DEV-0"].is_connected
