"""Copy src/craigslist_auto/events.py → backend/app/schemas/events.py.

Run this after editing the source of truth in src/craigslist_auto/events.py.
Docker builds do the same copy automatically via the backend Dockerfile.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "craigslist_auto" / "events.py"
DST = ROOT / "backend" / "app" / "schemas" / "events.py"


def main() -> int:
    if not SRC.exists():
        print(f"ERROR: source not found: {SRC}", file=sys.stderr)
        return 1
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    print(f"Copied {SRC.relative_to(ROOT)} → {DST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
