"""The fixed SAM well test set: every saved clip (eval/sam_wells_set.json: the
wells, desk or hand) judged from its saved SAM output, no new SAM calls. Desk
clips are the main measure; hand clips are for reference.

Run from repo root:
    .venv/bin/python eval/sam_wells_set.py [--save]
Prints the table (also eval/results/sam_wells_set.md). A clip that hits fewer
wells or has more false ones than eval/sam_wells_baseline.json exits 1.
--save takes this run as the new baseline.
"""
import argparse
import json
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import sam_wells as sw  # noqa: E402

ROOT = Path("server/data/lab/sam_wells")
SET = Path("eval/sam_wells_set.json")
BASELINE = Path("eval/sam_wells_baseline.json")
OUT = Path("eval/results/sam_wells_set.md")


def run(item: dict) -> dict:
    d = ROOT / item["clip"]
    out = {**item, "found": [], "hits": 0, "total": len(item["targets"]), "missed": item["targets"], "false": []}
    text = d / "sam_video.txt"
    if not text.exists():
        return {**out, "note": "no SAM output"}
    recs = sw.video_records(text.read_text())
    a1 = json.loads((d.parent / "session.json").read_text())["a1"]
    track, *_ = sw.locate(recs, max(recs) + 1, a1)
    found = [w for w, *_ in sw.found_wells(track, sw.ffprobe(next(d.glob("clip.*")))["fps"])]
    j = sw.judge(item["targets"], found)
    return {**out, "found": found, "hits": j["hits"], "false": j["false"],
            "missed": [w["well"] for w in j["wells"] if not w["ok"]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    items = json.loads(SET.read_text())
    with Pool(len(items)) as pool:
        rows = pool.map(run, items)
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    lines = ["| clip | where | hit | missed | false | found | vs baseline |", "|---|---|---|---|---|---|---|"]
    worse = []
    for r in rows:
        b = base.get(r["clip"])
        worse_here = b and (r["hits"] < b["hits"] or len(r["false"]) > b["false"])
        if worse_here:
            worse.append(r["clip"])
        was = f"{b['hits']}/{r['total']}, {b['false']} false" if b else "-"
        lines.append(f"| {r['clip']} | {r['where']} | {r['hits']}/{r['total']} | {' '.join(r['missed']) or '-'} "
                     f"| {' '.join(r['false']) or '-'} | {r.get('note') or ' '.join(r['found']) or '-'} "
                     f"| {'WORSE than ' if worse_here else ''}{was} |")
    for where in ("desk", "hand"):
        rs = [r for r in rows if r["where"] == where and "note" not in r]  # a clip SAM couldn't read isn't a miss
        lines.append(f"\n{where}: {sum(r['hits'] for r in rs)}/{sum(r['total'] for r in rs)} wells, "
                     f"{sum(len(r['false']) for r in rs)} false, {len(rs)} clips")
    print("\n".join(lines))
    OUT.write_text("\n".join(lines) + "\n")
    if a.save:
        BASELINE.write_text(json.dumps({r["clip"]: {"hits": r["hits"], "false": len(r["false"])} for r in rows}, indent=2) + "\n")
    elif worse:
        sys.exit(f"worse than the baseline: {', '.join(worse)}")


if __name__ == "__main__":
    main()
