"""Loaders for local reference config (TL aliases etc.)."""
import json
from pathlib import Path

DEFAULT_ALIASES_PATH = Path("config/tl_aliases.json")


def load_aliases(path=DEFAULT_ALIASES_PATH):
    """Return {lowercased key -> {'email','name'}}, or {} if the file is absent.

    Ignores keys starting with '_' (used for comments)."""
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in data.items() if not k.startswith("_")}
