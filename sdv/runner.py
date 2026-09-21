"""Run the whole pipeline from the console: a background job with live progress.

`Runner.start(fresh)` processes every email of the attached inbox in a small thread pool and saves each result to the
case store the moment it is done, so the console fills up while the job runs. `fresh=True` first clears the processed
cases (uploads and reviewer decisions are kept). Only one job runs at a time.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Runner:
    def __init__(self, store, inbox, jev=None, jev_mode: str = "off", vision=None, workers: int = 4):
        self.store, self.inbox, self.jev, self.jev_mode, self.vision, self.workers = store, inbox, jev, jev_mode, vision, workers
        self._lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "error": None, "started_at": None, "seconds": None,
                      "counts": {}}

    def status(self) -> dict:
        with self._lock:
            return dict(self.state, counts=dict(self.state["counts"]))

    def start(self, fresh: bool = True) -> bool:
        """-> False if a job is already running."""
        with self._lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, done=0, total=0, error=None, started_at=time.time(), seconds=None, counts={})
        threading.Thread(target=self._work, args=(fresh,), daemon=True).start()
        return True

    def _work(self, fresh: bool) -> None:
        from .pipeline import _safe_id, _safe_process

        try:
            emails = self.inbox.emails()
            with self._lock:
                self.state["total"] = len(emails)
            if fresh:
                self.store.clear_processed()
            jobs = [(e, f"malformed_{k + 1:04d}") for k, e in enumerate(emails)]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for r in pool.map(lambda j: _safe_process(j[0], self.inbox, self.jev, self.jev_mode, self.vision, j[1]), jobs):
                    self.store.upsert(r.to_dict())
                    with self._lock:
                        self.state["done"] += 1
                        key = r.status if r.category == "BL_COMPARISON" else r.category
                        self.state["counts"][key] = self.state["counts"].get(key, 0) + 1
        except Exception as e:  # e.g. a missing library: say so, do not die silently
            with self._lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self.state["running"] = False
                self.state["seconds"] = round(time.time() - self.state["started_at"], 1)
