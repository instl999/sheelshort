"""Estimated completion times: for every GPU job when it is queued, and for every phase of a run.

Each finished job teaches the model how long its kind takes on these servers: the server's own execution time (from
the ComfyUI history, so the Mac's and the server's clocks never mix) plus the download. A render is measured per
second of video, everything else per job. The history lives in .cache/job_times.json and is shared by all projects.
`python -m sheelshort status <project>` shows what a running command is doing and when each part should finish.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TIMES = REPO / ".cache" / "job_times.json"
KEEP = 30
# seconds per unit before anything was measured (RTX 5090, MiniMax H3 app): a render per second of video
DEFAULTS = {"render": 30.0, "foley": 40.0, "upscale": 25.0, "clipqc": 15.0, "qc": 20.0, "cast": 40.0, "music": 30.0,
            "perform": 30.0, "cover": 60.0}


def kind(key: str) -> str:
    return key.split("/", 1)[0]


def exec_seconds(hist: dict) -> float | None:
    """The server's execution time for a finished prompt, from its own timestamps."""
    ts = {m[0]: m[1].get("timestamp") for m in hist.get("status", {}).get("messages", [])
          if isinstance(m, list) and len(m) == 2 and isinstance(m[1], dict)}
    a, b = ts.get("execution_start"), ts.get("execution_success")
    return (b - a) / 1000 if a and b and b >= a else None


def clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def span(s: float) -> str:
    s = max(0.0, s)
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


class Timing:
    def __init__(self, path: Path = TIMES):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.rates: dict[str, list[float]] = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            self.rates = {}

    def per_unit(self, k: str) -> float:
        r = self.rates.get(k)
        return statistics.median(r) if r else DEFAULTS.get(k, 60.0)

    def expected(self, key: str, units: float = 1.0) -> float:
        return self.per_unit(kind(key)) * max(units, 0.1)

    def record(self, key: str, seconds: float, units: float = 1.0) -> None:
        with self.lock:
            r = self.rates.setdefault(kind(key), [])
            r.append(round(seconds / max(units, 0.1), 2))
            del r[:-KEEP]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.rates))
            tmp.replace(self.path)


class Progress:
    """One phase of a run (renders, checks, upscales): logs `done/total` with the time the phase should end.

    The end is estimated from the work not finished yet, spread over the servers: the expected seconds of the jobs not
    started, divided by the number of servers, after the longest job still running."""

    def __init__(self, p, phase: str, items: list[tuple[str, float]]):
        self.p, self.phase = p, phase
        self.todo = {k: p.timing.expected(k, u) for k, u in items}
        self.total, self.done = len(items), 0
        self.lock = threading.Lock()
        if self.total:
            self.report(started=True)

    def finish(self, key: str) -> None:
        with self.lock:
            self.todo.pop(key, None)
            self.done += 1
            self.report()

    def eta(self) -> float:
        now = time.time()
        running, waiting = [], 0.0
        for k, exp in self.todo.items():
            job = self.p.state["jobs"].get(k, {})
            if job.get("status") == "queued" and job.get("eta"):
                running.append(job["eta"])
            else:
                waiting += exp
        servers = max(1, len(self.p.cfg["comfy"]["servers"]))
        return max([now] + running) + waiting / servers

    def report(self, started: bool = False) -> None:
        from .project import log
        end = self.eta()
        if self.done < self.total:
            log(f"{self.phase}: {self.done}/{self.total} done, all done ~{clock(end)} (in ~{span(end - time.time())})")
        else:
            log(f"{self.phase}: {self.total}/{self.total} done")
        self.p.progress(self.phase, {"done": self.done, "total": self.total, "eta": None if self.done >= self.total else end})
