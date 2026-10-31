"""Waking the private model (Muse Glimmer on Modal, server/private/).

Its container sleeps when idle, so Private starts with a wake-up: state here,
words and timing in pip_voice.json `private`, the task in server/api.py.
"""

import asyncio
import json
import re
import statistics
import time
from pathlib import Path

from server.tone import section

#: Cold starts the ring remembers.
RECENT_COLDS = 7


def weights_gb(sizes: dict, files: list) -> int:
    """The model on the GPU in GB, from its files' real sizes in bytes."""
    return round(sum(sizes[f] for f in files) / 1e9)


def line_at(elapsed: float, gb: int | None) -> str:
    """The latest line whose time has come; one that needs the size waits for it."""
    out = ""
    for row in section("private")["lines"]:
        needs_size = re.search(r"{\w+}", row["text"])
        if row["at"] <= elapsed and not (needs_size and gb is None):
            out = row["text"].format(gb=gb) if needs_size else row["text"]
    return out


async def wake_up(post, timeout_s: float) -> bool:
    """One request and one retry, inside the whole timeout."""
    try:
        async with asyncio.timeout(timeout_s):
            for _ in range(2):
                try:
                    await post()
                    return True
                except Exception as e:
                    print(f"private wake: {type(e).__name__}: {e}", flush=True)
    except TimeoutError:
        print(f"private wake: no answer in {timeout_s}s", flush=True)
    return False


class PrivateWake:
    """idle -> warming -> ready | failed. `gen` retires a wake-up left running."""

    def __init__(self, clock=time.monotonic, path: Path | None = None):
        self.clock = clock
        self.path = path  # cold starts survive a restart: they set the wait's ring
        self.colds: list = json.loads(path.read_text()) if path and path.exists() else []
        self.state = "idle"
        self.gen = 0
        self.started = 0.0
        self.elapsed = 0.0
        self.ready_at = 0.0
        self.gb: int | None = None

    def begin(self) -> int:
        self.state, self.started, self.elapsed = "warming", self.clock(), 0.0
        self.gen += 1
        return self.gen

    def finish(self, ok: bool) -> None:
        self.state, self.elapsed = ("ready" if ok else "failed"), self.clock() - self.started
        self.ready_at = self.clock()
        if ok and self.elapsed >= section("private")["cold_min_s"]:  # a warm answer says nothing
            self.colds = (self.colds + [round(self.elapsed, 1)])[-RECENT_COLDS:]
            if self.path:
                self.path.write_text(json.dumps(self.colds))

    def reset(self) -> None:
        self.state = "idle"
        self.gen += 1

    def touch(self) -> None:
        """The model just answered: its idle clock restarts."""
        self.ready_at = self.clock()

    def fresh(self) -> bool:
        """Ready and not gone back to sleep since."""
        return self.state == "ready" and self.clock() - self.ready_at < section("private")["ready_ttl_s"]

    def expected_s(self) -> float:
        """Recent cold starts' median: the wait's ring fills against it."""
        return statistics.median(self.colds) if self.colds else section("private")["expected_s"]

    def view(self) -> dict | None:
        """Snapshot `private`: the app draws this as is."""
        if self.state == "idle":
            return None
        cfg = section("private")
        elapsed = self.clock() - self.started if self.state == "warming" else self.elapsed
        line = {"failed": "", "ready": cfg["awake_line"]}.get(self.state) if self.state != "warming" \
            else line_at(elapsed, self.gb)
        return {"state": self.state, "elapsed_s": int(elapsed), "expected_s": self.expected_s(),
                "title": cfg["failed_line"] if self.state == "failed" else cfg["title"], "line": line,
                "info": cfg["info"].format(s=int(elapsed))}
