"""Session task journaler — a lightweight LSL string-marker stream any session
can push task-start / task-end / flag events onto, alongside the sensor streams.

Core (not device-specific): `IntegratedPanel` owns the outlet and the
Session-dashboard accordion. Stdlib + pylsl only.

The stream name and the `task.txt` location come from `plasma.app_context` so a
wrapper app (YAMS) can rebrand the stream without editing this module.
"""
import os

from plasma.app_context import app_context

_DEFAULT_TASKS = ["A", "B", "C", "D", "E"]
MSG_TYPES = ["Task start", "Task end", "Flag"]


def task_labels():
    """Activity labels for the journaler dropdown — one per line of the app's
    `task.txt`, or a small default set when it's absent."""
    path = app_context().task_path
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                labels = [ln.strip() for ln in f if ln.strip()]
            if labels:
                return labels
        except Exception:
            pass
    return list(_DEFAULT_TASKS)


def format_journal_msg(msg_type, task="", free_text=""):
    """`"<type> [<task>] <free text>"` — the string pushed onto the LSL stream."""
    return f"{msg_type} [{task}] {free_text}".rstrip()


def open_journal_outlet():
    """A 1-channel string `StreamOutlet` named `app_context().journal_stream`, or
    None if LSL is unavailable (missing liblsl, etc.) — never fatal to startup."""
    try:
        from pylsl import StreamInfo, StreamOutlet
        info = StreamInfo(name=app_context().journal_stream, type="string",
                          channel_count=1, channel_format="string")
        return StreamOutlet(info)
    except Exception:
        return None
