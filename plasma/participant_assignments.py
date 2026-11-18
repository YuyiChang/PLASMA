"""Freeze configured per-wristband subject overrides for each recording."""
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

from plasma.config import device_config


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
    overridden: bool = False


class ParticipantAssignments:
    """Read saved configuration at Start; keep recording identities immutable."""

    def _assignment_devices(self):
        return [d for d in self.available_devices
                if getattr(d, "CONFIG_KEY", None) in ("msense", "msense_demo")]

    def _assignments_running(self):
        return (getattr(self, "_collection_started", None) is not None
                and getattr(self, "_collection_stopped", None) is None)

    def _require_assignment_edit(self):
        if self._assignments_running():
            raise ValueError("Stop collection before changing participant assignments.")

    def _load_participant_overrides(self):
        from plasma.devices.msense import config as real_config
        from plasma.devices.msense_demo import config as demo_config
        draft = {}
        for dev in self._assignment_devices():
            config = demo_config if dev.CONFIG_KEY == "msense_demo" else real_config
            overrides = config.participant_overrides(device_config.get_plugin_config(dev.CONFIG_KEY))
            for addr in dev.device_list:
                if addr in overrides:
                    draft[device_key(dev.CONFIG_KEY, addr)] = "sub-" + overrides[addr]
        self.participant_overrides = draft
        self.participant_assignment_mode = bool(draft)
        return draft

    def _compile_assignments(self, draft=None):
        if draft is None:
            draft = self._load_participant_overrides()
        result = []
        for dev in self._assignment_devices():
            for addr, name in dev.device_list.items():
                sub, ses, enc = identity(
                    draft.get(device_key(dev.CONFIG_KEY, addr)) or self.session_info.sub_id,
                    self.session_info.ses_id)
                outlet = dev.active_outlets.get(addr)
                stream = getattr(outlet, "stream_name", f"{name} [{addr}]")
                overridden = bool(draft.get(device_key(dev.CONFIG_KEY, addr)))
                result.append((dev, addr, Assignment(
                    dev.CONFIG_KEY, addr, dev.display_name(addr), sub, ses, enc,
                    stream, addr, overridden)))
        return result

    def _start_assigned_collection(self, assignments):
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
