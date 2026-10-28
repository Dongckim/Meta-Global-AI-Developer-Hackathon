"""Headless app double: subscribes to snapshots, renders the bench as
text, sends text + card answers. Two backends share one render/assert core:

- TestBackend: in-process TestClient + store.subscribe (no TCP).
- LiveBackend: httpx against a running server (SSE + POST).

"Server state = screen" is checked by parsing the render back and
comparing with the record.
"""

import json
import queue
import threading


class Backend:
    def say(self, text, session="fake"):
        raise NotImplementedError

    def answer(self, card_id, ok, session="fake"):
        raise NotImplementedError

    def wait_version(self, after, timeout=10):
        """Snapshot with version > after. Raises TimeoutError."""
        raise NotImplementedError

    def record(self):
        raise NotImplementedError


class TestBackend(Backend):
    def __init__(self, client, store):
        self.c = client
        self.store = store
        self.q: queue.Queue = queue.Queue()
        store.subscribe(self.q.put_nowait)
        self.last = store.snapshot()

    def say(self, text, session="fake"):
        return self.c.post("/orchestrate", json={"text": text, "session": session}).json()

    def answer(self, card_id, ok, session="fake"):
        return self.c.post("/confirm", json={"card_id": card_id, "ok": ok,
                                             "session": session}).json()

    def wait_version(self, after, timeout=10):
        import queue as _q
        end = __import__("time").perf_counter() + timeout
        while True:
            left = end - __import__("time").perf_counter()
            if left <= 0:
                raise TimeoutError(f"no snapshot past v{after}")
            try:
                msg = self.q.get(timeout=left)
            except _q.Empty:
                raise TimeoutError(f"no snapshot past v{after}")
            if msg.get("kind") == "snapshot" and msg.get("version", -1) > after:
                self.last = msg
                return msg

    def record(self):
        return self.store.snapshot()


class LiveBackend(Backend):
    def __init__(self, base="http://localhost:8000"):
        import httpx
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=30.0)
        self.q: queue.Queue = queue.Queue()
        self.last: dict = {}
        t = threading.Thread(target=self._listen, daemon=True)
        t.start()

    def _listen(self):
        import httpx
        try:
            with self.c.stream("GET", "/events") as r:
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        try:
                            self.q.put_nowait(json.loads(line[5:]))
                        except ValueError:
                            pass
        except httpx.HTTPError:
            pass

    def say(self, text, session="fake"):
        return self.c.post("/orchestrate", json={"text": text, "session": session}).json()

    def answer(self, card_id, ok, session="fake"):
        return self.c.post("/confirm", json={"card_id": card_id, "ok": ok,
                                             "session": session}).json()

    def wait_version(self, after, timeout=10):
        import queue as _q
        import time
        end = time.perf_counter() + timeout
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                raise TimeoutError(f"no snapshot past v{after}")
            try:
                msg = self.q.get(timeout=left)
            except _q.Empty:
                raise TimeoutError(f"no snapshot past v{after}")
            if msg.get("kind") == "snapshot" and msg.get("version", -1) > after:
                self.last = msg
                return msg

    def record(self):
        return self.c.get("/record").json()


def render(snap: dict) -> str:
    """One screenful of text: step line, marked wells, timers, cards, notes."""
    plan = snap.get("plan") or {}
    steps = plan.get("steps") or []
    idx = snap.get("step_index", 0)
    name = (steps[idx].get("text") or "")[:28] if idx < len(steps) else "-"
    done = set(snap.get("done") or [])
    excl = set((snap.get("excluded") or {}))
    redo = set((snap.get("redo") or {}))
    marks = []
    for well in sorted(done | excl | redo):
        m = "x" if well in excl else ("r" if well in redo else "*")
        marks.append(f"{well}{m}")
    timers = ",".join(f"{t.get('label', '?')}" for t in snap.get("timers") or []) or "-"
    cards = ";".join(c.get("question", "?") for c in (snap.get("cards") or {}).values()) or "-"
    notes = len(snap.get("notes") or [])
    screen = "home" if not snap.get("run") else "bench"
    head = (f"v{snap.get('version')} {screen} step {idx + 1}/{len(steps)} {name} | "
            f"wells: {','.join(marks) or '-'} | timers: {timers} | "
            f"cards: {cards} | notes: {notes}")
    rows = []
    conds = plan.get("conditions") or {}
    for key in sorted(conds):
        ws = (conds[key] or {}).get("wells") or []
        cells = []
        for w in ws:
            cells.append(f"{w}{'*' if w in done else ('x' if w in excl else ('r' if w in redo else ''))}")
        rows.append(f"{key}: {' '.join(cells)}")
    return head + "\n" + "\n".join(rows)


def check_screen_matches_record(text: str, record: dict) -> list[str]:
    """Parse the render back; every mismatch is one error string."""
    errs = []
    head = text.split("\n")[0]
    want_done = set(record.get("done") or [])
    cells = head.split("wells:")[1].split("|")[0].split(",") if "wells:" in head else []
    got_done = {w.strip()[:-1] for w in cells if w.strip().endswith("*")}
    if got_done != (want_done - set((record.get("excluded") or {}))):
        errs.append(f"done mismatch screen={sorted(got_done)}")
    want_step = record.get("step_index", 0) + 1
    if f"step {want_step}/" not in head:
        errs.append(f"step mismatch in {head[:40]!r}")
    want_cards = len(record.get("cards") or {})
    got_cards = 0 if "cards: -" in head else head.split("cards:")[1].split("|")[0].count(";") + 1
    if got_cards != want_cards:
        errs.append(f"cards {got_cards} != {want_cards}")
    want_notes = len(record.get("notes") or [])
    if f"notes: {want_notes}" not in head:
        errs.append("notes count mismatch")
    want_screen = "home" if not record.get("run") else "bench"
    if f" {want_screen} step " not in head:
        errs.append(f"screen mismatch in {head[:40]!r}")
    return errs
