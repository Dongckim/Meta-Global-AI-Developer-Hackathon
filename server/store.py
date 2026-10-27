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
    call_wells,
    find_durations,
    join_wells_ranges,
    parse_wells,
    verbatim_span,
    wells_label,
    where_am_i_reply,
)

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
    return isinstance(w, str) and parse_wells(w.upper()) == [w.upper()]


def invalid_message(bad) -> str:
    bad = list(bad)
    return f"I couldn't record {', '.join(bad)}: not {'a plate well' if len(bad) == 1 else 'plate wells'}."


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
            return
        plan = {"groups": {}, "steps": []}
        current = 1
        if SEED_PATH.exists():
            seed = json.loads(SEED_PATH.read_text())
            plan = {"groups": seed.get("groups", {}), "steps": seed.get("steps", []),
                    "backups": seed.get("backups", [])}
            current = seed.get("current_step", 1)
        self.r = {
            "version": 0,
            "plan": plan,
            "step_index": max(min(current - 1, len(plan["steps"]) - 1), 0) if plan["steps"] else 0,
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
        self._save()

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
        names = [s.get("name", "") for s in self.r["plan"]["steps"]]
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
        """Every well named anywhere in the plan (all steps + all groups)."""
        out: set[str] = set()
        for s in self.r["plan"]["steps"]:
            out |= set(s.get("wells", []))
        for g in self.r["plan"]["groups"].values():
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
                out |= set(parse_wells(t["wells"] or ""))
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
        nu = near.upper() if isinstance(near, str) else ""
        if is_well(nu):
            nr, nc = "ABCDEFGH".index(nu[0]), int(nu[1:])

            def key(w: str):
                r, c = "ABCDEFGH".index(w[0]), int(w[1:])
                return (abs(r - nr), 0 if c >= nc else 1, abs(c - nc), r, c)

            rest.sort(key=key)
        else:
            rest.sort(key=lambda w: ("ABCDEFGH".index(w[0]), int(w[1:])))
        return (out + rest)[:n]

    def _affinity_group(self, well: str) -> str | None:
        """Group sharing the well's row (most wells wins, name breaks ties)."""
        row = well[0]
        best: str | None = None
        best_n = 0
        for gname in sorted(self.r["plan"]["groups"]):
            n = sum(1 for w in self.r["plan"]["groups"][gname].get("wells", [])
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
        st = {
            "step": idx,
            "total": len(steps),
            "step_name": steps[idx] if idx < len(steps) else "",
            "plan_summary": f"Step {idx + 1}/{len(steps)}: {steps[idx]}" if idx < len(steps) else "",
            "done": sorted(self.r["done"]),
            "excluded": sorted(self.r["excluded"]),
            "step_wells": self.step_wells(),
            "remaining": self.remaining_wells(),
            "timers": [{"label": t["label"], "minutes": t["minutes"]} for t in self.r["timers"]],
            "recent": self.r["recent"][-5:],
        }
        if session.get("pending"):
            st["pending_question"] = session["pending"]
        return st

    def plan_times(self) -> dict[int, list]:
        """step_index -> durations parsed from step names."""
        out: dict[int, list] = {}
        for i, s in enumerate(self.r["plan"]["steps"]):
            ds = sorted(find_durations(s.get("name", "")))
            if ds:
                out[i] = ds
        return out

    def model_state(self, session: dict) -> dict:
        """Compact state for the model: summaries only, no memo bodies or transcripts."""
        steps = self.steps()
        idx = self.r["step_index"]
        name = steps[idx] if idx < len(steps) else ""
        pt = "; ".join(f"{i + 1}: {', '.join(f'{d}m' for d in ds)}"
                       for i, ds in sorted(self.plan_times().items()))
        usable = set(self.r["done"]) - set(self.r["excluded"])
        groups = []
        for gname in sorted(self.r["plan"]["groups"]):
            g = self.r["plan"]["groups"][gname]
            have = len(usable & set(g.get("wells", [])))
            groups.append(f"{gname} {have}/{g.get('n', '?')}")
        now = time.time()
        timers = []
        for t in self.r["timers"]:
            left = int((t["started_at"] + t["minutes"] * 60 - now) // 60)
            timers.append(f"{t['label']} ({left}m left)" if left > 0 else f"{t['label']} (done)")
        st = {
            "step": f"{idx + 1}/{len(steps)} {name}",
            "plan_times": pt,
            "groups": "; ".join(groups),
            "step_wells": join_wells_ranges(self.step_wells()),
            "remaining": join_wells_ranges(self.remaining_wells()),
            "done": join_wells_ranges(self.r["done"]),
            "excluded": join_wells_ranges(self.r["excluded"]),
            "timers": timers,
            "recent": self.r["recent"][-5:],
        }
        if session.get("pending"):
            st["pending_question"] = session["pending"]
        return st

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

    def _card(self, res: dict, kind: str, question: str, detail, payload: dict) -> dict:
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

    def apply_calls(self, calls, source: str, origin: str, route: str, session: dict) -> dict:
        with self._lock:
            return self._apply_calls_locked(calls, source, origin, route, session)

    def _apply_calls_locked(self, calls, source: str, origin: str, route: str, session: dict) -> dict:
        res: dict = {"messages": [], "cards": [], "info": {"dropped": 0, "drop_reasons": []},
                     "outcomes": []}
        for c in calls:
            m0, c0 = len(res["messages"]), len(res["cards"])
            self._apply_call(c, source, origin, route, session, res, force=False)
            for card in res["cards"][c0:]:
                card["payload"]["_call_id"] = c.get("call_id")
            res["outcomes"].append({"messages": res["messages"][m0:], "cards": res["cards"][c0:]})
        if calls:
            self.record_recent(route, calls)
        self._commit()
        return res

    def _apply_call(self, call, source, origin, route, session, res, force: bool):
        name = call.get("name")
        args = call.get("args") or {}
        now = time.time()
        if name == "mark_done":
            wells = [w for w in (args.get("wells") or []) if isinstance(w, str)]
            bad = [w for w in wells if not is_well(w)]
            good = [w for w in wells if is_well(w)]
            if not force and good and self.unplanned(good):
                bad_plan = self.unplanned(good)
                q = (
                    f"{bad_plan[0]} is not in the step {self.r['step_index'] + 1} plan. Record it anyway?"
                    if len(bad_plan) == 1
                    else f"{', '.join(bad_plan)} are not in the step {self.r['step_index'] + 1} plan. Record them anyway?"
                )
                self._card(res, "catch", q, None, {"op": "mark_done", "wells": good})
                return
            msg = ""
            if good:
                self.r["done"] = sorted(set(self.r["done"]) | set(good))
                self._stamp(good, source, now)
                msg = f"✓ {', '.join(good)} done"
            if bad:
                msg = invalid_message(bad) if not msg else msg + ". " + invalid_message(bad)
            if msg:
                res["messages"].append(msg)
        elif name == "exclude_well":
            well = args.get("well")
            reason = args.get("reason")
            if not isinstance(well, str) or not is_well(well):
                res["messages"].append(invalid_message([well]))
                return
            if not force:
                self._card(res, "confirm_exclude", f"Exclude {well}?", reason,
                           {"op": "exclude_well", "well": well, "reason": reason})
                return
            self.r["excluded"][well] = reason or ""
            self._stamp([well], source, now)
            res["messages"].append(f"{well} excluded ({trimmed(reason)})" if reason else f"{well} excluded")
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
                        gw = self.r["plan"]["groups"][g].get("wells", [])
                        if w not in gw:
                            self.r["plan"]["groups"][g]["wells"] = sort_wells(set(gw) | {w})
                self._stamp(good, source, now)
                msg = f"{', '.join(good)} marked for redo ({trimmed(note)})" if note else f"{', '.join(good)} marked for redo"
            if bad:
                msg = invalid_message(bad) if not msg else msg + ". " + invalid_message(bad)
            if msg:
                res["messages"].append(msg)
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
            memo = {
                "id": uuid.uuid4().hex[:8],
                "target": target,
                "text": verbatim_span(origin, text),
                "transcript": origin,
                "ts": now,
                "route": route,
                "wells": parse_wells(target),
                "history": [],
            }
            self.r["notes"].append(memo)
            self._stamp([target], source, now)
            res["messages"].append(f"Note saved ({target})")
            res["info"]["saved_memo"] = {"id": memo["id"], "target": target, "text": memo["text"]}
        elif name == "edit_note":
            text = args.get("text")
            if not self.r["notes"]:
                res["messages"].append("No notes yet.")
                return
            memo = self.r["notes"][-1]
            memo["history"].append({"text": memo["text"], "ts": memo["ts"]})
            memo["text"] = verbatim_span(origin, text if isinstance(text, str) else "")
            memo["ts"] = now
            res["messages"].append("Note updated.")
            res["info"]["edited"] = {"id": memo["id"], "target": memo["target"]}
        elif name == "save_tip":
            step, text = args.get("step_id"), args.get("text")
            if not isinstance(step, str) or not isinstance(text, str):
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({"call": name, "args": args, "reason": "missing text"})
                return
            pending = session.get("pending") or {}
            pool = pending.get("candidate", "") if pending.get("kind") == "attach" else origin
            final = verbatim_span(pool, text)
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
            ws = explicit or (self.step_wells() if args.get("each") is True else [])
            if len(ws) > 1:
                for w in ws:
                    self._single_timer(res, minutes, on_done, [w], source, now, force)
            else:
                self._single_timer(res, minutes, on_done, explicit or ws or None, source, now, force)
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
                self._cancel(res, label, source, now)
            elif action == "extend":
                self._extend(res, label, args.get("minutes"), source, now)
            else:
                res["messages"].append(f"Unknown timer action: {args.get('action')}")
        elif name == "where_am_i":
            res["messages"].append(where_am_i_reply(self.prompt_state(session)))
        elif name == "next_step":
            steps = self.steps()
            if self.r["step_index"] >= len(steps) - 1:
                res["messages"].append("Already at the last step")
            elif not force:
                nxt = self.r["step_index"] + 1
                self._card(res, "next_step", f"Move to step {nxt + 1} ({steps[nxt]})?", None, {"op": "next_step"})
            else:
                self.r["step_index"] += 1
                self._drop_step_cards_locked()
                res["messages"].append(f"Next: {steps[self.r['step_index']]}")
        elif name == "show":
            view = args.get("view", "?")
            res["messages"].append(f"Viewing: {view} {args.get('target')}" if args.get("target") else f"Viewing: {view}")
        elif name == "make_robot":
            res["messages"].append("Building robot protocol…")
            res["info"]["robot"] = True
        elif name == "get_well":
            well = args.get("well")
            if not isinstance(well, str) or not is_well(well):
                res["messages"].append(invalid_message([well]))
        elif name == "find_free_wells":
            near = args.get("near")
            if not isinstance(near, str) or not is_well(near):
                res["messages"].append(invalid_message([near]))
        elif name == "answer":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                res["info"]["dropped"] += 1
                res["info"]["drop_reasons"].append({"call": name, "args": args, "reason": "missing text"})
            else:
                res["messages"].append(text.strip()[:200])
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

    def _single_timer(self, res, minutes, on_done, wells, source, now, force):
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
        res["messages"].append(f"Timer started: {label}")

    def _purge(self, now: float):
        self.r["timers"] = [t for t in self.r["timers"] if now - t["started_at"] < t["minutes"] * 60 + 60]

    def _running_list(self) -> str:
        return ", ".join(t["label"] for t in self.r["timers"])

    def _cancel(self, res, label, source, now):
        if not self.r["timers"]:
            if label:
                res["messages"].append(f"I couldn't find {timer_noun(label)}. No timers running.")
            else:
                res["messages"].append("I couldn't find a timer to cancel. No timers running.")
            return
        hits = resolve_timers(self.r["timers"], label)
        if not hits:
            if label:
                res["messages"].append(f"I couldn't find {timer_noun(label)}. Running: {self._running_list()}.")
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
        name = self.r["timers"][hits[0]]["label"]
        gone = {self.r["timers"][i]["label"] for i in hits}
        self.r["timers"] = [t for t in self.r["timers"] if t["label"] not in gone]
        res["messages"].append(f"Timer canceled: {name}")

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
        res: dict = {"messages": [], "cards": [], "info": {"dropped": 0, "drop_reasons": []}}
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
            self._single_timer(res, p.get("minutes"), p.get("on_done"), p.get("wells"), source, time.time(), True)
        elif op == "cancel_timer":
            self._purge(time.time())
            self._cancel(res, p.get("label"), source, time.time())
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

    def get_well_data(self, well: str) -> dict:
        up = well.upper() if isinstance(well, str) else ""
        if up in self.r["excluded"]:
            status = "excluded"
        elif up in self.r["redo"]:
            status = "redo"
        elif up in self.r["done"]:
            status = "done"
        elif any(up in g.get("wells", []) for g in self.r["plan"]["groups"].values()):
            status = "planned"
        else:
            status = "empty"
        group = next((n for n, g in self.r["plan"]["groups"].items() if up in g.get("wells", [])), "")
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
                events.append({"kind": "timer_warning", "label": t["label"], "minutes_left": 5})
            if not t["notified"] and now >= end:
                t["notified"] = True
                events.append({"kind": "timer_done", "label": t["label"], "message": timer_done_message(t)})
                newly_done.append(t)
        if newly_done:
            self._suggest_next_locked(newly_done, now)
        if events:
            self._commit()
        for ev in events:
            self._broadcast(ev)
        return events

    def _suggest_next_locked(self, done_timers: list[dict], now: float):
        """One card for finished timers; folds into an unanswered suggest card."""
        if self.r["step_index"] >= len(self.steps()) - 1:
            return
        fresh = list(dict.fromkeys(w for t in done_timers for w in (parse_wells(t["wells"]) if t["wells"] else [])))
        old = next((c for c in self.r["cards"].values() if c["kind"] == "suggest_next"), None)
        if old is not None:
            wells = list(dict.fromkeys((old["payload"].get("wells") or []) + fresh))
            count = old["payload"].get("count", 1) + len(done_timers)
            old["payload"]["wells"] = wells
            old["payload"]["count"] = count
            old["question"] = self._suggest_question(count, wells)
            old["ts"] = now
            return
        if len(done_timers) == 1:
            question = f"{timer_done_message(done_timers[0])} Move to step {self.r['step_index'] + 2}?"
        else:
            question = self._suggest_question(len(done_timers), fresh)
        cid = uuid.uuid4().hex[:8]
        self.r["cards"][cid] = {
            "id": cid,
            "kind": "suggest_next",
            "question": question,
            "detail": None,
            "payload": {"op": "next_step", "wells": fresh, "count": len(done_timers)},
            "ts": now,
        }

    def _suggest_question(self, n: int, wells: list[str]) -> str:
        rng = join_wells_ranges(wells)
        head = f"{n} timers done: {rng}." if rng else f"{n} timers done."
        return f"{head} Move to step {self.r['step_index'] + 2}?"

    def _drop_step_cards_locked(self):
        for cid in [cid for cid, c in self.r["cards"].items() if c["kind"] in ("suggest_next", "next_step")]:
            del self.r["cards"][cid]
