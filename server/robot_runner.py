"""Simulate one Opentrons protocol and print its run log as JSON events.

Runs inside the simulator's Python (.venv-sim or the Modal image), never in
the API process: `python robot_runner.py protocol.py`. One JSON line out:
{"ok": true, "events": [...]} or {"ok": false, "error": "..."}.
Events: comment {text}; aspirate / dispense {volume, well, labware,
pipette}; pick_up_tip {labware, pipette}; delay {seconds}. Mix strokes
are left out (they move no liquid between wells).
"""

import json
import sys


def _well(loc):
    lab = getattr(loc, "labware", None)
    w = lab.as_well() if lab is not None and hasattr(lab, "as_well") else loc
    return getattr(w, "well_name", None), getattr(getattr(w, "parent", None), "load_name", None)


def _pipette(p):
    """The pipette's size in µL (50 or 1000)."""
    v = getattr(p.get("instrument"), "max_volume", None)
    return int(v) if v else None


def _walk(entries, out, mix=None):
    mix = mix if mix is not None else [0]  # strokes of a mix still to skip (they follow it as siblings)
    for e in entries:
        p = e.get("payload") or {}
        text = p.get("text") or ""
        if text.startswith("Mixing"):
            mix[0] = 2 * int(p.get("repetitions") or 0)
        elif text.startswith(("Aspirating", "Dispensing")) and mix[0] > 0:
            mix[0] -= 1
        elif text.startswith(("Aspirating", "Dispensing")) and "location" in p:
            well, labware = _well(p["location"])
            out.append({"op": "aspirate" if text.startswith("Aspirating") else "dispense",
                        "volume": float(p.get("volume") or 0), "well": well, "labware": labware,
                        "pipette": _pipette(p)})
        elif text.startswith("Picking up tip") and "location" in p:
            well, labware = _well(p["location"])
            out.append({"op": "pick_up_tip", "labware": labware, "pipette": _pipette(p)})
        elif text.startswith("Delaying") and "minutes" in p:
            # seconds throughout (a wait of 0.3 s stays 0.3 s, never a fraction of a minute)
            out.append({"op": "delay", "seconds": float(p["minutes"]) * 60 + float(p.get("seconds") or 0)})
        elif set(p) == {"text"}:
            out.append({"op": "comment", "text": text})
        _walk(e.get("subsequent") or [], out, mix)


def main(path: str) -> None:
    try:
        from opentrons.simulate import simulate
        with open(path) as f:
            runlog, _ = simulate(f, file_name="protocol.py")
        events: list = []
        _walk(runlog, events)
        print(json.dumps({"ok": True, "events": events}))
    except Exception as e:  # the protocol's own error is the result
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[-2000:]}))


if __name__ == "__main__":
    main(sys.argv[1])
