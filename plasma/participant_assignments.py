"""Session-scoped wristband identities, independent of BLE connections."""
import csv
import functools
import json
import os
import re
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass

import gradio as gr


def session_locked(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        # Also supports panel fixtures constructed without __init__.
        if not hasattr(self, "_session_lock"):
            self._session_lock = threading.RLock()
        with self._session_lock:
            return fn(self, *args, **kwargs)
    return wrapped


def identity(subject, session):
    sub = re.fullmatch(r"sub-(\d+)", str(subject).strip())
    ses = re.fullmatch(r"ses-(\d{1,2})", str(session).strip())
    if not sub or not ses:
        raise ValueError("Use sub-<digits> and a shared ses-00 through ses-99.")
    number, visit = int(sub[1]), int(ses[1])
    encoding = number * 100 + visit
    if encoding > 0xFFFFFFFF:
        raise ValueError("Participant encoding exceeds the wristband's uint32 limit.")
    return f"sub-{number:04d}", f"ses-{visit:02d}", encoding


def device_key(plugin_id, address):
    return f"{plugin_id}:{address.strip().upper()}"


@dataclass(frozen=True)
class Assignment:
    plugin_id: str
    address: str
    label: str
    subject_id: str
    session_id: str
    encoding: int
    stream_name: str
    source_id: str


class ParticipantAssignments:
    """Dashboard operations; all mutations share the session lifecycle lock."""

    def _assignment_devices(self):
        return [d for d in self.available_devices
                if getattr(d, "CONFIG_KEY", None) in ("msense", "msense_demo")]

    def _assignments_running(self):
        return (getattr(self, "_collection_started", None) is not None
                and getattr(self, "_collection_stopped", None) is None)

    def _require_assignment_edit(self):
        if self._assignments_running():
            raise ValueError("Stop collection before changing participant assignments.")

    def _compile_assignments(self):
        draft = getattr(self, "participant_overrides", {})
        result = []
        for dev in self._assignment_devices():
            for addr, name in dev.device_list.items():
                sub, ses, enc = identity(
                    draft.get(device_key(dev.CONFIG_KEY, addr)) or self.session_info.sub_id,
                    self.session_info.ses_id)
                outlet = dev.active_outlets.get(addr)
                stream = getattr(outlet, "stream_name", f"{name} [{addr}]")
                result.append((dev, addr, Assignment(
                    dev.CONFIG_KEY, addr, dev.display_name(addr), sub, ses, enc, stream, addr)))
        return result

    @session_locked
    def assignment_preview(self):
        rows, choices = [], []
        draft = getattr(self, "participant_overrides", {})
        for dev, addr, a in self._compile_assignments():
            key = device_key(a.plugin_id, addr)
            client = dev.active_devices.get(addr)
            choices.append((f"{a.label} · {addr} ({a.plugin_id})", key))
            subject, session, encoding = (a.subject_id, a.session_id, a.encoding)
            if not getattr(self, "participant_assignment_mode", False):
                subject, session, encoding = identity(self.session_info.sub_id, self.session_info.ses_id)
            rows.append([a.label, subject, a.plugin_id, addr, draft.get(key, ""),
                         session, encoding,
                         "Connected" if client and client.is_connected else "Missing"])
        return rows, gr.update(choices=choices, value=[])

    @session_locked
    def set_assignment_mode(self, enabled):
        self._require_assignment_edit()
        self.participant_assignment_mode = bool(enabled)

    @session_locked
    def assign_participants(self, keys, participant):
        self._require_assignment_edit()
        allowed = {device_key(d.CONFIG_KEY, addr)
                   for d in self._assignment_devices() for addr in d.device_list}
        if not keys or not set(keys) <= allowed:
            raise ValueError("Select initialized wristbands first; refresh after Initialize.")
        participant = str(participant or "").strip()
        if participant:
            participant = identity(participant, self.session_info.ses_id)[0]
        draft = dict(getattr(self, "participant_overrides", {}))
        for key in keys:
            draft[key] = participant
        self.participant_overrides = draft
        return self.assignment_preview()

    @session_locked
    def clear_assignments(self):
        self._require_assignment_edit()
        self.participant_overrides = {}
        return self.assignment_preview()

    @session_locked
    def import_assignments(self, path):
        self._require_assignment_edit()
        allowed = {device_key(d.CONFIG_KEY, addr)
                   for d in self._assignment_devices() for addr in d.device_list}
        draft = {}
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not {"plugin_id", "address", "participant_id"} <= set(reader.fieldnames or []):
                raise ValueError("CSV requires plugin_id, address, participant_id columns.")
            for row in reader:
                key = device_key(row["plugin_id"], row["address"])
                if key not in allowed or key in draft:
                    raise ValueError(f"Unknown or duplicate wristband: {key}")
                value = row["participant_id"].strip()
                draft[key] = identity(value, self.session_info.ses_id)[0] if value else ""
        self.participant_overrides = draft
        return self.assignment_preview()

    @session_locked
    def export_assignments(self):
        from tempfile import mkdtemp
        path = os.path.join(mkdtemp(prefix="plasma-assignments-"), "participant_assignments.csv")
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["plugin_id", "address", "participant_id"])
            for dev, addr, _ in self._compile_assignments():
                writer.writerow([dev.CONFIG_KEY, addr, getattr(self, "participant_overrides", {}).get(
                    device_key(dev.CONFIG_KEY, addr), "")])
        return path

    @session_locked
    def assignment_controls(self, previous=None):
        locked = self._assignments_running()
        state = [locked, getattr(self, "participant_assignment_mode", False)]
        if state == previous:
            return [gr.skip()] * (len(self._assignment_controls) + 1) + [state]
        return ([gr.update(value=getattr(self, "participant_assignment_mode", False), interactive=not locked)]
                + [gr.update(interactive=not locked)] * (len(self._assignment_controls) - 1)
                + ["Assignments locked for the current recording." if locked else "Assignments editable until Start.", state])

    def _participant_interface(self, subject, session):
        with gr.Accordion("MSense participant assignments", open=False):
            gr.Markdown("Initialize wristbands, then refresh this table. Blank overrides use "
                        "the Subject ID above. All participants share the Session ID. "
                        "Subject ID to use shows the effective ID for each wristband. "
                        "Assignments are locked during collection and are not saved to the device catalog.")
            mode = gr.Checkbox(False, label="Assign participants per MSense wristband")
            table = gr.Dataframe(headers=["Wristband", "Subject ID to use", "Plugin", "UUID / MAC",
                                         "Override", "Session", "Encoding", "Connection"],
                                 datatype=["str"] * 6 + ["number", "str"], interactive=False)
            selected = gr.Dropdown(choices=[], multiselect=True, label="Wristbands to assign")
            participant = gr.Textbox(label="Participant ID (blank = dashboard Subject ID)")
            with gr.Row():
                apply = gr.Button("Apply to selected")
                clear = gr.Button("Clear overrides")
                refresh = gr.Button("Refresh assignments")
            with gr.Row():
                upload = gr.File(label="Import assignments CSV", file_types=[".csv"], type="filepath", height=90)
                export = gr.Button("Export assignments CSV")
            download = gr.File(label="Assignments CSV", interactive=False, height=70, visible=False)
            state = gr.Markdown("Assignments editable until Start.")
            mode.input(self.set_assignment_mode, mode).success(
                self.assignment_preview, outputs=[table, selected])
            apply.click(self.assign_participants, [selected, participant], [table, selected])
            clear.click(self.clear_assignments, outputs=[table, selected])
            refresh.click(self.assignment_preview, outputs=[table, selected])
            upload.upload(self.import_assignments, upload, [table, selected])
            export.click(lambda: gr.update(value=self.export_assignments(), visible=True), outputs=download)
        self._assignment_outputs = [table, selected]
        self._assignment_controls = [mode, selected, participant, apply, clear, upload, subject, session]
        last_state = gr.State(None)
        timer = gr.Timer(1)
        timer.tick(self.assignment_controls, inputs=last_state,
                   outputs=self._assignment_controls + [state, last_state], show_progress="hidden")

    def _start_assigned_collection(self):
        assignments = self._compile_assignments()
        if not assignments:
            raise ValueError("Initialize at least one MSense wristband before starting group mode.")
        self.sts = "Starting..."
        started = []
        drivers = self._assignment_devices()
        previous = [(d, dict(getattr(d, "participant_assignments", {})),
                     dict(getattr(d, "participant_log_dirs", {}))) for d in drivers]
        # Prevent reconnect or manual encoding writes between preflight and start.
        with ExitStack() as stack:
            for dev in drivers:
                stack.enter_context(dev._connections_guard())
            try:
                for dev in drivers:
                    dev.bind_participants({addr: a for d, addr, a in assignments if d is dev})
                for dev in drivers:
                    dev.verify_participants()  # ALL readbacks precede ANY acquisition enable
                self.session_dir = os.path.join(self.log_root, "group-sessions",
                    assignments[0][2].session_id, time.strftime("%y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
                os.makedirs(self.session_dir)
                records = [asdict(a) for _, _, a in assignments]
                self._participant_manifest = {"schema_version": 1, "status": "starting",
                                              "assignments": records}
                self._save_participant_manifest()
                with open(os.path.join(self.session_dir, "session_encoding.csv"), "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["subject_id", "session_id", "encoding"])
                    writer.writerows(sorted({(a.subject_id, a.session_id, a.encoding)
                                             for _, _, a in assignments}))
                if self.record_lsl:
                    self._start_recorder()
                    if self.lsl_recorder is None:
                        raise RuntimeError("Group recording could not start the XDF recorder.")
                    from plasma.app_context import app_context
                    streams = self.lsl_recorder.status()["streams"]
                    if not any(s["name"] == app_context().journal_stream for s in streams):
                        raise RuntimeError("XDF recorder did not resolve the participant-map marker stream. Retry Start.")
                    if self.journal_outlet is None or not self.journal_outlet.wait_for_consumers(3):
                        raise RuntimeError("Participant-map marker stream has no ready recording consumer.")
                self.journal("[PARTICIPANT_MAP] " + json.dumps(self._participant_manifest))
                for dev in self.available_devices:
                    dev.session_info, dev.session_dir = self.session_info, self.session_dir
                    started.append(dev)
                    dev.start()
                self._collection_started, self._collection_stopped = time.monotonic(), None
                self._participant_manifest["status"] = "recording"
                self._save_participant_manifest()
                self.sts = "Collection in progress"
            except Exception as exc:
                for dev in started:
                    try:
                        dev.stop()
                    except Exception:
                        self.logger.exception("Group start rollback failed for %s", dev.tag)
                if self.lsl_recorder is not None:
                    try:
                        self.lsl_recorder.stop()
                    except Exception:
                        self.logger.exception("Could not stop XDF recorder after failed group start")
                    self.lsl_recorder = None
                for dev in drivers:
                    dev.participant_assignment_active = False
                for dev, mapping, folders in previous:
                    if dev not in started:
                        dev.bind_participants(mapping)
                        dev.participant_log_dirs = folders
                self._collection_stopped = time.monotonic()
                self.sts = "Collection start failed"
                if getattr(self, "_participant_manifest", None):
                    self._participant_manifest.update(status="failed", error=str(exc))
                    self._save_participant_manifest()
                raise RuntimeError(f"Group start aborted: {exc}") from exc

    def _save_participant_manifest(self):
        path = os.path.join(self.session_dir, "participant_assignments.json")
        with open(path + ".tmp", "w") as f:
            json.dump(self._participant_manifest, f, indent=2)
        os.replace(path + ".tmp", path)
