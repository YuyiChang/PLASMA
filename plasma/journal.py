"""Session task journaler — a lightweight LSL string-marker stream any session
can push task-start / task-end / flag events onto, alongside the sensor streams.

Core (not device-specific): `IntegratedPanel` owns the outlet and the
Session-dashboard accordion. Stdlib + pylsl only.
"""
import os

JOURNAL_STREAM = "PLASMA"          # LSL stream name; only an .xdf label
_TASK_FILE = "task.txt"            # newline-delimited labels, relative to CWD
_DEFAULT_TASKS = ["A", "B", "C", "D", "E"]
MSG_TYPES = ["Task start", "Task end", "Flag"]


def task_labels():
    """Activity labels for the journaler dropdown — one per line of `task.txt`
    in the working directory, or a small default set when it's absent."""
    if os.path.exists(_TASK_FILE):
        try:
            with open(_TASK_FILE, "r") as f:
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
    """A 1-channel string `StreamOutlet` named `JOURNAL_STREAM`, or None if LSL
    is unavailable (missing liblsl, etc.) — never fatal to app startup."""
    try:
        from pylsl import StreamInfo, StreamOutlet
        info = StreamInfo(name=JOURNAL_STREAM, type="string",
                          channel_count=1, channel_format="string")
        return StreamOutlet(info)
    except Exception:
        return None
