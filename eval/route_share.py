"""How many turns reach the model: Private's slow path, per eval corpus.

Every utterance goes through the real /orchestrate on a fresh mid-run bench;
only the model call is stubbed, so this costs nothing and needs no key.
Run from repo root:
    .venv/bin/python eval/route_share.py
Appends to eval/results/private_glimmer.md.
"""
import ast
import contextlib
import io
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

CORPORA = {"smoke_turns": ("TURNS",), "latency_rounds": ("TURNS",), "tool_choice": ("TURNS",),
           "multi_action": ("CASES",), "instruction_report": ("TURNS", "SMALLTALK"),
           "web_search": ("SEARCH", "RECORD", "CHAT")}


def said(node) -> list:
    """Utterances in an eval list without importing it (some run on import)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Tuple):
            first = next((e for e in n.elts[:2] if isinstance(e, ast.Constant) and isinstance(e.value, str)), None)
            if first is not None and first.value not in ("say", "confirm"):
                out.append(first.value)
            elif len(n.elts) > 1 and isinstance(n.elts[1], ast.Constant) and n.elts[1].value:
                out.append(n.elts[1].value)
        elif isinstance(n, ast.comprehension):
            out += [e.value for e in getattr(n.iter, "elts", []) if isinstance(e, ast.Constant)]
    return out


def from_eval(name: str, lists: tuple) -> list:
    tree = ast.parse((ROOT / f"{name}.py").read_text())
    out = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", "") in lists:
            if isinstance(stmt.value, ast.List) and all(isinstance(e, ast.Constant) for e in stmt.value.elts):
                out += [e.value for e in stmt.value.elts]
            else:
                out += said(stmt.value)
    return out


def recordings() -> list:
    return [t["text"] for f in sorted((ROOT / "live_asr_scripts").glob("*.json"))
            for t in json.loads(f.read_text()) if t.get("who") == "owner"]


async def no_model(*a, **k):
    return [], "", 0, False, {}


def route_of(client, text: str) -> str:
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as latency_rounds
    api._SESSIONS.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        return client.post("/orchestrate", json={"text": text, "session": "route"}).json().get("route", "?")


def main():
    api._call_spark = no_model
    client = TestClient(api.app)
    corpora = {name: from_eval(name, lists) for name, lists in CORPORA.items()}
    corpora["live_asr recordings"] = recordings()
    lines = ["\n## Turns that reach the model\n", "| corpus | turns | model | no model | model % |", "|---|---|---|---|---|"]
    every = {}
    for name, turns in corpora.items():
        routes = {t: route_of(client, t) for t in dict.fromkeys(turns)}
        every |= routes
        c = Counter(routes[t] for t in turns)
        model = c["spark"]
        lines.append(f"| {name} | {len(turns)} | {model} | {len(turns) - model} | {model * 100 // max(len(turns), 1)} |")
    c = Counter(every.values())
    lines.append(f"| all (unique) | {len(every)} | {c['spark']} | {len(every) - c['spark']} | "
                 f"{c['spark'] * 100 // len(every)} |")
    print("\n".join(lines))
    print("other routes:", dict(c))
    print("no-model turns:", sorted(t for t, r in every.items() if r != "spark"))
    with open("eval/results/private_glimmer.md", "a") as f:
        f.write("\n".join(lines) + "\n")


main()
