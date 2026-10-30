"""One-time library cleanup: protocols that are the same original become one.

Same = the same source hash (imports from now on), or, for older items with
none, the same title and the same step texts. The earliest import is kept;
the others' runs move under it (Run 1, 2, ... follow their start order) and
their files go to a backup folder next to the data, with runs/ and
record.json copied there first.

    python -m server.library_merge            # list what would merge
    python -m server.library_merge --apply    # do it (stop the server first)
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from server import library as lib


def _key(doc: dict) -> tuple:
    if doc.get("source_hash"):
        return ("source", doc["source_hash"])
    steps = tuple(" ".join((s or {}).get("text", "").split()) for s in (doc.get("plan") or {}).get("steps") or [])
    return ("title+steps", " ".join((doc.get("name") or "").lower().split()), steps)


def groups(root: Path | None = None) -> list[list[dict]]:
    """Protocols that would merge, each group earliest import first."""
    by: dict[tuple, list[dict]] = {}
    for d in lib.all_docs(root):
        by.setdefault(_key(d), []).append(d)
    out = [sorted(g, key=lambda d: d.get("imported_at") or 0) for g in by.values() if len(g) > 1]
    return sorted(out, key=lambda g: g[0].get("name") or "")


def _runs_of(pid: str, root: Path) -> list[Path]:
    return [p for p in sorted((root / "runs").glob("*.json"))
            if (json.loads(p.read_text()) or {}).get("protocol_id") == pid]


def merge(root: Path | None = None, apply: bool = False) -> list[dict]:
    """The plan (and, with apply, the merge): [{keep, drop: [ids], runs: n}]."""
    root = root or lib.DATA_DIR
    plan = []
    for g in groups(root):
        keep, drop = g[0], g[1:]
        runs = [p for d in drop for p in _runs_of(d["id"], root)]
        plan.append({"keep": keep, "drop": drop, "runs": runs})
    if not apply or not plan:
        return plan
    backup = root / f"backup-merge-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True)
    shutil.copytree(root / "runs", backup / "runs", dirs_exist_ok=True)
    if (root / "record.json").exists():
        shutil.copy2(root / "record.json", backup / "record.json")
    (backup / "protocols").mkdir()
    for item in plan:
        keep = item["keep"]
        for p in item["runs"]:
            doc = json.loads(p.read_text())
            doc["protocol_id"], doc["protocol_name"] = keep["id"], keep.get("name")
            p.write_text(json.dumps(doc, ensure_ascii=False))
        rec = root / "record.json"
        if rec.exists():
            r = json.loads(rec.read_text())
            if (r.get("run") or {}).get("protocol_id") in {d["id"] for d in item["drop"]}:
                r["run"]["protocol_id"], r["run"]["protocol_name"] = keep["id"], keep.get("name")
                rec.write_text(json.dumps(r, ensure_ascii=False))
        for d in item["drop"]:
            src = root / "protocols" / f"{d['id']}.json"
            shutil.move(str(src), str(backup / "protocols" / src.name))
    return plan


def main() -> None:
    apply = "--apply" in sys.argv
    plan = merge(apply=apply)
    if not plan:
        print("Nothing to merge.")
        return
    for item in plan:
        k = item["keep"]
        print(f"KEEP {k['id']}  {k.get('name')!r}  (imported {time.strftime('%b %d %H:%M', time.localtime(k.get('imported_at') or 0))})")
        for d in item["drop"]:
            print(f"  merge {d['id']}  {d.get('name')!r}  (imported {time.strftime('%b %d %H:%M', time.localtime(d.get('imported_at') or 0))})")
        print(f"  runs moving under it: {len(item['runs'])}")
    print("Applied (backup next to the data)." if apply else "Dry run. Stop the server, then: python -m server.library_merge --apply")


if __name__ == "__main__":
    main()
