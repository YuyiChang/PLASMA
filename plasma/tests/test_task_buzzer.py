"""Native audio is queued by Python completion, without any browser events."""
from array import array
from pathlib import Path
import subprocess
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import gradio as gr
import pytest

from plasma import task_buzzer as module
from plasma.task_buzzer import TaskBuzzer, play_host_sound, write_buzzer_wave
from plasma.task_sequence import Task, TaskSequence
from plasma.integrated_panel import IntegratedPanel


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_generated_wave_has_two_tones_and_a_silent_gap(tmp_path):
    path = tmp_path / "buzzer.wav"
    write_buzzer_wave(path)
    with wave.open(str(path), "rb") as sound:
        assert sound.getnchannels() == 1
        assert sound.getsampwidth() == 2
        rate = sound.getframerate()
        assert rate == 44100
        samples = array("h", sound.readframes(sound.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    assert max(abs(x) for x in samples[:int(rate * 0.22)]) > 1000
    assert all(x == 0 for x in samples[int(rate * 0.23):int(rate * 0.29)])
    assert max(abs(x) for x in samples[int(rate * 0.3):int(rate * 0.52)]) > 1000


def test_macos_player_uses_system_afplay_with_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.subprocess, "run", run)
    path = tmp_path / "file with spaces.wav"
    play_host_sound(path)
    assert run.call_args.args[0] == ["/usr/bin/afplay", str(path)]
    assert run.call_args.kwargs["timeout"] == 5


def test_linux_requires_an_audio_player(monkeypatch, tmp_path):
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="install paplay or aplay"):
        play_host_sound(tmp_path / "beep.wav")


def test_windows_uses_standard_library_winsound(monkeypatch, tmp_path):
    player = Mock()
    monkeypatch.setitem(sys.modules, "winsound", SimpleNamespace(PlaySound=player, SND_FILENAME=1, SND_NODEFAULT=2))
    monkeypatch.setattr(module.sys, "platform", "win32")
    path = tmp_path / "beep.wav"
    play_host_sound(path)
    player.assert_called_once_with(str(path), 3)


def test_expiry_sounds_without_polling_browser_or_snapshot():
    played, ended = threading.Event(), threading.Event()
    paths, markers = [], []

    def play(path):
        assert Path(path).is_file()
        paths.append(path)
        played.set()

    def emit(marker):
        markers.append(marker)
        if marker.endswith(" end"):
            ended.set()

    buzzer = TaskBuzzer(player=play, platform="test")
    runner = TaskSequence(emit, on_complete=buzzer.complete,
                          on_running_changed=buzzer.set_task_running)
    try:
        runner.load([Task("Short", "", 0.03)])
        runner.start()
        assert played.wait(3) and ended.wait(3)
        assert markers == ["[Short] start", "[Short] end"]
        assert len(paths) == 1
        assert not buzzer.complete(1)  # duplicate delivery / later view refresh
        assert wait_for(lambda: buzzer.pending == 0)
    finally:
        runner.end()
        buzzer.close()
    assert not Path(paths[0]).exists()


def test_test_clicks_coalesce_but_do_not_consume_expiry_alarm():
    entered, release = threading.Event(), threading.Event()
    played = []

    def play(path):
        played.append(path)
        entered.set()
        assert release.wait(3)

    buzzer = TaskBuzzer(player=play, platform="test")
    try:
        assert buzzer.test()
        assert entered.wait(3)
        assert not buzzer.test()
        assert buzzer.complete(1)
        assert not buzzer.complete(1)
        release.set()
        assert wait_for(lambda: buzzer.pending == 0)
        assert len(played) == 2 and played[0] == played[1]
    finally:
        release.set()
        buzzer.close()


def test_sleep_assertion_outlives_countdown_until_audio_finishes():
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    entered, release = threading.Event(), threading.Event()

    def play(path):
        entered.set()
        assert release.wait(3)

    buzzer = TaskBuzzer(player=play, platform="darwin", popen=popen)
    try:
        buzzer.set_task_running(True)
        assert popen.call_args.args[0] == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
        buzzer.complete(1)
        assert entered.wait(3)
        buzzer.set_task_running(False)
        process.terminate.assert_not_called()
        release.set()
        assert wait_for(lambda: process.terminate.called)
        process.terminate.assert_called_once()
        popen.assert_called_once()
    finally:
        release.set()
        buzzer.close()


def test_audio_failure_is_visible_and_does_not_break_task_marker():
    logger = Mock()
    buzzer = TaskBuzzer(logger, player=Mock(side_effect=RuntimeError("No output device")), platform="test")
    markers = []
    runner = TaskSequence(markers.append, on_complete=buzzer.complete,
                          on_running_changed=buzzer.set_task_running)
    try:
        runner.load([Task("Short", "", 0.02)])
        runner.start()
        assert wait_for(lambda: "playback failed" in buzzer.status())
        assert markers == ["[Short] start", "[Short] end"]
        assert "No output device" in buzzer.status()
        logger.warning.assert_called()
    finally:
        runner.end()
        buzzer.close()


def test_sleep_assertion_failure_is_reported_without_preventing_audio():
    played = threading.Event()
    buzzer = TaskBuzzer(player=lambda path: played.set(), platform="darwin",
                        popen=Mock(side_effect=OSError("not available")))
    try:
        buzzer.set_task_running(True)
        buzzer.complete(1)
        assert played.wait(3)
        assert "Idle-sleep protection unavailable" in buzzer.status()
    finally:
        buzzer.close()


def test_backend_test_button_does_not_send_markers():
    panel = IntegratedPanel.__new__(IntegratedPanel)
    panel.journal = Mock()
    panel.task_buzzer = TaskBuzzer(player=Mock(), platform="test")
    try:
        panel._test_buzzer()
        assert wait_for(lambda: panel.task_buzzer.pending == 0)
        panel.task_buzzer.player.assert_called_once()
        panel.journal.assert_not_called()
    finally:
        panel.task_buzzer.close()


def test_closing_buzzer_prevents_future_playback():
    player = Mock()
    buzzer = TaskBuzzer(player=player, platform="test")
    buzzer.close()
    buzzer.close()
    assert not buzzer.test() and not buzzer.complete(1)
    player.assert_not_called()


def test_worker_start_failure_leaves_no_pending_audio(monkeypatch):
    worker = Mock()
    worker.start.side_effect = RuntimeError("No more threads")
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: worker)
    buzzer = TaskBuzzer(player=Mock(), platform="test")
    assert not buzzer.test()
    assert buzzer.pending == 0 and not buzzer.test_pending
    assert "failed to start" in buzzer.status()
    buzzer.close()


def test_audio_cleanup_happens_after_recorder_flush_and_cannot_prevent_it():
    panel = IntegratedPanel.__new__(IntegratedPanel)
    panel.available_devices = []
    calls = []
    panel.lsl_recorder = Mock()
    panel.lsl_recorder.stop.side_effect = lambda: calls.append("flush")
    panel.task_buzzer = Mock()

    def close():
        calls.append("close audio")
        raise RuntimeError("Audio cleanup failed")

    panel.task_buzzer.close.side_effect = close
    panel._atexit_cleanup()
    assert calls == ["flush", "close audio"]


def test_task_view_renders_audio_status_as_text_not_button_interactivity():
    panel = IntegratedPanel.__new__(IntegratedPanel)
    panel.journal_outlet = Mock()
    panel.task_sequence = TaskSequence(Mock())
    panel.task_buzzer = Mock()
    panel.task_buzzer.status.return_value = "Buzzer played on the PLASMA computer."
    rendered = panel._task_view_refresh(None)
    assert rendered[6] == "Buzzer played on the PLASMA computer."
    assert panel._task_view_refresh(rendered[-1])[6] == gr.skip()
