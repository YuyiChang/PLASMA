"""Instructor scheduling, instance isolation, restoration and private logging."""
import asyncio
import json
from types import SimpleNamespace
import threading
import uuid
from unittest.mock import Mock

import pytest

from plasma.__main__ import _launch_options
from plasma.devices.msense_demo.device import MSenseDemo, FakePeripheral
from plasma.devices.template import PlasmaMemo
from plasma.training import TrainingController, build_instructor_blocks


def simulated():
    driver = MSenseDemo.__new__(MSenseDemo)
    driver.training_instance_id = uuid.uuid4().hex
    driver._training_lock = threading.RLock()
    driver._training_faults = {}
    driver._demo_faults = {"AA": "none"}
    driver.device_list = {"AA": "Demo PPG"}
    driver.display_labels = {"AA": "Demo PPG"}
    driver.memo = {"AA": PlasmaMemo("Demo PPG")}
    driver.get_live_stream_status = Mock(return_value={"status": "idle"})
    driver._on_unexpected_disconnect = Mock()
    peripheral = FakePeripheral(driver, "AA", "AA")
    peripheral.is_connected = True
    driver.active_devices = {"AA": peripheral}
    return driver


@pytest.fixture
def training():
    driver = simulated()
    panel = SimpleNamespace(available_devices=[driver], journal=Mock())
    now = [0.0]
    controller = TrainingController(panel, clock=lambda: now[0], background=False)
    target = next(iter(controller._targets()))
    yield controller, driver, target, now
    controller.close()


def test_requires_exercise_and_ignores_real_devices(training):
    controller, driver, target, _ = training
    real = SimpleNamespace(device_list={"REAL": "Physical wristband"}, set_training_fault=Mock())
    controller.panel.available_devices.append(real)
    assert len(controller._targets()) == 1
    with pytest.raises(ValueError, match="Begin an exercise"):
        controller.schedule(target, "offline")
    controller.begin("Disconnect practice")
    with pytest.raises(ValueError, match="initialized simulated"):
        controller.schedule("REAL", "offline")
    real.set_training_fault.assert_not_called()


def test_scheduled_offline_blocks_reconnect_then_restores(training):
    controller, driver, target, now = training
    controller.begin("Practice")
    controller.schedule(target, "offline", delay=5, duration=10)
    now[0] = 4
    controller.tick()
    assert driver.active_devices["AA"].is_connected
    now[0] = 5
    controller.tick()
    assert not driver.active_devices["AA"].is_connected
    assert driver.training_fault_for("AA") == "offline"
    replacement = FakePeripheral(driver, "AA", "AA")
    with pytest.raises(ConnectionError, match="unavailable"):
        asyncio.run(replacement.connect())
    now[0] = 15
    controller.tick()
    assert driver.training_fault_for("AA") is None
    asyncio.run(replacement.connect())
    assert replacement.is_connected
    assert not controller.incidents
    assert driver._demo_faults == {"AA": "none"}


def test_reinitialize_cancels_stale_incident(training):
    controller, old, target, now = training
    controller.begin("Practice")
    controller.schedule(target, "offline", delay=5)
    new = simulated()
    controller.panel.available_devices = [new]
    now[0] = 10
    controller.tick()
    assert not controller.incidents
    assert new.training_fault_for("AA") is None
    assert new.active_devices["AA"].is_connected
    assert any(e["event"] == "incident_canceled" for e in controller.events)


def test_restore_all_cancels_pending_and_preserves_saved_faults(training):
    controller, driver, target, now = training
    controller.begin("Practice")
    driver._demo_faults["AA"] = "acq_stop_ignored"
    controller.schedule(target, "offline")
    controller.restore_all()
    assert driver.fault_for("AA") == "acq_stop_ignored"
    controller.schedule(target, "sqc_error", delay=5)
    controller.cancel_pending()
    now[0] = 10
    controller.tick()
    assert not controller.incidents
    assert driver.training_fault_for("AA") is None


def test_rejects_stacking_incidents_and_invalid_times(training):
    controller, driver, target, _ = training
    controller.begin("Practice")
    with pytest.raises(ValueError, match="between"):
        controller.schedule(target, "offline", delay=float("nan"))
    controller.schedule(target, "offline", delay=5)
    with pytest.raises(ValueError, match="already has"):
        controller.schedule(target, "sqc_error")


def test_stream_fault_is_consumed_once_and_pending_sqc_is_restorable(training):
    controller, driver, target, _ = training
    controller.begin("Practice")
    controller.schedule(target, "stream_stall")
    assert not controller.incidents  # no active stream: canceled, with reason
    driver.get_live_stream_status.return_value = {"status": "streaming"}
    controller.schedule(target, "stream_stall")
    assert driver.consume_training_fault("AA", "stream_stall")
    assert not driver.consume_training_fault("AA", "stream_stall")
    controller.tick()
    assert not controller.incidents
    controller.schedule(target, "sqc_error")
    assert driver.training_fault_for("AA") == "sqc_error"
    controller.restore(target)
    assert not driver.consume_training_fault("AA", "sqc_error")


def test_coaching_and_assessments_are_private_but_events_are_marked(training):
    controller, driver, target, _ = training
    controller.begin("Private exercise", "Private objective")
    controller.schedule(target, "disconnect")
    controller.assess("Needed prompting", "Private mentor note")
    controller.restore_all()
    rows = [json.loads(line) for line in controller.log_path.read_text().splitlines()]
    assert rows[0]["objectives"] == "Private objective"
    assert any(row.get("note") == "Private mentor note" for row in rows)
    markers = "\n".join(call.args[0] for call in controller.panel.journal.call_args_list)
    assert "[TRAINING]" in markers and "incident_applied" in markers
    assert "Private" not in markers


def test_controller_close_is_idempotent_and_stops_incidents(training):
    controller, driver, target, now = training
    controller.begin("Practice")
    controller.schedule(target, "offline", delay=10)
    controller.close()
    count = len(controller.events)
    controller.close()
    now[0] = 20
    controller.tick()
    assert len(controller.events) == count
    assert driver.training_fault_for("AA") is None


def test_instructor_ui_builds_with_only_simulated_targets(training):
    controller, driver, target, _ = training
    app = build_instructor_blocks(controller)
    labels = [component.get("props", {}).get("value") for component in app.config["components"]]
    assert "Introduce incident" in labels
    assert "Restore all / reset exercise conditions" in labels


def test_remote_instructor_requires_token(monkeypatch):
    monkeypatch.delenv("PLASMA_INSTRUCTOR_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _launch_options(["--training", "--instructor-host", "0.0.0.0"])
    monkeypatch.setenv("PLASMA_INSTRUCTOR_TOKEN", "test-token")
    assert _launch_options(["--training", "--instructor-host", "0.0.0.0"]).training
    with pytest.raises(SystemExit):
        _launch_options(["--training", "--instructor-port", "7860"])
    assert not _launch_options([]).training


def test_training_startup_selects_simulator_without_saving_lab_config(monkeypatch):
    from plasma import __main__ as main_module
    from plasma.config import device_config
    import plasma.training as module

    monkeypatch.setattr(device_config, "_active", ["MSense Wristbands"])
    monkeypatch.setattr(device_config, "demo_mode", False)
    save = Mock()
    monkeypatch.setattr(device_config, "_save", save)
    monkeypatch.setattr(main_module.plugins, "load_plugins", Mock())
    monkeypatch.setattr(main_module.plugins, "catalog", lambda: {"MSense Demo (simulated)": None})
    monkeypatch.setattr(main_module.signal, "signal", Mock())
    panel, trainee, mentor, controller = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(main_module, "IntegratedPanel", lambda: panel)
    monkeypatch.setattr(main_module, "build_blocks", lambda ip: trainee)
    monkeypatch.setattr(main_module, "app", None)
    monkeypatch.setattr(main_module, "instructor_app", None)
    monkeypatch.setattr(module, "TrainingController", lambda ip: controller)
    monkeypatch.setattr(module, "build_instructor_blocks", lambda c: mentor)
    main_module.main(["--training"])
    assert device_config._active == ["MSense Demo (simulated)"]
    save.assert_not_called()
    assert mentor.launch.call_args.kwargs["server_port"] == 7861
    assert trainee.launch.call_args.kwargs["server_port"] == 7860
    controller.close.assert_called_once()
