"""File-downloader panel: session→tag resolution and the streaming transfer
generator (`_download`). No real USB drive — a temp dir per fake device, each
holding a couple of junk `.bin` files and a `uuid.txt`."""
import os

import gradio as gr
import pytest

from plasma.devices.msense.extract.options import PANEL_FIELDS, ExtractionOptions
from plasma.devices.msense.panels import downloader as D

_OPT_DEFAULTS = [getattr(ExtractionOptions(), f) for f in PANEL_FIELDS]


def _val(update):
    """The value a yielded output carries — a plain str for gr.Markdown, or the
    `value` constructor-arg for a gr.Component update."""
    if isinstance(update, str):
        return update
    return getattr(update, "constructor_args", {}).get("value", None)


@pytest.fixture
def drives(tmp_path):
    """{drive_path: [file, ...]} for two fake devices, MACs …EE:01 / …EE:02."""
    state = {}
    for i in (1, 2):
        d = tmp_path / f"drive{i}"
        d.mkdir()
        (d / "12345601ac.bin").write_bytes(b"\0" * 2_000_000)
        (d / "12345601ecg.bin").write_bytes(b"\0" * 3_000_000)
        (d / "uuid.txt").write_text(f"AA:BB:CC:DD:EE:0{i}\n")
        state[str(d)] = sorted(str(p) for p in d.iterdir())
    return state


def test_resolve_per_drive_tags_by_uuid(drives):
    per_drive = D._resolve_per_drive(["123456"], drives)
    assert set(per_drive) == {"AA-BB-CC-DD-EE-01", "AA-BB-CC-DD-EE-02"}
    assert all(len(v) == 3 for v in per_drive.values())  # 2 bins + uuid.txt


def test_resolve_per_drive_selection_filters_bins(drives):
    assert not any(f.endswith(".bin")
                   for v in D._resolve_per_drive(["999"], drives).values() for f in v)


def test_resolve_per_drive_prefers_uuid_name(tmp_path):
    d = tmp_path / "drive"
    d.mkdir()
    (d / "12345601ac.bin").write_bytes(b"\0" * 100)
    (d / "uuid.txt").write_text("EA:94:11:E5:D4:34 (random)\nName: MSense4ECG-EX4BT\n")
    per_drive = D._resolve_per_drive([], {str(d): [str(d / "12345601ac.bin"), str(d / "uuid.txt")]})
    assert list(per_drive) == ["MSense4ECG-EX4BT"]


def test_resolve_per_drive_falls_back_to_mac_without_name(tmp_path):
    d = tmp_path / "drive"
    d.mkdir()
    (d / "12345601ac.bin").write_bytes(b"\0" * 100)
    (d / "uuid.txt").write_text("EA:94:11:E5:D4:34 (random)\n")  # old firmware, no Name
    per_drive = D._resolve_per_drive([], {str(d): [str(d / "12345601ac.bin"), str(d / "uuid.txt")]})
    assert list(per_drive) == ["EA-94-11-E5-D4-34"]


def test_download_streams_progress_and_returns_zip(drives):
    updates = list(D._download(["123456"], drives, False, gr.Progress(), *_OPT_DEFAULTS))

    # more than a single terminal update — it actually streamed
    assert len(updates) > 3
    head, table, log, btn = updates[-1]

    rows = _val(table)
    assert [r[0] for r in rows] == ["AA-BB-CC-DD-EE-01", "AA-BB-CC-DD-EE-02", "archive"]
    assert all("done" in r[1] or "zipped" in r[1] for r in rows)
    assert "Copied 6/6" in _val(head)
    assert "Starting: 6 file(s)" in _val(log)

    b = btn.constructor_args
    assert b["interactive"] and b["value"].endswith("_msense.zip") and os.path.exists(b["value"])


def test_download_without_browsing_is_a_no_op(drives):
    updates = list(D._download([], {}, False, gr.Progress(), *_OPT_DEFAULTS))
    assert len(updates) == 1
    assert "Browse a drive first" in updates[0][0]
    assert updates[0][3].constructor_args["interactive"] is False


def test_download_reports_a_failed_drive(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "12345601ac.bin").write_bytes(b"\0" * 1000)
    (good / "uuid.txt").write_text("AA:BB:CC:DD:EE:01\n")
    files_state = {
        str(good): [str(good / "12345601ac.bin"), str(good / "uuid.txt")],
        "/no/such/drive": ["/no/such/drive/12345601ac.bin"],
    }
    updates = list(D._download([], files_state, False, gr.Progress(), *_OPT_DEFAULTS))
    head, table, log, btn = updates[-1]
    phases = [r[1] for r in _val(table)]
    assert any("failed" in p for p in phases)
    assert any("done" in p or "zipped" in p for p in phases)  # good drive + archive
    assert "Failed:" in _val(head)
    # the good drive's data is still delivered
    assert btn.constructor_args["interactive"]
