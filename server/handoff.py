"""Hand off: a finished run -> approved as-run protocol -> robot code that
matches it (docs/design/handoff.md).

Code computes what changed, the as-run steps and the match against the
simulator's run log; Spark only writes the robot code (run_loop).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from server.grammar import wells_label
from server.labels import step_short_title
from server.runs import done_marks, done_wells

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "robot_runner.py"
SIM_PYTHON = ROOT / ".venv-sim" / "bin" / "python"
SIM_TIMEOUT = 30
SIM_OUTPUT = 64 * 1024
#: Failed simulations before the person is asked.
MAX_FAILS = 3
MAX_ROUNDS = 12

PLATE = "corning_96_wellplate_360ul_flat"
RESERVOIR = "nest_12_reservoir_15ml"
DECK = {"robot": "Flex", "api_level": "2.20",
        "pipette": {"name": "flex_1channel_1000", "mount": "left"},
        "tips": {"load_name": "opentrons_flex_96_tiprack_1000ul", "slot": "C1"},
        "reservoir": {"load_name": RESERVOIR, "slot": "D1"},
        "plate": {"load_name": PLATE, "slot": "D2"},
        "trash": "A3"}

_UL = {"ul": 1.0, "µl": 1.0, "ml": 1000.0}
_STEP = re.compile(r"^step (\d+)$", re.I)


def _wkey(w: str):
    return (w[:1], int(w[1:]) if w[1:].isdigit() else 0)


def _sorted(ws) -> list[str]:
    return sorted(ws, key=_wkey)


def _minutes(step: dict):
    m = step.get("minutes")
    return m if isinstance(m, (int, float)) and m > 0 else None


def _robot(step: dict) -> bool:
    """A step the robot does: a transfer, or a timed wait (a delay)."""
    return bool(step.get("pipette")) or _minutes(step) is not None


def _unit(u) -> str:
    return {"ul": "µL", "ml": "mL"}.get(str(u or "").lower(), str(u or ""))


def _fmt(n) -> str:
    return f"{n:g}"


def _liquids(plan: dict) -> dict[str, str]:
    """Each liquid's reservoir well, in plan order (A1, A2, ...)."""
    out: dict[str, str] = {}
    for st in plan.get("steps") or []:
        liq = ((st or {}).get("pipette") or {}).get("liquid")
        if liq and liq not in out and len(out) < 12:
            out[liq] = f"A{len(out) + 1}"
    return out


def _timer(record: dict, i: int) -> dict | None:
    """The last timer of step i that ended or was cancelled (timer_log)."""
    logs = [t for t in record.get("timer_log") or [] if t.get("step_index") == i]
    return logs[-1] if logs else None


def _reached(record: dict, steps: list) -> int:
    """Steps the run got to (1-based count)."""
    return min((record.get("step_index") or 0) + 1, len(steps))


def changes(record: dict) -> dict:
    """What ⑨0 shows: the import review's rows (robot steps) for the run as
    it went, each with `plan`, `run` and its changes, every change with the
    record event it came from."""
    plan = record.get("plan") or {}
    steps = plan.get("steps") or []
    excluded = record.get("excluded") or {}
    redo = record.get("redo") or {}
    reached = _reached(record, steps)
    used = {w for c in (plan.get("conditions") or {}).values() for w in (c or {}).get("wells") or []}
    rows, others = [], []
    for i, st in enumerate(steps):
        st = st or {}
        if not _robot(st):
            others.append(step_short_title(st))
            continue
        if i >= reached:
            continue  # the run never got here: not in the robot code ("n of m")
        ch: list[dict] = []
        if st.get("pipette"):
            p = st["pipette"]
            vol = f"{_fmt(p['volume'])} {_unit(p.get('unit'))}" if isinstance(p.get("volume"), (int, float)) else ""
            planned = set(st.get("wells") or [])
            done = done_wells(record, i + 1)
            run = done - set(excluded)
            gone, extra = planned - run, run - planned
            skipped = {m["well"]: m for m in record.get("skipped") or [] if m.get("step") == i + 1}
            by: dict[str, list] = defaultdict(list)
            for w in _sorted(gone):
                kind = "excluded" if w in excluded else "skipped" if w in skipped else "not_marked"
                by[kind].append(w)
            marks = {m["well"]: m for m in done_marks(record) if m.get("step") == i + 1}
            for w in _sorted(extra):
                by["redo" if w in redo else "off_plan"].append(w)
            for kind, ws in by.items():
                ev = _event(kind, ws, excluded, skipped, redo, marks)
                ch.append({"kind": kind, "wells": ws, "from": "", "to": "", **ev,
                           "text": _change_text(kind, ws, ev)})
            where = lambda ws: f"All {len(ws)} wells" if ws and set(ws) == used else wells_label(_sorted(ws)) if ws else "no wells"  # noqa: E731
            plan_d = " · ".join(x for x in (vol, where(planned)) if x)
            run_d = " · ".join(x for x in (vol, where(run)) if x)
        else:
            m = _minutes(st)
            t = _timer(record, i)
            actual = t.get("minutes") if t else None
            if isinstance(actual, (int, float)) and abs(actual - m) > 1e-6:
                ch.append({"kind": "timer", "wells": [], "from": _fmt(m), "to": _fmt(actual), "event": "timer",
                           "status": t.get("status") or "", "label": t.get("label") or "",
                           "text": f"{_fmt(m)} → {_fmt(actual)} min (timer {'cancelled' if t.get('status') == 'cancelled' else 'extended' if actual > m else 'shortened'})"})
            minutes = actual if ch else m
            plan_d, run_d = f"{_fmt(m)} min", f"{_fmt(minutes)} min"
        rows.append({"n": len(rows) + 1, "index": i, "title": step_short_title(st), "detail": run_d,
                     "flagged": bool(ch), "reason": "; ".join(c["text"] for c in ch),
                     "quote": st.get("quote") or "", "page": st.get("quote_page"),
                     "plan": plan_d, "run": run_d, "changes": ch,
                     **({"minutes": minutes} if not st.get("pipette") else {})})
    total = sum(1 for s in steps if _robot(s or {}))
    return {"rows": rows, "others": others, "conditions": len(plan.get("conditions") or {}),
            "wells": len(used), "steps": len(steps), "of": total,
            "changed": sum(1 for r in rows if r["flagged"])}


def _event(kind: str, ws: list[str], excluded: dict, skipped: dict, redo: dict, marks: dict) -> dict:
    """The record event behind a well change."""
    if kind == "excluded":
        return {"event": "excluded", "why": "; ".join(sorted({excluded[w] for w in ws if excluded[w]}))}
    if kind == "skipped":
        return {"event": "skipped", "ts": max(skipped[w].get("ts") or 0 for w in ws)}
    if kind == "redo":
        return {"event": "redo", "why": "; ".join(sorted({redo[w] for w in ws if redo[w]}))}
    if kind == "off_plan":
        return {"event": "done", "source": marks[ws[0]].get("source") or "", "ts": marks[ws[0]].get("ts") or 0}
    return {"event": "none"}


def _change_text(kind: str, ws: list[str], ev: dict) -> str:
    label = wells_label(ws)
    why = f": {ev['why']}" if ev.get("why") else ""
    return {"excluded": f"{label} excluded{why}",
            "skipped": f"{label} skipped",
            "not_marked": f"{label} not marked done",
            "redo": f"{label} redo{why}",
            "off_plan": f"{label} marked off plan"}[kind]


def as_run(record: dict) -> list[dict]:
    """The approved protocol: one entry per robot step the run reached, in
    order; a transfer with no wells left drops out."""
    plan = record.get("plan") or {}
    steps = plan.get("steps") or []
    liquids = _liquids(plan)
    out = []
    for r in changes(record)["rows"]:
        st = steps[r["index"]]
        if st.get("pipette"):
            p = st["pipette"]
            wells = _sorted(done_wells(record, r["index"] + 1) - set(record.get("excluded") or {}))
            if not wells:
                continue
            vol = p.get("volume")
            ul = vol * _UL[str(p.get("unit") or "ul").lower()] if isinstance(vol, (int, float)) \
                and str(p.get("unit") or "ul").lower() in _UL else None
            out.append({"n": 0, "title": r["title"], "kind": "transfer", "liquid": p.get("liquid") or "",
                        "source": liquids.get(p.get("liquid") or "", ""), "volume_ul": ul, "wells": wells})
        else:
            out.append({"n": 0, "title": r["title"], "kind": "delay", "minutes": float(r["minutes"])})
    for k, s in enumerate(out):
        s["n"] = k + 1
    return out


def blockers(steps: list[dict]) -> list[str]:
    """Why an as-run protocol can't go to the robot (said, never guessed)."""
    return [f"Step {s['n']} ({s['title']}) has no volume in µL." for s in steps
            if s["kind"] == "transfer" and s.get("volume_ul") is None]


def record_hash(record: dict) -> str:
    keys = ("plan", "done", "excluded", "redo", "skipped", "timer_log", "step_index")
    blob = json.dumps({k: record.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- the match: simulator run log vs the approved protocol ---

def check(events: list[dict], steps: list[dict]) -> dict:
    """Pass = every step, in order, under its `step N` comment: a transfer
    dispenses exactly its wells into the plate at its volume from its
    reservoir well; a delay waits its minutes; nothing else moves liquid."""
    segs: dict[int, list] = {}
    order: list[int] = []
    cur = None
    problems: list[str] = []
    for e in events or []:
        if e.get("op") == "comment" and (m := _STEP.match((e.get("text") or "").strip())):
            cur = int(m.group(1))
            order.append(cur)
            segs.setdefault(cur, [])
        elif cur is None:
            if e.get("op") in ("aspirate", "dispense", "delay"):
                problems.append("Liquid moved before the first `step 1` comment.")
                break
        else:
            segs[cur].append(e)
    want = [s["n"] for s in steps]
    if [n for n in order if n in want] != want or len(order) != len(set(order)):
        problems.append(f"Step comments are {order or 'missing'}; expected {want} in order.")
    per = []
    for s in steps:
        seg = segs.get(s["n"], [])
        why = _check_step(s, seg)
        per.append({"n": s["n"], "title": s["title"], "ok": not why, "why": why})
    extra = sorted(set(segs) - set(want))
    if extra:
        problems.append(f"Unknown steps {extra}.")
    return {"ok": not problems and all(p["ok"] for p in per), "steps": per, "problems": problems}


def _check_step(s: dict, seg: list[dict]) -> str:
    if not seg and s["kind"] == "transfer":
        return "missing"
    if s["kind"] == "delay":
        if any(e["op"] in ("aspirate", "dispense") for e in seg):
            return "liquid moved during a wait"
        got = sum(e.get("minutes") or 0 for e in seg if e["op"] == "delay")
        return "" if abs(got - s["minutes"]) < 0.01 else f"waits {_fmt(got)} min, not {_fmt(s['minutes'])}"
    vols: dict[str, float] = defaultdict(float)
    for e in seg:
        if e["op"] == "dispense" and e.get("labware") == PLATE:
            vols[e["well"]] += e.get("volume") or 0
        elif e["op"] == "aspirate" and not (e.get("labware") == RESERVOIR and e.get("well") == s["source"]):
            return f"draws from {e.get('well')} of {e.get('labware')}, not {s['source']} of the reservoir"
        elif e["op"] == "delay":
            return "waits inside a transfer"
    if set(vols) != set(s["wells"]):
        miss, more = _sorted(set(s["wells"]) - set(vols)), _sorted(set(vols) - set(s["wells"]))
        return "; ".join(x for x in (f"missing {wells_label(miss)}" if miss else "",
                                     f"extra {wells_label(more)}" if more else "") if x)
    bad = [w for w in s["wells"] if abs(vols[w] - s["volume_ul"]) > 0.01]
    return f"{wells_label(bad)} got {_fmt(vols[bad[0]])} µL, not {_fmt(s['volume_ul'])}" if bad else ""


# --- the simulator ---

def simulate(code: str) -> dict:
    """{ok, events} or {ok: False, error}. ROBOT_SIM=local (default) | modal."""
    if os.environ.get("ROBOT_SIM", "local") == "modal":
        return _simulate_modal(code)
    return _simulate_local(code)


def _parse(out: str, err: str) -> dict:
    for line in reversed(out.strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and "ok" in doc:
            return doc
    return {"ok": False, "error": (err or out or "the simulator said nothing").strip()[-2000:]}


def _simulate_local(code: str) -> dict:
    """The runner in .venv-sim, in a temp folder, PATH (+ opentrons' config
    folder, kept in the temp folder) as its only environment, 30 s."""
    if not SIM_PYTHON.exists():
        return {"ok": False, "error": "The simulator isn't installed (.venv-sim, see docs/design/handoff.md)."}
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "protocol.py").write_text(code)
        out_p, err_p = Path(d) / "out.txt", Path(d) / "err.txt"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OT_API_CONFIG_DIR": str(Path(d) / "ot")}
        try:
            with out_p.open("w") as out, err_p.open("w") as err:
                subprocess.run([str(SIM_PYTHON), str(RUNNER), "protocol.py"], cwd=d, env=env,
                               stdout=out, stderr=err, timeout=SIM_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"The simulation ran over {SIM_TIMEOUT} s."}
        return _parse(_tail(out_p), _tail(err_p))


def _tail(p: Path) -> str:
    with p.open("rb") as f:
        f.seek(max(0, p.stat().st_size - SIM_OUTPUT))
        return f.read().decode("utf-8", "replace")


def _simulate_modal(code: str) -> dict:
    # shortcut: not run here (needs a Modal account); upgrade by testing on one
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
    try:
        proc = subprocess.run(["modal", "run", "server/robot_sim.py", "--code-path", f.name], cwd=ROOT,
                              capture_output=True, text=True, timeout=330)
    except FileNotFoundError:
        return {"ok": False, "error": "The modal CLI isn't installed."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The Modal simulation timed out."}
    finally:
        os.unlink(f.name)
    return _parse(proc.stdout[-SIM_OUTPUT:], proc.stderr[-SIM_OUTPUT:])


# --- files ---

def plate_csv(record: dict) -> str:
    """plate_map.csv: one row per well the run touched."""
    plan = record.get("plan") or {}
    group = {w: (c or {}).get("label") or k for k, c in (plan.get("conditions") or {}).items()
             for w in (c or {}).get("wells") or []}
    excluded, redo = record.get("excluded") or {}, record.get("redo") or {}
    skipped = {m["well"] for m in record.get("skipped") or []}
    notes = defaultdict(list)
    for m in record.get("notes") or []:
        for w in m.get("wells") or [m.get("target", "")]:
            notes[w].append(m.get("text", ""))
    marks = defaultdict(list)
    for m in done_marks(record):
        marks[m["well"]].append(m)
    wells = _sorted(set(marks) | set(excluded) | set(redo) | skipped)
    q = lambda s: '"{}"'.format(str(s).replace('"', '""'))  # noqa: E731
    lines = ["well,group,status,steps,source,note"]
    for w in wells:
        status = "excluded" if w in excluded else "redo" if w in redo else "done" if w in marks else "skipped"
        steps = " ".join(str(m["step"]) for m in marks[w])
        src = sorted({m.get("source") or "" for m in marks[w]} - {""})
        note = "; ".join(x for x in [excluded.get(w) or "", redo.get(w) or "", *notes[w]] if x)
        lines.append(",".join([w, q(group.get(w, "")), status, steps, " ".join(src), q(note)]))
    return "\n".join(lines) + "\n"


# --- the loop ---

INSTRUCTIONS = (
    "You write Opentrons Python API v2 protocols for a Flex robot. "
    "Call read_run first. Use exactly the deck it gives (load names, slots, mount, API level) "
    "and only the steps it gives, in order. Put protocol.comment(\"step N\") right before step N, "
    "with N the step's n. A transfer moves volume_ul µL from its source well of the reservoir into "
    "each of its wells of the plate (a new tip per well is fine). A delay step is "
    "protocol.delay(minutes=...). Do nothing else: no mixing, no other liquid moves. "
    "Then call generate_protocol with the whole file, then simulate. If simulate fails, read the "
    "error and the diff, fix the code, and generate_protocol + simulate again. When simulate "
    "passes, call finish."
)

TOOLS = [
    {"type": "function", "name": "read_run", "description": "The approved as-run steps and the deck.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "generate_protocol", "description": "Store the whole protocol.py text.",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}},
                    "required": ["code"], "additionalProperties": False}},
    {"type": "function", "name": "simulate",
     "description": "Simulate the stored protocol; returns ok, error and the match against the run.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "finish", "description": "Done: the last simulate passed.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                    "required": ["summary"], "additionalProperties": False}},
]


async def run_loop(call, steps: list[dict], sim, on_stage) -> dict:
    """Spark drives; code keeps the stage and decides pass. `call(body)` posts
    one Responses API request and returns its JSON; `sim(code)` simulates;
    `on_stage(stage, **state)` saves and shows where it is.
    Returns {stage: passed | needs_you, code, check, error, attempt}."""
    st = {"code": "", "fails": 0, "sims": 0, "passed": False, "check": None, "error": ""}
    inp: list = [{"role": "user", "content": "Write the robot protocol for this run."}]

    def end(stage: str, error: str = "") -> dict:
        out = {"stage": stage, "code": st["code"], "check": st["check"], "error": error or st["error"],
               "attempt": st["sims"]}
        on_stage(stage, **{k: v for k, v in out.items() if k != "stage"})
        return out

    for _ in range(MAX_ROUNDS):
        try:
            resp = await call({"instructions": INSTRUCTIONS, "input": inp, "tools": TOOLS})
        except Exception as e:  # the model is unreachable: the person decides
            return end("needs_you", f"The model didn't answer ({e}).")
        calls = [o for o in resp.get("output") or [] if o.get("type") == "function_call"]
        if not calls:
            break
        for c in calls:
            inp.append({"type": "function_call", "name": c.get("name"), "arguments": c.get("arguments") or "{}",
                        "call_id": c.get("call_id")})
            try:
                args = json.loads(c.get("arguments") or "{}")
            except ValueError:
                args = {}
            name = c.get("name")
            if name == "read_run":
                out = {"deck": DECK, "steps": steps}
            elif name == "generate_protocol":
                st["code"], st["passed"] = str(args.get("code") or ""), False
                on_stage("fixing" if st["fails"] else "generating", code=st["code"], attempt=st["sims"])
                out = {"ok": True, "lines": len(st["code"].splitlines())}
            elif name == "simulate":
                if not st["code"]:
                    out = {"ok": False, "error": "No code yet: call generate_protocol first."}
                else:
                    st["sims"] += 1
                    on_stage("simulating", attempt=st["sims"])
                    res = await sim(st["code"])
                    st["check"] = check(res.get("events") or [], steps) if res.get("ok") else None
                    st["passed"] = bool(res.get("ok")) and st["check"]["ok"]
                    st["error"] = "" if st["passed"] else res.get("error") or _diff_text(st["check"])
                    out = {"ok": st["passed"], "error": st["error"], "match": st["check"]}
                    if not st["passed"]:
                        st["fails"] += 1
                        if st["fails"] >= MAX_FAILS:
                            return end("needs_you")
            elif name == "finish":
                if st["passed"]:
                    return end("passed")
                out = {"ok": False, "error": "Not passed yet: simulate must pass first."}
            else:
                out = {"ok": False, "error": f"Unknown tool {name}."}
            inp.append({"type": "function_call_output", "call_id": c.get("call_id"), "output": json.dumps(out)})
    if st["passed"]:
        return end("passed")  # the code passed; a missing finish doesn't undo it
    return end("needs_you", st["error"] or "The model stopped before the code passed.")


def _diff_text(chk: dict | None) -> str:
    if not chk:
        return ""
    bad = [f"step {p['n']}: {p['why']}" for p in chk["steps"] if not p["ok"]]
    return "; ".join(chk["problems"] + bad)
