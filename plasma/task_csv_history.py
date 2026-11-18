"""Keep validated copies of recent task CSVs outside Gradio's upload cache."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from plasma.task_sequence import read_tasks


class TaskCsvHistory:
    def __init__(self, directory, limit=10):
        self.directory = Path(directory)
        self.limit = limit
        self.lock = threading.RLock()

    def _entries(self):
        try:
            entries = json.loads((self.directory / "recent.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)
                and re.fullmatch(r"[0-9a-f]{64}", str(e.get("id", "")))
                and isinstance(e.get("name"), str) and isinstance(e.get("loaded_at"), str)
                and (self.directory / (e["id"] + ".csv")).is_file()][:self.limit]

    def choices(self):
        with self.lock:
            return [(f'{e["name"]} · {e["loaded_at"]}', e["id"]) for e in self._entries()]

    def _write_index(self, entries):
        fd, path = tempfile.mkstemp(prefix=".recent-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
            os.replace(path, self.directory / "recent.json")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def remember(self, path):
        """Validate a private copy before publishing it; keep original files intact."""
        data = Path(path).read_bytes()
        name = Path(path).name
        identifier = hashlib.sha256(name.encode("utf-8") + b"\0" + data).hexdigest()
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".task-", suffix=".csv", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                read_tasks(temporary)
                os.replace(temporary, self.directory / (identifier + ".csv"))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            entries = self._entries()
            entry = {"id": identifier, "name": name,
                     "loaded_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")}
            updated = [entry, *(e for e in entries if e["id"] != identifier)][:self.limit]
            self._write_index(updated)
            keep = {e["id"] for e in updated}
            for old in entries:
                if old["id"] not in keep:
                    try:
                        (self.directory / (old["id"] + ".csv")).unlink(missing_ok=True)
                    except OSError:
                        pass  # an unused cache copy must not prevent loading a task
        return identifier

    def load(self, identifier):
        with self.lock:
            entries = self._entries()
            entry = next((e for e in entries if e["id"] == identifier), None)
            if entry is None:
                raise ValueError("Choose an available previous task CSV. Upload it again if it was removed.")
            tasks = read_tasks(self.directory / (identifier + ".csv"))
            # Keep the selected file at the top after a successful reload.
            self._write_index([entry, *(e for e in entries if e["id"] != identifier)])
            return tasks
