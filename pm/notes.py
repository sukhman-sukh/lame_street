"""One markdown file per company — the thesis behind holding it.

A portfolio tells you what was bought; it never tells you why. That reasoning is
the part worth keeping and the part nothing in the mail pipeline can supply, so
it gets written by hand and stored beside the overrides in data/manual/.

Plain .md files rather than rows in a JSON blob, on purpose: a thesis written
over years should stay greppable, diffable and readable without this program.
They are keyed by the same instrument key the dashboard uses — the ISIN when
there is one, otherwise the trading symbol.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import paths
from .events import IST, iso
from .store import write_text

# A thesis, not a book. Also the ceiling that keeps a stray paste from filling
# the disk, and keeps every note comfortably inside the dashboard payload.
MAX_LENGTH = 100_000

# Keys come from the URL/request body, and they become a filename. Anything that
# is not plainly an ISIN or a ticker is refused rather than sanitised, so there
# is no clever input that resolves to a path outside the notes directory.
_KEY = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}$")


def clean_key(key: str) -> str:
    key = (key or "").strip().upper()
    if not _KEY.match(key) or ".." in key:
        raise ValueError("that isn't a stock this dashboard knows about")
    return key


def path_for(key: str) -> Path:
    path = (paths.NOTES / f"{clean_key(key)}.md").resolve()
    if path.parent != paths.NOTES.resolve():
        raise ValueError("refusing to write outside the notes folder")
    return path


def read(key: str) -> dict | None:
    """The note for one company, or None if nothing has been written."""
    try:
        path = path_for(key)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=IST)
    except OSError:
        return None
    if not text.strip():
        return None
    return {"markdown": text, "updated_at": iso(stamp), "words": len(text.split())}


def write(key: str, markdown: str) -> dict:
    """Save (or, given nothing but whitespace, delete) one company's note."""
    path = path_for(key)
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"that is longer than {MAX_LENGTH:,} characters — "
                         "keep the thesis to the argument")

    if not text.strip():
        path.unlink(missing_ok=True)
        return {"key": clean_key(key), "cleared": True, "note": None}

    if not text.endswith("\n"):
        text += "\n"
    write_text(path, text)
    return {"key": clean_key(key), "cleared": False, "note": read(key)}


def load_all() -> dict[str, dict]:
    """Every note, keyed by instrument, for the dashboard build."""
    if not paths.NOTES.exists():
        return {}
    found: dict[str, dict] = {}
    for path in sorted(paths.NOTES.glob("*.md")):
        note = read(path.stem)
        if note:
            found[path.stem.upper()] = note
    return found
