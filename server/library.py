"""Protocol library: confirmed plans on disk, deduped by content hash.

<root>/protocols/<hash16>.json: {id, name, source_filename, imported_at,
plan_hash, plan}. Same hash re-imports the same protocol. PDF bytes are never
stored (originals live on the device only). root follows the RecordStore path
so tests stay isolated; production uses server/data.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED_PATH = Path(__file__).resolve().parent.parent / "samples" / "plan_demo.json"


def _dir(root: Path | None) -> Path:
    return (root or DATA_DIR) / "protocols"


def _norm_numbers(node):
    """int-valued floats read as ints so 100 and 100.0 hash alike."""
    if isinstance(node, float) and node.is_integer():
        return int(node)
    if isinstance(node, dict):
        return {k: _norm_numbers(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_norm_numbers(v) for v in node]
    return node


def canonical_plan(plan: dict) -> str:
    core = {"conditions": (plan or {}).get("conditions", {}),
            "steps": (plan or {}).get("steps", []),
            "backups": (plan or {}).get("backups", [])}
    return json.dumps(_norm_numbers(core), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(canonical_plan(plan).encode()).hexdigest()


def protocol_id(plan: dict) -> str:
    return plan_hash(plan)[:16]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def ensure_demo_seed(root: Path | None = None) -> dict | None:
    """First library access registers samples/plan_demo.json as Demo protocol."""
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    if any(d.glob("*.json")):
        return None
    try:
        seed = json.loads(SEED_PATH.read_text())
    except (OSError, ValueError):
        return None
    plan = {"conditions": seed.get("conditions", {}), "steps": seed.get("steps", []),
            "backups": seed.get("backups", [])}
    return save(plan, name="Demo protocol", source_filename="plan_demo.json", root=root)


def save(plan: dict, name: str, source_filename: str, root: Path | None = None,
         short_title: str = "") -> dict:
    """Store a confirmed plan; same content returns the existing protocol
    (its first import time and labels stay)."""
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    h, pid = plan_hash(plan), protocol_id(plan)
    path = d / f"{pid}.json"
    existing = _read(path)
    if existing and existing.get("plan_hash") == h:
        return existing
    doc = {"id": pid, "name": name or pid, "short_title": short_title or "",
           "source_filename": source_filename or "",
           "imported_at": time.time(), "plan_hash": h, "plan": plan}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    return doc


def get(protocol_id: str, root: Path | None = None) -> dict | None:
    if not protocol_id or "/" in protocol_id or protocol_id.startswith("."):
        return None
    return _read(_dir(root) / f"{protocol_id}.json")


def find_by_name(name: str, root: Path | None = None) -> dict | None:
    """Case-insensitive exact, then substring, match for voice open."""
    want = (name or "").strip().lower()
    if not want:
        return None
    docs = [d for p in sorted(_dir(root).glob("*.json")) if (d := _read(p))]
    for d in docs:
        if (d.get("name") or "").lower() == want:
            return d
    for d in docs:
        if want in (d.get("name") or "").lower():
            return d
    return None


def summarize(doc: dict) -> dict:
    from server.labels import protocol_label, protocol_short
    plan = (doc or {}).get("plan") or {}
    label = protocol_label(doc)
    return {"id": doc.get("id"), "name": doc.get("name"),
            "short_title": protocol_short(doc), "label": label["display"], "spoken": label["spoken"],
            "source_filename": doc.get("source_filename"),
            "imported_at": doc.get("imported_at"),
            "conditions": len(plan.get("conditions") or {}),
            "steps": len(plan.get("steps") or [])}


def all_docs(root: Path | None = None) -> list[dict]:
    """Every protocol doc (full), newest import first."""
    ensure_demo_seed(root)
    docs = [d for p in _dir(root).glob("*.json") if (d := _read(p))]
    docs.sort(key=lambda d: d.get("imported_at") or 0, reverse=True)
    return docs


def list_all(root: Path | None = None) -> list[dict]:
    ensure_demo_seed(root)
    docs = [d for p in _dir(root).glob("*.json") if (d := _read(p))]
    docs.sort(key=lambda d: d.get("imported_at") or 0, reverse=True)
    return [summarize(d) for d in docs]
