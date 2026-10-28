"""Single source of truth for the bench record.

Owns the plan, wells, memos, and timers. Clients subscribe to snapshots
and render only; every mutation persists to server/data/record.json.
Apply rules mirror the app's old LabState behavior message-for-message.
"""

from __future__ import annotations

import json
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
from server.screens import SCREENS, canonical, parent_of, title_of
from server.tone import say
from server import protocol as proto

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
        self._listeners: list = []
        self._lock = threading.Lock()
        self._load_or_seed()

    # --- persistence / broadcast ---

    def _load_or_seed(self):
        if self.path.exists():
            self.r = json.loads(self.path.read_text())
            self.r.pop("plans", None)  # legacy: plans folded into the card queue
            for m in self.r.get("notes", []):
                m.setdefault("raw", m.pop("transcript", None) or m.get("text", ""))
            self.r["plan"] = migrate_plan(self.r.get("plan") or {})
            return
        plan = {"conditions": {}, "steps": []}
        current = 1
        if SEED_PATH.exists():
            seed = json.loads(SEED_PATH.read_text())
            plan = {"conditions": seed.get("conditions", {}), "steps": seed.get("steps", []),
                    "backups": seed.get("backups", [])}
            current = seed.get("current_step", 1)
        self.r = self._fresh_record(migrate_plan(plan), current)
        self._save()

    @staticmethod
    def _fresh_record(plan: dict, current: int = 1) -> dict:
        steps = plan.get("steps", [])
        return {
            "version": 0,
            "plan": plan,
            "step_index": max(min(current - 1, len(steps) - 1), 0) if steps else 0,
            "done": [],
            "excluded": {},
            "redo": {},
            "notes": [],
            "tips": [],
            "timers": [],
            "stamps": {},
            "recent": [],
            "cards": {},
        }

    def save_draft(self, draft: dict) -> None:
        with self._lock:
            self.r["draft"] = draft
            self._commit()

    def clear_draft(self) -> None:
        with self._lock:
            self.r.pop("draft", None)
            self._commit()

    def start_run(self):
        """Archive the current record, reset on the draft plan. (ok, info)."""
        with self._lock:
            draft = self.r.get("draft")
            if not draft:
                return False, {"reason": "no draft open"}
            proto.recompute_draft(draft)
            if draft["errors"]:
                self._commit()
                return False, {"errors": draft["errors"]}
            runs = self.path.parent / "runs"
            runs.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            (runs / f"run-{stamp}.json").write_text(json.dumps(self.r, ensure_ascii=False))
            plan = proto.build_start_plan(draft["plan"])
            self.r = self._fresh_record(plan)
            self._save()
            self._commit()
            return True, {"conditions": len(plan["conditions"]), "steps": len(plan["steps"]),
                          "review": len(draft["review"])}

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
        return json.loads(json.dumps(self.r))

    # --- plan helpers ---

    def steps(self) -> list[str]:
        names = [s.get("text") or s.get("name", "") for s in self.r["plan"]["steps"]]
        return names or list(DEFAULT_STEPS)

    def current_range(self) -> str | None:
        steps = self.r["plan"]["steps"]
        if steps and self.r["step_index"] < len(steps):
            return wells_label(steps[self.r["step_index"]].get("wells", []))
        return None

    def current_planned(self) -> set[str]:
        steps = self.r["plan"]["steps"]
        if steps and self.r["step_index"] < len(steps):
            return set(steps[self.r["step_index"]].get("wells", []))
        return set()

    def unplanned(self, wells) -> list[str]:
        if not self.r["plan"]["steps"]:
            return []
        planned = self.current_planned()
        return [w for w in wells if w not in planned]

    def step_wells(self) -> list[str]:
        return sort_wells(self.current_planned())

    def planned_wells(self) -> set[str]:
        """Every well named anywhere in the plan (all steps + all conditions)."""
        out: set[str] = set()
        for s in self.r["plan"]["steps"]:
            out |= set(s.get("wells", []))
        for g in self.r["plan"]["conditions"].values():
            out |= set(g.get("wells", []))
        return out

    def remaining_wells(self) -> list[str]:
        done = set(self.r["done"])
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
        return (set(self.r["done"]) | set(self.r["excluded"])
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
        if well in self.r["done"]:
            return "already done"
        return "has a running timer"

    def prompt_state(self, session: dict) -> dict:
        steps = self.steps()
        idx = self.r["step_index"]
        raw = self.r["plan"]["steps"]
        st = {
            "step": idx,
            "total": len(steps),
            "step_name": steps[idx] if idx < len(steps) else "",
            "next_step": steps[idx + 1] if idx + 1 < len(steps) else "",
            "caution": (raw[idx].get("caution") or "") if idx < len(raw) else "",
            "next_caution": (raw[idx + 1].get("caution") or "") if idx + 1 < len(raw) else "",
            "plan_summary": f"Step {idx + 1}/{len(steps)}: {steps[idx]}" if idx < len(steps) else "",
            "done": sorted(self.r["done"]),
            "excluded": sorted(self.r["excluded"]),
            "step_wells": self.step_wells(),
            "remaining": self.remaining_wells(),
            "timers": [{"label": t["label"], "minutes": t["minutes"]} for t in self.r["timers"]],
            "recent": self.r["recent"][-5:],
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
        usable = set(self.r["done"]) - set(self.r["excluded"])
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
        st = {
            "step": f"{idx + 1}/{len(steps)} {name}",
            "plan_times": pt,
            "conditions": "; ".join(conds),
            "step_wells": join_wells_ranges(self.step_wells()),
            "remaining": join_wells_ranges(self.remaining_wells()),
            "done": join_wells_ranges(self.r["done"]),
            "excluded": join_wells_ranges(self.r["excluded"]),
            "timers": timers,
            "recent": self.r["recent"][-5:],
            "current_screen": session.get("screen", "main"),
            "selected_well": session.get("selected_well", ""),
            "open_sheet": session.get("open_sheet", ""),
            "pending_cards": len(self.r["cards"]),
            "draft_open": bool(self.r.get("draft")),
        }
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
            f"Done: {sorted(self.r['done'])}. Excluded: {sorted(self.r['excluded'])}. "
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
            subject = tuple(sorted(p.get("wells") or []))
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
        else:
            subject = None
        return (op, subject if subject != [] else None)

    def _card(self, res: dict, kind: str, question: str, detail, payload: dict) -> dict:
        key = self._card_key(payload)
        for cid in [cid for cid, c in self.r["cards"].items()
                    if self._card_key(c.get("payload")) == key]:
            del self.r["cards"][cid]
        card = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "question": question,
            "detail": detail,
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
        for c in calls:
            m0, c0 = len(res["messages"]), len(res["cards"])
            self._apply_call(c, source, origin, route, session, res, force=False,
                             evidence=evidence)
            for card in res["cards"][c0:]:
                card["payload"]["_call_id"] = c.get("call_id")
            res["outcomes"].append({"messages": res["messages"][m0:], "cards": res["cards"][c0:]})
        if calls:
            self.record_recent(route, calls)
        res["info"]["ui"] = res["ui"]
        self._commit()
        for a in res["ui"]:
            self._broadcast({"kind": "ui", **a})
        return res

    def _apply_call(self, call, source, origin, route, session, res, force: bool,
                    evidence=None):
        name = call.get("name")
        args = call.get("args") or {}
        now = time.time()
        if evidence is None:
            evidence = build_evidence(origin, "", frozenset(), "")
        if name == "mark_done":
            wells = [w for w in (args.get("wells") or []) if isinstance(w, str)]
            bad = [w for w in wells if not is_well(w)]
            good = [w for w in wells if is_well(w)]
            if not force and good and self.unplanned(good):
                self._card(res, "catch",
                           say("catch_off_plan", session,
                               wells=join_wells_ranges(self.unplanned(good))),
                           None, {"op": "mark_done", "wells": good})
                return
            msg = ""
            if good:
                self.r["done"] = sorted(set(self.r["done"]) | set(good))
                self._stamp(good, source, now)
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
                "wells": parse_wellref(target),
                "history": [],
            }
            self.r["notes"].append(memo)
            self._stamp([target], source, now)
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
        elif name == "manage_timers":
            self._purge(now)
            action = str(args.get("action") or "").lower()
            label = args.get("label")
            label = label.strip() if isinstance(label, str) else None
            if action == "list":
                if not self.r["timers"]:
                    res["messages"].append("No running timers")
                else:
                    bits = [f"{t['label']} {fmt_minutes(t['minutes'])} min" for t in self.r["timers"]]
                    res["messages"].append("Timers: " + ", ".join(bits))
            elif action == "cancel":
                self._cancel(res, label, source, now, session)
            elif action == "extend":
                self._extend(res, label, args.get("minutes"), source, now)
            else:
                res["messages"].append(f"Unknown timer action: {args.get('action')}")
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
        elif name == "next_step":
            steps = self.steps()
            if self.r["step_index"] >= len(steps) - 1:
                res["messages"].append("Already at the last step")
            elif not force:
                nxt = self.r["step_index"] + 1
                self._card(res, "next_step", say("step_move", session, step=nxt + 1),
                           steps[nxt], {"op": "next_step", "target": nxt + 1})
            else:
                self.r["step_index"] += 1
                self._drop_step_cards_locked()
                res["messages"].append(f"Next: {steps[self.r['step_index']]}")
                self._suggest_entry_timer(res, session)
        elif name == "show":
            view = args.get("view", "?")
            res["messages"].append(f"Viewing: {view} {args.get('target')}" if args.get("target") else f"Viewing: {view}")
        elif name == "navigate":
            raw = args.get("screen")
            screen = canonical(raw) if isinstance(raw, str) else None
            if screen is None:
                res["messages"].append(
                    "I couldn't open that: no screen given."
                    if not isinstance(raw, str) or not raw.strip()
                    else f"I couldn't open the {raw.strip()} screen: no such screen.")
                return
            res["ui"].append({"action": "navigate", "screen": screen})
            res["messages"].append(f"Showing {title_of(screen)}.")
        elif name == "open":
            raw = args.get("target")
            t = raw.strip() if isinstance(raw, str) else ""
            if t and is_well(t):
                res["ui"].append({"action": "open", "target": t.upper(), "sheet": "well_card"})
                res["messages"].append(f"Opened {t.upper()}.")
                return
            screen = canonical(t)
            if screen is not None:
                res["ui"].append({"action": "navigate", "screen": screen})
                res["messages"].append(f"Showing {title_of(screen)}.")
                return
            res["messages"].append(
                "I couldn't open that: no target given." if not t
                else f"I couldn't open {t}: not a plate well or screen.")
        elif name == "back":
            here = session.get("screen", "main")
            if here not in SCREENS:
                here = "main"
            parent = parent_of(here)
            if parent == here:
                res["messages"].append("Already on the main screen.")
                return
            res["ui"].append({"action": "back", "screen": parent})
            res["messages"].append(f"Back to {title_of(parent)}.")
        elif name == "make_robot":
            res["messages"].append("Building robot protocol…")
            res["info"]["robot"] = True
        elif name == "read_record":
            if args.get("kind") not in self.READ_KINDS:
                res["messages"].append("I couldn't read that: unknown section.")
            # Success is silent; the data rides the call outcome.
        elif name == "find_free_wells":
            near = args.get("near")
            if not isinstance(near, str) or not parse_wellref(near):
                res["messages"].append(invalid_message([near], session))
        elif name == "answer":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({"call": name, "args": args, "reason": "missing text"})
            else:
                res["messages"].append(text.strip()[:200])
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
        else:
            res["info"]["dropped"] += 1
            res["info"]["drop_reasons"].append({"call": name or "?", "args": args, "reason": "unknown tool"})

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
        })
        res["messages"].append(say("timer_start", session, minutes=fmt_minutes(minutes),
                                   wells=frozen))
        res["info"]["timer_started"] = True
        if wells:
            res["ui"].append({"action": "flash", "wells": list(wells)})

    def _purge(self, now: float):
        self.r["timers"] = [t for t in self.r["timers"] if now - t["started_at"] < t["minutes"] * 60 + 60]

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

    def confirm(self, card_id: str, ok: bool, source: str, session: dict):
        with self._lock:
            return self._confirm_locked(card_id, ok, source, session)

    def _confirm_locked(self, card_id: str, ok: bool, source: str, session: dict):
        card = self.r["cards"].pop(card_id, None)
        if card is None:
            return "That card is gone.", [], None
        if not ok:
            self._commit()
            return "Canceled.", [], card
        p = card.get("payload") or {}
        op = p.get("op")
        res: dict = {"messages": [], "cards": [], "ui": [],
                     "info": {"dropped": 0, "drop_reasons": []}}
        origin = ""
        if op == "mark_done":
            self._apply_call({"name": "mark_done", "args": {"wells": p.get("wells", [])}},
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
            self._apply_call({"name": "next_step", "args": {}}, source, origin, "card", session, res, force=True)
        else:
            res["messages"].append("That card is gone.")
        self._commit()
        return " ".join(res["messages"]) or "Done.", res["cards"], card

    # --- memos / recent / timers ---

    READ_KINDS = ("wells", "notes", "timers", "steps", "history")

    def filter_wells(self, filt: str) -> list[str]:
        """Well range, condition key/label, 'all', or blank (= current step)."""
        f = (filt or "").strip()
        if not f:
            return self.step_wells()
        if f.lower() == "all":
            return ALL_WELLS
        for gname, g in self.r["plan"]["conditions"].items():
            if f.lower() in (gname.lower(), (g.get("label") or "").lower()):
                return sort_wells(g.get("wells", []))
        return parse_wellref(f)

    def read_record(self, kind: str, filt="", limit=10) -> dict:
        """One free read over the record. Lenient: bad filters yield []."""
        n = _num(limit)
        n = 10 if n is None else max(1, min(50, int(n)))
        f = filt.strip() if isinstance(filt, str) else ""
        if kind == "wells":
            return {"wells": [self.get_well_data(w) for w in self.filter_wells(f)][:n]}
        if kind == "notes":
            ws = None if not f or f.lower() == "all" else set(self.filter_wells(f))
            out = [{"target": m["target"], "text": m["text"]}
                   for m in self.r["notes"] if ws is None or m["target"] in ws]
            return {"notes": out[-n:]}
        if kind == "timers":
            now = time.time()
            out = []
            for t in self.r["timers"]:
                if f and f.lower() not in t["label"].lower():
                    continue
                left = round(t["minutes"] * 60 - (now - t["started_at"]), 1)
                out.append({"label": t["label"], "wells": t["wells"],
                            "minutes_left": max(0.0, left),
                            "status": "done" if is_over(t, now) else "active"})
            return {"timers": out[:n]}
        if kind == "steps":
            raw = self.r["plan"]["steps"]
            idx = self.r["step_index"]
            out = [{"i": i + 1, "name": s.get("text") or s.get("name", ""),
                    "kind": s.get("kind", ""), "current": i == idx}
                   for i, s in enumerate(raw)]
            if not f or f.lower() == "all":
                return {"steps": out[:n]}
            if f.lower() == "current":
                return {"steps": [o for o in out if o["current"]]}
            if f.isdigit() and 1 <= int(f) <= len(out):
                return {"steps": [out[int(f) - 1]]}
            return {"steps": [o for o in out if f.lower() in o["name"].lower()][:n]}
        if kind == "history":
            now = time.time()
            out = [{"route": e["route"], "action": e["action"], "wells": e["wells"],
                    "ago": f"{int((now - e['ts']) // 60)}m"} for e in self.r["recent"]]
            return {"history": out[-n:]}
        return {"error": f"unknown kind: {kind}. Use {', '.join(self.READ_KINDS)}."}

    def describe_well(self, well: str) -> str:
        """One factual sentence from the record: status plus memos."""
        d = self.get_well_data(well)
        status = {"redo": "marked for redo"}.get(d["status"], d["status"])
        s = f"{d['well']} is {status}"
        extra = d["reason"] or d["redo_note"]
        if extra:
            s += f" ({extra})"
        if d["memos"]:
            s += ". Memos: " + "; ".join(d["memos"])
        return s + "."

    def get_well_data(self, well: str) -> dict:
        up = well.upper() if isinstance(well, str) else ""
        if up in self.r["excluded"]:
            status = "excluded"
        elif up in self.r["redo"]:
            status = "redo"
        elif up in self.r["done"]:
            status = "done"
        elif any(up in g.get("wells", []) for g in self.r["plan"]["conditions"].values()):
            status = "planned"
        else:
            status = "empty"
        group = next((n for n, g in self.r["plan"]["conditions"].items() if up in g.get("wells", [])), "")
        return {
            "well": up,
            "status": status,
            "group": group,
            "memos": [m["text"] for m in self.r["notes"] if m["target"] == up],
            "reason": self.r["excluded"].get(up, ""),
            "redo_note": self.r["redo"].get(up, ""),
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
        if newly_done:
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
