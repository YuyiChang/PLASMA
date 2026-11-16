"""Instructor-only controls for the running simulated MSense devices.

One controller is shared with the acquisition panel. No second device registry,
LSL outlet, or recorder is created. Faults never touch persisted configuration.
"""
from collections import deque
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from plasma.app_context import app_context


INCIDENTS = {
    "disconnect": "Brief disconnect (normal auto-reconnect)",
    "offline": "Hold sensor offline",
    "stream_stall": "Stall the active live ECG/PPG stream",
    "sqc_error": "Stall the next SQC snapshot",
    "acq_stop_ignored": "Ignore acquisition Stop until restored",
}


class TrainingController:
    def __init__(self, panel, *, clock=time.monotonic, background=True):
        self.panel = panel
        self.clock = clock
        self.lock = threading.RLock()
        self.events = deque(maxlen=500)
        self.incidents = {}
        self.observed = {}
        self.exercise_id = None
        self.exercise_name = "No exercise started"
        self.log_path = None
        self.log_error = ""
        self.closed = False
        self._stop = threading.Event()
        self._worker = None
        if background:
            self._worker = threading.Thread(target=self._run, name="plasma-instructor", daemon=True)
            self._worker.start()

    def _targets(self):
        from plasma.devices.msense_demo.device import MSenseDemo
        targets = {}
        for driver in list(self.panel.available_devices):
            if isinstance(driver, MSenseDemo):
                for address in driver.device_list:
                    key = f"{driver.training_instance_id}:{address}"
                    targets[key] = (driver, address)
        return targets

    @staticmethod
    def _connected(driver, address):
        return bool(getattr(driver.active_devices.get(address), "is_connected", False))

    def _event(self, kind, *, incident=None, **details):
        row = dict(time=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   event=kind, exercise_id=self.exercise_id, **details)
        if incident:
            row.update(incident_id=incident["id"], sensor=incident["label"],
                       target=incident["target"], fault=incident["fault"])
        self.events.append(row)
        # Coaching notes and assessment stay in the instructor log, not LSL.
        marker = {k: row[k] for k in ("event", "exercise_id", "incident_id", "target", "fault")
                  if k in row}
        try:
            self.panel.journal("[TRAINING] " + json.dumps(marker, separators=(",", ":")))
        except Exception as exc:
            row["marker_error"] = str(exc)
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as log:
                    log.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.log_error = str(exc)

    def begin(self, name, objectives=""):
        with self.lock:
            if self.closed:
                raise ValueError("Instructor console is closed")
            name = (name or "").strip()
            if not name:
                raise ValueError("Enter an exercise name")
            root = Path(app_context().data_dir) / "training"
            root.mkdir(parents=True, exist_ok=True)
            exercise_id = uuid.uuid4().hex
            path = root / f"{time.strftime('%Y%m%d_%H%M%S')}_{exercise_id}.jsonl"
            # Verify storage before disturbing an existing exercise.
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            self.restore_all()
            self.events.clear()
            self.log_error = ""
            self.exercise_id = exercise_id
            self.exercise_name = name
            self.log_path = path
            self.observed = {key: self._connected(*target) for key, target in self._targets().items()}
            self._event("exercise_begin", name=name, objectives=objectives)

    @staticmethod
    def _seconds(value, label):
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ValueError(f"{label} must be a number") from None
        if not math.isfinite(value) or not 0 <= value <= 86400:
            raise ValueError(f"{label} must be between 0 and 86400 seconds")
        return value

    def schedule(self, target, fault, delay=0, duration=0):
        delay = self._seconds(delay, "Delay")
        duration = self._seconds(duration, "Duration")
        with self.lock:
            if self.closed or not self.exercise_id:
                raise ValueError("Begin an exercise first")
            if fault not in INCIDENTS:
                raise ValueError("Select an incident")
            targets = self._targets()
            if target not in targets:
                raise ValueError("Refresh sensors and select an initialized simulated wristband")
            if target in self.incidents:
                raise ValueError("This wristband already has an incident; restore it first")
            driver, address = targets[target]
            incident = dict(id=uuid.uuid4().hex[:12], target=target, fault=fault,
                            label=driver.display_name(address), due=self.clock() + delay,
                            duration=duration, status="scheduled", restore_at=None)
            self.incidents[target] = incident
            self._event("incident_scheduled", incident=incident, delay=delay, duration=duration)
            if delay == 0:
                self._apply(incident)
            return f"Incident {incident['id']} {incident['status']}"

    def _apply(self, incident):
        target = self._targets().get(incident["target"])
        if target is None:
            self._finish(incident, "canceled", reason="Device was reinitialized or removed")
            return
        driver, address = target
        fault = incident["fault"]
        if not self._connected(driver, address):
            self._finish(incident, "canceled", reason="Wristband is not connected")
            return
        if fault == "stream_stall" and driver.get_live_stream_status(address).get("status") not in {"streaming", "requesting"}:
            self._finish(incident, "canceled", reason="No active live ECG/PPG stream; start one in YAMS first")
            return
        incident["status"] = "active"
        if fault == "disconnect":
            driver.trigger_disconnect(address)
        else:
            driver.set_training_fault(address, fault)
            if incident["duration"]:
                incident["restore_at"] = self.clock() + incident["duration"]
        self._event("incident_applied", incident=incident)

    def _finish(self, incident, status, **details):
        target = self._targets().get(incident["target"])
        if target:
            target[0].clear_training_fault(target[1])
        self.incidents.pop(incident["target"], None)
        incident["status"] = status
        self._event("incident_" + status, incident=incident, **details)

    def restore(self, target):
        with self.lock:
            incident = self.incidents.get(target)
            if not incident:
                return "No instructor incident on this sensor"
            self._finish(incident, "restored")
            return "Instructor condition cleared; normal recovery can proceed"

    def restore_all(self):
        with self.lock:
            for incident in list(self.incidents.values()):
                self._finish(incident, "restored")
            return "All instructor incidents and pending triggers cleared; recordings preserved"

    def cancel_pending(self):
        with self.lock:
            for incident in list(self.incidents.values()):
                if incident["status"] == "scheduled":
                    self._finish(incident, "canceled", reason="Canceled by instructor")
            return "Pending incidents canceled"

    def assess(self, result, note=""):
        with self.lock:
            if not self.exercise_id:
                raise ValueError("Begin an exercise first")
            if result not in {"Observed", "Needed prompting", "Missed"}:
                raise ValueError("Select an assessment")
            self._event("assessment", result=result, note=note)
            return "Assessment saved to instructor log"

    def tick(self):
        with self.lock:
            if self.closed or not self.exercise_id:
                return
            now = self.clock()
            targets = self._targets()
            for incident in list(self.incidents.values()):
                target = targets.get(incident["target"])
                if target is None:
                    self._finish(incident, "canceled", reason="Device instance changed")
                elif incident["status"] == "scheduled" and now >= incident["due"]:
                    self._apply(incident)
                elif incident["restore_at"] is not None and now >= incident["restore_at"]:
                    self._finish(incident, "restored", reason="Duration elapsed")
                elif incident["status"] == "active":
                    driver, address = target
                    if incident["fault"] in {"sqc_error", "stream_stall"} and driver.training_fault_for(address) is None:
                        self._finish(incident, "triggered", reason="One transfer affected; driver recovery remains in progress")
                    elif incident["fault"] == "disconnect" and self._connected(driver, address):
                        self._finish(incident, "reconnected")
            for key, target in targets.items():
                connected = self._connected(*target)
                if key in self.observed and self.observed[key] != connected:
                    self._event("observed_reconnected" if connected else "observed_disconnected",
                                target=key, sensor=target[0].display_name(target[1]))
                self.observed[key] = connected
            self.observed = {key: value for key, value in self.observed.items() if key in targets}

    def _run(self):
        while not self._stop.wait(0.25):
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self._event("controller_error", detail=str(exc))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.restore_all()
            if self.exercise_id:
                self._event("exercise_closed")
            self.closed = True
            self._stop.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)

    def view(self):
        with self.lock:
            targets = self._targets()
            choices, rows = [], []
            for key, (driver, address) in targets.items():
                label = driver.display_name(address)
                choices.append((f"{label} [{address}]", key))
                incident = self.incidents.get(key)
                memo = driver.memo.get(address)
                timing = "—"
                if incident and incident["status"] == "scheduled":
                    timing = f"Trigger in {max(0, math.ceil(incident['due'] - self.clock()))} s"
                elif incident and incident["restore_at"] is not None:
                    timing = f"Restore in {max(0, math.ceil(incident['restore_at'] - self.clock()))} s"
                rows.append([label, "Connected" if self._connected(driver, address) else "Disconnected",
                             getattr(memo, "sts", ""), incident["fault"] if incident else "—",
                             incident["status"] if incident else "—", timing])
            def table(headers, values):
                return '<table style="width:100%;font-size:13px"><thead><tr>' + ''.join(
                    f'<th style="text-align:left">{html.escape(str(h))}</th>' for h in headers
                ) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(
                    f'<td style="padding:6px">{html.escape(str(v))}</td>' for v in row
                ) + '</tr>' for row in values) + '</tbody></table>'
            sensors = table(["Sensor", "Connection", "PLASMA status", "Incident", "State", "Timing"], rows)
            events_rows = []
            for event in reversed(self.events):
                detail = []
                if event.get("fault"):
                    detail.append(INCIDENTS[event["fault"]])
                for key in ("name", "objectives", "result", "note", "reason", "detail", "marker_error"):
                    if event.get(key):
                        detail.append(str(event[key]))
                if event["event"] == "incident_scheduled":
                    detail.append(f"Delay {event['delay']:g}s; restoration " +
                                  (f"after {event['duration']:g}s" if event["duration"] else "manual"))
                events_rows.append([event["time"][11:23], event["event"].replace("_", " ").capitalize(),
                                    event.get("sensor", "—"), " · ".join(detail)])
            events = table(["UTC", "Event", "Sensor", "Details"], events_rows)
            status = self.exercise_name + (f" · Log: {self.log_path}" if self.log_path else "")
            if self.log_error:
                status += f" · Log write failed: {self.log_error}"
            return choices, sensors, events, status


def build_instructor_blocks(controller):
    import gradio as gr

    with gr.Blocks(title="PLASMA Instructor Console", theme=gr.themes.Ocean(),
                   css="#training-timeline {max-height:360px;overflow:auto;}") as app:
        gr.Markdown("# Instructor Console\n"
                    "Control simulated MSense wristbands in the trainee's running PLASMA session. "
                    "Initialize them in PLASMA first. Use **Fault = none** in saved demo settings "
                    "for a healthy baseline. Instructor changes are temporary.")
        with gr.Row():
            with gr.Column():
                name = gr.Textbox(label="Exercise name", placeholder="Wristband disconnects during recording")
                objectives = gr.Textbox(label="Private coaching objectives", lines=3)
                begin = gr.Button("Begin exercise", variant="primary")
            with gr.Column():
                assessment = gr.Dropdown(["Observed", "Needed prompting", "Missed"], value="Observed", label="Mentor assessment")
                note = gr.Textbox(label="Private assessment notes", lines=3)
                save = gr.Button("Save assessment")
        status = gr.Textbox(label="Exercise and log", lines=2, max_lines=3, interactive=False)
        gr.Markdown("### Simulated sensors")
        sensors = gr.HTML()
        target = gr.Dropdown(choices=[], label="Target wristband")
        fault = gr.Dropdown(choices=[(label, key) for key, label in INCIDENTS.items()], value="disconnect", label="Incident", elem_id="training-incident")
        with gr.Row():
            delay = gr.Number(value=0, minimum=0, maximum=86400, label="Trigger after (seconds; 0 = now)", elem_id="training-delay")
            duration = gr.Number(value=0, minimum=0, maximum=86400, label="Restore after (seconds; 0 = manually)")
        gr.Markdown("Duration applies to held offline/Stop conditions and untriggered stream/SQC faults. "
                    "Brief disconnect uses normal auto-reconnect. Stream and SQC stalls affect one transfer; "
                    "the trainee's normal watchdog/recovery path still runs.")
        with gr.Row():
            inject = gr.Button("Introduce incident", variant="primary")
            restore = gr.Button("Restore selected sensor")
            cancel = gr.Button("Cancel pending incidents")
            reset = gr.Button("Restore all / reset exercise conditions")
        message = gr.Textbox(label="Instructor action", interactive=False)
        gr.Markdown("### Instructor event timeline")
        timeline = gr.HTML(elem_id="training-timeline")

        last_view = gr.State(None)

        def refresh(selected, last):
            choices, sensor_html, event_html, summary = controller.view()
            values = {key for _, key in choices}
            resolved = selected if selected in values else choices[0][1] if choices else None
            current = (choices, sensor_html, event_html, summary)
            target_update = (gr.skip() if last and last[0] == choices and resolved == selected
                             else gr.update(choices=choices, value=resolved))
            return [target_update, *[gr.skip() if last and last[i] == current[i] else current[i]
                                     for i in range(1, 4)], current]

        def action(fn):
            def call(*args):
                try:
                    return fn(*args) or "Exercise started"
                except (ValueError, OSError) as exc:
                    return str(exc)
            return call

        begin.click(action(controller.begin), inputs=[name, objectives], outputs=message)
        save.click(action(controller.assess), inputs=[assessment, note], outputs=message)
        inject.click(action(controller.schedule), inputs=[target, fault, delay, duration], outputs=message)
        restore.click(action(controller.restore), inputs=target, outputs=message)
        cancel.click(action(controller.cancel_pending), outputs=message)
        reset.click(action(controller.restore_all), outputs=message)
        outputs = [target, sensors, timeline, status, last_view]
        app.load(refresh, inputs=[target, last_view], outputs=outputs)
        gr.Timer(value=1).tick(refresh, inputs=[target, last_view], outputs=outputs, show_progress="hidden")
    return app
