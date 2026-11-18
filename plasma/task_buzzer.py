"""Host audio for Journaler expiry, independent of browser visibility."""
from array import array
import logging
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import wave


def write_buzzer_wave(path):
    """Small mono PCM WAV matching the previous two 880 Hz browser tones."""
    rate = 44100
    samples = array("h")
    for index in range(int(rate * 0.56)):
        t = index / rate
        value = 0.0
        for offset in (0.0, 0.3):
            local = t - offset
            if 0 <= local < 0.22:
                envelope = min(local / 0.015, (0.22 - local) / 0.205)
                value += 0.2 * max(0, envelope) * math.sin(2 * math.pi * 880 * local)
        samples.append(round(32767 * value))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(samples.tobytes())


def play_host_sound(path):
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        return
    if sys.platform == "darwin":
        command = ["/usr/bin/afplay", str(path)]
    else:
        player = shutil.which("paplay") or shutil.which("aplay")
        if not player:
            raise RuntimeError("No host audio player found; install paplay or aplay on the PLASMA computer")
        command = [player, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Audio player exited with status {result.returncode}")


class TaskBuzzer:
    """One lazy worker per panel; serialize audio and deduplicate completions.

    On macOS inhibit idle sleep during a task and until its queued alarm ends.
    Display sleep is permitted. Repeated Test clicks coalesce while one test is
    pending; completion alarms are never coalesced with test sounds.
    """

    def __init__(self, logger=None, *, player=play_host_sound, platform=None, popen=subprocess.Popen):
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.player = player
        self.platform = sys.platform if platform is None else platform
        self.popen = popen
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.worker = None
        self.closed = False
        self.task_running = False
        self.pending = 0
        self.test_pending = False
        self.last_completion = 0
        self.sleep_process = None
        self.sleep_error = ""
        self.message = "Buzzer ready — sound plays on the PLASMA computer."

    def _sync_sleep(self):
        if self.platform != "darwin":
            return
        awake = not self.closed and (self.task_running or self.pending > 0)
        if self.sleep_process is not None and self.sleep_process.poll() is not None:
            self.sleep_process = None
            self.sleep_error = "Idle-sleep protection stopped unexpectedly"
            self.logger.warning(self.sleep_error)
        if awake and self.sleep_process is None:
            try:
                self.sleep_process = self.popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                               stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.sleep_error = ""
            except OSError as exc:
                self.sleep_error = f"Idle-sleep protection unavailable: {exc}"
                self.logger.warning(self.sleep_error)
        elif not awake and self.sleep_process is not None:
            process, self.sleep_process = self.sleep_process, None
            try:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.sleep_error = f"Could not release idle-sleep protection: {exc}"
                self.logger.warning(self.sleep_error)

    def set_task_running(self, running):
        with self.lock:
            self.task_running = bool(running) and not self.closed
            self._sync_sleep()

    def complete(self, completion_id):
        return self._enqueue(completion_id)

    def test(self):
        return self._enqueue(None)

    def _enqueue(self, completion_id):
        with self.lock:
            if self.closed:
                return False
            if completion_id is not None:
                if completion_id <= self.last_completion:
                    return False
                self.last_completion = completion_id
            elif self.test_pending:
                return False
            else:
                self.test_pending = True
            self.pending += 1
            self.message = "Buzzer queued on the PLASMA computer."
            self._sync_sleep()
            self.jobs.put(completion_id)
            if self.worker is None:
                self.worker = threading.Thread(target=self._play_loop, name="plasma-buzzer", daemon=True)
                try:
                    self.worker.start()
                except Exception as exc:
                    self.worker = None
                    self.jobs.get_nowait()
                    self.jobs.task_done()
                    self.pending -= 1
                    if completion_id is None:
                        self.test_pending = False
                    self.message = f"Buzzer worker failed to start: {exc}"
                    self.logger.warning(self.message)
                    self._sync_sleep()
                    return False
            return True

    def _play_loop(self):
        temporary = None
        try:
            while True:
                job = self.jobs.get()
                try:
                    with self.lock:
                        if self.closed:
                            break
                        self.message = "Playing buzzer on the PLASMA computer."
                    try:
                        if temporary is None:
                            temporary = tempfile.TemporaryDirectory(prefix="plasma-buzzer-")
                            path = Path(temporary.name) / "buzzer.wav"
                            write_buzzer_wave(path)
                        self.player(path)
                        with self.lock:
                            self.message = "Buzzer played on the PLASMA computer."
                        self.logger.info("BUZZER: played (%s)", "test" if job is None else f"completion {job}")
                    except Exception as exc:
                        with self.lock:
                            self.message = f"Buzzer playback failed: {exc}"
                        self.logger.warning(self.message)
                    finally:
                        with self.lock:
                            self.pending -= 1
                            if job is None:
                                self.test_pending = False
                            self._sync_sleep()
                finally:
                    self.jobs.task_done()
        finally:
            with self.lock:
                self.pending = 0
                self.test_pending = False
                self._sync_sleep()
                while True:
                    try:
                        self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self.jobs.task_done()
            if temporary is not None:
                temporary.cleanup()

    def status(self):
        with self.lock:
            return self.message + (f" · {self.sleep_error}" if self.sleep_error else "")

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.task_running = False
            self._sync_sleep()
            if self.worker is not None:
                self.jobs.put(None)
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=6)
