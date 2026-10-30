"""Hand off: a finished run -> approved as-run protocol -> robot code that
matches it (docs/design/handoff.md).

Code computes what changed, the as-run steps and the match against the
simulator's run log; Spark only writes the robot code (run_loop).
"""

from __future__ import annotations

import hashlib
import json
import math
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
TUBES = "opentrons_24_tuberack_nest_1.5ml_snapcap"
#: Where liquids sit (code picks): a liquid that fits one 1.5 mL tube with its
#: dead volume goes in a tube, else in 12-well reservoir wells (split over
#: wells when one can't hold it). max_ul: the labware definition's
#: totalLiquidVolume (opentrons_shared_data labware v2: nest_12_reservoir_15ml/3,
#: opentrons_24_tuberack_nest_1.5ml_snapcap/2). dead_ul: neither Opentrons nor
#: NEST publish one; estimated (추정) as the liquid left at a 4 mm liquid height,
#: the tip's default 1 mm aspirate clearance (opentrons instrument_context
#: _DEFAULT_ASPIRATE_CLEARANCE) plus 3 mm to stay submerged, from each well's
#: innerLabwareGeometry: 1782 µL (reservoir well), 43 µL (tube), rounded up.
CONTAINERS = {
    "reservoir": {"load_name": RESERVOIR, "slot": "D1", "var": "res", "max_ul": 15000, "dead_ul": 1800,
                  "wells": [f"A{c}" for c in range(1, 13)], "say": "Res"},
    "tubes": {"load_name": TUBES, "slot": "B1", "var": "tubes", "max_ul": 1500, "dead_ul": 50,
              "wells": [f"{r}{c}" for c in range(1, 7) for r in "ABCD"], "say": "Tube"},
}
#: The lowest apiLevel with what the file uses (Labware.load_liquid and
#: load_empty need 2.22): the reader's Opentrons App and robot aren't ours to pick.
API_LEVEL = "2.22"
#: Pipette rule (code, not the model): up to 50 µL the 50, else the 1000.
P50_MAX_UL = 50
DECK = {"robot": "Flex", "api_level": API_LEVEL,
        "pipettes": {"p50": {"name": "flex_1channel_50", "mount": "left", "max_ul": 50,
                             "tips": {"load_name": "opentrons_flex_96_tiprack_50ul", "slot": "C2"}},
                     "p1000": {"name": "flex_1channel_1000", "mount": "right", "max_ul": 1000,
                               "tips": {"load_name": "opentrons_flex_96_tiprack_1000ul", "slot": "C1"}}},
        "plate": {"load_name": PLATE, "slot": "D2"},
        "trash": "A3"}
#: Liquid colors in the Opentrons App (the one place they're set): a liquid
#: that treats one condition takes that condition's color (conditions in
#: sorted key order: control, drug A, drug B, ...); one that goes to several
#: (medium) takes the neutral.
LIQUID_COLORS = ["#9E9E9E", "#2F80ED", "#F2994A", "#27AE60", "#9B51E0", "#EB5757"]
NEUTRAL_COLOR = "#F7C6D0"
#: A wait (no liquid) on Home's code map.
WAIT_COLOR = "#C7C7CC"
#: Reservoir fill: what the steps draw plus 10 %.
OVERAGE = 1.1

_UL = {"ul": 1.0, "µl": 1.0, "ml": 1000.0}
_STEP = re.compile(r"^step (\d+)\b", re.I)


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


def _liquids(plan: dict) -> list[str]:
    """The plan's liquids in the order its steps first use them."""
    return list(dict.fromkeys(liq for st in plan.get("steps") or []
                              if (liq := ((st or {}).get("pipette") or {}).get("liquid"))))


def _timer(record: dict, i: int) -> dict | None:
    """The last timer of step i that ended or was cancelled (timer_log)."""
    logs = [t for t in record.get("timer_log") or [] if t.get("step_index") == i]
    return logs[-1] if logs else None


def _run_s(t: dict) -> float:
    """A logged timer's actual seconds (older logs kept minutes)."""
    s = t.get("duration_s")
    return float(s) if isinstance(s, (int, float)) else float(t.get("minutes") or 0) * 60


def secs_text(s: float) -> str:
    """Seconds as people say them: "<1 s", "18 s", "90 s", "30 min", "2 min 5 s"."""
    if s < 1:
        return "<1 s"
    if s < 120:
        return f"{round(s)} s"
    m, r = divmod(round(s), 60)
    return f"{m} min" if not r else f"{m} min {r} s"


def _reached(record: dict, steps: list) -> int:
    """Steps the run got to (1-based count)."""
    return min((record.get("step_index") or 0) + 1, len(steps))


def changes(record: dict, timing: dict | None = None) -> dict:
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
            plan_s = _minutes(st) * 60
            t = _timer(record, i)
            run_s = _run_s(t) if t else None
            choice = (timing or {}).get(str(i)) or "run"
            if run_s is not None and abs(run_s - plan_s) > 0.05:
                said = (f"stopped after {secs_text(run_s)}" if t.get("status") == "cancelled"
                        else f"ran {secs_text(run_s)}")
                ch.append({"kind": "timer", "wells": [], "from": secs_text(plan_s), "to": secs_text(run_s),
                           "event": "timer", "status": t.get("status") or "", "label": t.get("label") or "",
                           "text": f"{said} (plan {secs_text(plan_s)})" + (" · using plan" if choice == "plan" else ""),
                           "speech": f"{step_short_title(st).lower()} {said}, plan was {secs_text(plan_s)}"})
            seconds = run_s if ch and choice == "run" else plan_s
            plan_d, run_d = secs_text(plan_s), secs_text(seconds)
        rows.append({"n": len(rows) + 1, "index": i, "title": step_short_title(st), "detail": run_d,
                     "flagged": bool(ch), "reason": "; ".join(c["text"] for c in ch),
                     # what Milo says when the step opens in the review: one sentence
                     "said": "; ".join(c.get("speech") or c["text"] for c in ch),
                     "quote": st.get("quote") or "", "page": st.get("quote_page"),
                     "plan": plan_d, "run": run_d, "changes": ch,
                     **({"seconds": seconds, "plan_s": plan_s, "timing": choice} if not st.get("pipette") else {})})
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


def as_run(record: dict, timing: dict | None = None) -> list[dict]:
    """The approved protocol: one entry per robot step the run reached, in
    order; a transfer with no wells left drops out. Each transfer carries the
    pipette the code picks (≤ 50 µL: p50), each wait its seconds."""
    plan = record.get("plan") or {}
    steps = plan.get("steps") or []
    out = []
    for r in changes(record, timing)["rows"]:
        st = steps[r["index"]]
        if st.get("pipette"):
            p = st["pipette"]
            wells = _sorted(done_wells(record, r["index"] + 1) - set(record.get("excluded") or {}))
            if not wells:
                continue
            vol = p.get("volume")
            ul = vol * _UL[str(p.get("unit") or "ul").lower()] if isinstance(vol, (int, float)) \
                and str(p.get("unit") or "ul").lower() in _UL else None
            out.append({"n": 0, "index": r["index"], "title": r["title"], "kind": "transfer",
                        "liquid": p.get("liquid") or "", "volume_ul": ul, "wells": wells,
                        "pipette": "p50" if ul is not None and ul <= P50_MAX_UL else "p1000"})
        else:
            temp = st.get("temp_c")
            what = f"{r['title']} at {_fmt(temp)} °C" if isinstance(temp, (int, float)) else r["title"]
            out.append({"n": 0, "index": r["index"], "title": r["title"], "kind": "delay",
                        "seconds": _whole(round(float(r["seconds"]), 1)),
                        # the comment: "as run 18 s (plan 30 min)" when the run's time is kept, else the time
                        "said": f"as run {r['run']} (plan {r['plan']})" if r["flagged"] and r["timing"] == "run"
                        else r["run"],
                        "msg": f"{what} (as run: {secs_text(ran) if (ran := _timer_s(record, r['index'])) is not None else r['plan']}; plan: {r['plan']})"})
    for k, s in enumerate(out):
        s["n"] = k + 1
        if s["kind"] == "transfer":
            s["comment"] = f"Step {s['n']}: {s['title']} · {_fmt(s['volume_ul'] or 0)} µL"
        else:
            s["comment"] = f"Step {s['n']}: {s['title']} · {s['said']}"
    return out


def _whole(x: float):
    """18.0 -> 18 (code reads "seconds=18"), 0.3 stays 0.3."""
    return int(x) if x == int(x) else x


def _timer_s(record: dict, i: int) -> float | None:
    t = _timer(record, i)
    return _run_s(t) if t else None


def place(record: dict, steps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Where each liquid sits and how much goes there (CONTAINERS): one
    1.5 mL tube when what the steps draw + 10 % + its dead volume fits, else
    reservoir wells, a new well when one would overflow. Each container holds
    drawn × 1.1 + dead volume. Steps get `sources` (container well → plate
    wells); every liquid gets a color the Opentrons App tells apart: its
    condition's, neutral when it goes to several (medium).
    Returns (steps, containers); a container with no well left has well ""."""
    plan = record.get("plan") or {}
    conds = plan.get("conditions") or {}
    keys = sorted(conds)
    steps = json.loads(json.dumps(steps))
    for s in steps:
        s.pop("sources", None)  # placed fresh every time
    free = {k: list(c["wells"]) for k, c in CONTAINERS.items()}
    out: list[dict] = []
    for liq in _liquids(plan):
        draws = [(s, w) for s in steps if s["kind"] == "transfer" and s["liquid"] == liq
                 and s.get("volume_ul") is not None for w in s["wells"]]
        if not draws:
            continue
        low = liq.lower()
        mine = {w for _, w in draws}
        k = next((k for k in keys if low in {str(t).lower() for t in (conds[k] or {}).get("treatments") or []}), None)
        if k is None:
            k = next((k for k in keys if mine <= set((conds[k] or {}).get("wells") or [])), None)
        color = LIQUID_COLORS[keys.index(k) % len(LIQUID_COLORS)] if k else NEUTRAL_COLOR
        total = sum(s["volume_ul"] for s, _ in draws)
        tube = CONTAINERS["tubes"]
        kind = "tubes" if total * OVERAGE + tube["dead_ul"] <= tube["max_ul"] else "reservoir"
        c = CONTAINERS[kind]
        room = (c["max_ul"] - c["dead_ul"]) / OVERAGE  # what one container can give
        groups: list[list] = [[]]
        for s, w in draws:
            if groups[-1] and sum(x["volume_ul"] for x, _ in groups[-1]) + s["volume_ul"] > room + 1e-9:
                groups.append([])
            groups[-1].append((s, w))
        for g in groups:
            well = free[kind].pop(0) if free[kind] else ""
            drawn = sum(x["volume_ul"] for x, _ in g)
            out.append({"name": liq[:1].upper() + liq[1:], "liquid": liq, "labware": kind, "well": well,
                        "drawn_ul": drawn, "dead_ul": c["dead_ul"],
                        "volume_ul": float(math.ceil(round(drawn * OVERAGE + c["dead_ul"], 6))), "color": color})
            for s, w in g:
                src = s.setdefault("sources", [])
                if not src or src[-1]["labware"] != c["var"] or src[-1]["well"] != well:
                    src.append({"labware": c["var"], "well": well, "wells": []})
                src[-1]["wells"].append(w)
    return steps, out


def _description(cs: list[dict]) -> str:
    drawn = sum(c["drawn_ul"] for c in cs)
    where = "tube" if cs[0]["labware"] == "tubes" else "reservoir well"
    return (f"{_fmt(drawn)} µL drawn by the run + 10 % + {_fmt(cs[0]['dead_ul'])} µL dead volume per {where}"
            + (f" ({len(cs)} {where}s)" if len(cs) > 1 else ""))


def setup_code(steps: list[dict], liqs: list[dict], title: str, run_n: int, date: str) -> str:
    """The protocol's fixed head (metadata, deck, pipettes, liquids, empty
    plate wells), made from the approved run; Spark copies it as is and
    writes only the steps after it."""
    d, p = DECK, DECK["pipettes"]
    used = _sorted({w for s in steps if s["kind"] == "transfer" for w in s["wells"]})
    lines = [
        "from opentrons import protocol_api", "",
        "metadata = {",
        f"    \"protocolName\": {json.dumps(f'{title} — Run {run_n} (as-run)', ensure_ascii=False)},",
        "    \"author\": \"Glassistant (Milo)\",",
        f"    \"description\": {json.dumps(f'{date}. Generated from the recorded run; simulated with opentrons_simulate.')},",
        "}",
        f"requirements = {{\"robotType\": \"Flex\", \"apiLevel\": \"{d['api_level']}\"}}", "", "",
        "def run(protocol: protocol_api.ProtocolContext):",
        "    # --- setup: from the approved run record (do not edit) ---",
        f"    tips50 = protocol.load_labware(\"{p['p50']['tips']['load_name']}\", \"{p['p50']['tips']['slot']}\")",
        f"    tips1000 = protocol.load_labware(\"{p['p1000']['tips']['load_name']}\", \"{p['p1000']['tips']['slot']}\")",
        f"    plate = protocol.load_labware(\"{d['plate']['load_name']}\", \"{d['plate']['slot']}\")",
        f"    protocol.load_trash_bin(\"{d['trash']}\")",
        f"    p50 = protocol.load_instrument(\"{p['p50']['name']}\", \"{p['p50']['mount']}\", tip_racks=[tips50])",
        f"    p1000 = protocol.load_instrument(\"{p['p1000']['name']}\", \"{p['p1000']['mount']}\", tip_racks=[tips1000])",
    ]
    for kind, c in CONTAINERS.items():
        if any(lq["labware"] == kind for lq in liqs):
            lines.append(f"    {c['var']} = protocol.load_labware(\"{c['load_name']}\", \"{c['slot']}\")")
    names = list(dict.fromkeys(lq["liquid"] for lq in liqs))
    for k, liq in enumerate(names):
        cs = [lq for lq in liqs if lq["liquid"] == liq]
        lines.append(f"    liquid_{k + 1} = protocol.define_liquid({json.dumps(cs[0]['name'], ensure_ascii=False)}, "
                     f"description={json.dumps(_description(cs), ensure_ascii=False)}, display_color=\"{cs[0]['color']}\")")
        for lq in cs:
            lines.append(f"    {CONTAINERS[lq['labware']]['var']}.load_liquid([\"{lq['well']}\"], "
                         f"{lq['volume_ul']:.0f}, liquid_{k + 1})")
    if used:
        lines.append(f"    plate.load_empty({json.dumps(used)})")
    lines.append("    # --- steps ---")
    return "\n".join(lines) + "\n"


def expected_seconds(record: dict, steps: list[dict], timing: dict | None = None) -> dict[str, float]:
    """The verifier's own reading of each wait, straight from the record
    (timer_log seconds, or the plan's minutes when the plan was chosen); it
    shares nothing with how as_run turns times into code."""
    out: dict[str, float] = {}
    plan_steps = (record.get("plan") or {}).get("steps") or []
    for s in steps:
        if s["kind"] != "delay":
            continue
        i = s["index"]
        logs = [t for t in record.get("timer_log") or [] if t.get("step_index") == i]
        planned = float(plan_steps[i].get("minutes") or 0) * 60
        if (timing or {}).get(str(i)) == "plan" or not logs:
            out[str(s["n"])] = planned
        else:
            t = logs[-1]
            out[str(s["n"])] = float(t["duration_s"]) if "duration_s" in t else float(t.get("minutes") or 0) * 60
    return out


def blockers(steps: list[dict], liqs: list[dict] | None = None) -> list[str]:
    """Why an as-run protocol can't go to the robot (said, never guessed)."""
    return [f"Step {s['n']} ({s['title']}) has no volume in µL." for s in steps
            if s["kind"] == "transfer" and s.get("volume_ul") is None] + \
        [f"No {lq['labware']} well left for {lq['name']}." for lq in liqs or [] if not lq["well"]] + \
        [f"{lq['name']} needs a single {lq['volume_ul']:.0f} µL draw, more than its container holds."
         for lq in liqs or [] if lq["well"] and lq["volume_ul"] > CONTAINERS[lq["labware"]]["max_ul"]]


def record_hash(record: dict) -> str:
    keys = ("plan", "done", "excluded", "redo", "skipped", "timer_log", "step_index")
    blob = json.dumps({k: record.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- the match: simulator run log vs the approved protocol ---

def check(events: list[dict], steps: list[dict], seconds: dict[str, float] | None = None,
          liquids: list[dict] | None = None) -> dict:
    """Pass = every step, in order, under its `Step N` comment: a transfer
    dispenses exactly its wells into the plate at its volume from its
    reservoir well, with its pipette and a new tip per well; a wait waits
    the seconds the record holds (`seconds`, the verifier's own reading);
    nothing else moves liquid."""
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
        why = _check_step(s, seg, (seconds or {}).get(str(s["n"])))
        per.append({"n": s["n"], "title": s["title"], "ok": not why, "why": why})
    extra = sorted(set(segs) - set(want))
    if extra:
        problems.append(f"Unknown steps {extra}.")
    # every container gives no more than what sits above its dead volume
    drawn: dict[tuple, float] = defaultdict(float)
    for e in events or []:
        if e.get("op") == "aspirate":
            drawn[(e.get("labware"), e.get("well"))] += e.get("volume") or 0
    for lq in liquids or []:
        c = CONTAINERS[lq["labware"]]
        got = drawn.get((c["load_name"], lq["well"]), 0)
        if got > lq["volume_ul"] - lq["dead_ul"] + 0.01:
            problems.append(f"{lq['name']}: {_fmt(round(got, 1))} µL drawn from {c['say']} {lq['well']}, "
                            f"more than its {_fmt(lq['volume_ul'] - lq['dead_ul'])} µL above the dead volume.")
    return {"ok": not problems and all(p["ok"] for p in per), "steps": per, "problems": problems}


def _check_step(s: dict, seg: list[dict], want_s: float | None) -> str:
    if not seg and s["kind"] == "transfer":
        return "missing"
    if s["kind"] == "delay":
        if any(e["op"] in ("aspirate", "dispense") for e in seg):
            return "liquid moved during a wait"
        got = sum(e.get("seconds") or 0 for e in seg if e["op"] == "delay")
        want = s["seconds"] if want_s is None else want_s
        return "" if abs(got - want) < 0.06 else f"waits {_fmt(round(got, 1))} s, not {_fmt(round(want, 1))} s"
    vols: dict[str, float] = defaultdict(float)
    loads = {c["var"]: c["load_name"] for c in CONTAINERS.values()}
    want = {w: (loads[src["labware"]], src["well"]) for src in s.get("sources") or [] for w in src["wells"]}
    last = None
    for e in seg:
        if e["op"] == "aspirate":
            last = (e.get("labware"), e.get("well"))
        elif e["op"] == "dispense" and e.get("labware") == PLATE:
            if e["well"] in want and last != want[e["well"]]:
                return f"{e['well']} gets liquid from {last[1] if last else '?'} of {last[0] if last else '?'}, " \
                       f"not {want[e['well']][1]} of {want[e['well']][0]}"
            vols[e["well"]] += e.get("volume") or 0
        elif e["op"] == "delay":
            return "waits inside a transfer"
    if set(vols) != set(s["wells"]):
        miss, more = _sorted(set(s["wells"]) - set(vols)), _sorted(set(vols) - set(s["wells"]))
        return "; ".join(x for x in (f"missing {wells_label(miss)}" if miss else "",
                                     f"extra {wells_label(more)}" if more else "") if x)
    bad = [w for w in s["wells"] if abs(vols[w] - s["volume_ul"]) > 0.01]
    if bad:
        return f"{wells_label(bad)} got {_fmt(vols[bad[0]])} µL, not {_fmt(s['volume_ul'])}"
    max_ul = DECK["pipettes"][s["pipette"]]["max_ul"]
    wrong = {e.get("pipette") for e in seg if e["op"] in ("aspirate", "dispense")} - {max_ul}
    if wrong:
        return f"uses the {'/'.join(str(x) for x in sorted(wrong, key=str))} µL pipette, not the {max_ul} µL"
    tips = sum(1 for e in seg if e["op"] == "pick_up_tip")
    return "" if tips == len(s["wells"]) else f"{tips} tips for {len(s['wells'])} wells (a new tip per well)"


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


def tips_used(events: list[dict]) -> dict[str, int]:
    """Tips picked up per tip rack (the simulation's summary)."""
    out: dict[str, int] = defaultdict(int)
    for e in events or []:
        if e.get("op") == "pick_up_tip":
            out[e.get("labware") or "?"] += 1
    return dict(out)


# --- files ---

def plate_csv(record: dict, title: str = "", run_n: int = 0, date: str = "",
              liquids: list[dict] | None = None) -> str:
    """plate_map.csv: a commented head (protocol, run, date), then one row per
    well the run touched: what went in by hand (each done step's liquid and
    volume), the total, status, why excluded, who logged it, notes."""
    plan = record.get("plan") or {}
    psteps = plan.get("steps") or []
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
    lines = [f"# protocol: {title}", f"# run: Run {run_n}", f"# date: {date}"] + [
        f"# reagent: {lq['name']} · {CONTAINERS[lq['labware']]['say']} {lq['well']} · {_fmt(lq['volume_ul'])} µL "
        f"({_fmt(lq['drawn_ul'])} drawn + 10 % + {_fmt(lq['dead_ul'])} dead)" for lq in liquids or []] + [
        "well,group,contents,total_uL,status,excluded_reason,logged_by,note"]
    for w in wells:
        parts, total = [], 0.0
        for m in marks[w]:
            p = ((psteps[m["step"] - 1] if 0 < m["step"] <= len(psteps) else {}) or {}).get("pipette") or {}
            vol, unit = p.get("volume"), str(p.get("unit") or "ul").lower()
            if p.get("liquid") and isinstance(vol, (int, float)) and unit in _UL:
                ul = vol * _UL[unit]
                parts.append(f"{p['liquid'][:1].upper() + p['liquid'][1:]} {_fmt(ul)} µL")
                total += ul
        status = "excluded" if w in excluded else "redo" if w in redo else "done" if w in marks else "skipped"
        src = sorted({m.get("source") or "" for m in marks[w]} - {""})
        note = "; ".join(x for x in [redo.get(w) or "", *notes[w]] if x)
        lines.append(",".join([w, q(group.get(w, "")), q("; ".join(parts)), _fmt(total), status,
                               q(excluded.get(w) or ""), " ".join(src), q(note)]))
    return "\n".join(lines) + "\n"


# --- the loop ---

INSTRUCTIONS = (
    "You write Opentrons Python API v2 protocols for a Flex robot. "
    "Call read_run first. The file starts with setup_code exactly as given (metadata, deck, pipettes "
    "p50 and p1000, liquids, empty wells): copy it character for character, never change it. "
    "After it, inside run(), write only the steps, in order. Each step starts with "
    "protocol.comment(<its comment>). A transfer: for each of its sources, with its pipette variable "
    "(p50 or p1000), move volume_ul µL from <source labware>[<source well>] (res or tubes, loaded in "
    "setup_code) into each of that source's wells of plate, a new tip per well "
    "(new_tip=\"always\"). A delay: protocol.delay(seconds=<seconds>, msg=<msg>). "
    "Do nothing else: no mixing, no other liquid moves. "
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


async def run_loop(call, approved: dict, sim, on_stage) -> dict:
    """Spark drives; code keeps the stage and decides pass. `approved` is the
    approval (steps, liquids, setup, seconds); `call(body)` posts one
    Responses API request and returns its JSON; `sim(code)` simulates;
    `on_stage(stage, **state)` saves and shows where it is.
    Returns {stage: passed | needs_you, code, check, error, attempt, tips}."""
    steps, setup = approved["steps"], approved["setup"]
    st = {"code": "", "fails": 0, "sims": 0, "passed": False, "check": None, "error": "", "tips": {}}
    inp: list = [{"role": "user", "content": "Write the robot protocol for this run."}]

    def end(stage: str, error: str = "") -> dict:
        out = {"stage": stage, "code": st["code"], "check": st["check"], "error": error or st["error"],
               "attempt": st["sims"], "tips": st["tips"]}
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
                out = {"setup_code": setup, "steps": steps, "liquids": approved.get("liquids") or []}
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
                    if setup not in st["code"]:
                        # names, wells, volumes and colors come from the record: the head is checked as text
                        res = {"ok": False, "error": "setup_code was changed: copy it exactly as read_run gave it."}
                    else:
                        res = await sim(st["code"])
                    st["check"] = check(res.get("events") or [], steps, approved.get("seconds"),
                                        approved.get("liquids")) if res.get("ok") else None
                    st["tips"] = tips_used(res.get("events") or [])
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
