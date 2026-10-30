"""Demo reset: an empty library and history before a demo.

Clears saved protocols (with their import hashes and review copies), runs,
web searches and the record (the active run, an open draft, the screen).
Everything cleared moves to server/data/backup-<time>/ first; nothing is
deleted outright. The Demo protocol seeds itself again on the next start.

    python -m server.demo_reset            # list what would be cleared (dry run)
    python -m server.demo_reset --apply    # do it (the server must be off)
"""

from __future__ import annotations

import shutil
import socket
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def targets(root: Path) -> list[Path]:
    """What a reset clears, under root."""
    out = sorted((root / "protocols").glob("*.json")) + sorted((root / "runs").glob("*.json"))
    return out + [p for p in (root / "searches.json", root / "record.json") if p.exists()]


def server_up(port: int = 8000) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def reset(root: Path | None = None, apply: bool = False) -> tuple[list[Path], Path | None]:
    """(cleared paths, backup folder); with apply False nothing moves."""
    root = root or DATA_DIR
    found = targets(root)
    if not apply or not found:
        return found, None
    backup = root / f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
    for p in found:
        dest = backup / p.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
    return found, backup


def main() -> None:
    apply = "--apply" in sys.argv
    if apply and server_up():
        print("The server is running on port 8000. Stop it first, then run this again.")
        sys.exit(1)
    found, backup = reset(apply=apply)
    if not found:
        print("Nothing to clear.")
        return
    for p in found:
        print(f"  {p.relative_to(DATA_DIR)}")
    print(f"Cleared {len(found)} files; backup in {backup}" if apply
          else f"Dry run: {len(found)} files. Stop the server, then: python -m server.demo_reset --apply")


if __name__ == "__main__":
    main()
