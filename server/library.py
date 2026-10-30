"""Protocol library: confirmed plans on disk, deduped by content hash.

<root>/protocols/<hash16>.json: {id, name, source_filename, origin (pdf |
text | photo), source_text, original_id (the device's file key), imported_at,
plan_hash, plan}. Same hash re-imports
the same protocol. PDF and photo bytes are never stored (they live on the
device only); a text or photo import keeps its text (the photo transcript),
the original its quotes were checked against. root follows the RecordStore path
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


def source_hash(files: list[bytes], text: str = "") -> str:
    """What makes two imports the same protocol: the original itself (file
    bytes, photos in order, or pasted text with spacing evened out), never the
    extraction, which differs run to run."""
    import re
    h = hashlib.sha256()
    for raw in files:
        h.update(hashlib.sha256(raw).digest())
    h.update(re.sub(r"\s+", " ", text or "").strip().encode())
    return h.hexdigest()[:16]


def find_by_source(src: str, root: Path | None = None) -> dict | None:
    """The protocol any of whose versions came from this original."""
    if not src:
        return None
    d = _dir(root)
    return next((x for p in sorted(d.glob("*.json")) if (x := _read(p)) and src in (
        [x.get("source_hash")] + [v.get("source_hash") for v in x.get("versions") or []])), None) \
        if d.exists() else None


def title_key(title: str) -> str:
    """Titles compare with case, spacing and punctuation ignored."""
    import re
    return re.sub(r"[\W_]+", "", (title or "").lower())


def page_title(doc: dict) -> str:
    """The title the original says (its reviewed draft's), else the library name."""
    return (((doc.get("draft") or {}).get("plan") or {}).get("title") or "").strip() or doc.get("name") or ""


def find_by_title(title: str, root: Path | None = None) -> dict | None:
    want = title_key(title)
    d = _dir(root)
    return next((x for p in sorted(d.glob("*.json")) if (x := _read(p)) and want in (
        title_key(x.get("name")), title_key(page_title(x)))), None) if want and d.exists() else None


#: The fields one version of a protocol owns; older versions keep theirs under `versions`.
VERSION_FIELDS = ("plan", "plan_hash", "source_hash", "source_filename", "origin", "source_text",
                  "original_id", "imported_at", "draft")


def save(plan: dict, name: str, source_filename: str, root: Path | None = None,
         short_title: str = "", origin: str = "pdf", source_text: str = "",
         original_id: str = "", source: str = "", draft: dict | None = None,
         edit: str = "", new_version_of: str = "", separate: bool = False) -> dict:
    """Store a confirmed plan. The same original (source hash) is the same
    protocol, whatever its extraction; else the same plan content is.

    edit: that protocol's plan is replaced (reviewed again; same version).
    new_version_of: the plan becomes that protocol's next version; the old
    one moves to `versions` (runs keep the plan they ran). separate: a new
    protocol even when the plan matches one already saved. `draft` is kept
    so the protocol can be reviewed again without extracting."""
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    h = plan_hash(plan)
    fields = {"plan": plan, "plan_hash": h, "source_hash": source, "source_filename": source_filename or "",
              "origin": origin, "source_text": source_text if origin != "pdf" else "",
              "original_id": original_id, "imported_at": time.time(), "draft": draft}
    base = get(edit or new_version_of, root) if edit or new_version_of else None
    if base is not None:
        if new_version_of:
            old = {k: base.get(k) for k in VERSION_FIELDS}
            base["versions"] = (base.get("versions") or []) + [{"version": base.get("version") or 1, **old}]
            base["version"] = (base.get("version") or 1) + 1
        else:
            fields["imported_at"] = base.get("imported_at")  # the same protocol, fixed
        base.update(fields)
        (d / f"{base['id']}.json").write_text(json.dumps(base, ensure_ascii=False, indent=1))
        return base
    same = None if separate else find_by_source(source, root)
    if same:
        return same
    pid = protocol_id(plan)
    path = d / f"{pid}.json"
    existing = _read(path)
    if existing and existing.get("plan_hash") == h and not separate:
        return existing
    n = 2
    while path.exists():  # a separate copy of a plan already saved
        path = d / f"{pid}-{n}.json"
        n += 1
    doc = {"id": path.stem, "name": name or pid, "short_title": short_title or "", **fields}
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
            "origin": doc.get("origin") or "pdf", "original_id": doc.get("original_id") or "",
            "imported_at": doc.get("imported_at"),
            "conditions": len(plan.get("conditions") or {}),
            "steps": len(plan.get("steps") or []),
            "wells": len(plan_wells(plan))}


def plan_wells(plan: dict) -> set[str]:
    """Every well the plan uses: its conditions' wells, else its steps'."""
    conds = {w for c in (plan.get("conditions") or {}).values() for w in (c or {}).get("wells") or []}
    return conds or {w for s in plan.get("steps") or [] for w in (s or {}).get("wells") or []}


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
