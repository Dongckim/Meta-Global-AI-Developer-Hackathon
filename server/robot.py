"""Fixed robot-protocol pipeline: record -> CSV -> template code -> Modal sim -> check.

No LLM loop; every stage is code. Simulation runs through the Modal CLI
(`modal run server/robot_sim.py`), which mirrors server/sandbox_test.py.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from server.store import DATA_DIR

PROTOCOL_PATH = DATA_DIR / "robot_protocol.py"
SIM_TIMEOUT = 330

TEMPLATE = '''from opentrons import protocol_api

requirements = {{"robotType": "Flex", "apiLevel": "2.20"}}

def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "C1")
    res = protocol.load_labware("nest_12_reservoir_15ml", "D1")
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D2")
    protocol.load_trash_bin("A3")
    p = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    for well in {wells}:   # from Pip record
        p.transfer(50, res["A1"], plate[well])
'''

_WELL = re.compile(r"\b([A-H])(1[0-2]|[1-9])\b")


def export_csv(store) -> str:
    lines = ["well,status,detail"]
    for w in sorted(store.r["done"]):
        lines.append(f"{w},done,")
    for w, reason in sorted(store.r["excluded"].items()):
        lines.append(f'{w},excluded,"{reason}"')
    for w, note in sorted(store.r["redo"].items()):
        lines.append(f'{w},redo,"{note}"')
    return "\n".join(lines) + "\n"


def render_protocol(store) -> str:
    return TEMPLATE.format(wells=sorted(store.r["done"]))


def _run_sim() -> dict:
    try:
        proc = subprocess.run(
            ["modal", "run", "server/robot_sim.py"],
            capture_output=True, text=True, timeout=SIM_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "skipped": "modal CLI not found", "exit": None, "log": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": "", "exit": None, "log": "simulation timed out"}
    log = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"exit code:\s*(\d+)", log)
    exit_code = int(m.group(1)) if m else proc.returncode
    ok = exit_code == 0 and proc.returncode == 0
    return {"ok": ok, "skipped": "", "exit": exit_code, "log": log[-4000:]}


def check_sim(log: str, expected: list[str]) -> dict:
    simulated = sorted(set(f"{r}{c}" for r, c in _WELL.findall(log or "")))
    exp = sorted(expected)
    missing = [w for w in exp if w not in simulated]
    extra = [w for w in simulated if w not in exp]
    return {"expected": exp, "simulated": simulated, "missing": missing, "extra": extra,
            "ok": not missing and not extra}


def run_pipeline(store) -> dict:
    csv_text = export_csv(store)
    code = render_protocol(store)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROTOCOL_PATH.write_text(code)
    sim = _run_sim()
    check = check_sim(sim.get("log", ""), sorted(store.r["done"]))
    return {
        "csv": csv_text,
        "code": code,
        "sim": sim,
        "check": check,
        "preview": {
            "csv_head": "\n".join(csv_text.splitlines()[:6]),
            "code_head": "\n".join(code.splitlines()[:20]),
        },
    }
