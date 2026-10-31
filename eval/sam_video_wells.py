"""Can SAM 3.1 tell which wells a pipette tip went into, from a glasses clip?
Feasibility only. The logic is server/sam_wells.py (shared with the
Diagnostics "SAM well test"); this runs it on one clip and logs the result.

Run from repo root (MODEL_API_KEY from .env only):
    .venv/bin/python eval/sam_video_wells.py CLIP.mp4 --wells C2,C3,C4 [--a1 tl|tr|br|bl] [--pointer pen]
--a1: the plate corner nearest A1 as seen in the clip (default tl).
Appends to eval/results/sam_video.md; check images in eval/results/sam_video/<clip>/images/.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from server import sam_wells as sw  # noqa: E402

OUT = Path("eval/results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--wells", default="", help="wells actually filled, in order: C2,C3,C4")
    ap.add_argument("--a1", default="tl", choices=sw.CORNERS)
    ap.add_argument("--pointer", default=sw.TIP, choices=sw.POINTERS)
    a = ap.parse_args()
    load_dotenv()
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        sys.exit("MODEL_API_KEY not in .env")
    clip = Path(a.clip)
    truth = sw.parse_targets(a.wells) if a.wells else []
    out_dir = OUT / "sam_video" / clip.stem
    r = sw.analyze(clip, a.a1, sw.MetaSam(key), out_dir, a.pointer)
    names = [f["well"] for f in r["found"]]
    j = sw.judge(truth, names)
    lines = [
        f"\n## {time.strftime('%Y-%m-%d %H:%M')} — {clip.name}",
        f"- clip: {r['w']}x{r['h']}, {r['fps']:.2f} fps, {r['frames']} frames ({r['duration_s']:.1f} s), "
        f"{r['codec']}; SAM lines {r['sam_frames']}, rounds {r['rounds']}, prompts {r['prompts']}",
        f"- truth: {', '.join(truth) or '-'} | found: {', '.join(names) or '-'} | {j['line']}",
        "- per found well: " + "; ".join(f"{f['well']} frames {f['f0']}-{f['f1']} rep {f['rep']}" for f in r["found"]),
        f"- well pitch {r['pitch_px']:.1f} px (well {r['well_px']:.1f} px across), median over frames",
        f"- failed frames: {r['fails']} of {r['frames']} ({r['failed_ratio']:.0%})",
        "- time: " + ", ".join(f"{k} {v:.1f}" for k, v in r["timing"].items()),
        f"- images: {out_dir / 'images'}",
    ]
    print("\n".join(lines))
    with open(OUT / "sam_video.md", "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
