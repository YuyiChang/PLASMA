"""Headless control + situational awareness for PLASMA.

PLASMA is a browser-only Gradio app: one process, one process-global
``IntegratedPanel`` (``ip``), all state shared across every client. ``gradio_client``
can already *drive* the auto-named handlers, but it cannot *observe* the app
(the only status view is bound to a ``gr.Timer.tick``, which is not a callable
endpoint) and cannot reliably *report errors* (``gr.Warning`` is toast-only,
``start_collection`` swallows device failures).

This module is the additive layer that closes those gaps, without touching the
GUI:

* :func:`session_status` — the machine-readable status object the app lacks,
  built by reusing :mod:`plasma.status` (the failure-level model),
  ``SessionRecorder.status()`` and ``IntegratedPanel.phase``.
* :class:`SessionEventLog` — a durable, append-only fault history written to
  ``<data_dir>/events.jsonl`` (all sessions) *and* ``<session_dir>/events.jsonl``
  (bundled with the ``.xdf``); ``fault`` / ``recover`` transitions also land on
  the journaler LSL stream as ``[FAULT]`` / ``[RECOVER]`` markers, so they are in
  the recording too.
* :func:`start_session` / :func:`stop_session` / :func:`mark` — typed control
  wrappers that *report* outcomes instead of swallowing them.
* :func:`register` — wires curated ``gr.api()`` endpoints (``/status``,
  ``/start``, ``/stop``, ``/mark``, ``/events``) and starts the 1 Hz event-log
  pump. Call it once, inside the ``gr.Blocks`` context in ``plasma.__main__``.

Localhost only — no auth, no bind change.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time

from plasma import __version__
from plasma.app_context import app_context
from plasma import status as _status
from plasma.status import Level, classify, health_level


# ── recorder-stream ↔ device-memo matching ──────────────────────────────────

def match_recorder_streams(devices, snap):
    """Fold the recorder's per-stream stats into the device that publishes them.

    Returns ``(recorded, orphans)``:

    * ``recorded`` — ``{id(memo): stream-dict}`` for each LSL stream a live
      device publishes and the recorder is capturing.
    * ``orphans`` — the remaining recorder streams (external software, PLASMA's
      own journal stream, a device that has gone away).

    ``snap`` is ``SessionRecorder.status()`` or ``None``. Shared by
    ``integrated_panel.build_memo_html`` and :func:`session_status`.
    """
    name_to_memo = {}
    for dev in devices:
        try:
            src = dev.get_sources()
            for lsl_name, key in dev.lsl_streams().items():
                if key in src:
                    name_to_memo[lsl_name] = src[key]
        except Exception:
            pass

    recorded, orphans = {}, []
    if snap is not None:
        for s in snap["streams"]:
            m = name_to_memo.get(s["name"])
            if m is not None and not s["external"]:
                recorded[id(m)] = s
            else:
                orphans.append(s)
    return recorded, orphans


def _stream_view(s):
    """The subset of a recorder stream-dict we surface in the API."""
    return {
        "name": s["name"],
        "type": s.get("type"),
        "health": s["health"],
        "n_samples": s["n_samples"],
        "srate": s.get("srate", 0.0),
        "external": s.get("external", False),
        "xdf_basename": s.get("xdf_basename"),
    }


# ── session status aggregator ───────────────────────────────────────────────

def _log_file(ip) -> str:
    root = getattr(ip, "log_root", None) or app_context().data_dir
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    return os.path.join(root, f"{date}_plasma_session.log")


def _events_file(ip) -> str:
    root = getattr(ip, "log_root", None) or app_context().data_dir
    return os.path.join(root, "events.jsonl")


def session_status(ip) -> dict:
    """A JSON-serialisable snapshot of the whole session — phase, per-device /
    per-source failure levels (from :func:`plasma.status.classify`), the
    recorder state, and the worst level anywhere. This is the object a headless
    supervisor polls."""
    phase = ip.phase
    si = getattr(ip, "session_info", None)

    started = getattr(ip, "_collection_started", None)
    stopped = getattr(ip, "_collection_stopped", None)
    elapsed = None
    if started is not None:
        elapsed = round((stopped or time.monotonic()) - started, 1)

    rec = getattr(ip, "lsl_recorder", None)
    rec_status = rec.status() if rec is not None else {"state": "idle",
                                                       "summary": "not recording",
                                                       "file": "", "streams": []}

    devices_out = getattr(ip, "available_devices", []) or []
    snap = rec_status if rec_status.get("state") in ("recording", "stopped") else None
    recorded, orphans = match_recorder_streams(devices_out, snap)

    worst = Level.NONE
    worst_cat = "healthy" if phase == _status.COLLECTING else "info"
    devices = []
    for dev in devices_out:
        tag = getattr(dev, "tag", "?")
        try:
            sources = dev.get_sources()
        except Exception:
            sources = {}
        dev_worst = Level.NONE
        srcs = []
        for key, memo in sources.items():
            sts = getattr(memo, "sts", "")
            s = recorded.get(id(memo))
            hk = ({"stream_health": s["health"], "srate": s.get("srate", 0.0)}
                  if s is not None else {})
            level, category = classify(sts, phase, **hk)
            entry = {
                "name": getattr(memo, "label", getattr(memo, "name", key)),
                "key": key,
                "sts": sts,
                "latest": getattr(memo, "latest", ""),
                "stream": _stream_view(s) if s is not None else None,
            }
            # MSense-only extras — best effort, absent on other drivers
            for attr, call in (("acq_stop", "get_acq_stop_status"),
                               ("sqc", "get_sqc_status"),
                               ("live_stream", "get_live_stream_status")):
                fn = getattr(dev, call, None)
                if callable(fn):
                    try:
                        entry[attr] = fn(key)
                    except Exception:
                        pass

            # fold the SQC-snapshot / live-stream transfer state into the
            # source level (docs/failure-levels.md — "mapped for consistency")
            for probe in ("sqc", "live_stream"):
                info = entry.get(probe)
                if not isinstance(info, dict):
                    continue
                p_lvl, p_cat = _status.sqc_level(info.get("status"),
                                                 info.get("error"))
                if p_lvl > level:
                    level, category = p_lvl, p_cat
                    detail = f"{probe} {info.get('status')}"
                    if info.get("error"):
                        detail += f": {info['error']}"
                    entry["level_reason"] = detail

            entry["level"] = int(level)
            entry["level_name"] = level.name
            entry["category"] = category

            if level > dev_worst:
                dev_worst = level
            if level > worst:
                worst, worst_cat = level, category
            srcs.append(entry)
        devices.append({"tag": tag, "worst_level": int(dev_worst),
                        "worst_level_name": dev_worst.name, "sources": srcs})

    _orphan_cat = {Level.NONE: "healthy", Level.L1: "caution",
                   Level.L2: "caution", Level.L3: "warning"}
    other = []
    for s in orphans:
        lvl = health_level(s["health"], s.get("srate", 0.0), phase=phase)
        if lvl > worst:
            worst = lvl
            worst_cat = _orphan_cat[lvl]
        v = _stream_view(s)
        v.update(level=int(lvl), level_name=lvl.name)
        other.append(v)

    rec_state = rec_status.get("state", "idle")
    if rec_state == "unavailable" and phase == _status.COLLECTING and Level.L3 > worst:
        worst, worst_cat = Level.L3, "warning"

    return {
        "app": {"name": app_context().app_name, "version": __version__},
        "phase": phase,
        "session": {
            "sub_id": si["sub_id"] if si is not None else None,
            "ses_id": si["ses_id"] if si is not None else None,
            "participant_enc": si["participant_enc"] if si is not None else None,
            "session_dir": getattr(ip, "session_dir", None),
            "sts": getattr(ip, "sts", ""),
            "elapsed_s": elapsed,
        },
        "recorder": {
            "state": rec_state,
            "summary": rec_status.get("summary", ""),
            "file": rec_status.get("file", ""),
            "streams": [_stream_view(s) for s in rec_status.get("streams", [])],
        },
        "devices": devices,
        "external_streams": _safe_external(ip),
        "other_recorded_streams": other,
        "worst_level": int(worst),
        "worst_level_name": Level(worst).name,
        "worst_category": worst_cat,
        "log_file": _log_file(ip),
        "events_file": _events_file(ip),
        "ts": time.time(),
    }


def _safe_external(ip):
    try:
        return ip._external_lsl_streams()
    except Exception:
        return []


# ── durable event log ──────────────────────────────────────────────────────

_HEARTBEAT_S = 30.0


class SessionEventLog:
    """Diffs successive :func:`session_status` snapshots and appends one JSON
    line per change to a rolling ``events.jsonl`` (and, once a session dir
    exists, a per-session copy). ``fault`` / ``recover`` transitions are also
    pushed onto the journaler LSL stream.

    One lock guards :meth:`tick`; it is safe to call from the 1 Hz pump thread
    and opportunistically from the ``/status`` handler.
    """

    def __init__(self, ip, data_dir: str | None = None):
        self.ip = ip
        self.data_dir = data_dir or app_context().data_dir
        self._lock = threading.Lock()
        self._prev_levels: dict[tuple, int] = {}
        self._prev_phase = None
        self._prev_rec_state = None
        self._prev_sources: set[tuple] = set()
        self._last_heartbeat = 0.0

    # -- file I/O ----------------------------------------------------------

    @property
    def rolling_path(self) -> str:
        return os.path.join(self.data_dir, "events.jsonl")

    def _session_path(self):
        sd = getattr(self.ip, "session_dir", None)
        if sd and os.path.isdir(sd):
            return os.path.join(sd, "events.jsonl")
        return None

    def _emit(self, event: str, *, device=None, source=None, level=Level.NONE,
              category=None, sts="", detail=""):
        rec = {
            "ts": time.time(),
            "iso": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "phase": getattr(self.ip, "phase", None),
            "event": event,
            "device": device,
            "source": source,
            "level": int(level),
            "level_name": Level(int(level)).name,
            "category": category,
            "sts": sts,
            "detail": detail,
            "session_dir": getattr(self.ip, "session_dir", None),
        }
        line = json.dumps(rec, ensure_ascii=False)
        for path in (self.rolling_path, self._session_path()):
            if not path:
                continue
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass
        if event == "fault":
            self._journal(f"[FAULT] {device or ''} {category or ''}: {sts}".strip())
        elif event == "recover":
            self._journal(f"[RECOVER] {device or ''} {source or ''}".strip())
        return rec

    def _journal(self, msg):
        try:
            self.ip.journal(msg)
        except Exception:
            pass

    # -- diff engine -----------------------------------------------------

    def tick(self, st: dict | None = None):
        with self._lock:
            self._tick_locked(st)

    def _tick_locked(self, st):
        if st is None:
            st = session_status(self.ip)
        now = time.time()

        phase = st["phase"]
        if phase != self._prev_phase:
            if self._prev_phase is not None:
                self._emit("phase", detail=f"{self._prev_phase} -> {phase}")
            if phase == _status.COLLECTING:
                self._emit("session_start",
                           detail=st["session"].get("session_dir") or "")
            elif phase == _status.STOPPED:
                self._emit("session_stop",
                           detail=st["session"].get("session_dir") or "")
            self._prev_phase = phase

        rec_state = st["recorder"]["state"]
        if rec_state != self._prev_rec_state:
            if self._prev_rec_state is not None:
                lvl = Level.L3 if rec_state == "unavailable" else Level.NONE
                self._emit("recorder", level=lvl,
                           detail=f"{self._prev_rec_state} -> {rec_state}",
                           sts=st["recorder"].get("summary", ""))
            self._prev_rec_state = rec_state

        cur_levels: dict[tuple, int] = {}
        cur_sources: set[tuple] = set()
        for dev in st["devices"]:
            for s in dev["sources"]:
                k = (dev["tag"], s["key"])
                cur_sources.add(k)
                cur_levels[k] = s["level"]
        for s in st["other_recorded_streams"]:
            k = ("(stream)", s["name"])
            cur_sources.add(k)
            cur_levels[k] = s["level"]

        # device / source appeared or went away
        for k in cur_sources - self._prev_sources:
            self._emit("device_added", device=k[0], source=k[1])
        for k in self._prev_sources - cur_sources:
            self._emit("device_removed", device=k[0], source=k[1])

        # fault / recover transitions
        src_meta = {}
        for dev in st["devices"]:
            for s in dev["sources"]:
                src_meta[(dev["tag"], s["key"])] = s
        for s in st["other_recorded_streams"]:
            src_meta[("(stream)", s["name"])] = s

        for k, lvl in cur_levels.items():
            prev = self._prev_levels.get(k, 0)
            if lvl == prev:
                continue
            meta = src_meta.get(k, {})
            reason = (meta.get("level_reason") or meta.get("sts")
                      or meta.get("health", ""))
            if lvl >= int(Level.L2) and lvl > prev:
                self._emit("fault", device=k[0], source=k[1], level=lvl,
                           category=meta.get("category"), sts=reason)
            elif prev >= int(Level.L2) and lvl <= int(Level.L1):
                self._emit("recover", device=k[0], source=k[1], level=lvl,
                           category=meta.get("category"), sts=reason)

        self._prev_levels = cur_levels
        self._prev_sources = cur_sources

        if now - self._last_heartbeat >= _HEARTBEAT_S:
            self._last_heartbeat = now
            self._emit("heartbeat", level=st["worst_level"],
                       category=st["worst_category"],
                       detail=f"phase={phase} worst={st['worst_level_name']}")

    # -- read back ------------------------------------------------------

    def read(self, since: float = 0.0, min_level: int = 0, limit: int = 5000):
        """Return event records from the rolling log with ``ts > since`` and
        ``level >= min_level`` (most-recent ``limit`` lines scanned). Reads the
        file so it works even after a PLASMA restart."""
        out = []
        try:
            with open(self.rolling_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return out
        for ln in lines[-limit:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("ts", 0) > since and rec.get("level", 0) >= min_level:
                out.append(rec)
        return out


# ── typed control wrappers ─────────────────────────────────────────────────

def start_session(ip, sub: str, ses: str, devices: list[str],
                  record: bool = True) -> dict:
    """Initialise + start a collection and *report* what happened. Returns
    ``{"ok": bool, "errors": [...], "status": session_status(ip)}``."""
    errors = []
    for label, fn in (
        ("session-info", lambda: ip.get_participant_encoding(sub, ses)),
        ("record-flag", lambda: ip._set_record_lsl(record)),
        ("init", lambda: ip.init_devices(devices)),
        ("start", lambda: ip.start_collection()),
    ):
        try:
            fn()
        except Exception as e:
            errors.append(f"{label}: {e!r}")

    st = session_status(ip)
    for d in st["devices"]:
        for s in d["sources"]:
            if s["level"] >= int(Level.L3):
                errors.append(f'{d["tag"]}/{s["name"]}: {s["sts"]}')
    _tick(ip)
    return {"ok": not errors, "errors": errors, "status": st}


def stop_session(ip) -> dict:
    """Stop the collection. Flags an abnormal stop (unconfirmed / stop failed)
    in ``errors`` while still returning ``ok`` when the session ended cleanly."""
    errors = []
    try:
        ip.stop_collection()
    except Exception as e:
        errors.append(f"stop: {e!r}")

    st = session_status(ip)
    for d in st["devices"]:
        for s in d["sources"]:
            if s["category"] == "caution" and s["level"] >= int(Level.L2):
                errors.append(f'{d["tag"]}/{s["name"]}: {s["sts"]}')
    _tick(ip)
    return {"ok": not errors, "errors": errors, "status": st}


def mark(ip, text: str) -> dict:
    """Push a free-text marker onto the journaler LSL stream + the session log."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "errors": ["empty marker text"]}
    try:
        ip.journal(text)
    except Exception as e:
        return {"ok": False, "errors": [f"journal: {e!r}"]}
    return {"ok": True, "marker": text}


def _tick(ip):
    log = getattr(ip, "_event_log", None)
    if log is not None:
        try:
            log.tick()
        except Exception:
            pass


# ── endpoint registration ──────────────────────────────────────────────────

def register(ip):
    """Register the curated ``gr.api()`` endpoints and start the 1 Hz event-log
    pump. Must be called inside the ``gr.Blocks`` context."""
    import gradio as gr

    ip._event_log = SessionEventLog(ip)

    def _pump():
        while True:
            _tick(ip)
            time.sleep(1.0)

    threading.Thread(target=_pump, name="plasma-events", daemon=True).start()

    def status() -> dict:
        _tick(ip)
        return session_status(ip)

    def start(sub: str, ses: str, devices: list[str], record: bool = True) -> dict:
        return start_session(ip, sub, ses, devices, record)

    def stop() -> dict:
        return stop_session(ip)

    def mark_(text: str) -> dict:
        return mark(ip, text)

    def events(since: float = 0.0, min_level: int = 0) -> list:
        log = getattr(ip, "_event_log", None)
        return log.read(since, min_level) if log is not None else []

    for fn, name in [(status, "status"), (start, "start"), (stop, "stop"),
                     (mark_, "mark"), (events, "events")]:
        gr.api(fn, api_name=name)
