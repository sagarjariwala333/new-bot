"""
hub_menu_layout.py
==================

The owner's own look of the Strategy Hub MENU (2026-11-05): the name and the grey sub-text of every function, and the
ORDER of the functions. One setting for ALL strategies, saved on the server (DATA_DIR/hub_menu_layout.json) so it is
the same on every device.

It is only how the menu LOOKS. It never touches a plan, a rule, a number or an open trade.
Nothing is stored for a function the owner did not change (an empty text = the original text), so the original
texts of a new version still show for those.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from app.fsutil import atomic_write

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
LAYOUT_FILE = DATA_DIR / "hub_menu_layout.json"

MAX_FUNCS = 60
MAX_NAME = 60
MAX_DESC = 300
_ID = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_lock = threading.Lock()


class MenuLayoutError(ValueError):
    pass


def blank() -> dict:
    return {"order": [], "names": {}, "descs": {}}


def _clean(layout) -> dict:
    """Checks the shape. Raises MenuLayoutError. Returns a clean copy (texts trimmed, empty texts dropped)."""
    if not isinstance(layout, dict):
        raise MenuLayoutError("The menu layout must be an object.")
    order = layout.get("order", [])
    names = layout.get("names", {})
    descs = layout.get("descs", {})
    if not isinstance(order, list) or not isinstance(names, dict) or not isinstance(descs, dict):
        raise MenuLayoutError("order must be a list; names and descs must be objects.")
    if len(order) > MAX_FUNCS or len(names) > MAX_FUNCS or len(descs) > MAX_FUNCS:
        raise MenuLayoutError("Too many entries.")
    seen = set()
    for i in order:
        if not isinstance(i, str) or not _ID.match(i):
            raise MenuLayoutError("A function id in the order is not valid.")
        if i in seen:
            raise MenuLayoutError("A function appears twice in the order.")
        seen.add(i)
    out = {"order": list(order), "names": {}, "descs": {}}
    for key, limit, label in (("names", MAX_NAME, "name"), ("descs", MAX_DESC, "text")):
        src = names if key == "names" else descs
        for i, t in src.items():
            if not isinstance(i, str) or not _ID.match(i):
                raise MenuLayoutError("A function id is not valid.")
            if not isinstance(t, str):
                raise MenuLayoutError(f"A {label} must be text.")
            t = " ".join(t.split())
            if len(t) > limit:
                raise MenuLayoutError(f"A {label} can be at most {limit} characters.")
            if t:
                out[key][i] = t
    return out


def load() -> dict:
    with _lock:
        try:
            raw = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
            return _clean(raw)
        except (OSError, ValueError):
            return blank()          # missing or unreadable: the original menu


def save(layout) -> dict:
    clean = _clean(layout)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(LAYOUT_FILE, json.dumps(clean, indent=1), secret=False)
    return clean
