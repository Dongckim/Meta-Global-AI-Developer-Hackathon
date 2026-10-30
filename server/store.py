"""Single source of truth for the bench record.

Owns the plan, wells, memos, and timers. Clients subscribe to snapshots
and render only; every mutation persists to server/data/record.json.
Apply rules mirror the app's old LabState behavior message-for-message.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from server.grammar import (
    build_evidence,
    call_wells,
    evidence_summary,
    find_durations,
    grounded,
    join_wells_ranges,
    parse_wellref,
    speak_text,
    wells_label,
    what_step_reply,
    whats_next_reply,
    where_am_i_reply,
)
from server.screens import READ_KINDS as SCREEN_READ_KINDS
from server.screens import FLOWS, PLANNED, SCREENS, canonical, card_answers, card_choices, item_off, ITEMS, parent_of, result_of, title_of, ui_of
from server.tone import run_mode, say, section
from server import library as lib
from server import protocol as proto
from server import labels
from server import runs as runlog
from server import look as lookmod
from server import search as searchmod
from server import handoff as hand

DATA_DIR = Path(__file__).resolve().parent / "data"
RECORD_PATH = DATA_DIR / "record.json"
SEED_PATH = Path(__file__).resolve().parent.parent / "samples" / "plan_demo.json"
DEFAULT_STEPS = ["Prep", "Dispense", "Incubate", "Measure", "Clean up"]
ALL_WELLS = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]


def fmt_minutes(m) -> str:
    f = float(m)
    return str(int(f)) if f.is_integer() else str(f)


def fmt_clock(s: float) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def is_well(w: str) -> bool:
    return isinstance(w, str) and parse_wellref(w.upper()) == [w.upper()]


def invalid_message(bad, session=None) -> str:
    bad = [str(b) for b in (bad or []) if b is not None] or ["that"]
    return say("invalid_well", session, target=", ".join(bad))


def trimmed(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _well_coords(w: str):
    u = w.upper()
    if len(u) >= 2 and u[0] in "ABCDEFGH":
        try:
            c = int(u[1:])
        except ValueError:
            return (99, 99)
        if 1 <= c <= 12:
            return ("ABCDEFGH".index(u[0]), c)
    return (99, 99)


def sort_wells(wells) -> list[str]:
    return sorted(wells, key=lambda w: (_well_coords(w), w))


def migrate_plan(plan: dict) -> dict:
    """v1 {groups, name-steps} -> v2 {conditions, kind/text steps}. Idempotent."""
    if "groups" not in plan:
        return plan
    conditions = {}
    for key, g in (plan.get("groups") or {}).items():
        conditions[key] = {"label": key, "treatments": [],
                           "replicates": (g or {}).get("n"),
                           "wells": list((g or {}).get("wells") or [])}
    steps = []
    for s in plan.get("steps") or []:
        s = s or {}
        name = s.get("name", "")
        kind = "incubate" if s.get("minutes") is not None or "min" in name.lower() else "pipette"
        steps.append({"kind": kind, "text": name, "minutes": s.get("minutes"),
                      "temp_c": None, "tip": s.get("tip"), "caution": None,
                      "pipette": None, "wells": list(s.get("wells") or [])})
    return {"conditions": conditions, "steps": steps,
            "backups": list(plan.get("backups") or [])}


def timer_noun(q: str) -> str:
    l = q.lower()
    if l == "timer":
        return "the timer"
    if "timer" in l:
        return q
    return f"a {q} timer"


def _one_line(text: str, limit: int = 80) -> str:
    """First line of a note, cut at a word within `limit` chars."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def timer_label(step: str, range_: str | None, minutes, on_done) -> str:
    s = f"{step} · {range_}" if range_ else step
    s += f" · {fmt_minutes(minutes)} min"
    if on_done:
        s += f" → {on_done}"
    return s


def step_abbrev(steps, idx: int, current_range: str | None) -> str:
    name = steps[idx]
    if current_range and name != current_range:
        nn = name.replace("–", "-")
        rr = current_range.replace("–", "-")
        if nn.endswith(rr):
            return name[: len(name) - len(rr)].strip(" \t\n\r–-·")
    return name


def timer_done_message(t: dict) -> str:
    m = fmt_minutes(t["minutes"])
    head = f"{t['step']}: {m} minutes done." if not t["wells"] else f"{t['step']} on {t['wells']}: {m} minutes done."
    if t.get("on_done"):
        return head + f" Next: {t['on_done']}."
    return head


def describe_well_row(d: dict) -> str:
    """One factual sentence from a get_well_data row (scope-safe)."""
    status = {"redo": "marked for redo"}.get(d["status"], d["status"])
    s = f"{d['well']} is {status}"
    extra = d["reason"] or d["redo_note"]
    if extra:
        s += f" ({extra})"
    if d["memos"]:
        s += ". Memos: " + "; ".join(d["memos"])
    return s + "."


def is_over(t: dict, now: float) -> bool:
    return now - t["started_at"] >= t["minutes"] * 60


def resolve_timers(timers, query) -> list[int]:
    """Exact match first, then whole-well match, then substring. Blank hits a lone timer."""
    q = (query or "").strip()
    if not q:
        return [0] if len(timers) == 1 else []
    labels = [t["label"] for t in timers]
    exact = [i for i, l in enumerate(labels) if l.lower() == q.lower()]
    if exact:
        return exact
    import re

    if is_well(q):
        rx = re.compile(rf"\b{re.escape(q)}\b", re.IGNORECASE)
        return [i for i, l in enumerate(labels) if rx.search(l)]
    return [i for i, l in enumerate(labels) if q.lower() in l.lower()]


class RecordStore:
    def __init__(self, path: Path = RECORD_PATH):
        self.path = Path(path)
        self.root = self.path.parent
        self._listeners: list = []
        self._lock = threading.Lock()
        #: App-side lists on screen now (import_file's files, import_photo's pages).
        self.screen_items: dict[str, list[dict]] = {}
        #: Starts a run's hand off loop (api sets it; None in plain store tests).
        self.spawn_handoff = None
        self._load_or_seed()

    # --- persistence / broadcast ---

    def _load_or_seed(self):
        if self.path.exists():
            self.r = json.loads(self.path.read_text())
            self.r.pop("plans", None)  # legacy: plans folded into the card queue
            for m in self.r.get("notes", []):
                m.setdefault("raw", m.pop("transcript", None) or m.get("text", ""))
            self.r["plan"] = migrate_plan(self.r.get("plan") or {})
            if "run" not in self.r:
                if (self.r["plan"] or {}).get("steps"):
                    self.r["run"] = {"run_id": "legacy", "protocol_id": None,
                                     "protocol_name": None,
                                     "started_at": self.path.stat().st_mtime}
                else:
                    self.r["run"] = None
            if "epoch" not in self.r:
                self.r["epoch"] = uuid.uuid4().hex[:16]
                self._save()
            if "view" not in self.r:
                self.r["view"] = self._default_view(bool(self.r.get("run")))
                self._save()
            if any(isinstance(m, str) for m in self.r.get("done") or []):
                # API 4 -> 5: bare wells become step-1 marks (step unknown).
                stamps = self.r.get("stamps") or {}
                self.r["done"] = [
                    {**m, "source": (stamps.get(m["well"]) or {}).get("source", ""),
                     "ts": (stamps.get(m["well"]) or {}).get("ts", 0)}
                    for m in runlog.done_marks(self.r)]
                self._save()
            return
        self.r = self._fresh_record({"conditions": {}, "steps": [], "backups": []})
        self.r["epoch"] = uuid.uuid4().hex[:16]
        self._save()

    @staticmethod
    def _default_view(has_run: bool) -> dict:
        return {"current": {"screen": "main" if has_run else "home", "target": ""},
                "stack": [], "tab": "",
                "sheet": {"name": "", "target": ""}}

    @staticmethod
    def _fresh_record(plan: dict, current: int = 1, run: dict | None = None) -> dict:
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        return {
            "version": 0,
            "view": RecordStore._default_view(bool(run)),
            "run": run,
            "plan": plan,
            "step_index": max(min(current - 1, len(steps) - 1), 0) if steps else 0,
            "done": [],
            "excluded": {},
            "redo": {},
            "notes": [],
            "tips": [],
            "timers": [],
            "timer_log": [],
            "stamps": {},
            "recent": [],
            "cards": {},
        }

    def active_run_doc(self) -> dict | None:
        """Active run as a run doc (for selectors), else None."""
        run = self.r.get("run")
        if not run:
            return None
        return {"run_id": run.get("run_id"), "protocol_id": run.get("protocol_id"),
                "protocol_name": run.get("protocol_name"),
                "started_at": run.get("started_at"), "status": "active",
                "summary": runlog.summarize_record(self.r), "record": self.r}

    def all_run_docs(self) -> list[dict]:
        docs = runlog.list_all(root=self.root)
        active = self.active_run_doc()
        if active:
            docs = [active] + [d for d in docs if d.get("run_id") != active["run_id"]]
        return docs

    def _here_run(self) -> str:
        """The run whose record is on screen ("" elsewhere): "here" on a run record."""
        return (self._viewing_run() or {}).get("run_id") or ""

    def _viewing_run(self) -> dict | None:
        """view-target run doc, or None for the active scope."""
        v = (self.r.get("view") or {}).get("current") or {}
        if v.get("screen") != "run_view" or not v.get("target"):
            return None
        return runlog.resolve(v["target"], self.all_run_docs())

    def _push_view(self, screen: str, target: str = "") -> None:
        """Server-owned navigation: push current, show the new screen."""
        v = self.r["view"]
        new = {"screen": screen, "target": target or ""}
        if v["current"] == new:
            v["sheet"] = {"name": "", "target": ""}
            return
        if new in v["stack"]:  # a screen you came from: return to it, no loop
            v["stack"] = v["stack"][:v["stack"].index(new)]
        else:
            v["stack"].append(dict(v["current"]))
        v["current"] = new
        v["sheet"] = {"name": "", "target": ""}

    def _flow_gate(self, screen: str, session: dict) -> str:
        """A flow has one way in (registry `flows`): a step opens only through
        the step before it, on the way here in this pass through the flow (the
        view and its back stack); else the earliest step still needed opens.
        Each new import starts at Mode. Voice, taps and the model all come
        through here."""
        v = self.r["view"]
        path = {v["current"]["screen"], *[x.get("screen") for x in v.get("stack") or []]}
        for f in FLOWS:
            steps = f["steps"]
            k = next((i for i, st in enumerate(steps) if screen in st), None)
            if not k:
                continue
            need = next((i for i in range(k) if not path & set(steps[i])), None)
            if need is not None:
                return steps[need][0]
        return screen

    def _navigate_locked(self, res: dict, session: dict, screen: str, target) -> None:
        """Navigate with target validation; the app renders view."""
        screen = self._flow_gate(screen, session)
        t = target.strip() if isinstance(target, str) else ""
        if screen == "run_view":
            # no run named: the one on screen ("here"), else the latest
            self._open_run(res, session, t or self._here_run() or "latest")
            return
        if screen in self.HANDOFF_SCREENS:
            self._open_handoff(res, session, t.split(":", 1)[0], screen)
            return
        if screen == "well_card" and is_well(t):
            self._open_well(res, t)  # the same well card open{target} shows
            return
        if screen == "protocol_review" and not self.r.get("draft"):
            res["messages"].append("No draft open — import a protocol from Home.")
            return
        if screen == "protocol_detail":
            doc = lib.get(t, self.root) if t else None
            if doc is None and t:
                doc = lib.find_by_name(t, self.root)
            if doc is None:
                res["messages"].append(
                    "I couldn't open that protocol: which one?" if not t
                    else f"I couldn't find the {t} protocol.")
                return
            if off := self._protocol_off(doc, session):
                res["messages"].append(off)
                return
            self._push_view(screen, doc["id"])
            res["messages"].append(self.screen_line(screen, doc["id"], name=doc.get("name") or "the protocol"))
            return
        if screen == "protocol_source":
            if not t:
                # "this protocol" = the one being viewed, else the active run's
                v = (self.r.get("view") or {}).get("current") or {}
                pid = v.get("target") if v.get("screen") == "protocol_detail" else None
                t = pid or (self.r.get("run") or {}).get("protocol_id") or ""
            base = t.split("#", 1)[0]
            pid = (base[len("protocol:"):] if base.startswith("protocol:") else base).strip()
            if not pid or lib.get(pid, self.root) is None:
                res["messages"].append("I couldn't open that original: which protocol?")
                return
            self._push_view(screen, t)
            res["messages"].append(result_of(screen))
            return
        if SCREENS.get(screen, {}).get("needs_target") and not t:
            res["messages"].append(f"I couldn't open the {title_of(screen).lower()}: which one?")
            return
        if ui_of(screen):
            # a sheet (importer), not a view: the app opens it from ui
            res["ui"].append({"action": ui_of(screen)})
            res["messages"].append(result_of(screen))
            return
        self._push_view(screen)
        res["messages"].append(self.screen_line(screen))

    def _open_well(self, res: dict, well: str) -> None:
        self.r["view"]["sheet"] = {"name": "well_card", "target": well.upper()}
        res["ui"].append({"action": "flash", "wells": [well.upper()]})
        res["messages"].append(f"Opened {well.upper()}.")

    def _open_protocol(self, res: dict, session: dict, selector: str) -> None:
        doc, cands = labels.select_protocol(selector, lib.all_docs(self.root))
        if doc is None:
            res["messages"].append(
                labels.which_one([labels.protocol_label(c)["spoken"] for c in cands]) if cands
                else "I couldn't find that protocol.")
            return
        self._navigate_locked(res, session, "protocol_detail", doc["id"])

    # --- what each screen says on entry (registry `prompt` -> pip_voice.json `prompts`) ---

    def view_slots(self, screen: str, target: str = "") -> dict:
        """The data a screen's prompt and templated buttons ("Say Step {step}")
        are filled from. Only slots with a value are returned."""
        def n_of(k, word):
            return f"{k} {word}{'' if k == 1 else 's'}"
        out: dict = {}
        run = self.r.get("run")
        steps = (self.r.get("plan") or {}).get("steps") or []
        if screen in ("home", "protocol_detail"):
            # buttons show by state: Continue while a run goes (on a protocol's
            # page, a run of that protocol), Review while a draft waits
            mine = run and (screen == "home" or run.get("protocol_id") == target)
            out = {"running": "yes" if mine else "", "draft": "yes" if self.r.get("draft") else ""}
            if screen == "home" and run and steps:
                out.update({"run": self.run_label(self.active_run_doc())["display"], "step": self.current_step()})
        elif screen == "main" and run and steps:
            i = min(self.r.get("step_index", 0), len(steps) - 1)
            out = {"step": i + 1, "of": len(steps), "title": labels.step_short_title(steps[i]),
                   "last": "yes" if i >= len(steps) - 1 else "no"}  # the last step's button is Finish
        elif screen == "run_view" and (doc := self._viewing_run()):
            lab = self.run_label(doc)
            out = {"name": lab["display"], "state": lab["state"],
                   "status": doc.get("status") if doc.get("status") in ("active", "paused") else "finished"}
            if out["status"] == "finished":  # its robot code passed: Share instead of a greyed Start run
                out["code"] = "yes" if self.handoff_code(doc) else "no"
        elif screen == "import_file" and "import_file" in self.screen_items:
            # only once the app said what's there: never "no files" before the list arrives
            k = len(self.screen_items["import_file"])
            out = {"files": n_of(k, "file") if k else "", "app": section("persona").get("app_name") or ""}
        elif screen == "import_photo" and (k := len(self.screen_items.get("import_photo") or [])):
            out = {"pages": n_of(k, "page")}
        elif screen == "web_source":
            sid, _, n = (target or "").partition(":")
            doc = next((d for d in self.searches() if d["id"] == sid), None)
            k = int(n) if n.isdigit() else 1
            src = ((doc or {}).get("sources") or [None] * k)[k - 1] if doc and 0 < k <= len(doc.get("sources") or []) else None
            if src:
                out = {"title": src.get("title") or "", "domain": src.get("domain") or "", "url": src.get("url") or ""}
                if k == 1 and len(doc["sources"]) > 1:
                    out["second"] = "2"
        elif screen == "web_searches" and (k := len(self.answered_searches())):
            out = {"searches": n_of(k, "web search")}
        elif screen == "web_answer" and (doc := next((d for d in self.searches() if d["id"] == target), None)):
            lab = labels.search_label(doc)
            st = doc.get("status")
            out = {"when": lab["day"], "time": lab["time"], "query": doc.get("query") or "",
                   "status": "ok" if st == "ok" else "pending" if st in ("pending", "looking", "reading") else "failed",
                   "sources": len(doc.get("sources") or []), "kind": doc.get("kind") or "web"}
            if st == "looking":  # a photo question waits for its photo: Take it
                out["looking"] = say("look_show", None)
                out["camera"] = "on"
            if doc.get("kind") == "photo" and st in ("reading", "ok"):
                out["retake"] = "on"  # a photo is in: Retake, while it's read and after
            if st == "ok" and doc.get("basis") != "photo":
                out["source"] = "web"  # a searched answer: Open source; a photo-only one has none
            if st == "ok" and doc.get("basis") == "photo":
                out["answer_photo"] = (doc.get("answer_short") or "").rstrip(" .")  # read, no search
            elif st == "ok":
                # answer, then where from, then when; cited and pages-searched never sound alike
                srcs = doc.get("sources") or []
                out["answer" if doc.get("basis", "cited") == "cited" else "answer_pages"] = \
                    (doc.get("answer_short") or "").rstrip(" .")
                out["domain"] = (srcs[0].get("domain") or "").removeprefix("www.") if srcs else ""
                out["searched"] = lab["searched"]
            if out["sources"] > 1:
                out["second"] = "2"
        elif screen in self.HANDOFF_SCREENS:
            out = self._handoff_slots(screen, target)
        elif screen in self.DRAFT_SCREENS and (draft := self.r.get("draft")):
            d = proto.draft_rows(draft)
            rows = d["rows"]
            flagged = next((r for r in rows if r["flagged"]), None)
            origin = (draft.get("meta") or {}).get("origin") or "pdf"
            out = {"title": proto.resolve_short_title(draft), "conditions": n_of(d["conditions"], "condition"),
                   "wells": n_of(d["wells"], "well"), "steps": n_of(d["steps"], "step"),
                   "pipetting": n_of(len(rows), "pipetting step"), "of": len(rows),
                   "doc": {"pdf": "PDF", "photo": "photo"}.get(origin, "text")}
            row = rows[int(target) - 1] if str(target).isdigit() and 0 < int(target) <= len(rows) else None
            if row:
                out.update({"step": row["n"], "reason": row["reason"], "page": row["page"]})
            elif flagged:
                out["step"] = flagged["n"]
        return {k: v for k, v in out.items() if v not in (None, "")}

    def screen_line(self, screen: str, target: str = "", **fallback) -> str:
        """The screen's prompt, its first template with no empty slot; else
        the registry result line."""
        from collections import defaultdict
        key = SCREENS.get(screen, {}).get("prompt")
        slots = self.view_slots(screen, target)
        for tpl in section("prompts").get(key) or [] if key else []:
            need = re.findall(r"{(\w+)}", tpl)
            if all(k in slots for k in need):
                return tpl.format_map(defaultdict(str, slots))
        return result_of(screen, **fallback)

    # --- labels (server/labels.py is the one place) ---

    def run_short(self, doc: dict) -> str:
        return labels.protocol_short(lib.get(doc.get("protocol_id") or "", self.root),
                                     doc.get("protocol_name") or "")

    def run_label(self, doc: dict, docs: list[dict] | None = None) -> dict:
        docs = self.all_run_docs() if docs is None else docs
        return labels.run_label(doc, docs, self.run_short, (self.r.get("run") or {}).get("run_id"))

    def run_summaries(self, docs: list[dict]) -> list[dict]:
        everything = self.all_run_docs()
        out = []
        for d in docs:
            lab = self.run_label(d, everything)
            out.append({**runlog.summarize(d), "label": lab["display"], "spoken": lab["spoken"],
                        "has_code": self.handoff_code(d) is not None,
                        "number": lab["number"], "day": lab["day"], "step": lab["step"],
                        "of": lab["of"], "state": lab["state"]})
        return out

    def protocol_summaries(self) -> list[dict]:
        """Library list with each protocol's run counts, most recently used
        first (its newest run's start, else its import). `status` and
        `enabled` are the row's state: the app draws only from them."""
        runs = self.all_run_docs()
        out = []
        for p in lib.list_all(self.root):
            mine = [d for d in runs if d.get("protocol_id") == p["id"]]
            status = self.protocol_status(p["id"], runs)
            out.append({**p, "runs": len(mine), "status": status,
                        "enabled": not item_off("protocol", {"status": status}),
                        "in_progress": sum(d.get("status") == "active" for d in mine),
                        "last_used": max([d.get("started_at") or 0 for d in mine] + [p.get("imported_at") or 0])})
        return sorted(out, key=lambda p: p["last_used"], reverse=True)

    def protocol_by_title(self, said: str) -> str:
        """A protocol's title said as the whole turn ("Drug A Dosing", "open
        drug a dosing"): its id, when exactly one protocol has that title
        (case, spaces and punctuation ignored); else ""."""
        want = lib.title_key(re.sub(r"^\s*(?:open|show)\s+(?:the\s+)?", "", said or "", flags=re.I))
        hits = [d["id"] for d in lib.all_docs(self.root) if want and want in {
            lib.title_key(d.get("short_title")), lib.title_key(d.get("name")), lib.title_key(lib.page_title(d))}]
        return hits[0] if len(hits) == 1 else ""

    def protocol_status(self, pid: str, runs: list[dict] | None = None) -> str:
        """active / paused while a run of it goes or waits; done when its
        newest run finished; new before any run."""
        mine = [d for d in (self.all_run_docs() if runs is None else runs) if d.get("protocol_id") == pid]
        for st in ("active", "paused"):
            if any(d.get("status") == st for d in mine):
                return st
        return "done" if mine else "new"

    def _protocol_off(self, doc: dict, session: dict | None) -> str:
        """The line for a protocol whose row is off (registry `items`), else ""."""
        if not item_off("protocol", {"status": self.protocol_status(doc["id"])}):
            return ""
        return say(ITEMS["protocol"]["line"], session, name=labels.protocol_short(doc))

    def context_protocol(self) -> str | None:
        """The protocol "run 2" means: the one on screen, else the active run's."""
        v = (self.r.get("view") or {}).get("current") or {}
        if v.get("screen") == "protocol_detail" and v.get("target"):
            return v["target"]
        doc = self._viewing_run()
        return (doc or self.r.get("run") or {}).get("protocol_id")

    def select_run(self, phrase: str) -> tuple[dict | None, list[dict]]:
        return labels.select_run(phrase, self.all_run_docs(), self.run_short,
                                 (self.r.get("run") or {}).get("run_id"),
                                 context=self.context_protocol())

    def _open_run(self, res: dict, session: dict, selector: str) -> None:
        docs = self.all_run_docs()
        doc, cands = self.select_run(selector.strip() or "latest")
        if doc is None:
            res["messages"].append(
                "No runs yet — start one from Home." if not docs
                else labels.which_one([self.run_label(c, docs)["spoken"] for c in cands]) if cands
                else "I couldn't find that run.")
            return
        print(f"open run: {self.run_label(doc, docs)['display']}", flush=True)
        # Every run opens its record; the active one's Continue goes to the bench.
        self._push_view("run_view", doc["run_id"])
        res["messages"].append(self.screen_line("run_view", doc["run_id"], name=self.run_label(doc, docs)["spoken"]))

    def _open_source(self, res: dict, session: dict, t: str) -> None:
        """'The original' for the current (or named) step of the viewing run."""
        doc = self._viewing_run()
        if doc is not None:
            rec, pid = doc.get("record") or {}, doc.get("protocol_id")
        else:
            rec = self.r
            pid = (self.r.get("run") or {}).get("protocol_id")
        if not pid or lib.get(pid, self.root) is None:
            if doc is not None:
                msg = "That run has no saved protocol."
            elif (self.r.get("run") or {}).get("run_id"):
                msg = "This run has no saved protocol."
            else:
                msg = "No active run — open a protocol from Home to start."
            res["messages"].append(msg)
            return
        steps = (rec.get("plan") or {}).get("steps", [])
        m = re.search(r"step\s*(\d+)", (t or "").lower())
        n = int(m.group(1)) if m else (rec.get("step_index", 0) + 1)
        if not steps or not 1 <= n <= len(steps):
            res["messages"].append("I couldn't find that step in the original.")
            return
        self._navigate_locked(res, session, "protocol_source", f"protocol:{pid}#step:{n}")

    def start_demo_run(self) -> dict:
        """Fresh-store helper (tests, evals, dev): run the Demo protocol."""
        with self._lock:
            lib.ensure_demo_seed(self.root)
            demo = lib.find_by_name("Demo protocol", self.root)
            if self.r.get("run"):
                self._finish_locked("finished")
            return self._begin_locked(demo["plan"], demo["id"], demo["name"])

    def _begin_locked(self, plan: dict, protocol_id: str | None,
                      protocol_name: str | None) -> dict:
        draft = self.r.get("draft")
        run = {"run_id": runlog.new_run_id(), "protocol_id": protocol_id,
               "protocol_name": protocol_name, "started_at": time.time(), "mode": run_mode()}
        prev_version = self.r.get("version", 0)
        prev_epoch = self.r.get("epoch") or uuid.uuid4().hex[:16]
        self.r = self._fresh_record(plan, 1, run)
        self.r["version"] = prev_version  # _commit() bumps; version never regresses
        self.r["epoch"] = prev_epoch  # same record lineage, not a reset
        if draft is not None:
            self.r["draft"] = draft
        self._save()
        self._commit()
        return run

    def _finish_locked(self, status: str) -> dict | None:
        """Archive the active run (draft excluded); the record goes empty."""
        run = self.r.get("run")
        if not run:
            return None
        if status == "finished":
            self._log_timers(self.r["timers"], time.time())
            self.r["timers"] = []
        doc = {"run_id": run.get("run_id"), "protocol_id": run.get("protocol_id"),
               "protocol_name": run.get("protocol_name"), "protocol_version": run.get("protocol_version") or 1,
               "started_at": run.get("started_at"), "ended_at": time.time(),
               "status": status, "summary": runlog.summarize_record(self.r),
               "record": {k: v for k, v in self.r.items() if k != "draft"}}
        runlog.write_run(doc, self.root)
        draft = self.r.get("draft")
        prev_version = self.r.get("version", 0)
        prev_epoch = self.r.get("epoch") or uuid.uuid4().hex[:16]
        self.r = self._fresh_record({"conditions": {}, "steps": [], "backups": []})
        self.r["version"] = prev_version  # _commit() bumps; version never regresses
        self.r["epoch"] = prev_epoch  # same record lineage, not a reset
        if draft is not None:
            self.r["draft"] = draft
        self._save()
        self._commit()
        return doc

    def _save_protocol(self, draft: dict, plan: dict) -> dict:
        """Reviewed again: that protocol is fixed. Same title (④): its next
        version, or a separate "{title} (2)". Else a new protocol."""
        meta = draft.get("meta") or {}
        name, short = proto.resolve_name(draft), proto.resolve_short_title(draft)
        same, how = meta.get("same_title") or "", meta.get("save_as")
        if how == "separate" and (doc := lib.get(same, self.root)):
            k = 2
            while lib.find_by_title(f"{lib.page_title(doc)} ({k})", self.root):
                k += 1
            name, short = f"{lib.page_title(doc)} ({k})", f"{labels.protocol_short(doc)} ({k})"
        keep = {k: v for k, v in meta.items() if k not in ("protocol_id", "same_title", "save_as", "review_at")}
        return lib.save(plan, name, meta.get("source") or "", self.root,
                        short_title=short, origin=meta.get("origin") or "pdf",
                        source_text=(meta.get("evidence") or {}).get("text") or "",
                        original_id=meta.get("original_id") or "", source=meta.get("content_hash") or "",
                        draft={**draft, "meta": keep,
                               "plan": {**draft["plan"], "title": name} if how == "separate" else draft["plan"]},
                        edit=meta.get("protocol_id") or "",
                        new_version_of=same if how == "version" else "", separate=how == "separate")

    def _ask_save_as(self, draft: dict, then: str, session: dict | None) -> dict | None:
        """④ a revised original with a saved protocol's title: version or separate?"""
        meta = draft.get("meta") or {}
        doc = lib.get(meta.get("same_title") or "", self.root)
        if doc is None or meta.get("save_as") or meta.get("protocol_id"):
            return None
        res = {"cards": []}
        name, version = labels.protocol_short(doc), (doc.get("version") or 1) + 1
        return self._card(res, "save_as", say("save_as", session, name=name, version=version), None,
                          {"op": "save_as", "protocol_id": doc["id"], "then": then},
                          {"name": name, "version": version})

    def save_draft_to_library(self):
        """Draft -> resolved plan -> library. (ok, doc|reason). Run untouched."""
        with self._lock:
            ok, info = self._save_draft_locked()
            self._commit()
            return ok, info

    def _save_draft_locked(self):
        draft = self.r.get("draft")
        if not draft:
            return False, "no draft open"
        proto.recompute_draft(draft)
        if draft["errors"]:
            return False, {"errors": draft["errors"]}
        if card := self._ask_save_as(draft, "save_draft", None):
            return False, {"card": card}
        plan = proto.build_start_plan(draft["plan"])
        doc = self._save_protocol(draft, plan)
        self.r.pop("draft", None)
        self._leave_draft(saved=doc["id"])
        return True, doc

    def _saved(self, out: tuple, session: dict, res: dict) -> None:
        ok, info = out
        if ok:
            res["messages"].append(say("protocol_saved", session, name=labels.protocol_short(info)))
        elif isinstance(info, dict) and info.get("card"):
            res["cards"].append(info["card"])
        else:
            res["messages"].append(self._fix_first(info))

    #: The draft review screens (Start ⑤b-⑤f); leaving the draft leaves them all.
    DRAFT_SCREENS = ("protocol_review", "draft_steps", "draft_step", "draft_page", "plan_check")

    def _leave_draft(self, saved: str = "") -> None:
        """The draft is done (saved, started or dropped): its screens and the
        import flow that led to it (Mode, the choice, the file list…) all
        close. A saved protocol is the one on screen (Home, picked); else
        the screen before the flow shows, with any card."""
        v = self.r["view"]
        done = set(self.DRAFT_SCREENS) | {sid for f in FLOWS for step in f["steps"] for sid in step}
        v["stack"] = [x for x in v.get("stack") or [] if x.get("screen") not in done | {"protocol_detail"}]
        if v["current"].get("screen") in done:
            v["current"] = v["stack"].pop() if v["stack"] else {"screen": "home", "target": ""}
        if saved:
            v["stack"].append(dict(v["current"]))  # Back returns to Home
            v["current"] = {"screen": "protocol_detail", "target": saved}
        v["sheet"] = {"name": "", "target": ""}

    def ask_known_import(self, protocol_id: str, session: dict) -> dict:
        """② the same original, already a protocol: it shows, and Milo asks
        to open it or review it again (no extraction either way)."""
        with self._lock:
            doc = lib.get(protocol_id, self.root) or {}
            self._leave_draft(saved=protocol_id)
            name = labels.protocol_short(doc)
            card = self._card({"cards": []}, "import_known", say("import_known", session, name=name), None,
                              {"op": "known_import", "protocol_id": protocol_id}, {"name": name})
            self._commit()
            return card

    def resume_review(self, session: dict) -> str:
        """③ the same original, its draft's review unfinished: the review
        picks up at the last step looked at (Draft ready if none yet)."""
        with self._lock:
            draft = self.r["draft"]
            n = (draft.get("meta") or {}).get("review_at")
            self._leave_draft()
            self._push_view("protocol_review")
            if n:
                self._push_view("draft_steps")
                self._push_view("draft_step", str(n))
            self._commit()
            return say("review_resumed", session, step=n) if n else self.screen_line("protocol_review")

    def _review_again(self, protocol_id: str, session: dict, res: dict) -> None:
        """② review again: the saved steps reopen as a draft of that protocol;
        saving it fixes the same protocol."""
        doc = lib.get(protocol_id, self.root)
        if doc is None:
            res["messages"].append("That protocol is gone.")
            return
        if not ((doc.get("plan") or {}).get("steps")):
            res["messages"].append(say("review_unavailable", session, name=labels.protocol_short(doc),
                                       reason="no steps saved"))
            return
        # its review copy, else the saved plan as a draft (quotes, pages kept; never extracted again)
        draft = json.loads(json.dumps(doc["draft"])) if doc.get("draft") else proto.draft_from_plan(
            doc["plan"], doc.get("source_filename") or "", lib.page_title(doc), doc.get("short_title") or "",
            doc.get("source_text") or "")
        draft["meta"] = {**(draft.get("meta") or {}), "protocol_id": doc["id"],
                         "content_hash": doc.get("source_hash") or "", "origin": doc.get("origin") or "pdf",
                         "original_id": doc.get("original_id") or ""}
        self.r["draft"] = draft
        self._leave_draft()
        self._push_view("protocol_review")
        self._push_view("draft_steps")
        res["messages"].append(self.screen_line("draft_steps"))

    def open_known_protocol(self, protocol_id: str, session: dict) -> str:
        """An import of an original already in the library: its protocol opens
        (the import flow closes), ready to start another run."""
        with self._lock:
            self._leave_draft(saved=protocol_id)
            self._commit()
            doc = lib.get(protocol_id, self.root) or {}
            return say("already_saved", session, name=labels.protocol_short(doc))

    def request_start_run(self, protocol_id: str, session: dict):
        """Start now, or card when an active run must finish first."""
        with self._lock:
            return self._request_start_locked(protocol_id, session)

    def _request_start_locked(self, protocol_id: str, session: dict, again: bool = False):
        """Every start goes through here; a finished protocol doesn't start
        (a draft of it just reviewed does: that is its review's Start)."""
        doc = lib.get(protocol_id, self.root)
        if doc is None:
            return False, {"reason": f"unknown protocol: {protocol_id}"}
        if not again and (off := self._protocol_off(doc, session)):
            return False, {"line": off}
        # Start never asks: a run already going is paused (kept as it is,
        # Continue picks it up), and the new run opens on its Step 1.
        paused = None
        if self.r.get("run"):
            cur = self.active_run_doc()
            paused = self.run_label(cur)["display"]
            self._finish_locked("paused")
        run = self._begin_locked(doc["plan"], doc["id"], doc.get("name"))
        run["protocol_version"] = doc.get("version") or 1
        return True, {"run": run, "protocol_id": doc["id"], "paused": paused,
                      "conditions": len((doc["plan"] or {}).get("conditions") or {}),
                      "steps": len((doc["plan"] or {}).get("steps") or [])}

    def request_end_run(self, session: dict):
        """End run always asks first. (ok, card|reason)."""
        with self._lock:
            if not self.r.get("run"):
                return False, {"reason": "no active run"}
            res = {"messages": [], "cards": [], "ui": [],
                   "info": {"dropped": 0, "drop_reasons": []}}
            card = self._card(res, "end_run", say("end_run", session), None,
                              {"op": "end_run",
                               "run_id": (self.r.get("run") or {}).get("run_id")})
            self._commit()
            return False, {"card": card}

    def save_draft(self, draft: dict) -> None:
        with self._lock:
            self.r["draft"] = draft
            if self.r["view"]["current"]["screen"] != "protocol_review":
                self._push_view("protocol_review")
            self._commit()

    def clear_draft(self) -> None:
        with self._lock:
            self.r.pop("draft", None)
            self._leave_draft()
            self._commit()

    def _pop_view(self) -> None:
        """Leave the current screen without navigating (draft closed)."""
        v = self.r["view"]
        if v.get("stack"):
            v["current"] = v["stack"].pop()
        v["sheet"] = {"name": "", "target": ""}

    def start_run(self, session: dict):
        """Draft -> library, then start (cards when a run is active)."""
        with self._lock:
            ok, info = self._start_draft_locked(session)
            if not ok and "errors" in info:
                self._commit()
            return ok, info

    def _start_draft_locked(self, session: dict):
        draft = self.r.get("draft")
        if not draft:
            return False, {"reason": "no draft open"}
        proto.recompute_draft(draft)
        if draft["errors"]:
            return False, {"errors": draft["errors"]}
        if card := self._ask_save_as(draft, "start_run", session):
            return False, {"card": card}
        plan = proto.build_start_plan(draft["plan"])
        doc = self._save_protocol(draft, plan)
        review_n = len(draft["review"])
        self.r.pop("draft", None)
        self._leave_draft(saved=doc["id"])
        ok, info = self._request_start_locked(doc["id"], session, again=True)
        if ok:
            info["review"] = review_n
            info["protocol_name"] = doc.get("name")
        else:
            info["protocol_id"] = doc["id"]
            info["protocol_name"] = doc.get("name")
        return ok, info

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.r, ensure_ascii=False))
        tmp.replace(self.path)

    def subscribe(self, cb):
        self._listeners.append(cb)

    def _broadcast(self, msg: dict):
        for cb in list(self._listeners):
            try:
                cb(msg)
            except Exception:
                pass

    def _commit(self):
        self.r["version"] += 1
        self._save()
        self._broadcast({"kind": "snapshot", **self.snapshot()})

    def snapshot(self) -> dict:
        snap = json.loads(json.dumps(self.r))
        for st in (snap.get("plan") or {}).get("steps") or []:
            st["short_title"] = labels.step_short_title(st)  # one rule, old plans included
        conds = (snap.get("plan") or {}).get("conditions") or {}
        for key, abbr in labels.condition_abbrs(conds).items():
            conds[key]["abbr"] = abbr  # plate letters, same rule for old plans
        v = snap.get("view") or {}
        cur = v.get("current") or {}
        if cur.get("screen"):  # the screen's line and the slots its buttons fill from
            v["prompt"] = self.screen_line(cur["screen"], cur.get("target") or "")
            v["prompt_speak"] = speak_text(v["prompt"])  # what TTS says (symbols as words)
            v["slots"] = {k: str(x) for k, x in self.view_slots(cur["screen"], cur.get("target") or "").items()}
        if snap.get("draft"):
            snap["draft"]["summary"] = proto.draft_rows(snap["draft"])
            dconds = (snap["draft"].get("plan") or {}).get("conditions") or {}
            for key, abbr in labels.condition_abbrs(dconds).items():
                dconds[key]["abbr"] = abbr  # plate letters for the plan check, same rule
        steps = (self.r.get("plan") or {}).get("steps") or []
        for t in snap.get("timers") or []:
            # the header chip's name: its step's short title (older timers: the step, shortened)
            i = t.get("step_index")
            t["chip"] = labels.step_short_title(steps[i]) if isinstance(i, int) and 0 <= i < len(steps) \
                else labels.shorten_title(t.get("step") or t.get("label") or "")
        snap["handoff"] = self.handoff_view(self._handoff_here()) if self._handoff_here() else None
        snap["bench"] = self.bench_view(self.r)
        snap["header"] = {"mode": self.header_mode(cur.get("screen") or ("main" if snap.get("run") else "home"))}
        if snap.get("run"):  # what the app shows instead of the run id
            snap["run"]["label"] = self.run_label(self.active_run_doc() or snap["run"])["display"]
        return snap

    #: Done rows kept above the current one on the bench's right column.
    BENCH_DONE_ROWS = 2

    def bench_view(self, rec: dict) -> dict:
        """What the bench draws, from the run record so far (the app draws it
        as is): each well's state on the plate (done: filled in its group;
        now: a target of this step not done in it; excluded), the legend
        (wells per condition), and the last steps passed as Done rows."""
        plan = rec.get("plan") or {}
        steps = plan.get("steps") or []
        idx = rec.get("step_index") or 0
        targets = self.current_planned(rec) if steps and idx < len(steps) else set()
        done_now, done_any = runlog.done_wells(rec, idx + 1), runlog.done_wells(rec)
        wells = {}
        for w in sorted(targets | done_any):
            wells[w] = "now" if w in targets and w not in done_now else "done"
        for m in rec.get("skipped") or []:
            if m.get("well") not in wells:
                wells[m["well"]] = "skipped"  # passed over unmarked, never done since
        for w in (rec.get("excluded") or {}):
            wells[w] = "excluded"
        legend = [{"key": k, "label": c.get("label") or k, "n": len(c.get("wells") or [])}
                  for k, c in sorted((plan.get("conditions") or {}).items())]
        rows = [{"wells": wells_label(s.get("wells") or []), "name": (s.get("pipette") or {}).get("liquid") or "",
                 "state": "done"}
                for s in steps[:idx] if s.get("kind") == "pipette" and s.get("wells")]
        return {"wells": wells, "legend": legend, "rows": rows[-self.BENCH_DONE_ROWS:]}

    def header_mode(self, screen: str) -> dict | None:
        """The header's mode label, on screens after the mode was chosen
        (registry `header_mode`): a run's own screens show that run's mode,
        the start flow the mode chosen now; else None (not drawn)."""
        if not SCREENS.get(screen, {}).get("header_mode"):
            return None
        mode = (self.r.get("run") or {}).get("mode") if parent_of(screen) == "main" else None
        mode = mode or run_mode()
        return {"id": mode, "label": (section("modes").get(mode) or {}).get("label") or mode.capitalize()}

    # --- plan helpers ---

    def steps(self) -> list[str]:
        names = [s.get("text") or s.get("name", "") for s in self.r["plan"]["steps"]]
        return names or list(DEFAULT_STEPS)

    def current_range(self) -> str | None:
        steps = self.r["plan"]["steps"]
        if steps and self.r["step_index"] < len(steps):
            return wells_label(steps[self.r["step_index"]].get("wells", []))
        return None

    def current_planned(self, rec: dict | None = None) -> set[str]:
        rec = self.r if rec is None else rec
        steps = (rec.get("plan") or {}).get("steps", [])
        idx = rec.get("step_index", 0)
        if steps and idx < len(steps):
            return set((steps[idx] or {}).get("wells", []))
        return set()

    def current_step(self) -> int:
        """1-based step that unqualified marks land on."""
        return self.r.get("step_index", 0) + 1

    def done_now(self) -> set[str]:
        """Wells done in the current step."""
        return runlog.done_wells(self.r, self.current_step())

    def unplanned(self, wells, step: int | None = None) -> list[str]:
        steps = self.r["plan"]["steps"]
        if not steps:
            return []
        n = self.current_step() if step is None else step
        planned = set((steps[n - 1] or {}).get("wells", [])) if 0 < n <= len(steps) else set()
        return [w for w in wells if w not in planned]

    def step_wells(self, rec: dict | None = None) -> list[str]:
        return sort_wells(self.current_planned(rec))

    def planned_wells(self) -> set[str]:
        """Every well named anywhere in the plan (all steps + all conditions)."""
        out: set[str] = set()
        for s in self.r["plan"]["steps"]:
            out |= set(s.get("wells", []))
        for g in self.r["plan"]["conditions"].values():
            out |= set(g.get("wells", []))
        return out

    def remaining_wells(self) -> list[str]:
        done = self.done_now()
        return [w for w in self.step_wells() if w not in done and w not in self.r["excluded"]]

    def timer_wells(self) -> set[str]:
        """Wells with a still-running timer scoped to them."""
        now = time.time()
        out: set[str] = set()
        for t in self.r["timers"]:
            if not is_over(t, now):
                out |= set(parse_wellref(t["wells"] or ""))
        return out

    def occupied_wells(self) -> set[str]:
        return (runlog.done_wells(self.r) | set(self.r["excluded"])
                | set(self.r["redo"]) | self.timer_wells())

    def find_free_wells(self, near: str, count=3) -> list[str]:
        """Candidate backup wells: plan backups first, else spares toward row ends.

        Pool = valid wells minus occupied (done/excluded/redo/running timer)
        minus planned step/group wells; backups skip the planned cut. Sort =
        higher columns first (spares live at row ends), then nearest, then
        adjacent rows. Deterministic: row/col break every tie.
        A range near searches from the cell after the range end.
        """
        n = _num(count)
        n = 3 if n is None else max(1, min(12, int(n)))
        occ = self.occupied_wells()
        out: list[str] = []
        for w in self.r["plan"].get("backups", []) or []:
            u = w.upper() if isinstance(w, str) else ""
            if is_well(u) and u not in occ and u not in out:
                out.append(u)
        planned = self.planned_wells()
        rest = [w for w in ALL_WELLS if w not in planned and w not in occ and w not in out]
        refs = parse_wellref(near) if isinstance(near, str) else []
        anchor = None
        if len(refs) == 1:
            anchor = refs[0]
        elif refs:
            r, c = "ABCDEFGH".index(refs[-1][0]), int(refs[-1][1:])
            c += 1
            if c > 12:
                r, c = min(r + 1, 7), 1
            anchor = f"{'ABCDEFGH'[r]}{c}"
        if anchor is not None:
            nr, nc = "ABCDEFGH".index(anchor[0]), int(anchor[1:])

            def key(w: str):
                r, c = "ABCDEFGH".index(w[0]), int(w[1:])
                return (abs(r - nr), 0 if c >= nc else 1, abs(c - nc), r, c)

            rest.sort(key=key)
        else:
            rest.sort(key=lambda w: ("ABCDEFGH".index(w[0]), int(w[1:])))
        return (out + rest)[:n]

    def _affinity_group(self, well: str) -> str | None:
        """Condition sharing the well's row (most wells wins, name breaks ties)."""
        row = well[0]
        best: str | None = None
        best_n = 0
        for gname in sorted(self.r["plan"]["conditions"]):
            n = sum(1 for w in self.r["plan"]["conditions"][gname].get("wells", [])
                    if w.startswith(row))
            if n > best_n:
                best, best_n = gname, n
        return best

    def _busy_reason(self, well: str) -> str:
        if well in self.r["excluded"]:
            return "already excluded"
        if well in self.r["redo"]:
            return "already marked for redo"
        if well in runlog.done_wells(self.r):
            return "already done"
        return "has a running timer"

    def prompt_state(self, session: dict) -> dict:
        steps = self.steps()
        idx = self.r["step_index"]
        raw = self.r["plan"]["steps"]
        st = {
            "has_run": bool(self.r.get("run")),
            "step": idx,
            "total": len(steps),
            "step_name": steps[idx] if idx < len(steps) else "",
            "next_step": steps[idx + 1] if idx + 1 < len(steps) else "",
            "caution": (raw[idx].get("caution") or "") if idx < len(raw) else "",
            "next_caution": (raw[idx + 1].get("caution") or "") if idx + 1 < len(raw) else "",
            "plan_summary": f"Step {idx + 1}/{len(steps)}: {steps[idx]}" if idx < len(steps) else "",
            "done": sorted(self.done_now()),
            "excluded": sorted(self.r["excluded"]),
            "step_wells": self.step_wells(),
            "remaining": self.remaining_wells(),
            "timers": [{"label": t["label"], "minutes": t["minutes"]} for t in self.r["timers"]],
            "recent": self.r["recent"][-5:],
            "draft_open": bool(self.r.get("draft")),
        }
        last_asked = (session.get("turns") or [{}])[-1].get("asked") or {}
        if last_asked.get("question"):
            st["last_asked"] = last_asked
        return st

    def plan_times(self) -> dict[int, list]:
        """step_index -> durations from the minutes field, else parsed text."""
        out: dict[int, list] = {}
        for i, s in enumerate(self.r["plan"]["steps"]):
            if isinstance(s.get("minutes"), (int, float)):
                out[i] = [float(s["minutes"])]
                continue
            ds = sorted(find_durations(s.get("text") or s.get("name", "")))
            if ds:
                out[i] = ds
        return out

    def model_state(self, session: dict) -> dict:
        """Compact state for the model: summaries only, no memo bodies or transcripts."""
        steps = self.steps()
        idx = self.r["step_index"]
        name = steps[idx] if idx < len(steps) else ""
        pt = "; ".join(f"{i + 1}: {', '.join(f'{fmt_minutes(d)}m' for d in ds)}"
                       for i, ds in sorted(self.plan_times().items()))
        usable = runlog.done_wells(self.r) - set(self.r["excluded"])
        conds = []
        for gname in sorted(self.r["plan"]["conditions"]):
            g = self.r["plan"]["conditions"][gname]
            have = len(usable & set(g.get("wells", [])))
            conds.append(f"{g.get('label', gname)} {have}/{g.get('replicates', '?')}")
        now = time.time()
        timers = []
        for t in self.r["timers"]:
            left = int((t["started_at"] + t["minutes"] * 60 - now) // 60)
            timers.append(f"{t['label']} ({left}m left)" if left > 0 else f"{t['label']} (done)")
        now_dt = time.strftime("%I:%M %p").lstrip("0")
        st = {
            "run": (self.r.get("run") or {}).get("protocol_name") or "none",
            "clock": f"{now_dt} local",
            "step": f"{idx + 1}/{len(steps)} {name}",
            "plan_times": pt,
            "conditions": "; ".join(conds),
            "step_wells": join_wells_ranges(self.step_wells()),
            "remaining": join_wells_ranges(self.remaining_wells()),
            "done": join_wells_ranges(self.done_now()),
            "excluded": join_wells_ranges(self.r["excluded"]),
            "timers": timers,
            "recent": self.r["recent"][-5:],
            "current_screen": (self.r.get("view") or {}).get("current", {}).get("screen", "main"),
            # the run record on screen: "here", "this run" mean it, not the latest
            **({"viewing_run": self.run_label(v)["spoken"]} if (v := self._viewing_run()) else {}),
            "pending_cards": len(self.r["cards"]),
            "draft_open": bool(self.r.get("draft")),
        }
        cur = (self.r.get("view") or {}).get("current") or {}
        if cur.get("screen") == "web_answer":
            doc = next((d for d in self.searches() if d["id"] == cur.get("target")), None)
            if doc and doc.get("kind") == "photo" and doc.get("status") == "ok":
                # a follow-up on a photo answer: what was just read, and the protocol it's for
                st["photo_answer"] = {"question": doc.get("question") or "",
                                      "read": doc.get("answer_short") or "", "detail": doc.get("detail") or ""}
                plan = self.r.get("plan") or {}
                if not plan.get("steps") and doc.get("run_id"):
                    rd = runlog.resolve(doc["run_id"], self.all_run_docs())
                    plan = ((rd or {}).get("record") or {}).get("plan") or {}
                if plan.get("steps"):
                    st["protocol"] = self._protocol_brief(plan)
        if self.r.get("draft"):
            draft = self.r["draft"]
            st["draft"] = proto.draft_summary(draft.get("plan") or {})
            st["draft_issues"] = (f"review {len(draft.get('review', []))}, "
                                  f"errors {len(draft.get('errors', []))}")
        if session.get("turns"):
            ts = session["turns"]
            st["recent_turns"] = [self._turn_line(t, i == len(ts) - 1) for i, t in enumerate(ts)]
            last_asked = (session["turns"][-1].get("asked") or {}).get("question", "")
            if session.get("last_web") and "attach" in last_asked.lower():
                st["last_answer"] = session["last_web"]
        return st

    @staticmethod
    def _protocol_brief(plan: dict) -> dict:
        """The protocol in a few lines for a follow-up: what goes in (liquid and
        volume per step), the conditions by name, the steps, the notes."""
        steps = plan.get("steps") or []
        liquids = []
        for st in steps:
            p = st.get("pipette") or {}
            if p.get("liquid"):
                vol = f" {fmt_minutes(p['volume'])} {p.get('unit') or ''}".rstrip() if p.get("volume") else ""
                liquids.append(f"{p['liquid']}{vol}")
        conds = "; ".join(f"{(c or {}).get('label') or k} {join_wells_ranges((c or {}).get('wells') or [])}"
                          for k, c in sorted((plan.get("conditions") or {}).items()))
        return {"liquids": "; ".join(dict.fromkeys(liquids)), "conditions": conds,
                "steps": "; ".join(f"{i + 1}. {(st.get('text') or '')[:60]}" for i, st in enumerate(steps)),
                "notes": "; ".join(st.get("caution") or st.get("tip") for st in steps
                                   if st.get("caution") or st.get("tip"))}

    @staticmethod
    def _turn_line(t: dict, with_asked: bool = True) -> str:
        line = f"{t['text']} → {', '.join(t['calls'])}: {' | '.join(t['messages']) or '-'}"
        asked = t.get("asked") or {}
        if asked.get("question") and with_asked:
            line += f" | asked: {asked['question']}"
            if asked.get("target"):
                line += f" for {asked['target']}"
        return line

    def session_summary(self) -> str:
        steps = self.steps()
        idx = self.r["step_index"]
        labels = [t["label"] for t in self.r["timers"]]
        recent = "; ".join(
            f"{a.get('route')}:{a.get('action')}[{','.join(a.get('wells') or [])}]"
            for a in self.r["recent"][-5:]
        )
        return (
            f"Step {idx + 1}/{len(steps)} {steps[idx] if idx < len(steps) else ''}. "
            f"Done this step: {sort_wells(self.done_now())}. Excluded: {sorted(self.r['excluded'])}. "
            f"Timers: {labels}. Recent: {recent}."
        )

    # --- apply ---

    def _stamp(self, wells, source: str, now: float):
        for w in wells:
            self.r["stamps"][w] = {"source": source, "ts": now}

    @staticmethod
    def _card_key(payload: dict):
        """(action, subject): one card per key; a repeat replaces, never stacks."""
        op = (payload or {}).get("op")
        p = payload or {}
        if op == "mark_done":
            subject = (p.get("step"), tuple(sorted(p.get("wells") or [])))
        elif op == "exclude_well":
            subject = p.get("well")
        elif op == "record_redo":
            subject = tuple(sorted(p.get("wells") or []))
        elif op == "save_tip":
            subject = p.get("step")
        elif op == "set_timer":
            subject = (p.get("minutes"), tuple(sorted(p.get("wells") or [])))
        elif op in ("cancel_timer", "extend_timer"):
            subject = p.get("label")
        elif op == "next_step":
            subject = p.get("target")
        elif op in ("start_run", "new_run"):
            subject = p.get("protocol_id")
        elif op == "end_run":
            subject = p.get("run_id")
        elif op == "web_search":
            subject = p.get("query")
        else:
            subject = None
        return (op, subject if subject != [] else None)

    @staticmethod
    def _tell(res: dict, key: str, session, wells: list, **slots):
        """A result line about wells, kept with its parts so the reply can
        fold same-key, same-slot lines into one sentence over the joined
        range (api._compose_reply)."""
        line = say(key, session, wells=join_wells_ranges(wells), **slots)
        res["messages"].append(line)
        res.setdefault("told", []).append({"key": key, "wells": list(wells), "slots": slots,
                                           "line": line})

    def _card(self, res: dict, kind: str, question: str, detail, payload: dict,
              slots: dict | None = None) -> dict:
        key = self._card_key(payload)
        same = {k: v for k, v in (payload or {}).items() if not k.startswith("_")}
        for c in self.r["cards"].values():
            if {k: v for k, v in (c.get("payload") or {}).items() if not k.startswith("_")} == same:
                # the same question is already up: ask it again, never stack a copy
                res["cards"].append(c)
                return c
        for cid in [cid for cid, c in self.r["cards"].items()
                    if self._card_key(c.get("payload")) == key]:
            del self.r["cards"][cid]
        p = payload or {}
        wells = p.get("wells") or ([p["well"]] if p.get("well") else [])
        conds = (self.r.get("plan") or {}).get("conditions") or {}
        group = next((c.get("label") for c in conds.values() if wells and wells[0] in (c.get("wells") or [])), "")
        target = p.get("target") if isinstance(p.get("target"), int) else None
        fill = {
            "question": question, "detail": detail or "", "wells": join_wells_ranges(wells),
            "group": group, "reason": p.get("reason"), "note": p.get("note"), "label": p.get("label"),
            "step": target, "done": target - 1 if target else None,
            "minutes": fmt_minutes(p["minutes"]) if p.get("minutes") is not None else None,
            **(slots or {})}
        title, sub = labels.card_lines(kind, fill)
        context, header = labels.card_more(kind, fill)
        card = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "question": question,  # spoken
            "title": title,  # shown big, and the detail small (pip_voice.json `cards`)
            "detail": sub,
            **({"context": context} if context else {}),  # small line over the title
            **({"header": header} if header else {}),  # the header's one line while it waits
            "payload": payload,
            "ts": time.time(),
        }
        self.r["cards"][card["id"]] = card
        res["cards"].append(card)
        return card

    def apply_calls(self, calls, source: str, origin: str, route: str, session: dict,
                    reads_text: str = "") -> dict:
        with self._lock:
            return self._apply_calls_locked(calls, source, origin, route, session, reads_text)

    def _apply_calls_locked(self, calls, source: str, origin: str, route: str, session: dict,
                            reads_text: str = "") -> dict:
        res: dict = {"messages": [], "cards": [], "info": {"dropped": 0, "drop_reasons": []},
                     "outcomes": [], "ui": []}
        evidence = build_evidence(origin, evidence_summary(session.get("turns")),
                                  frozenset(), reads_text)
        step_before, left_before = self.r.get("step_index"), self._left_now()
        for c in calls:
            m0, c0 = len(res["messages"]), len(res["cards"])
            if self._sharing() and c.get("name") not in ("share_result", "read_record", "ask"):
                # the iOS share sheet is up: the session stays, the screen holds (like Browse files)
                res["messages"].append(say("handoff_sharing", session))
                res["outcomes"].append({"messages": res["messages"][m0:], "cards": []})
                continue
            self._apply_call(c, source, origin, route, session, res, force=False,
                             evidence=evidence)
            for card in res["cards"][c0:]:
                card["payload"]["_call_id"] = c.get("call_id")
            res["outcomes"].append({"messages": res["messages"][m0:], "cards": res["cards"][c0:]})
        if calls:
            self.record_recent(route, calls)
        if left_before and self.r.get("step_index") == step_before and self._left_now() == set():
            self._offer_next_locked(res, session)  # the step's last target just got done: Milo asks
        for a in res["ui"]:
            a.setdefault("id", uuid.uuid4().hex[:8])  # the reply and /events carry it; the app runs it once
        res["info"]["ui"] = res["ui"]
        self._commit()
        for a in res["ui"]:
            self._broadcast({"kind": "ui", **a})
        return res

    RUN_WRITES = frozenset({"mark_done", "exclude_well", "record_redo", "add_note",
                                "edit_note", "save_tip", "set_timer", "cancel_timer",
                                "extend_timer", "next_step", "end_run"})

    def _apply_call(self, call, source, origin, route, session, res, force: bool,
                    evidence=None):
        name = call.get("name")
        args = call.get("args") or {}
        now = time.time()
        if evidence is None:
            evidence = build_evidence(origin, "", frozenset(), "")
        if name == "answer_card":
            if call.get("from_model"):
                res["messages"].append("I couldn't do that.")
                return
            ok = args.get("ok", True)
            if not isinstance(ok, bool):
                ok = str(ok).strip().lower() not in ("no", "cancel", "false", "0", "dismiss")
            message, fresh, card = self._confirm_locked(args.get("card_id", ""), ok, source, session,
                                                        str(args.get("answer") or ""))
            p = (card or {}).get("payload") or {}
            if ok and p.get("op") == "web_search":
                # the search is a network call: the api layer runs it after this turn
                res["info"]["search"] = {"question": p.get("question", ""), "query": p.get("query", "")}
                return
            if ok and p.get("search_id") and p.get("op") == "look":
                res["info"]["look"] = {"search_id": p["search_id"]}
            res["messages"].append(message)
            res["cards"] += fresh
            return
        if not force and name in self.RUN_WRITES:
            viewing = self._viewing_run()
            if viewing is not None and (viewing.get("run_id") != (self.r.get("run") or {}).get("run_id")):
                res["messages"].append("That run is finished — read only.")
                return
            if not self.r.get("run"):
                res["messages"].append("No active run — open a protocol from Home to start.")
                return
        if name == "mark_done":
            wells = [w for w in (args.get("wells") or []) if isinstance(w, str)]
            bad = [w for w in wells if not is_well(w)]
            good = [w for w in wells if is_well(w)]
            n_steps = len(self.r["plan"]["steps"])
            step = args.get("step")
            if step is None:
                step = self.current_step()
            if not isinstance(step, int) or isinstance(step, bool) or not 0 < step <= max(n_steps, 1):
                res["messages"].append(f"I couldn't mark that: there's no step {step}.")
                return
            if not force and good and self.unplanned(good, step):
                off = join_wells_ranges(self.unplanned(good, step))
                self._card(res, "catch", say("catch_off_plan", session, wells=off),
                           None, {"op": "mark_done", "wells": good, "step": step}, slots={"wells": off})
                return
            msg = ""
            if good:
                have = runlog.done_wells(self.r, step)
                self.r["done"] = sorted(
                    runlog.done_marks(self.r)
                    + [{"step": step, "well": w, "source": source, "ts": now}
                       for w in good if w not in have],
                    key=lambda m: (m["step"], _well_coords(m["well"]), m["well"]))
                self._stamp(good, source, now)
                if not bad:
                    self._tell(res, "mark_done", session, good)
                else:
                    msg = say("mark_done", session, wells=join_wells_ranges(good))
                res["ui"].append({"action": "flash", "wells": good})
            if bad:
                bad_msg = invalid_message(bad, session)
                msg = bad_msg if not msg else msg + " " + bad_msg
            if msg:
                res["messages"].append(msg)
        elif name == "exclude_well":
            well = args.get("well")
            reason = args.get("reason")
            if not isinstance(well, str) or not is_well(well):
                res["messages"].append(invalid_message([well], session))
                return
            if well in self.r["excluded"]:
                # already out (e.g. a repeat of a card just answered): no new card
                res["messages"].append(say("already_out", session, wells=well))
                return
            if not force:
                self._card(res, "confirm_exclude", say("confirm_exclude", session, wells=well),
                           reason, {"op": "exclude_well", "well": well, "reason": reason})
                return
            self.r["excluded"][well] = reason or ""
            self._stamp([well], source, now)
            res["messages"].append(f"{well} excluded ({trimmed(reason)})" if reason else f"{well} excluded")
            res["ui"].append({"action": "flash", "wells": [well]})
        elif name == "record_redo":
            wells = [w for w in (args.get("wells") or []) if isinstance(w, str)]
            note = args.get("note")
            bad = [w for w in wells if not is_well(w)]
            good = [w for w in wells if is_well(w)]
            occ = self.occupied_wells()
            busy = [w for w in good if w in occ]
            good = [w for w in good if w not in occ]
            if busy:
                res["messages"].append(
                    f"I couldn't mark {', '.join(busy)} for redo: "
                    f"{self._busy_reason(busy[0])}."
                )
            if not force and good:
                self._card(res, "confirm_redo", f"Redo {', '.join(good)}?", note,
                           {"op": "record_redo", "wells": good, "note": note})
                return
            msg = ""
            if good:
                for w in good:
                    self.r["redo"][w] = note or ""
                    g = self._affinity_group(w)
                    if g is not None:
                        gw = self.r["plan"]["conditions"][g].get("wells", [])
                        if w not in gw:
                            self.r["plan"]["conditions"][g]["wells"] = sort_wells(set(gw) | {w})
                self._stamp(good, source, now)
                msg = f"{', '.join(good)} marked for redo ({trimmed(note)})" if note else f"{', '.join(good)} marked for redo"
            if bad:
                bad_msg = invalid_message(bad, session)
                msg = bad_msg if not msg else msg + " " + bad_msg
            if msg:
                res["messages"].append(msg)
            if good and force:
                res["ui"].append({"action": "flash", "wells": good})
        elif name == "add_note":
            target, text = args.get("target"), args.get("text")
            if not isinstance(target, str) or not isinstance(text, str):
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({
                    "call": name,
                    "args": args,
                    "reason": "missing target" if not isinstance(target, str) else "missing text",
                })
                return
            ok, _ = grounded(text, evidence)
            memo_text = text.strip() if ok else origin
            memo = {
                "id": uuid.uuid4().hex[:8],
                "target": target,
                "text": memo_text,
                "raw": origin,
                "ts": now,
                "route": route,
                "source": {"voice": "voice", "tap": "tap"}.get(source, "typed"),
                "step": self.current_step(),
                "wells": parse_wellref(target),
                "history": [],
            }
            self.r["notes"].append(memo)
            self._stamp([target], source, now)
            if memo["wells"] == [target]:
                self._tell(res, "note_saved", session, [target])
            else:
                res["messages"].append(say("note_saved", session, wells=target))
            res["info"]["saved_memo"] = {"id": memo["id"], "target": target, "text": memo["text"]}
            if memo["wells"]:
                res["ui"].append({"action": "flash", "wells": memo["wells"]})
                res["ui"].append({"action": "peek", "target": memo["wells"][0], "seconds": 3})
        elif name == "edit_note":
            text = args.get("text")
            well = args.get("well")
            if isinstance(well, str) and well.strip():
                wid = well.strip().upper()
                if not is_well(wid):
                    res["messages"].append(invalid_message([well], session))
                    return
                memo = next((m for m in reversed(self.r["notes"])
                             if wid in (m.get("wells") or [])), None)
                if memo is None:
                    res["messages"].append(f"No note on {wid} yet.")
                    return
            else:
                if not self.r["notes"]:
                    res["messages"].append("No notes yet.")
                    return
                memo = self.r["notes"][-1]
            ok, _ = grounded(text if isinstance(text, str) else "", evidence)
            if ok:
                memo["history"].append({"text": memo["text"], "ts": memo["ts"]})
                memo["text"] = text.strip()
                memo["ts"] = now
                res["messages"].append("Note updated.")
            else:
                res["messages"].append(f"Kept the old note ({memo['target']}).")
            res["info"]["edited"] = {"id": memo["id"], "target": memo["target"]}
            if memo.get("wells"):
                res["ui"].append({"action": "flash", "wells": memo["wells"]})
                res["ui"].append({"action": "peek", "target": memo["wells"][0], "seconds": 3})
        elif name == "save_tip":
            step, text = args.get("step_id"), args.get("text")
            if not isinstance(step, str) or not isinstance(text, str):
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({"call": name, "args": args, "reason": "missing text"})
                return
            last_asked = (session.get("turns") or [{}])[-1].get("asked") or {}
            pool = origin
            if "attach" in last_asked.get("question", "").lower() and session.get("last_web"):
                pool = session["last_web"]
            tip_evidence = evidence if pool == origin else build_evidence(pool, "", frozenset(), "")
            ok, _ = grounded(text, tip_evidence)
            final = text.strip() if ok else pool
            if not force:
                self._card(res, "confirm_tip", "Save this tip?", f"[{step}] {final}",
                           {"op": "save_tip", "step": step, "text": final})
                return
            self.r["tips"].append({"step": step, "text": final, "ts": now, "route": route})
            res["messages"].append(f"Tip saved ({step})")
        elif name == "set_timer":
            minutes = _num(args.get("minutes"))
            if minutes is None:
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({"call": name, "args": args, "reason": "no minutes stated"})
                return
            on_done = args.get("on_done")
            explicit = [w for w in (args.get("wells") or []) if isinstance(w, str)]
            if not force:
                src = args.get("_source")
                if src in ("conversation", "plan"):
                    key = "confirm_reuse" if src == "conversation" else "confirm_plan"
                    self._card(res, "confirm_timer",
                               say(key, session, minutes=fmt_minutes(minutes),
                                   wells=join_wells_ranges(explicit)),
                               None, {"op": "set_timer", "minutes": minutes,
                                      "on_done": on_done, "wells": explicit or None})
                    return
                asked = (session.get("turns") or [{}])[-1].get("asked") or {}
                asked_wells = set(parse_wellref(asked.get("target") or ""))
                if explicit and asked_wells and not (set(explicit) & asked_wells):
                    self._card(res, "confirm_timer",
                               say("confirm_minutes", session, minutes=fmt_minutes(minutes),
                                   wells=join_wells_ranges(explicit)),
                               None, {"op": "set_timer", "minutes": minutes,
                                      "on_done": on_done, "wells": explicit})
                    return
            ws = explicit or (self.step_wells() if args.get("each") is True else [])
            if len(ws) > 1:
                for w in ws:
                    self._single_timer(res, minutes, on_done, [w], source, now, force, session)
            else:
                self._single_timer(res, minutes, on_done, explicit or ws or None, source, now, force, session)
        elif name == "cancel_timer":
            self._purge(now)
            label = args.get("label")
            label = label.strip() if isinstance(label, str) else None
            self._cancel(res, label, source, now, session)
        elif name == "extend_timer":
            self._purge(now)
            label = args.get("label")
            label = label.strip() if isinstance(label, str) else None
            self._extend(res, label, args.get("minutes"), source, now)
        elif name == "where_am_i":
            # Grammar-only canned read (grammar has no round 2); the model reads steps instead.
            about = args.get("about")
            st = self.prompt_state(session)
            if about == "next":
                res["messages"].append(whats_next_reply(st))
            elif about == "step":
                res["messages"].append(what_step_reply(st))
            else:
                res["messages"].append(where_am_i_reply(st))
        elif name == "retry_sync":
            # Read-grade: the app re-runs its on-demand device checks.
            res["messages"].append(say("sync_retry", session))
            res["info"]["sync_retry"] = True
        elif name == "skip_sync":
            # Read-grade: no record write; the app leaves setup.
            session["setup_done"] = True
            res["messages"].append(say("sync_skip", session))
            res["info"]["setup_done"] = True
        elif name == "next_step":
            steps = self.steps()
            steps_full = self.r["plan"]["steps"]
            if self.r["step_index"] >= len(steps) - 1:
                res["messages"].append("Already at the last step")
            elif not force and (left := sort_wells(self._left_now() or [])):
                # targets still unmarked: ask, never mark (or drop) them quietly
                nxt = self.r["step_index"] + 1
                rng = join_wells_ranges(left)
                self._card(res, "unmarked", say("unmarked_step", session, wells=rng, n=len(left), step=nxt + 1),
                           None, {"op": "next_step", "target": nxt + 1, "unmarked": left},
                           {"wells": rng, "n": len(left), "step": nxt + 1, "now": nxt,
                            "now_title": labels.step_short_title(steps_full[nxt - 1])})
            elif not force:
                nxt = self.r["step_index"] + 1
                self._card(res, "next_step", say("step_move", session, step=nxt + 1),
                           steps[nxt], {"op": "next_step", "target": nxt + 1})
            else:
                self.r["step_index"] += 1
                self._drop_step_cards_locked()
                res["messages"].append(f"Next: {steps[self.r['step_index']]}")
                self._suggest_entry_timer(res, session)
        elif name == "end_run":
            if not self.r.get("run"):
                res["messages"].append("No active run.")
            else:
                self._card(res, "end_run", say("end_run", session), None,
                           {"op": "end_run",
                            "run_id": (self.r.get("run") or {}).get("run_id")})
        elif name == "show":
            # the person-facing line is the registry result, never the raw view id
            wells = parse_wellref(args.get("target")) if args.get("target") else []
            sid = "well_card" if wells else canonical(str(args.get("view") or "")) or "main"
            res["messages"].append(result_of(sid, target=wells_label(wells), name=args.get("target")))
            if wells:
                res["ui"].append({"action": "flash", "wells": wells})
        elif name == "navigate":
            raw = args.get("screen")
            screen = canonical(raw) if isinstance(raw, str) else None
            if screen is None and isinstance(raw, str) and raw.strip() in PLANNED:
                res["messages"].append(say("not_ready", session))  # registry: planned
                return
            if screen is None:
                res["messages"].append(
                    "I couldn't open that: no screen given."
                    if not isinstance(raw, str) or not raw.strip()
                    else f"I couldn't open the {raw.strip()} screen: no such screen.")
                return
            self._navigate_locked(res, session, screen, args.get("target"))
        elif name == "open":
            raw = args.get("target")
            t = raw.strip() if isinstance(raw, str) else ""
            if not t:
                res["messages"].append("I couldn't open that: no target given.")
                return
            low = t.lower()
            if low.startswith("protocol:"):
                self._open_protocol(res, session, t[9:])
                return
            if low.startswith("run:"):
                self._open_run(res, session, t[4:])
                return
            if low.startswith("search"):
                self._open_search(res, session, t[7:] if low.startswith("search:") else t)
                return
            if low.startswith("run ") or low in ("run", "runs"):
                self._open_run(res, session, t if low.startswith("run ") else self._here_run() or "latest")
                return
            if runlog.resolve(t, self.all_run_docs()) is not None:
                self._open_run(res, session, t)
                return
            if "original" in low or low.startswith("source"):
                self._open_source(res, session, t)
                return
            if is_well(t):
                self._open_well(res, t)
                return
            screen = canonical(t)
            if screen is not None:
                self._navigate_locked(res, session, screen, None)
                return
            proto_doc = lib.find_by_name(t, self.root)
            if proto_doc is not None:
                self._navigate_locked(res, session, "protocol_detail", proto_doc["id"])
                return
            res["messages"].append(f"I couldn't open {t}: not a plate well or screen.")
        elif name == "set_tab":
            tab = args.get("tab")
            if tab in ("liquids", "steps", "layout"):
                self.r["view"]["tab"] = tab
            else:
                res["messages"].append("I couldn't open that tab.")
        elif name == "back":
            v = self.r["view"]
            if (v.get("sheet") or {}).get("name"):
                v["sheet"] = {"name": "", "target": ""}
                res["messages"].append("Closed.")
                return
            if v.get("stack"):
                v["current"] = v["stack"].pop()
                res["messages"].append(f"Back to {title_of(v['current']['screen'])}.")
                return
            here = (v.get("current") or {}).get("screen", "main")
            res["messages"].append("Already on the main screen." if here == "main"
                                   else f"Already on {title_of(here)}.")
        elif name == "make_robot":
            self._open_handoff(res, session, str(args.get("target") or ""))
        elif name in self.HANDOFF_ACTIONS:
            self._handoff_action(name, args, session, res)
        elif name == "read_record":
            if args.get("kind") not in self.READ_KINDS:
                res["messages"].append("I couldn't read that: unknown section.")
            # Success is silent; the data rides the call outcome.
        elif name == "edit_draft":
            draft = self.r.get("draft")
            parsed = proto.parse_draft_path(args.get("path"))
            if not draft:
                res["messages"].append("I couldn't edit the draft: no draft open.")
            elif parsed is None:
                res["messages"].append(
                    f"I couldn't edit the draft: unknown path {args.get('path')}.")
            else:
                ok, value = proto.parse_draft_value(draft, parsed, args.get("value"))
                if not ok:
                    res["messages"].append(f"I couldn't edit the draft: {value}.")
                else:
                    ok2, reason = proto.apply_draft_edit(draft, parsed, value)
                    if not ok2:
                        res["messages"].append(f"I couldn't edit the draft: {reason}.")
                    else:
                        proto.recompute_draft(draft)
                        msg = say("draft_edited", session, target=proto.edit_target(parsed))
                        if draft["errors"]:
                            msg += f" Heads up: {draft['errors'][0]['reason']}."
                        res["messages"].append(msg)
        elif name == "open_source":
            self._open_web_source(res, args)
        elif name == "control":
            self._control(args, res)
        elif name == "pick":
            self._pick(args, res)
        elif name == "look":
            self._start_look(str(args.get("question") or origin or "").strip(), res, session)
        elif name == "retake_look":
            self._retake_look(res, session)
        elif name == "resume_run":
            self._resume_locked(str(args.get("target") or ""), session, res)
        elif name in ("show_step", "open_source_page") and self._handoff_here():
            self._final_step(name, args, session, res)
        elif name in self.DRAFT_ACTIONS:
            self._draft_action(name, args, session, res)
        else:
            res["info"]["dropped"] += 1
            res["info"]["drop_reasons"].append({"call": name or "?", "args": args, "reason": "unknown tool"})

    # --- web searches (server/search.py runs them; this keeps what was sent) ---

    def searches(self) -> list[dict]:
        """Every search sent, newest first (status ok / no_source / timeout)."""
        try:
            docs = json.loads((self.root / "searches.json").read_text())
        except (OSError, ValueError):
            docs = []
        return sorted(docs, key=lambda d: d.get("ts") or 0, reverse=True)

    def answered_searches(self) -> list[dict]:
        return [d for d in self.searches() if d.get("status") == "ok"]

    def add_search(self, rec: dict) -> None:
        with self._lock:
            self._add_search_locked(rec)

    def _add_search_locked(self, rec: dict) -> None:
        docs = self.searches() + [rec]
        (self.root / "searches.json").write_text(json.dumps(docs, ensure_ascii=False))

    # --- a question that needs a photo (server/look.py) ---

    def _start_look(self, question: str, res: dict, session: dict | None, confirmed: bool = False) -> None:
        """The photo question begins: a mode that asks first shows its card
        (pip_voice `modes.<mode>.confirm_photo`); else the record opens ⑧ in
        `looking` and the app shows its camera ("Show me.")."""
        if not confirmed and (section("modes").get(run_mode()) or {}).get("confirm_photo"):
            self._card(res, "confirm_photo", say("confirm_photo", session), None,
                       {"op": "look", "question": question})
            return
        rec = searchmod.record(question, "", None, "looking", (self.r.get("run") or {}).get("run_id"), 0)
        rec.update({"kind": "photo", "target": lookmod.target_of(question), "attempt": 1})
        self._add_search_locked(rec)
        self._push_view("web_answer", rec["id"])
        res["ui"].append({"action": "camera", "search_id": rec["id"], "id": uuid.uuid4().hex[:8]})
        res["info"]["look"] = {"search_id": rec["id"]}
        res["messages"].append(say("look_show", session, target=rec["target"]))

    def update_look(self, search_id: str, attempt: int | None = None, **fields) -> dict | None:
        """A photo question's record moves on (reading, its crop, its answer).
        With `attempt`: only if that photo is still the current one (a Retake
        since then makes it stale: None, nothing written)."""
        with self._lock:
            docs = self.searches()
            rec = next(d for d in docs if d["id"] == search_id)
            if attempt is not None and rec.get("attempt", 1) != attempt:
                return None
            rec.update(fields)
            (self.root / "searches.json").write_text(json.dumps(docs, ensure_ascii=False))
            self._commit()  # the open ⑧ redraws
            return rec

    def _retake_look(self, res: dict, session: dict | None) -> None:
        """Retake: the photo question on screen goes back to "Show me."; the
        photo being read (or its answer) is dropped and a later reply for it
        is ignored (the attempt moves on)."""
        v = self.r["view"]["current"]
        docs = self.searches()
        rec = next((d for d in docs if d["id"] == v.get("target") and d.get("kind") == "photo"), None)
        if v.get("screen") != "web_answer" or rec is None:
            res["messages"].append("There's no photo to retake.")
            return
        rec.update({"status": "looking", "attempt": rec.get("attempt", 1) + 1, "answer_short": "", "detail": "",
                    "read": "", "sources": [], "basis": "", "outline": [], "query": ""})
        (self.root / "searches.json").write_text(json.dumps(docs, ensure_ascii=False))
        self.look_crop_path(rec["id"]).unlink(missing_ok=True)
        res["ui"].append({"action": "camera", "search_id": rec["id"], "attempt": rec["attempt"],
                          "id": uuid.uuid4().hex[:8]})
        res["info"]["look"] = {"search_id": rec["id"]}
        res["messages"].append(say("look_show", session, target=rec.get("target") or "label"))

    def look_crop_path(self, search_id: str):
        """Where a photo question's crop is kept (only the crop, never the photo)."""
        return self.root / "looks" / f"{re.sub(r'[^0-9a-f]', '', search_id)}.jpg"

    def finish_search(self, search_id: str, result: dict | None, status: str, ms: int, line: str = "") -> dict:
        """A pending search's outcome lands on its record (⑧ fills in)."""
        with self._lock:
            docs = self.searches()
            rec = next(d for d in docs if d["id"] == search_id)
            r = result or {}
            rec.update({"status": status, "ms": ms, "answer_short": r.get("answer_short", ""),
                        "detail": r.get("detail", ""), "sources": r.get("sources", []),
                        "basis": r.get("basis", ""), "line": line})  # why not, as said
            (self.root / "searches.json").write_text(json.dumps(docs, ensure_ascii=False))
            self._commit()  # the open ⑧ redraws from the snapshot's slots
            return rec

    def search_summaries(self) -> list[dict]:
        """Every search sent, labeled, with its status (⓪c lists the answered ones)."""
        return [{**d, **labels.search_label(d)} for d in self.searches()]

    def search_item(self, search_id: str) -> dict | None:
        """One search in any state (pending, answered, failed), labeled."""
        d = next((x for x in self.searches() if x["id"] == search_id), None)
        return {**d, **labels.search_label(d)} if d else None

    def private_texts(self) -> list[str]:
        """What a search query must never carry: note texts and run names."""
        notes = [m.get("text") or "" for m in self.r.get("notes") or []]
        return notes + [(self.r.get("run") or {}).get("protocol_name") or ""]

    def show_search(self, search_id: str) -> str:
        """Open a search's answer (after it ran). Returns the screen line."""
        with self._lock:
            self._push_view("web_answer", search_id)
            self._commit()
            return self.screen_line("web_answer", search_id)

    def ask_search(self, question: str, query: str, line: str) -> dict:
        """The card a mode that asks first shows: "Search the web for '<query>'?"."""
        with self._lock:
            res = {"messages": [], "cards": [], "ui": [], "info": {"dropped": 0, "drop_reasons": []}}
            card = self._card(res, "confirm_search", line, None,
                              {"op": "web_search", "query": query, "question": question}, slots={"query": query})
            self._commit()
            return card

    def _open_search(self, res: dict, session: dict, selector: str) -> None:
        docs = self.answered_searches()
        doc, cands = labels.select_search(selector.strip() or "latest", docs)
        if doc is None:
            res["messages"].append(
                say("no_searches", session) if not docs
                else labels.which_one([labels.search_label(c)["spoken"] for c in cands]) if cands
                else "I couldn't find that search.")
            return
        self._push_view("web_answer", doc["id"])
        res["messages"].append(self.screen_line("web_answer", doc["id"]))

    def _open_web_source(self, res: dict, args: dict) -> None:
        """A source page inside the app (web_source), from an answer or another source."""
        v = self.r["view"]["current"]
        sid = (v.get("target") or "").split(":")[0] if v.get("screen") in ("web_answer", "web_source") else ""
        doc = next((d for d in self.searches() if d["id"] == sid), None) if sid else None
        n = int(_num(args.get("n")) or 1)
        if doc is None or not 1 <= n <= len(doc.get("sources") or []):
            res["messages"].append("There's no source to open here." if doc is None
                                   else f"There's no source {n}.")
            return
        if v.get("screen") == "web_source":
            self._pop_view()  # source 2 from source 1: one page deep, Back returns to the answer
        self._push_view("web_source", f"{doc['id']}:{n}")
        res["messages"].append(self.screen_line("web_source", f"{doc['id']}:{n}"))

    # --- the screen's own parts: app-side state, said or tapped the same way ---

    def set_items(self, screen: str, items: list[dict]) -> None:
        """What a list screen shows now (the app's files, the photos taken):
        the candidates "number 2" and a file's name pick from."""
        with self._lock:
            self.screen_items[screen] = [{"id": str(i.get("id")), "name": str(i.get("name") or "")}
                                         for i in items or []][:200]
            self._commit()  # the prompt counts them

    def _control(self, args: dict, res: dict) -> None:
        cur = self.r["view"]["current"]["screen"]
        sc = SCREENS.get(cur, {})
        allowed = {(b.get("args") or {}).get("cmd") for b in sc.get("buttons", []) if b.get("action") == "control"}
        cmd = str(args.get("cmd") or "")
        if cmd not in allowed | set(sc.get("controls", [])):
            res["messages"].append("I can't do that here.")
            return
        n = _num(args.get("n"))
        if cmd == "page" and not n:
            res["messages"].append("Which page?")
            return
        res["ui"].append({"action": "control", "target": cmd, **({"n": int(n)} if n else {})})

    def _pick(self, args: dict, res: dict) -> None:
        cur = self.r["view"]["current"]["screen"]
        items = self.screen_items.get(cur) or []
        if not SCREENS.get(cur, {}).get("pick") or not items:
            res["messages"].append("There's nothing to pick here.")
            return
        n, name = _num(args.get("n")), str(args.get("name") or "").strip().lower()
        if n:
            hit = items[int(n) - 1] if 1 <= int(n) <= len(items) else None
            if hit is None:
                res["messages"].append(f"There are only {len(items)}.")
                return
        else:
            hits = [it for it in items if name and name in it["name"].lower()]
            if len(hits) != 1:
                res["messages"].append(labels.which_one([h["name"] for h in hits]) if len(hits) > 1
                                       else "I couldn't find that one.")
                return
            hit = hits[0]
        res["ui"].append({"action": "pick", "target": hit["id"]})
        res["messages"].append(f"Opening {hit['name']}.")

    # --- Start: draft review (registry actions; same logic as the REST routes) ---

    DRAFT_ACTIONS = ("review_draft", "save_draft", "start_run", "discard_draft", "show_step",
                     "open_source_page")

    def _resume_locked(self, run_id: str, session: dict, res: dict) -> None:
        """A paused run picks up where it stopped; one going now pauses."""
        doc = runlog.resolve(run_id, self.all_run_docs()) if run_id else self._viewing_run()
        if doc is None or doc.get("status") != "paused":
            res["messages"].append("That run isn't paused.")
            return
        paused = None
        if self.r.get("run"):
            paused = self.run_label(self.active_run_doc())["display"]
            self._finish_locked("paused")
        draft, version, epoch = self.r.get("draft"), self.r.get("version", 0), self.r.get("epoch")
        self.r = json.loads(json.dumps(doc["record"]))
        self.r["run"] = {**(self.r.get("run") or {}), "run_id": doc["run_id"]}
        self.r["version"], self.r["epoch"] = version, epoch or uuid.uuid4().hex[:16]
        self.r["view"] = self._default_view(True)
        self.r["cards"] = {}
        if draft is not None:
            self.r["draft"] = draft
        runlog.write_run({**doc, "status": "active"}, self.root)
        lab = self.run_label(self.active_run_doc())
        line = say("run_resumed", session, run=lab["display"], step=self.current_step())
        res["messages"].append(line + (" " + say("run_paused", session, run=paused) if paused else ""))

    def _draft_action(self, name: str, args: dict, session: dict, res: dict) -> None:
        draft = self.r.get("draft")
        v = self.r["view"]["current"]
        run = self._viewing_run()
        if name == "start_run" and run is not None and run.get("status") != "active" \
                and not (args.get("target") or "").strip():
            # a finished run can't go on: its protocol's next run starts (draft or not)
            self._started(*self._request_start_locked(run.get("protocol_id") or "", session), session, res)
            return
        named = (args.get("target") or "").strip()
        if name == "start_run" and (not draft or named or v.get("screen") in ("protocol_detail", "home", "run_view")):
            # a protocol (named, on screen, or the run's) beats an open draft; the draft starts from its screens
            pid = named or (
                v.get("target") if v.get("screen") == "protocol_detail" else (run or {}).get("protocol_id") or "")
            if not pid:
                res["messages"].append("Which protocol? Say its name, or pick one on Home.")
                return
            doc, cands = labels.select_protocol(pid, lib.all_docs(self.root))
            if doc is None:
                res["messages"].append(labels.which_one([labels.protocol_label(c)["spoken"] for c in cands])
                                       if cands else "I couldn't find that protocol.")
                return
            self._started(*self._request_start_locked(doc["id"], session), session, res)
            return
        if not draft:
            res["messages"].append("No draft open — import a protocol from Home.")
            return
        if name == "review_draft":
            self._navigate_locked(res, session, "draft_steps", None)
        elif name == "save_draft":
            self._saved(self._save_draft_locked(), session, res)
        elif name == "start_run":
            self._started(*self._start_draft_locked(session), session, res)
        elif name == "discard_draft":
            self.r.pop("draft", None)
            self._leave_draft()
            res["messages"].append(say("draft_discarded", session))
        else:  # show_step / open_source_page: a robot step of the draft, 1-based
            rows = proto.draft_rows(draft)["rows"]
            v = self.r["view"]["current"]
            here = v.get("target") if v.get("screen") in ("draft_step", "draft_page") else ""
            n = _num(args.get("step")) or _num(here) or next((r["n"] for r in rows if r["flagged"]), 1)
            if not rows or not 1 <= int(n) <= len(rows):
                res["messages"].append(f"There's no step {int(n)} to check." if rows
                                       else "This draft has no pipetting steps.")
                return
            screen = "draft_step" if name == "show_step" else "draft_page"
            draft["meta"]["review_at"] = int(n)
            self._push_view(screen, str(int(n)))
            res["messages"].append(self.screen_line(screen, str(int(n))))

    # --- hand off: a finished run -> approved as-run protocol -> robot code (docs/design/handoff.md) ---

    HANDOFF_SCREENS = ("final_protocol", "final_step", "final_page", "simulate", "export_files", "exported")
    HANDOFF_ACTIONS = ("approve_handoff", "save_handoff", "next_change", "retry_handoff", "export_handoff",
                       "share_result", "set_timing")
    #: The files a hand off shares (server/data/exports/<run>/).
    HANDOFF_FILES = ("protocol.py", "plate_map.csv")
    #: A share sheet left open longer than this (app gone) no longer holds the screen.
    SHARE_HOLD_S = 600
    #: Screens that need an approval that still matches the run record.
    _HANDOFF_APPROVED = ("simulate", "export_files", "exported")

    def _handoff_here(self) -> str:
        """The run a hand off screen shows ("" elsewhere)."""
        v = self.r["view"]["current"]
        return (v.get("target") or "").split(":", 1)[0] if v.get("screen") in self.HANDOFF_SCREENS else ""

    def _handoff_doc(self, run_id: str) -> dict | None:
        doc = runlog.get(run_id, self.root) if run_id else None
        return doc if doc and doc.get("status") == "finished" else None

    def _handoff_ok(self, doc: dict) -> bool:
        """The approval still matches the run record."""
        a = (doc.get("handoff") or {}).get("approved")
        return bool(a) and a.get("hash") == hand.record_hash(doc.get("record") or {})

    def _open_handoff(self, res: dict, session: dict | None, selector: str, screen: str = "final_protocol") -> None:
        """A finished run's hand off screen: this run (on screen or named),
        else the latest finished one. Private runs have none; a screen past
        ⑨0 needs a live approval, and ⑨b/⑨d a passed simulation."""
        run_id = selector or self._handoff_here() or (self._viewing_run() or {}).get("run_id") or ""
        doc = self._handoff_doc(run_id) if run_id else next(
            (d for d in self.all_run_docs() if d.get("status") == "finished"), None)
        if doc is None:
            res["messages"].append(say("handoff_none", session))
            return
        if ((doc.get("record") or {}).get("run") or {}).get("mode") == "private":
            res["messages"].append(say("handoff_private", session))
            return
        h = doc.get("handoff") or {}
        if screen in self._HANDOFF_APPROVED and not self._handoff_ok(doc):
            if h.get("approved"):
                res["messages"].append(say("handoff_changed", session))
            screen = "final_protocol"
        elif screen in ("export_files", "exported") and h.get("stage") != "passed":
            screen = "simulate"
        if screen in ("final_step", "final_page"):
            screen = "final_protocol"  # a step opens by its number (show_step)
        self._push_view(screen, doc["run_id"])
        res["messages"].append(self.screen_line(screen, doc["run_id"]))

    def _final_step(self, name: str, args: dict, session: dict, res: dict) -> None:
        """Step n of the final protocol, or its original page (the review's words)."""
        run_id = self._handoff_here()
        doc = self._handoff_doc(run_id)
        rows = hand.changes(doc["record"], (doc.get("handoff") or {}).get("timing"))["rows"] if doc else []
        v = self.r["view"]["current"]
        here = v.get("target", "").partition(":")[2] if v.get("screen") in ("final_step", "final_page") else ""
        n = _num(args.get("step")) or _num(here) or next((r["n"] for r in rows if r["flagged"]), 1)
        if not rows or not 1 <= int(n) <= len(rows):
            res["messages"].append(f"There's no step {int(n)} to check." if rows else "This run has no robot steps.")
            return
        screen = "final_step" if name == "show_step" else "final_page"
        if screen == "final_step" and rows[int(n) - 1]["flagged"]:
            self._mark_reviewed(doc, rows[int(n) - 1]["index"])  # opened: this change was seen
        self._push_view(screen, f"{run_id}:{int(n)}")
        res["messages"].append(self.screen_line(screen, f"{run_id}:{int(n)}"))

    def _reviewed(self, doc: dict) -> set:
        """Changed steps (plan index) already opened in the review, while the run record stays the same."""
        h = doc.get("handoff") or {}
        return set(h.get("reviewed") or []) if h.get("reviewed_hash") == hand.record_hash(doc["record"]) else set()

    def _mark_reviewed(self, doc: dict, index: int) -> None:
        h = doc.setdefault("handoff", {})
        h["reviewed"] = sorted(self._reviewed(doc) | {index})
        h["reviewed_hash"] = hand.record_hash(doc["record"])
        runlog.write_run(doc, self.root)

    def _review_state(self, doc: dict, rows: list[dict]) -> str:
        """none (nothing changed) | open (a change not opened yet) | done."""
        changed = {r["index"] for r in rows if r["flagged"]}
        return "none" if not changed else "done" if changed <= self._reviewed(doc) else "open"

    def _share_run(self) -> str:
        """The finished run whose files Share sends: a hand off screen's, or
        the run record open on Home (run_view: Share and its result only)."""
        v = self.r["view"]["current"]
        return self._handoff_here() or (v.get("target") or "" if v.get("screen") == "run_view" else "")

    def _handoff_action(self, name: str, args: dict, session: dict, res: dict) -> None:
        run_id = self._handoff_here()
        if not run_id and name in ("export_handoff", "share_result"):
            run_id = self._share_run()
        doc = self._handoff_doc(run_id)
        if doc is None:
            res["messages"].append(say("handoff_none", session))
            return
        h = doc.setdefault("handoff", {})
        timing = h.get("timing") or {}
        if name == "next_change":
            # the next changed step not opened yet (after this one first), else back to the list
            rows = hand.changes(doc["record"], timing)["rows"]
            v = self.r["view"]["current"]
            here = _num(v.get("target", "").partition(":")[2]) if v.get("screen") == "final_step" else 0
            seen = self._reviewed(doc)
            left = [r for r in rows if r["flagged"] and r["index"] not in seen]
            nxt = next((r for r in left if r["n"] > (here or 0)), left[0] if left else None)
            if nxt is None:
                self._push_view("final_protocol", run_id)
                res["messages"].append(self.screen_line("final_protocol", run_id))
            else:
                self._final_step("show_step", {"step": str(nxt["n"])}, session, res)
            return
        if name in ("approve_handoff", "save_handoff") and \
                self._review_state(doc, hand.changes(doc["record"], timing)["rows"]) == "open":
            res["messages"].append(say("handoff_review_first", session))
            return
        if name in ("approve_handoff", "save_handoff"):
            rec = doc["record"]
            steps, liqs = hand.place(rec, hand.as_run(rec, timing))
            why = hand.blockers(steps, liqs) or ([] if steps else [say("handoff_nothing", session)])
            if why:
                res["messages"].append(why[0])
                return
            title, n, date = self._handoff_names(doc)
            code = name == "approve_handoff"
            doc["handoff"] = {"timing": timing, "stage": "generating" if code else "saved", "attempt": 0,
                              "reviewed": h.get("reviewed") or [], "reviewed_hash": h.get("reviewed_hash"),
                              "approved": {
                "hash": hand.record_hash(rec), "ts": time.time(), "steps": steps, "liquids": liqs,
                "setup": hand.setup_code(steps, liqs, title, n, date),
                "seconds": hand.expected_seconds(rec, steps, timing)}}
            runlog.write_run(doc, self.root)
            if not code:  # the approved as-run protocol is kept with the run; no robot code
                self._push_view("home")
                res["messages"].append(say("handoff_saved", session))
                return
            self._push_view("simulate", run_id)
            res["messages"].append(self.screen_line("simulate", run_id))
            if self.spawn_handoff:
                self.spawn_handoff(run_id)
        elif name == "retry_handoff":
            if not self._handoff_ok(doc):
                self._open_handoff(res, session, run_id)
                return
            doc["handoff"] = {"timing": timing, "approved": h["approved"], "stage": "generating", "attempt": 0}
            runlog.write_run(doc, self.root)
            res["messages"].append(self.screen_line("simulate", run_id))
            if self.spawn_handoff:
                self.spawn_handoff(run_id)
        elif name == "export_handoff":
            if not self._handoff_ok(doc) or h.get("stage") != "passed":
                if self.r["view"]["current"].get("screen") == "run_view":
                    res["messages"].append(say("handoff_no_code", session))
                    return
                self._open_handoff(res, session, run_id, "export_files")
                return
            out = self.root / "exports" / run_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "protocol.py").write_text(h.get("code") or "")
            (out / "plate_map.csv").write_text(h.get("csv") or "")
            h["sharing"] = time.time()  # the iOS share sheet is up until the app says how it closed
            runlog.write_run(doc, self.root)
            stem = self._share_stem(doc)
            res["ui"].append({"action": "share", "files": [
                {"name": f"{stem}_{f}", "url": f"/handoff/{run_id}/files/{f}"} for f in self.HANDOFF_FILES]})
            res["messages"].append(say("handoff_share", session))
        elif name == "set_timing":
            # a wait that ran off plan: keep what ran (default) or use the plan; a new choice needs a new look
            v = self.r["view"]["current"]
            rows = hand.changes(doc["record"], timing)["rows"]
            n = _num(v.get("target", "").partition(":")[2]) if v.get("screen") == "final_step" else None
            row = rows[int(n) - 1] if n and 0 < int(n) <= len(rows) else None
            choice = str(args.get("choice") or "")
            if row is None or "timing" not in row or choice not in ("run", "plan"):
                res["messages"].append("That step has no timer to choose.")
                return
            h["timing"] = {**timing, str(row["index"]): choice}
            h.pop("approved", None)
            runlog.write_run(doc, self.root)
            res["messages"].append(say("handoff_timing_" + choice, session, step=row["n"]))
        elif name == "share_result":
            h.pop("sharing", None)
            if str(args.get("done") or "") == "yes":
                h["exported"] = time.time()
                runlog.write_run(doc, self.root)
                if self.r["view"]["current"].get("screen") == "run_view":
                    res["messages"].append(say("handoff_shared", session))  # shared from Home: the record stays
                    return
                self._push_view("exported", run_id)
                res["messages"].append(self.screen_line("exported", run_id))
            else:
                runlog.write_run(doc, self.root)
                res["messages"].append(say("handoff_not_shared", session))

    def _share_stem(self, doc: dict) -> str:
        """DrugA_Run2: the protocol's short title and the run's number, file-name safe."""
        title = labels.protocol_short(lib.get(doc.get("protocol_id"), self.root), doc.get("protocol_name") or "")
        words = re.findall(r"[A-Za-z0-9]+", title)
        short = "".join(w[:1].upper() + w[1:] for w in words)[:24] or "Protocol"
        return f"{short}_Run{self.run_label(doc).get('number') or 1}"

    def _sharing(self) -> bool:
        """The share sheet is up (⑨b, or a run record on Home): it holds the screen."""
        if self.r["view"]["current"].get("screen") not in ("export_files", "run_view"):
            return False
        doc = self._handoff_doc(self._share_run())
        t = ((doc or {}).get("handoff") or {}).get("sharing")
        return bool(t) and time.time() - t < self.SHARE_HOLD_S

    def handoff_file(self, run_id: str, name: str) -> Path | None:
        """A shared hand off file on disk, only by its known name under its run."""
        if name not in self.HANDOFF_FILES or self._handoff_doc(run_id) is None:
            return None
        base = (self.root / "exports").resolve()
        p = (base / run_id / name).resolve()
        return p if p.parent.parent == base and p.is_file() else None

    def _handoff_names(self, doc: dict) -> tuple[str, int, str]:
        """The protocol's title, the run's number and its day, for the files."""
        title = labels.protocol_short(lib.get(doc.get("protocol_id"), self.root), doc.get("protocol_name") or "")
        day = time.strftime("%Y-%m-%d", time.localtime(doc.get("ended_at") or doc.get("started_at") or time.time()))
        return title, self.run_label(doc).get("number") or 1, day

    def handoff_plan(self, run_id: str) -> dict | None:
        """The approval the loop writes code for (None: not approved now)."""
        with self._lock:
            doc = self._handoff_doc(run_id)
            return doc["handoff"]["approved"] if doc and self._handoff_ok(doc) else None

    def handoff_stage(self, run_id: str, stage: str, **state) -> None:
        """The loop's progress, kept on the run and shown (snapshot handoff)."""
        with self._lock:
            doc = self._handoff_doc(run_id)
            if doc is None or not self._handoff_ok(doc):
                return
            h = doc["handoff"]
            h.update({"stage": stage,
                      **{k: v for k, v in state.items() if k in ("code", "check", "error", "attempt", "tips")}})
            if stage == "passed":
                h["csv"] = hand.plate_csv(doc["record"], *self._handoff_names(doc), h["approved"].get("liquids"))
            runlog.write_run(doc, self.root)
            self._commit()

    def handoff_view(self, run_id: str) -> dict | None:
        """What the hand off screens draw (snapshot `handoff`)."""
        doc = self._handoff_doc(run_id)
        if doc is None:
            return None
        rec = doc.get("record") or {}
        h = doc.get("handoff") or {}
        p = lib.get(doc.get("protocol_id"), self.root) or {}
        ok = self._handoff_ok(doc)
        summary = hand.changes(rec, h.get("timing"))
        seen = self._reviewed(doc)
        for r in summary["rows"]:
            r["reviewed"] = r["index"] in seen  # a check once the review opened it
        summary["review"] = self._review_state(doc, summary["rows"])
        # the review screen's small line (pip_voice `final_subtitle`)
        summary["subtitle"] = say(f"final_subtitle_{summary['review']}", None, n=summary["changed"],
                                  s="" if summary["changed"] == 1 else "s")
        return {"run_id": doc["run_id"], "label": self.run_label(doc)["display"],
                "summary": summary, "approved": ok,
                "stage": h.get("stage") if ok else None, "attempt": h.get("attempt") or 0,
                "error": h.get("error") or "", "check": h.get("check"),
                "code": (h.get("code") or "") if ok else "", "csv": (h.get("csv") or "") if ok else "",
                "exported": h.get("exported") if ok else None,
                # what the virtual robot replays on ⑨: the approved steps (wells, µL, minutes)
                "steps": ((h.get("approved") or {}).get("steps") or []) if ok else [],
                # the reservoir: each liquid's well, volume and color (⑨b's line)
                "liquids": ((h.get("approved") or {}).get("liquids") or []) if ok else [],
                "meta": {"source": p.get("source_filename") or p.get("name") or "", "layout": {},
                         "origin": p.get("origin"), "original_id": p.get("original_id"),
                         "evidence": {"text": p.get("source_text") or ""}}}

    def _handoff_slots(self, screen: str, target: str) -> dict:
        def n_of(k, word):
            return f"{k} {word}{'' if k == 1 else 's'}"
        run_id, _, step = (target or "").partition(":")
        v = self.handoff_view(run_id)
        if v is None:
            return {}
        d = v["summary"]
        rows = d["rows"]
        out: dict = {"run": run_id, "of": len(rows),
                     # steps the robot acts in; the ones left with nothing to do said apart
                     "robot": n_of(d["acting"], "robot step") + (f", {n_of(d['empty'], 'step')} skipped"
                                                                 if d["empty"] else ""),
                     "changed": n_of(d["changed"], "change") if d["changed"] else "",
                     "doc": {"pdf": "PDF", "photo": "photo"}.get(v["meta"]["origin"] or "pdf", "text")}
        row = rows[int(step) - 1] if step.isdigit() and 0 < int(step) <= len(rows) else None
        if row:
            out.update({"step": row["n"], "reason": row["reason"], "page": row["page"], "said": row["said"]})
            if any(c["kind"] == "timer" for c in row["changes"]):
                out["timer"] = "changed"  # Keep as run / Use plan show
        else:
            # Review opens the first change not seen yet (all seen: the first one, greyed)
            first = next((r for r in rows if r["flagged"] and not r["reviewed"]), None) or \
                next((r for r in rows if r["flagged"]), None)
            if first:
                out["step"] = first["n"]
        if screen == "final_protocol":
            review = d["review"]
            out.update({"review": review, "reviewed": "yes" if review != "open" else "",
                        "line": say(f"handoff_review_{review}", None, robot=out["robot"], changed=out["changed"])})
        if screen == "simulate":
            stage = v["stage"] or "generating"
            chk = v["check"] or {}
            matched = sum(1 for s in chk.get("steps") or [] if s["ok"])
            out.update({"stage": stage, "line": say(f"handoff_{stage}", None, attempt=v["attempt"] + 1,
                                                    matched=matched, of=len(rows), error=v["error"])})
        return out

    def _fix_first(self, info) -> str:
        n = len((info or {}).get("errors") or []) if isinstance(info, dict) else 0
        return f"Fix {n} red item{'s' if n != 1 else ''} first." if n else "No draft open."

    def _started(self, ok: bool, info: dict, session: dict, res: dict) -> None:
        if ok:
            steps = (self.r.get("plan") or {}).get("steps") or []
            first = labels.step_say(steps[0]) if steps else ""
            run = self.run_label(self.active_run_doc())["display"]
            key = "run_started_paused" if info.get("paused") else "run_started"
            res["messages"].append(say(key, session, run=run, step=first, paused=info.get("paused") or ""))
        elif info.get("card"):
            res["cards"].append(info["card"])  # its question is the reply
        elif info.get("errors"):
            res["messages"].append(self._fix_first(info))
        elif info.get("line"):
            res["messages"].append(info["line"])
        else:
            res["messages"].append(f"I couldn't start that: {info.get('reason', 'no protocol')}.")

    # --- timers ---

    def _plan_timer_label(self, minutes, on_done, wells) -> tuple[str, str, str]:
        range_ = wells_label(wells) if wells else None
        steps = self.steps()
        step = step_abbrev(steps, self.r["step_index"], self.current_range())
        return timer_label(step, range_, minutes, on_done), step, range_ or ""

    def _running_dup(self, minutes, on_done, wells, now):
        planned = self._plan_timer_label(minutes, on_done, wells)[0]
        return next((t for t in self.r["timers"] if t["label"] == planned and not is_over(t, now)), None)

    def _single_timer(self, res, minutes, on_done, wells, source, now, force, session=None):
        if not force:
            dup = self._running_dup(minutes, on_done, wells, now)
            if dup is not None:
                rem = max(0.0, dup["minutes"] * 60 - (now - dup["started_at"]))
                scope = dup["step"] if not dup["wells"] else f"{dup['step']} · {dup['wells']}"
                self._card(res, "dup_timer",
                           f"A {scope} timer is already running ({fmt_clock(rem)} left). Start another?",
                           None, {"op": "set_timer", "minutes": minutes, "on_done": on_done, "wells": wells})
                return
        label, step, frozen = self._plan_timer_label(minutes, on_done, wells)
        self.r["timers"].append({
            "id": uuid.uuid4().hex[:8], "label": label, "minutes": minutes, "on_done": on_done,
            "started_at": now, "step": step, "wells": frozen, "warned": False, "notified": False,
            "step_index": self.r["step_index"], "planned": minutes,
        })
        if wells:
            self._tell(res, "timer_start", session, list(wells), minutes=fmt_minutes(minutes))
        else:
            res["messages"].append(say("timer_start", session, minutes=fmt_minutes(minutes),
                                       wells=frozen))
        res["info"]["timer_started"] = True
        if wells:
            res["ui"].append({"action": "flash", "wells": list(wells)})

    def _purge(self, now: float):
        gone = [t for t in self.r["timers"] if now - t["started_at"] >= t["minutes"] * 60 + 60]
        self._log_timers(gone, now)
        self.r["timers"] = [t for t in self.r["timers"] if t not in gone]

    def handoff_code(self, doc: dict) -> dict | None:
        """A finished run's robot code for Home (Run record, Code tab): the
        final protocol's steps, each with its changes and the lines of
        protocol.py it became ("Step N" comment to the next), and the setup
        block's lines. None until the code passed (or once the run changed)."""
        h = doc.get("handoff") or {}
        if not (h.get("code") and h.get("stage") == "passed" and self._handoff_ok(doc)):
            return None
        lines = h["code"].splitlines()
        starts = {int(m.group(1)): i + 1 for i, line in enumerate(lines)
                  if (m := re.search(r'protocol\.comment\(\s*[rRfF]?["\']{1,3}Step (\d+)\b', line))}  # any quotes Spark picks
        order = sorted(starts.values())
        end = len(lines)
        while end and not lines[end - 1].strip():
            end -= 1

        def span(n: int) -> list[int]:
            a = starts[n]
            b = next((x - 1 for x in order if x > a), end)
            return [a, b]
        setup = [next((i + 1 for i, line in enumerate(lines) if "# --- setup" in line), 1),
                 next((i + 1 for i, line in enumerate(lines) if "# --- steps ---" in line), order[0] - 1 if order else end)]
        rows = {r["index"]: r for r in hand.changes(doc["record"], h.get("timing"))["rows"]}
        steps = []
        for s in h["approved"]["steps"]:
            row = rows.get(s["index"]) or {}
            color = next((lq["color"] for lq in h["approved"].get("liquids") or [] if lq["liquid"] == s.get("liquid")),
                         hand.WAIT_COLOR)  # the step's liquid, as the Opentrons App shows it
            steps.append({"n": s["n"], "title": s["title"], "detail": row.get("run") or "", "color": color,
                          "plan": row.get("plan") or "", "changes": [c["text"] for c in row.get("changes") or []],
                          "lines": span(s["n"]) if s["n"] in starts else None})
        return {"code": h["code"], "setup": setup, "steps": steps, "exported": bool(h.get("exported"))}

    def _log_timers(self, timers: list[dict], now: float) -> None:
        """Timers leave `timers`; their actual time stays, in seconds
        (timer_log, hand off): a done timer ran its minutes, a cancelled one
        the seconds until it stopped."""
        for t in timers:
            over = is_over(t, now)
            self.r.setdefault("timer_log", []).append({
                "step_index": t.get("step_index"), "label": t.get("label"),
                "planned_s": float(t.get("planned", t.get("minutes")) or 0) * 60,
                "duration_s": float(t["minutes"]) * 60 if over else round(now - t["started_at"], 1),
                "started_at": t["started_at"], "ended_at": now,
                "status": "done" if over else "cancelled"})

    def _running_list(self) -> str:
        return ", ".join(t["label"] for t in self.r["timers"])

    def _cancel(self, res, label, source, now, session=None):
        if not self.r["timers"]:
            if label:
                res["messages"].append(say("not_found_timer", session, label=label)
                                       + " No timers running.")
            else:
                res["messages"].append("I couldn't find a timer to cancel. No timers running.")
            return
        hits = resolve_timers(self.r["timers"], label)
        if not hits:
            if label:
                res["messages"].append(say("not_found_timer", session, label=label)
                                       + f" Running: {self._running_list()}.")
            else:
                res["messages"].append(f"Which timer should I cancel? Running: {self._running_list()}.")
            return
        exact = bool(label) and self.r["timers"][hits[0]]["label"].lower() == label.lower()
        if len(hits) > 1 and not exact:
            for i in hits:
                self._card(res, "cancel_timer", f"Cancel {self.r['timers'][i]['label']}?", None,
                           {"op": "cancel_timer", "label": self.r["timers"][i]["label"]})
            return
        if all(is_over(self.r["timers"][i], now) for i in hits):
            res["messages"].append("That timer already finished.")
            return
        t = self.r["timers"][hits[0]]
        gone = {self.r["timers"][i]["label"] for i in hits}
        self._log_timers([t for t in self.r["timers"] if t["label"] in gone], now)
        self.r["timers"] = [t for t in self.r["timers"] if t["label"] not in gone]
        res["messages"].append(say("timer_cancel", session, wells=t["wells"]))

    def _extend(self, res, label, minutes, source, now):
        n = _num(minutes)
        n = 5 if n is None else n
        if not self.r["timers"]:
            if label:
                res["messages"].append(f"I couldn't find {timer_noun(label)} to extend. No timers running.")
            else:
                res["messages"].append("I couldn't find a timer to extend. No timers running.")
            return
        hits = resolve_timers(self.r["timers"], label)
        if not hits:
            if label:
                res["messages"].append(f"I couldn't find {timer_noun(label)} to extend. Running: {self._running_list()}.")
            else:
                res["messages"].append("Which timer should I extend? Running: {self._running_list()}.")
            return
        exact = bool(label) and self.r["timers"][hits[0]]["label"].lower() == label.lower()
        if len(hits) > 1 and not exact:
            for i in hits:
                self._card(res, "extend_timer",
                           f"Extend {self.r['timers'][i]['label']} by {fmt_minutes(n)} minutes?", None,
                           {"op": "extend_timer", "label": self.r["timers"][i]["label"], "minutes": n})
            return
        self.r["timers"][hits[0]]["minutes"] += n
        t = self.r["timers"][hits[0]]
        res["messages"].append(f"+{fmt_minutes(n)} min: {t['label']} ({fmt_minutes(t['minutes'])} min)")

    # --- confirm ---

    def confirm(self, card_id: str, ok: bool, source: str, session: dict, answer: str = ""):
        with self._lock:
            return self._confirm_locked(card_id, ok, source, session, answer)

    def _confirm_locked(self, card_id: str, ok: bool, source: str, session: dict, answer: str = ""):
        card = self.r["cards"].get(card_id)
        if card is None:
            return "That card is gone.", [], None
        choices = card_choices(card["kind"])
        if ok and choices and (answer or "yes") not in card_answers(card["kind"]):
            return card["question"], [card], None  # "yes" to "open it or review again?": ask again
        self.r["cards"].pop(card_id)
        if not ok:
            self._commit()
            self._refresh_card_output(card, ok, "Canceled.", session)
            return "Canceled.", [], card
        p = card.get("payload") or {}
        op = p.get("op")
        res: dict = {"messages": [], "cards": [], "ui": [],
                     "info": {"dropped": 0, "drop_reasons": []}}
        origin = ""
        if op == "mark_done":
            self._apply_call({"name": "mark_done", "args": {"wells": p.get("wells", []),
                                                            "step": p.get("step")}},
                             source, origin, "card", session, res, force=True)
        elif op == "exclude_well":
            self._apply_call({"name": "exclude_well", "args": {"well": p.get("well"), "reason": p.get("reason")}},
                             source, origin, "card", session, res, force=True)
        elif op == "record_redo":
            self._apply_call({"name": "record_redo", "args": {"wells": p.get("wells", []), "note": p.get("note")}},
                             source, origin, "card", session, res, force=True)
        elif op == "save_tip":
            self._apply_call({"name": "save_tip", "args": {"step_id": p.get("step"), "text": p.get("text")}},
                             source, origin, "card", session, res, force=True)
        elif op == "set_timer":
            self._single_timer(res, p.get("minutes"), p.get("on_done"), p.get("wells"), source, time.time(), True,
                               session)
        elif op == "cancel_timer":
            self._purge(time.time())
            self._cancel(res, p.get("label"), source, time.time(), session)
        elif op == "extend_timer":
            self._purge(time.time())
            self._extend(res, p.get("label"), p.get("minutes"), source, time.time())
        elif op == "next_step":
            left = [w for w in p.get("unmarked") or [] if w not in runlog.done_wells(self.r, self.r["step_index"] + 1)]
            if left and answer == "skip":
                # skipped, on the record: the plate and the run record show them so
                self.r.setdefault("skipped", []).extend(
                    {"step": self.r["step_index"] + 1, "well": w, "ts": time.time()} for w in left)
            elif left:
                self._apply_call({"name": "mark_done", "args": {"wells": left}}, source, origin, "card",
                                 session, res, force=True)
            self._apply_call({"name": "next_step", "args": {}}, source, origin, "card", session, res, force=True)
        elif op == "start_run":
            doc = lib.get(p.get("protocol_id"), self.root)
            if doc is None:
                res["messages"].append("That protocol is gone.")
            else:
                if self.r.get("run"):
                    self._finish_locked("finished")
                self._begin_locked(doc["plan"], doc["id"], doc.get("name"))
                res["messages"].append(f"Run started: {doc.get('name')}.")
        elif op == "new_run":
            # yes to "Run 1 is finished. Start a new run?": the usual start
            # (an active run still asks "finish it first?")
            self._started(*self._request_start_locked(p.get("protocol_id") or "", session), session, res)
        elif op == "known_import":
            if answer == "review":
                self._review_again(p.get("protocol_id") or "", session, res)
            else:
                doc = lib.get(p.get("protocol_id") or "", self.root) or {}
                res["messages"].append(say("already_saved", session, name=labels.protocol_short(doc)))
        elif op == "save_as":
            draft = self.r.get("draft")
            if not draft:
                res["messages"].append("No draft open.")
            else:
                draft["meta"]["save_as"] = answer
                if p.get("then") == "start_run":
                    self._started(*self._start_draft_locked(session), session, res)
                else:
                    self._saved(self._save_draft_locked(), session, res)
        elif op == "web_search":
            res["messages"].append(say("looking_up", session))
        elif op == "look":
            self._start_look(p.get("question") or "", res, session, confirmed=True)
            p["search_id"] = (res["info"].get("look") or {}).get("search_id")
        elif op == "end_run":
            if not self.r.get("run"):
                res["messages"].append("No active run.")
            else:
                name = (self.r.get("run") or {}).get("protocol_name") or "run"
                mode = (self.r.get("run") or {}).get("mode")
                doc = self._finish_locked("finished")
                res["messages"].append(f"Run finished: {name}.")
                if doc and mode != "private":  # hand off: the run as it went, for the robot
                    self._open_handoff(res, session, doc["run_id"])
        else:
            res["messages"].append("That card is gone.")
        self._commit()
        for a in res["ui"]:
            self._broadcast({"kind": "ui", **a})  # a card's yes can open the camera (photo question)
        message = " ".join(res["messages"]) or "Done."
        self._refresh_card_output(card, ok, message, session)
        return message, res["cards"], card

    @staticmethod
    def _refresh_card_output(card, ok: bool, message: str, session: dict) -> None:
        """Chain mode: bind the answered card's call to its outcome."""
        cid = (card.get("payload") or {}).get("_call_id") if card else None
        if not cid:
            return
        for p in session.get("pending_outputs") or []:
            if p["call_id"] == cid:
                p["output"] = json.dumps(
                    {"ok": ok, "message": message if ok else "declined by user"})

    # --- memos / recent / timers ---

    #: Readable kinds, derived from shared/schemas/screens.json (single source).
    READ_KINDS = SCREEN_READ_KINDS

    def filter_wells(self, filt: str, rec: dict | None = None) -> list[str]:
        """Well range, condition key/label, 'all', or blank (= current step)."""
        rec = self.r if rec is None else rec
        f = (filt or "").strip()
        if not f:
            return self.step_wells(rec)
        if f.lower() == "all":
            return ALL_WELLS
        for gname, g in (rec.get("plan") or {}).get("conditions", {}).items():
            if f.lower() in (gname.lower(), ((g or {}).get("label") or "").lower()):
                return sort_wells((g or {}).get("wells", []))
        return parse_wellref(f)

    def _read_scope(self):
        """(record, run_id|None): the viewing run, else the active record."""
        doc = self._viewing_run()
        if doc is None:
            return self.r, (self.r.get("run") or {}).get("run_id")
        return doc.get("record") or {}, doc.get("run_id")

    def read_record(self, kind: str, filt="", limit=10) -> dict:
        """One free read: library/run lists, or the viewing (active) run's record."""
        n = _num(limit)
        n = 10 if n is None else max(1, min(50, int(n)))
        f = filt.strip() if isinstance(filt, str) else ""
        if kind not in self.READ_KINDS:
            return {"error": f"unknown kind: {kind}. Use {', '.join(self.READ_KINDS)}."}
        if kind == "protocols":
            if f:
                hit, cands = labels.select_protocol(f, lib.all_docs(self.root))
                docs = [lib.summarize(x) for x in ([hit] if hit else cands)]
            else:
                docs = lib.list_all(self.root)[:n]
            return {"protocols": docs}
        if kind == "runs":
            docs = self.all_run_docs()
            if f:
                hit, cands = self.select_run(f)
                docs = [hit] if hit else cands
            return {"runs": self.run_summaries(docs[:n])}
        rec, rid = self._read_scope()
        rec = rec or {}
        if kind == "free_wells":
            near = f or "A1"
            if not parse_wellref(near):
                return {"error": f"{near} isn't on the plate."}
            return {"run": rid, "near": near.strip().upper(),
                    "free": self.find_free_wells(near, n)}
        if kind == "wells":
            return {"run": rid,
                    "wells": [self.get_well_data(w, rec) for w in self.filter_wells(f, rec)][:n]}
        if kind == "notes":
            ws = None if not f or f.lower() == "all" else set(self.filter_wells(f, rec))
            # compact for the model's second round: where, one line, which step
            out = [{"target": m["target"], "text": _one_line(m["text"]),
                    **({"step": m["step"]} if m.get("step") else {})}
                   for m in rec.get("notes", []) if ws is None or m["target"] in ws]
            return {"run": rid, "notes": out[-n:]}
        if kind == "timers":
            now = time.time()
            out = []
            for t in rec.get("timers", []):
                if f and f.lower() not in t["label"].lower():
                    continue
                left = round(t["minutes"] * 60 - (now - t["started_at"]), 1)
                out.append({"label": t["label"], "wells": t["wells"],
                            "minutes_left": max(0.0, left),
                            "status": "done" if is_over(t, now) else "active"})
            return {"run": rid, "timers": out[:n]}
        if kind == "steps":
            raw = (rec.get("plan") or {}).get("steps", [])
            idx = rec.get("step_index", 0)
            out = [{"i": i + 1, "name": s.get("text") or s.get("name", ""),
                    "kind": s.get("kind", ""), "current": i == idx}
                   for i, s in enumerate(raw)]
            if not f or f.lower() == "all":
                return {"run": rid, "steps": out[:n]}
            if f.lower() == "current":
                return {"run": rid, "steps": [o for o in out if o["current"]]}
            if f.isdigit() and 1 <= int(f) <= len(out):
                return {"run": rid, "steps": [out[int(f) - 1]]}
            return {"run": rid,
                    "steps": [o for o in out if f.lower() in o["name"].lower()][:n]}
        if kind == "history":
            now = time.time()
            out = [{"route": e["route"], "action": e["action"], "wells": e["wells"],
                    "ago": f"{int((now - e['ts']) // 60)}m"} for e in rec.get("recent", [])]
            return {"run": rid, "history": out[-n:]}
        if kind == "source":
            raw = (rec.get("plan") or {}).get("steps", [])
            idx = rec.get("step_index", 0)
            pick = None
            if not f or f.lower() == "current":
                pick = idx if 0 <= idx < len(raw) else None
            elif f.isdigit() and 1 <= int(f) <= len(raw):
                pick = int(f) - 1
            else:
                pick = next((i for i, s in enumerate(raw)
                             if f.lower() in (s.get("text") or "").lower()), None)
            if pick is None:
                return {"error": "I couldn't find that step."}
            s = raw[pick] or {}
            return {"run": rid, "step": pick + 1, "text": s.get("text") or "",
                    "quote": s.get("quote") or "", "quote_page": s.get("quote_page"),
                    "caution": s.get("caution")}
        return {"error": f"unknown kind: {kind}. Use {', '.join(self.READ_KINDS)}."}

    def describe_well(self, well: str) -> str:
        """One factual sentence from the record: status plus memos."""
        return describe_well_row(self.get_well_data(well))

    def get_well_data(self, well: str, rec: dict | None = None) -> dict:
        rec = self.r if rec is None else rec
        up = well.upper() if isinstance(well, str) else ""
        if up in (rec.get("excluded") or {}):
            status = "excluded"
        elif up in (rec.get("redo") or {}):
            status = "redo"
        elif up in runlog.done_wells(rec):
            status = "done"
        elif any(up in (g or {}).get("wells", [])
                 for g in (rec.get("plan") or {}).get("conditions", {}).values()):
            status = "planned"
        else:
            status = "empty"
        group = next((n for n, g in (rec.get("plan") or {}).get("conditions", {}).items()
                      if up in (g or {}).get("wells", [])), "")
        return {
            "well": up,
            "status": status,
            "group": group,
            "memos": [m["text"] for m in rec.get("notes", []) if m["target"] == up],
            "reason": (rec.get("excluded") or {}).get(up, ""),
            "redo_note": (rec.get("redo") or {}).get(up, ""),
            "done_steps": sorted(m["step"] for m in runlog.done_marks(rec) if m["well"] == up),
        }

    def edit_note_manual(self, note_id: str, text: str):
        with self._lock:
            return self._edit_note_manual_locked(note_id, text)

    def _edit_note_manual_locked(self, note_id: str, text: str):
        for memo in self.r["notes"]:
            if memo["id"] == note_id:
                memo["history"].append({"text": memo["text"], "ts": memo["ts"]})
                memo["text"] = (text or "").strip()
                memo["ts"] = time.time()
                self._commit()
                return f"Note updated ({memo['target']})."
        return "That note is gone."

    def record_recent(self, route: str, calls):
        now = time.time()
        for c in calls:
            self.r["recent"].append({
                "route": route, "action": c.get("name"), "wells": call_wells(c), "ts": now,
            })
        self.r["recent"] = self.r["recent"][-10:]

    def check_timers(self, now: float) -> list[dict]:
        with self._lock:
            return self._check_timers_locked(now)

    def _check_timers_locked(self, now: float) -> list[dict]:
        events = []
        self._purge(now)
        newly_done: list[dict] = []
        for t in self.r["timers"]:
            end = t["started_at"] + t["minutes"] * 60
            if not t["warned"] and t["minutes"] * 60 > 300 and now >= end - 300:
                t["warned"] = True
                msg = f"{t['label']}: 5 minutes left"
                events.append({"kind": "timer_warning", "label": t["label"], "minutes_left": 5,
                               "message": msg, "speak": speak_text(msg)})
            if not t["notified"] and now >= end:
                t["notified"] = True
                msg = say("timer_done", None, minutes=fmt_minutes(t["minutes"]),
                          wells=t["wells"] or t["step"])
                if t.get("on_done"):
                    msg += f" Next: {t['on_done']}."
                events.append({"kind": "timer_done", "label": t["label"], "message": msg,
                               "speak": speak_text(msg)})
                newly_done.append(t)
        if newly_done and self._left_now() is None:
            # a step with no target wells (incubate) is done when its timer is
            card = self._offer_next_locked({"cards": []}, None)
            if card and events:
                last = next(e for e in reversed(events) if e["kind"] == "timer_done")
                last["message"] = f"{last['message']} {card['question']}"
                last["speak"] = speak_text(last["message"])
        elif newly_done:
            self._suggest_next_locked(newly_done, now)
        if events:
            self._commit()
        for ev in events:
            self._broadcast(ev)
        return events

    def _suggest_entry_timer(self, res: dict, session: dict):
        """Entering a timed step proposes its timer; nothing otherwise."""
        steps = self.r["plan"]["steps"]
        idx = self.r["step_index"]
        if idx >= len(steps):
            return
        s = steps[idx]
        m = s.get("minutes")
        if (s.get("kind") or "") not in proto.TIMED_KINDS or not isinstance(m, (int, float)):
            return
        wells = list(s.get("wells") or [])
        self._card(res, "timer_suggest",
                   say("timer_suggest", session, minutes=fmt_minutes(m)),
                   None, {"op": "set_timer", "minutes": m, "on_done": None,
                          "wells": wells or None})

    def _left_now(self) -> set | None:
        """The current step's targets not done or excluded; None when the step
        has no target wells (incubate, wait)."""
        steps = (self.r.get("plan") or {}).get("steps") or []
        idx = self.r.get("step_index", 0)
        if not self.r.get("run") or idx >= len(steps):
            return None
        targets = set((steps[idx] or {}).get("wells") or [])
        if not targets:
            return None
        return targets - runlog.done_wells(self.r, idx + 1) - set(self.r.get("excluded") or {})

    def _offer_next_locked(self, res: dict, session: dict | None) -> dict | None:
        """The step is done: Milo asks to move on ("Step 2 done. Move to step
        3?"), or on the last step to finish the run. One card per question."""
        steps = (self.r.get("plan") or {}).get("steps") or []
        idx = self.r.get("step_index", 0)
        if not self.r.get("run") or not steps:
            return None
        if idx >= len(steps) - 1:
            return self._card(res, "end_run", say("all_steps_done", session), None,
                              {"op": "end_run", "run_id": (self.r.get("run") or {}).get("run_id")})
        return self._card(res, "next_step", say("step_done", session, done=idx + 1, step=idx + 2),
                          steps[idx + 1].get("text") if isinstance(steps[idx + 1], dict) else None,
                          {"op": "next_step", "target": idx + 2})

    def _suggest_next_locked(self, done_timers: list[dict], now: float):
        """Timer-expiry step suggestion; merges into the same-key step card."""
        if self.r["step_index"] >= len(self.steps()) - 1:
            return
        target = self.r["step_index"] + 2
        fresh = list(dict.fromkeys(w for t in done_timers for w in (parse_wellref(t["wells"]) if t["wells"] else [])))
        old = next((c for c in self.r["cards"].values()
                    if self._card_key(c.get("payload")) == ("next_step", target)), None)
        wells, count = fresh, len(done_timers)
        if old is not None:
            wells = list(dict.fromkeys((old["payload"].get("wells") or []) + fresh))
            count = old["payload"].get("count", 1) + len(done_timers)
        if old is None and len(done_timers) == 1:
            detail = timer_done_message(done_timers[0])
        else:
            detail = self._suggest_detail(count, wells)
        self._card({"cards": []}, "suggest_next", say("step_move", None, step=target),
                   detail, {"op": "next_step", "target": target,
                            "wells": wells, "count": count})

    def _suggest_detail(self, n: int, wells: list[str]) -> str:
        rng = join_wells_ranges(wells)
        return f"{n} timers done: {rng}." if rng else f"{n} timers done."

    def _drop_step_cards_locked(self):
        for cid in [cid for cid, c in self.r["cards"].items() if c["kind"] in ("suggest_next", "next_step")]:
            del self.r["cards"][cid]
