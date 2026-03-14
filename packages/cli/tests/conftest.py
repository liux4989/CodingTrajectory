from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORE_SRC = ROOT.parent / "core" / "src"

for p in (SRC, CORE_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
