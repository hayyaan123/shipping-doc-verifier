"""Run the whole pipeline as a background job with live progress.

`Runner.start(fresh)` prepares the inbox (unpack a zip, download a link, find the bundle folder), processes every
email in a small thread pool and saves each result to the batch's case store the moment it is done, so a page can
show the list filling up. `fresh=True` first clears the processed cases (uploaded checks and reviewer decisions are
kept). Only one job runs at a time per runner.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Runner:
    def __init__(self, store, prepare, jev=None, jev_mode: str = "off", vision=None, workers: int = 4):
        """`prepare` is a callable returning the Inbox to read (called inside the job, so slow work shows as progress)."""
        self.store, self.prepare, self.jev, self.jev_mode, self.vision, self.workers = store, prepare, jev, jev_mode, vision, workers
        self._lock = threading.Lock()
        self.state = {"running": False, "phase": "idle", "done": 0, "total": 0, "error": None, "started_at": None,
                      "seconds": None, "counts": {}}

    def status(self) -> dict:
        with self._lock:
            return dict(self.state, counts=dict(self.state["counts"]))

    def start(self, fresh: bool = True) -> bool:
        """-> False if a job is already running."""
        with self._lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, phase="preparing", done=0, total=0, error=None, started_at=time.time(),
                              seconds=None, counts={})
        threading.Thread(target=self._work, args=(fresh,), daemon=True).start()
        return True

    def _work(self, fresh: bool) -> None:
        from .pipeline import _safe_process
        from .workspace import WorkspaceError

        try:
            inbox = self.prepare()
            emails = inbox.emails()
            with self._lock:
                self.state.update(total=len(emails), phase="processing")
            if fresh:
                self.store.clear_processed()
            jobs = [(e, f"malformed_{k + 1:04d}") for k, e in enumerate(emails)]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for r in pool.map(lambda j: _safe_process(j[0], inbox, self.jev, self.jev_mode, self.vision, j[1]), jobs):
                    self.store.upsert(r.to_dict())
                    with self._lock:
                        self.state["done"] += 1
                        key = r.status if r.category == "BL_COMPARISON" else r.category
                        self.state["counts"][key] = self.state["counts"].get(key, 0) + 1
        except WorkspaceError as e:  # a problem with what the user supplied: say it plainly
            with self._lock:
                self.state["error"] = str(e)
        except Exception as e:  # e.g. a missing library: say so, do not die silently
            with self._lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self.state.update(running=False, phase="error" if self.state["error"] else "done",
                                  seconds=round(time.time() - self.state["started_at"], 1))
