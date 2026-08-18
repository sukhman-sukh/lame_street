"""Editing endpoints — the two things a person changes by typing on the page.

Everything else in this app writes to the event log, because everything else is
a claim about what a document said. These two are not: a hand-set price and a
thesis are the person's own words, so they live in data/manual/ and are folded in
at build time instead.

Both rebuild the dashboard on the way out, since the dashboard is the only file
the viewer reads, and both schedule a backup — a value that exists on one
container's disk and nowhere else is not really saved.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import backup
from . import build as buildmod
from . import notes, overrides

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class OverrideIn(BaseModel):
    field: str = Field(max_length=20)
    key: str = Field(max_length=64)
    member: str = ""
    # A string rather than a number: this is what someone typed, commas and
    # rupee sign included, and an empty one means "go back to the derived value".
    value: str = Field("", max_length=200)


class NoteIn(BaseModel):
    key: str = Field(max_length=64)
    markdown: str = Field("", max_length=notes.MAX_LENGTH)


def _row(payload: dict, key: str) -> dict | None:
    return next((r for r in payload.get("consolidated", []) if r.get("key") == key), None)


@router.post("/override")
def set_override(payload: OverrideIn):
    """Set (or clear) one cell of the dashboard by hand."""
    try:
        result = overrides.set_value(payload.field, payload.key, payload.value,
                                     member=payload.member or None)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    dashboard = buildmod.build()
    backup.push_soon(f"{result['field']} set by hand")
    return {"ok": True, **result, "stock": _row(dashboard, result["key"])}


@router.delete("/override")
def clear_overrides():
    """Forget every typed-in value at once, and go back to what was derived."""
    dropped = overrides.clear_all()
    buildmod.build()
    if dropped:
        backup.push_soon("hand-set values cleared")
    return {"ok": True, "cleared": dropped}


@router.post("/note")
def set_note(payload: NoteIn):
    """Save one company's thesis. Markdown in, markdown out — it is never parsed
    here; the viewer renders it, and the file stays readable on its own."""
    try:
        result = notes.write(payload.key, payload.markdown)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    dashboard = buildmod.build()
    backup.push_soon("a thesis was edited")
    return {"ok": True, **result, "stock": _row(dashboard, result["key"])}


@router.get("/note/{key}")
def get_note(key: str):
    """The raw markdown for one company — for editing it, and for reading it
    without the dashboard."""
    try:
        note = notes.read(notes.clean_key(key))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"key": key.upper(), "note": note}
