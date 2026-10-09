"""CSV task sequences and a browser-independent, monotonic countdown."""
import csv
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class Task:
    name: str
    instruction: str
    seconds: float


def read_tasks(path):
    tasks = []
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"Condition", "Move_Instruction", "load"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
        for row in reader:
            name = (row.get("Condition") or "").strip()
            try:
                seconds = float(row.get("load") or "")
            except (ValueError, TypeError):
                seconds = float("nan")
            if not name or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"CSV row ending at line {reader.line_num}: "
                                 "Condition must be nonempty and load must be a positive finite number.")
            tasks.append(Task(name, (row.get("Move_Instruction") or "").replace("\\n", "\n"), seconds))
    if not tasks:
        raise ValueError("CSV contains no task rows.")
    return tasks


def format_remaining(seconds):
    minutes, seconds = divmod(max(0, math.ceil(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


class TaskSequence:
    """One shared sequence per acquisition panel; serialize markers and actions.

    A daemon timer ends the task even when the browser stops polling. Generation
    checks prevent a canceled timer from ending a subsequently started task.
    """

    def __init__(self, emit, clock=time.monotonic, timer_factory=threading.Timer):
        self.emit = emit
        self.clock = clock
        self.timer_factory = timer_factory
        self.lock = threading.RLock()
        self.tasks = []
        self.index = 0
        self.status = "No CSV loaded"
        self.deadline = None
        self.timer = None
        self.generation = 0
        self.error = ""
        self.completion_id = 0

    def load(self, tasks):
        with self.lock:
            self._end()
            self.tasks = list(tasks)
            self.index = 0
            self.status = "Ready" if tasks else "No CSV loaded"

    def start(self):
        with self.lock:
            if not self.tasks or self.deadline is not None or self.status != "Ready":
                return
            self.error = ""
            self.emit(f"[{self.tasks[self.index].name}] start")
            self.deadline = self.clock() + self.tasks[self.index].seconds
            self.status = "Running"
            self.generation += 1
            self.timer = self.timer_factory(self.tasks[self.index].seconds,
                                            self._expire, args=(self.generation,))
            self.timer.daemon = True
            self.timer.start()

    def _expire(self, generation):
        with self.lock:
            if generation == self.generation and self.deadline is not None:
                self._end("Completed")

    def _end(self, status="Ended"):
        if self.deadline is None:
            return
        self.deadline = None
        self.generation += 1
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.status = status
        if status == "Completed":
            self.completion_id += 1
        try:
            self.emit(f"[{self.tasks[self.index].name}] end")
        except Exception as exc:
            self.error = f"End marker failed: {exc}"

    def end(self):
        with self.lock:
            self._end()

    def move(self, offset):
        with self.lock:
            target = self.index + offset
            if not 0 <= target < len(self.tasks):
                return
            self._end()
            self.index = target
            self.status = "Ready"

    def snapshot(self):
        with self.lock:
            if self.deadline is not None and self.clock() >= self.deadline:
                self._end("Completed")
            task = self.tasks[self.index] if self.tasks else None
            remaining = (max(0, self.deadline - self.clock()) if self.deadline is not None
                         else task.seconds if task and self.status == "Ready" else 0)
            return dict(task=task, index=self.index, total=len(self.tasks),
                        remaining=format_remaining(remaining), status=self.status,
                        error=self.error, running=self.deadline is not None,
                        completion_id=self.completion_id)
