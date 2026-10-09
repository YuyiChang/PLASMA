"""Task boundaries, CSV compatibility, and browser-independent completion."""
import threading
from unittest.mock import Mock

import gradio as gr
import pytest

from plasma.task_sequence import Task, TaskSequence, format_remaining, read_tasks
from plasma.integrated_panel import IntegratedPanel


class FakeTimer:
    def __init__(self, seconds, callback, args):
        self.callback, self.args = callback, args

    def start(self):
        pass

    def cancel(self):
        pass

    def fire(self):
        self.callback(*self.args)


def sequence():
    messages = []
    now = [10.0]
    runner = TaskSequence(messages.append, clock=lambda: now[0], timer_factory=FakeTimer)
    runner.load([Task("A", "Move A", 60), Task("B", "Move B", 605)])
    return runner, messages, now


def test_bom_csv_with_literal_newlines_and_duplicate_conditions(tmp_path):
    path = tmp_path / "stimuli.csv"
    path.write_text('Condition,Move_Instruction,load\n'
                    'Be still,Please be still.\\n\\nPress [SPACE] to start.,420\n'
                    'Video,"Watch, and relax.",605\n'
                    'Be still,Recovery,300\n', encoding="utf-8-sig")
    tasks = read_tasks(path)
    assert len(tasks) == 3
    assert tasks[0].name == "Be still"
    assert "\n\n" in tasks[0].instruction
    assert "\\n" not in tasks[0].instruction
    assert format_remaining(tasks[0].seconds) == "07:00"
    assert format_remaining(tasks[1].seconds) == "10:05"
    assert tasks[-1].name == tasks[0].name  # duplicate names retain separate rows


@pytest.mark.parametrize("body", [
    "Condition,load\nA,1\n",
    "Condition,Move_Instruction,load\n",
    "Condition,Move_Instruction,load\n,move,60\n",
    *[f"Condition,Move_Instruction,load\nA,move,{value}\n"
      for value in ["", "x", "0", "-1", "nan", "inf"]],
])
def test_invalid_csv(tmp_path, body):
    path = tmp_path / "tasks.csv"
    path.write_text(body)
    with pytest.raises(ValueError):
        read_tasks(path)


def test_navigation_and_stale_timer():
    runner, messages, now = sequence()
    runner.move(-1)  # boundary must not change state
    runner.start()
    old_timer = runner.timer
    runner.start()  # duplicate clicks do not duplicate markers
    now[0] += 1.2
    assert runner.snapshot()["remaining"] == "00:59"
    runner.move(1)
    assert runner.snapshot()["remaining"] == "10:05"
    runner.start()
    old_timer.fire()  # canceled callback racing the next task
    assert runner.snapshot()["running"]
    runner.move(1)  # boundary must not end a running task
    runner.end()
    runner.end()
    assert messages == ["[A] start", "[A] end", "[B] start", "[B] end"]


def test_expiry_then_next_requires_start():
    runner, messages, now = sequence()
    runner.start()
    timer = runner.timer
    now[0] += 60
    assert runner.snapshot()["remaining"] == "00:00"
    timer.fire()
    runner.end()
    assert runner.snapshot()["status"] == "Completed"
    runner.start()  # completed row cannot accidentally restart
    runner.move(1)
    assert runner.snapshot()["status"] == "Ready"
    assert messages == ["[A] start", "[A] end"]


def test_completion_without_browser_polling():
    finished = threading.Event()
    messages = []

    def emit(message):
        messages.append(message)
        if message.endswith(" end"):
            finished.set()

    runner = TaskSequence(emit)
    runner.load([Task("Short", "", 0.02)])
    runner.start()
    try:
        assert finished.wait(2)
        assert messages == ["[Short] start", "[Short] end"]
    finally:
        runner.end()


def test_upload_and_clear_end_running_task():
    runner, messages, _ = sequence()
    runner.start()
    runner.load([Task("Replacement", "", 3)])
    assert runner.snapshot()["status"] == "Ready"
    runner.start()
    runner.load([])
    assert runner.snapshot()["total"] == 0
    assert messages == ["[A] start", "[A] end", "[Replacement] start", "[Replacement] end"]


def test_marker_failure_is_visible_and_does_not_duplicate_end():
    runner, messages, _ = sequence()
    runner.emit = Mock(side_effect=RuntimeError("outlet failed"))
    with pytest.raises(RuntimeError):
        runner.start()
    assert runner.snapshot()["status"] == "Ready"
    runner.emit = messages.append
    runner.start()
    runner.emit = Mock(side_effect=RuntimeError("outlet failed"))
    runner.timer.fire()
    runner.end()
    assert runner.emit.call_count == 1
    assert "outlet failed" in runner.snapshot()["error"]
    assert not runner.snapshot()["running"]


def test_stop_collection_sends_end_before_recorder_stop():
    ip = IntegratedPanel.__new__(IntegratedPanel)
    calls = []
    ip.task_sequence = TaskSequence(calls.append, timer_factory=FakeTimer)
    ip.task_sequence.load([Task("A", "", 5)])
    ip.task_sequence.start()
    ip.available_devices = []
    ip.logger = Mock()
    ip.lsl_recorder = Mock()
    ip.lsl_recorder.stop.side_effect = lambda: calls.append("recorder stop")
    ip.lsl_recorder.status.return_value = {"file": "test.xdf"}
    ip.stop_collection()
    assert calls == ["[A] start", "[A] end", "recorder stop"]


def test_task_ui_builds_and_uses_existing_outlet():
    ip = IntegratedPanel.__new__(IntegratedPanel)
    ip.journal_outlet = Mock()
    ip.logger = Mock()
    ip.task_sequence = TaskSequence(ip._sequence_marker, timer_factory=FakeTimer)
    with gr.Blocks() as app:
        ip._task_sequence_interface()
    assert any(component.get("props", {}).get("label") == "Task sequence CSV"
               for component in app.config["components"])
    ip.task_sequence.load([Task("A", "", 5)])
    ip.task_sequence.start()
    ip.task_sequence.end()
    assert [call.args[0] for call in ip.journal_outlet.push_sample.call_args_list] == [
        ["[A] start"], ["[A] end"]]


def test_task_displays_have_independent_caches_and_share_controller():
    ip = IntegratedPanel.__new__(IntegratedPanel)
    ip.journal_outlet = Mock()
    ip.task_sequence, messages, _ = sequence()
    dashboard = ip._task_view_refresh(None)
    focus = ip._task_view_refresh(None)
    assert dashboard[:6] == focus[:6]
    assert "Move A" == focus[1]
    unchanged = ip._task_view_refresh(focus[-1])
    assert unchanged[:6] == [gr.skip()] * 6
    ip.task_sequence.move(1)
    focus_next = ip._task_view_refresh(focus[-1])
    dashboard_next = ip._task_view_refresh(dashboard[-1])
    assert focus_next[1] == dashboard_next[1] == "Move B"
    assert "10:05" in dashboard_next[0]
    ip.task_sequence.start()
    ip.task_sequence.start()
    ip.task_sequence.end()
    assert messages == ["[B] start", "[B] end"]


def test_memo_displays_do_not_consume_each_others_updates():
    ip = IntegratedPanel.__new__(IntegratedPanel)
    ip._render_memo = Mock(return_value="Ready")
    dashboard = ip._memo_view_refresh(None)
    focus = ip._memo_view_refresh(None)
    assert dashboard == focus == ["Ready", "Ready"]
    assert ip._memo_view_refresh(focus[-1])[0] == gr.skip()
    ip._render_memo.return_value = "Recording"
    assert ip._memo_view_refresh(dashboard[-1]) == ["Recording", "Recording"]
    assert ip._memo_view_refresh(focus[-1]) == ["Recording", "Recording"]


def test_focus_view_has_no_upload_or_extra_controller():
    ip = IntegratedPanel.__new__(IntegratedPanel)
    ip.journal_outlet = Mock()
    ip.task_sequence, _, _ = sequence()
    runner = ip.task_sequence
    ip._render_memo = Mock(return_value="Memo status")
    with gr.Blocks() as app:
        ip.focus_interface()
    assert ip.task_sequence is runner
    assert not any(component["type"] == "file" for component in app.config["components"])
    assert len(ip._session_views) == 2
    assert all(view["tab"] == "Focus" and not view["timer"].active
               for view in ip._session_views)
