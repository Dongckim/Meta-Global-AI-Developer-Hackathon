"""Demo reset: dry run lists, apply moves protocols, runs, searches and the
record into a backup and leaves the library empty (the demo reseeds).

Run: python -m server.tests.test_demo_reset (runs on import).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import demo_reset, library as lib  # noqa: E402

root = Path(tempfile.mkdtemp())
for rel in ("protocols/a.json", "runs/run-1.json", "searches.json", "record.json"):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("{}")
found, backup = demo_reset.reset(root)
assert len(found) == 4 and backup is None and (root / "record.json").exists()
found, backup = demo_reset.reset(root, apply=True)
assert sorted(p.relative_to(backup).as_posix() for p in backup.rglob("*.json")) == [
    "protocols/a.json", "record.json", "runs/run-1.json", "searches.json"]
assert demo_reset.targets(root) == []
assert [d["name"] for d in lib.all_docs(root)] == ["Demo protocol"], "the demo seeds again"
print("DR1 ok: dry run lists, apply backs up and clears")
print("ALL PASS")
