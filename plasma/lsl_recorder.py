"""Built-in LSL -> XDF session recorder.

While a PLASMA session is recording, this resolves every LSL stream on the
network (PLASMA's own outlets included), pulls all of their samples, and
writes them to a single ``.xdf`` file — the same container LabRecorder
produces and ``pyxdf`` / MNE / EEGLAB read — so no external recording tool is
needed alongside PLASMA.

Threading model: exactly one daemon "pump" thread owns the ``XDFWriter`` and
every ``StreamInlet``. It is the only thing that ever writes the file.
``status()`` only reads a snapshot of counter state under ``self._lock`` and
never touches pylsl or the file, so the Gradio timer thread that calls it
1 Hz can never race the writer.

Graceful degradation: ``start()`` never raises. If pylsl / liblsl is missing
(or the file cannot be opened) it logs, sets state ``"unavailable"`` and
returns ``False`` — mirroring ``plasma.journal.open_journal_outlet()``.
"""
import os
import threading
import time

from plasma.xdf_writer import XDFWriter
from plasma.lsl_util import channel_format_name, is_plasma_origin
from plasma.status import STALE_S, IRREGULAR_STALE_S


def _stale_after(srate):
    """How long a stream may be silent before it's '🟡 stale'. A periodic
    stream (srate > 0) at a few seconds; an irregular/event stream (the
    journaler, Pupil eye events) is silent by nature — only stale after
    minutes, so it doesn't raise a false alarm."""
    return STALE_S if (srate and srate > 0) else IRREGULAR_STALE_S


class _StreamStat:
    """Per-stream snapshot state. Counters are mutated by the pump thread and
    read by ``status()`` — both under ``SessionRecorder._lock``."""

    __slots__ = ("sid", "name", "type", "source_id", "external", "srate",
                 "xdf_basename", "health", "n_samples", "last_ts", "last_recv",
                 "dead")

    def __init__(self, sid, name, stype, source_id, external, srate, xdf_basename):
        self.sid = sid
        self.name = name
        self.type = stype
        self.source_id = source_id
        self.external = external
        self.srate = srate
        self.xdf_basename = xdf_basename
        self.health = "🟢"
        self.n_samples = 0
        self.last_ts = None
        self.last_recv = time.monotonic()
        self.dead = False


class SessionRecorder:
    def __init__(self, logger=None, clock_sync_interval=5.0,
                 boundary_interval=10.0, boundary_bytes=10 * 1024 * 1024,
                 resolve_interval=5.0):
        self._log = logger
        self._clock_sync_interval = clock_sync_interval
        self._boundary_interval = boundary_interval
        self._boundary_bytes = boundary_bytes
        self._resolve_interval = resolve_interval

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

        self._lsl = None                 # the pylsl module, once imported
        self._LostError = _NeverError    # pylsl.util.LostError, once imported
        self._writer = None
        self._resolver = None
        self._file_path = None
        self._state = "idle"             # idle|recording|stopped|unavailable

        self._streams = {}   # sid -> _StreamStat   (guarded by _lock)
        self._inlets = {}    # sid -> StreamInlet   (pump thread only)
        self._seen = {}      # StreamInfo.uid() -> sid   (dedupe, pump thread only)
        self._next_sid = 1

    # ── logging ────────────────────────────────────────────────────────────

    def _logmsg(self, msg, level="info"):
        if self._log is None:
            return
        try:
            getattr(self._log, level, self._log.info)("[lsl-recorder] %s", msg)
        except Exception:
            pass

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, session_dir):
        """Resolve every stream on the network and begin recording to
        ``<session_dir>/plasma_recording_<YYMMDD_HHMMSS>.xdf``. Returns True on
        success, False if LSL is unavailable or the file cannot be opened.
        Never raises."""
        if self._writer is not None:
            return True  # already recording

        try:
            import pylsl
        except Exception as e:  # liblsl missing, bad build, ...
            self._logmsg(f"pylsl/liblsl unavailable — recording disabled: {e}",
                         "warning")
            with self._lock:
                self._state = "unavailable"
            return False
        self._lsl = pylsl
        try:
            from pylsl.util import LostError
            self._LostError = LostError
        except Exception:
            self._LostError = _NeverError

        try:
            fname = "plasma_recording_" + time.strftime("%y%m%d_%H%M%S") + ".xdf"
            path = os.path.join(session_dir, fname)
            writer = XDFWriter()
            writer.open(path)
        except Exception as e:
            self._logmsg(f"could not open XDF file: {e}", "error")
            with self._lock:
                self._state = "unavailable"
            return False
        self._writer = writer
        self._file_path = os.path.abspath(path)
        self._stop.clear()

        try:
            infos = pylsl.resolve_streams(wait_time=1.0)
        except Exception as e:
            self._logmsg(f"resolve_streams failed: {e}", "warning")
            infos = []
        try:
            self._resolver = pylsl.ContinuousResolver()
        except Exception as e:
            self._logmsg(f"ContinuousResolver unavailable (no late-joiner "
                         f"capture): {e}")
            self._resolver = None

        for info in infos:
            self._open_inlet(info)

        with self._lock:
            self._state = "recording"
        self._thread = threading.Thread(target=self._pump, name="lsl-recorder",
                                        daemon=True)
        self._thread.start()
        self._logmsg(f"recording {len(self._streams)} stream(s) -> "
                     f"{self._file_path}")
        return True

    def stop(self):
        """Stop the pump, do a bounded final drain, write footers, close the
        file. Idempotent; keeps the object (and its final ``status()``) alive
        until the next ``start()``."""
        if self._writer is None:
            with self._lock:
                if self._state not in ("stopped", "unavailable"):
                    self._state = "stopped"
            return

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

        # bounded (~2s) final drain of whatever is still buffered
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            drained = self._drain_once(time.monotonic(), final=True)
            if not drained:
                break
            time.sleep(0.05)

        for sid, inlet in list(self._inlets.items()):
            stat = self._streams.get(sid)
            if stat is None or stat.dead:
                continue
            try:
                off = inlet.time_correction(timeout=0.5)
                self._writer.write_clock_offset(sid, self._lsl.local_clock(), off)
            except Exception:
                pass
            try:
                self._writer.write_stream_footer(sid)
            except Exception as e:
                self._logmsg(f"footer failed for {stat.name}: {e}", "warning")

        try:
            self._writer.close()
        except Exception as e:
            self._logmsg(f"error closing XDF: {e}", "error")

        for inlet in list(self._inlets.values()):
            try:
                inlet.close_stream()
            except Exception:
                pass
        self._inlets.clear()
        self._resolver = None
        self._writer = None
        with self._lock:
            self._state = "stopped"
        self._logmsg(f"recording stopped -> {self._file_path}")

    # ── inlet setup ────────────────────────────────────────────────────────

    def _open_inlet(self, info):
        """Open one inlet, write its StreamHeader + first ClockOffset, and
        register a ``_StreamStat``. Called from ``start()`` (before the pump
        thread exists) and from the pump loop for late joiners — never
        concurrently."""
        pylsl = self._lsl
        try:
            uid = info.uid()
        except Exception:
            uid = ""
        if uid and uid in self._seen:
            return

        try:
            inlet = pylsl.StreamInlet(info, max_buflen=360, max_chunklen=0,
                                      recover=False,
                                      processing_flags=pylsl.proc_none)
        except Exception as e:
            self._logmsg(f"could not open inlet for "
                         f"{_safe_name(info)}: {e}", "warning")
            return

        try:
            full = inlet.info(timeout=5.0)
            name = full.name()
            stype = full.type()
            source_id = full.source_id()
            srate = float(full.nominal_srate() or 0.0)
            nchan = int(full.channel_count())
            fmt = channel_format_name(full.channel_format())
            external = not is_plasma_origin(full)
            info_xml = full.as_xml()
        except Exception as e:
            self._logmsg(f"skipping unrecordable stream {_safe_name(info)}: {e}",
                         "warning")
            _close_quietly(inlet)
            return

        sid = self._next_sid
        try:
            self._writer.write_stream_header(sid, info_xml, fmt, nchan)
        except Exception as e:
            self._logmsg(f"stream header failed for {name}: {e}", "error")
            _close_quietly(inlet)
            return
        self._next_sid += 1

        try:
            self._writer.write_clock_offset(
                sid, pylsl.local_clock(), inlet.time_correction(timeout=2.0))
        except Exception:
            pass  # a first estimate can take a moment; the pump retries

        stat = _StreamStat(sid, name, stype, source_id, external, srate,
                           os.path.basename(self._file_path))
        self._inlets[sid] = inlet
        if uid:
            self._seen[uid] = sid
        with self._lock:
            self._streams[sid] = stat
        self._logmsg(f"+ stream {sid}: {name} ({stype or '?'}, {nchan}ch "
                     f"{fmt}{', external' if external else ''})")

    # ── pump ───────────────────────────────────────────────────────────────

    def _pump(self):
        now = time.monotonic()
        next_clock = now + self._clock_sync_interval
        next_boundary = now + self._boundary_interval
        next_resolve = now + self._resolve_interval
        boundary_mark = self._writer.bytes_written

        while not self._stop.is_set():
            now = time.monotonic()
            try:
                self._drain_once(now)
            except Exception as e:
                self._logmsg(f"pump drain error: {e}")

            if now >= next_clock:
                self._sync_clocks(timeout=0.5)
                next_clock = now + self._clock_sync_interval

            if (now >= next_boundary
                    or self._writer.bytes_written - boundary_mark
                    >= self._boundary_bytes):
                try:
                    self._writer.write_boundary()
                except Exception:
                    pass
                boundary_mark = self._writer.bytes_written
                next_boundary = now + self._boundary_interval

            if now >= next_resolve:
                self._resolve_new()
                next_resolve = now + self._resolve_interval

            self._stop.wait(0.02)

    def _drain_once(self, now, final=False):
        """Pull and write one chunk from every live inlet. Returns True if any
        samples were written (used by the final-drain loop in ``stop()``)."""
        wrote_any = False
        for sid, inlet in list(self._inlets.items()):
            stat = self._streams.get(sid)
            if stat is None or stat.dead:
                continue
            try:
                samples, tstamps = inlet.pull_chunk(timeout=0.0, max_samples=4096)
            except self._LostError:
                self._mark_lost(sid, stat)
                continue
            except Exception as e:
                self._logmsg(f"pull_chunk error on {stat.name}: {e}", "warning")
                continue

            if tstamps:
                try:
                    self._writer.write_samples(sid, tstamps, samples)
                except Exception as e:
                    self._logmsg(f"write_samples failed for {stat.name}: {e}",
                                 "error")
                    continue
                wrote_any = True
                with self._lock:
                    stat.n_samples += len(tstamps)
                    stat.last_ts = tstamps[-1]
                    stat.last_recv = now
                    stat.health = "🟢"
            elif not final and now - stat.last_recv > _stale_after(stat.srate):
                with self._lock:
                    stat.health = "🟡 stale"
        return wrote_any

    def _mark_lost(self, sid, stat):
        try:
            self._writer.write_stream_footer(sid)
        except Exception:
            pass
        with self._lock:
            stat.dead = True
            stat.health = "🔴 lost"
        self._logmsg(f"stream lost: {stat.name} (sid {sid})", "warning")

    def _sync_clocks(self, timeout):
        pylsl = self._lsl
        for sid, inlet in list(self._inlets.items()):
            stat = self._streams.get(sid)
            if stat is None or stat.dead:
                continue
            try:
                off = inlet.time_correction(timeout=timeout)
                self._writer.write_clock_offset(sid, pylsl.local_clock(), off)
            except Exception:
                pass

    def _resolve_new(self):
        if self._resolver is None:
            return
        try:
            infos = self._resolver.results()
        except Exception:
            return
        for info in infos:
            try:
                uid = info.uid()
            except Exception:
                continue
            if not uid or uid not in self._seen:
                self._open_inlet(info)

    # ── status ─────────────────────────────────────────────────────────────

    def status(self):
        with self._lock:
            state = self._state
            stats = sorted(self._streams.values(), key=lambda s: s.sid)
            total = sum(s.n_samples for s in stats)
            streams = [{
                "name": s.name,
                "type": s.type,
                "source_id": s.source_id,
                "external": s.external,
                "health": "🔴 lost" if s.dead else s.health,
                "n_samples": s.n_samples,
                "srate": s.srate,
                "xdf_basename": s.xdf_basename,
                "last_ts": s.last_ts,
            } for s in stats]

        n = len(streams)
        plural = "" if n == 1 else "s"
        if state == "unavailable":
            summary = "unavailable (liblsl/pylsl missing?)"
        elif state == "stopped":
            summary = f"stopped · {n} stream{plural} · {total:,} samp"
        else:
            summary = f"{n} stream{plural} · {total:,} samp"

        return {
            "state": state,
            "summary": summary,
            "file": self._file_path or "",
            "streams": streams,
        }


class _NeverError(Exception):
    """Placeholder for ``pylsl.util.LostError`` before pylsl is imported (or if
    that symbol ever moves) — an ``except`` clause that can never match."""


def _safe_name(info):
    try:
        return info.name() or "<unnamed>"
    except Exception:
        return "<?>"


def _close_quietly(inlet):
    try:
        inlet.close_stream()
    except Exception:
        pass
