"""Load configurable business/jurisdiction rules from config/rules.json."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config" / "rules.json"


@lru_cache(maxsize=4)
def load_rules(path: str | None = None) -> dict:
    p = Path(path) if path else RULES_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)
