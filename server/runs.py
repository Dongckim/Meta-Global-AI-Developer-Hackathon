"""Experiment runs: one active run in record.json, past runs on disk.

<root>/runs/run-<stamp>.json: {run_id, protocol_id, protocol_name,
started_at, ended_at, status, summary, record}. Legacy bare-record files
migrate to finished on read. root follows the RecordStore path.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def _dir(root: Path | None) -> Path:
    return (root or DATA_DIR) / "runs"


def new_run_id() -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:4]}"


def summarize_record(record: dict) -> dict:
    """Frozen-at-end (or live) overview: per-condition have/need + counts."""
    record = record or {}
    plan = record.get("plan") or {}
    usable = set(record.get("done") or []) - set(record.get("excluded") or {})
    conds = []
    for key in sorted(plan.get("conditions") or {}):
        c = plan["conditions"][key] or {}
        wells = set(c.get("wells") or [])
        conds.append({"key": key, "label": c.get("label") or key,
                      "have": len(usable & wells),
                      "need": c.get("replicates")})
    return {"conditions": conds,
            "excluded": len(record.get("excluded") or {}),
            "notes": len(record.get("notes") or []),
            "timers": len(record.get("timers") or [])}


def _read(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if "record" in doc and "run_id" in doc:
        return doc
    if "plan" in doc:
        meta = path.stat().st_mtime if path.exists() else time.time()
        return {"run_id": path.stem, "protocol_id": None, "protocol_name": None,
                "started_at": meta, "ended_at": meta, "status": "finished",
                "summary": summarize_record(doc), "record": doc}
    return None


def write_run(doc: dict, root: Path | None = None) -> None:
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['run_id']}.json").write_text(
        json.dumps(doc, ensure_ascii=False))


def get(run_id: str, root: Path | None = None) -> dict | None:
    if not run_id or "/" in run_id or run_id.startswith("."):
        return None
    return _read(_dir(root) / f"{run_id}.json")


def list_all(protocol_id: str | None = None, root: Path | None = None) -> list[dict]:
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    docs = [x for p in d.glob("*.json") if (x := _read(p))]
    if protocol_id:
        docs = [x for x in docs if x.get("protocol_id") == protocol_id]
    docs.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return docs


def summarize(doc: dict) -> dict:
    return {"run_id": doc.get("run_id"), "protocol_id": doc.get("protocol_id"),
            "protocol_name": doc.get("protocol_name"),
            "started_at": doc.get("started_at"), "ended_at": doc.get("ended_at"),
            "status": doc.get("status"), "summary": doc.get("summary")}


def _day(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def resolve(selector: str, docs: list[dict]) -> dict | None:
    """run_id | latest | today | yesterday | YYYY-MM-DD over run docs."""
    sel = (selector or "").strip().lower()
    sel = re.sub(r"'s\b", "", sel).strip()
    sel = re.sub(r"\s+(runs?|experiments?)$", "", sel).strip()
    if not sel or not docs:
        return None
    ordered = sorted(docs, key=lambda d: d.get("started_at") or 0, reverse=True)
    if sel == "latest":
        return ordered[0]
    day = sel
    if sel == "today":
        day = _dt.datetime.now().strftime("%Y-%m-%d")
    elif sel == "yesterday":
        day = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    if day != sel or (len(day) == 10 and day[4] == "-" and day[7] == "-"):
        return next((d for d in ordered if _day(d.get("started_at")) == day), None)
    want = selector.strip()
    return next((d for d in docs if d.get("run_id") == want), None)
