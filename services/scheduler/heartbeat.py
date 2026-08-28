import os
import threading
import time
from pathlib import Path


class HeartbeatWriter:
    def __init__(self, path: str, interval_seconds: int = 10):
        self.path = Path(path)
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop_event.is_set():
            self.path.write_text(str(int(time.time())), encoding="utf-8")
            os.utime(self.path, None)
            self._stop_event.wait(self.interval_seconds)

