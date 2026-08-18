"""Values typed in by hand, which outrank whatever the pipeline derived.

Some numbers no document can supply. A recently listed security has no price on
Yahoo. A position bought before the mailbox history begins has a quantity but no
cost. A demerged company arrives under a name nobody recognises. Every one of
those leaves a cell on the dashboard that is blank or wrong, and the only source
left is the person looking at it.

So: an override file that `build()` consults after replaying the log and after
reading the price cache. It never touches the event log — the log stays the
record of what documents actually said — and it never touches the price cache,
so a price refresh cannot quietly wipe a typed-in price.

Two scopes, because two different questions:

    instrument   price, name        — one company, everyone who holds it
    position     qty, avg, cost     — one company in one person's book

`avg` and `cost` state the same fact two ways (cost = qty × avg), so setting
either clears the other rather than leaving the pair to contradict itself.

The file lives in data/manual/, which travels in the encrypted backup — these
values exist on this disk and nowhere else.
"""
from __future__ import annotations

import re

from . import paths
from .events import iso, now_ist
from .store import read_json, write_json

# field -> scope
FIELDS = {
    "price": "instrument",
    "name": "instrument",
    "qty": "position",
    "avg": "position",
    "cost": "position",
}

NUMERIC = {"price", "qty", "avg", "cost"}

# Setting one of a pair clears the other; keeping both would be a contradiction
# with a silent winner.
PAIRED = {"avg": "cost", "cost": "avg"}

MAX_NAME = 120

# Generous, but a share price is not a phone number.
MAX_NUMBER = 1e12

_KEY = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}$")
_MEMBER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}$")


def blank() -> dict:
    return {"version": 1, "instrument": {}, "position": {}}


def load() -> dict:
    data = read_json(paths.OVERRIDES, default=None)
    if not isinstance(data, dict):
        return blank()
    for scope in ("instrument", "position"):
        if not isinstance(data.get(scope), dict):
            data[scope] = {}
    return data


def save(data: dict) -> None:
    write_json(paths.OVERRIDES, data)


# ------------------------------------------------------------------ addressing

def clean_key(key: str) -> str:
    """An instrument key: the ISIN when there is one, else the trading symbol."""
    key = (key or "").strip().upper()
    if not _KEY.match(key):
        raise ValueError("that isn't a stock this dashboard knows about")
    return key


def clean_member(member: str | None) -> str:
    member = (member or "").strip().lower()
    if not _MEMBER.match(member):
        raise ValueError("which person's position is this?")
    return member


def _slot(field: str, key: str, member: str | None) -> tuple[str, str]:
    """(scope, record id) for one field of one thing."""
    scope = FIELDS.get(field)
    if not scope:
        raise ValueError(f"{field!r} is not something that can be set by hand")
    key = clean_key(key)
    if scope == "instrument":
        return scope, key
    return scope, f"{clean_member(member)}|{key}"


# --------------------------------------------------------------------- reading

def for_instrument(data: dict, key: str) -> dict:
    return {k: v for k, v in (data.get("instrument", {}).get((key or "").upper()) or {}).items()
            if k in FIELDS}


def for_position(data: dict, member: str, key: str) -> dict:
    record = data.get("position", {}).get(f"{member}|{(key or '').upper()}") or {}
    return {k: v for k, v in record.items() if k in FIELDS}


def count(data: dict) -> int:
    """How many individual values are currently in force."""
    return sum(1 for scope in ("instrument", "position")
               for record in data.get(scope, {}).values()
               for field in record if field in FIELDS)


def summary(data: dict) -> dict:
    fields: dict[str, int] = {}
    for scope in ("instrument", "position"):
        for record in data.get(scope, {}).values():
            for field in record:
                if field in FIELDS:
                    fields[field] = fields.get(field, 0) + 1
    return {"values": count(data), "fields": fields}


# --------------------------------------------------------------------- writing

def parse_value(field: str, raw) -> float | str | None:
    """Turn what someone typed into a stored value. None means "clear it"."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    if field not in NUMERIC:
        if len(text) > MAX_NAME:
            raise ValueError(f"keep it under {MAX_NAME} characters")
        return text

    # People paste what they see: ₹1,23,456.78, or a stray space.
    cleaned = re.sub(r"[,\s₹]", "", text)
    try:
        number = float(cleaned)
    except ValueError:
        raise ValueError(f"“{text}” isn't a number")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("that isn't a number")
    if number < 0:
        raise ValueError("it can't be negative")
    if number > MAX_NUMBER:
        raise ValueError("that is implausibly large — check the decimal point")
    if field in ("price", "qty") and number == 0:
        # Zero quantity would mean "sold", which belongs in the log as a trade,
        # not here; zero price would silently zero the position's value.
        raise ValueError("zero isn't a useful value here — leave it empty to clear instead")
    return round(number, 4)


def set_value(field: str, key: str, value, member: str | None = None) -> dict:
    """Store (or, for an empty value, clear) one field. Returns what changed."""
    scope, slot = _slot(field, key, member)
    parsed = parse_value(field, value)

    data = load()
    record = dict(data[scope].get(slot) or {})
    before = record.get(field)

    if parsed is None:
        record.pop(field, None)
    else:
        record[field] = parsed
        paired = PAIRED.get(field)
        if paired:
            record.pop(paired, None)

    if any(f in FIELDS for f in record):
        record["updated_at"] = iso(now_ist())
        data[scope][slot] = record
    else:
        # Nothing left worth keeping — don't leave an empty shell behind.
        data[scope].pop(slot, None)

    save(data)
    return {
        "field": field,
        "scope": scope,
        "key": clean_key(key),
        "member": member if scope == "position" else None,
        "value": parsed,
        "previous": before,
        "cleared": parsed is None,
        "manual": summary(data),
    }


def clear_all() -> int:
    """Forget every typed-in value. Returns how many were dropped."""
    data = load()
    dropped = count(data)
    save(blank())
    return dropped
