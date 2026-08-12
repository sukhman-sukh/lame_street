"""The event log: schema, deterministic IDs, append, and read-back.

The log is the only source of truth. `data/state/` is a cache derived from it and
can be deleted at any time.

Deterministic IDs are what make ingestion safe to repeat. The same contract note
parsed a hundred times produces the same IDs a hundred times, so re-running a
sync — or re-parsing every raw document after fixing a parser — can never
double-count a trade.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from . import paths
from .store import append_jsonl, read_jsonl

IST = timezone(timedelta(hours=5, minutes=30))

TRADE = "trade"
SNAPSHOT = "snapshot"
ADJUSTMENT = "adjustment"

# Where a fact came from, roughly in descending order of trust.
SRC_HOLDINGS_STATEMENT = "holdings_statement"  # depository/broker — authoritative quantity
SRC_BROWSER = "browser"                        # logged-in fetch — authoritative qty + cost
SRC_CONTRACT_NOTE = "contract_note"            # daily trades
SRC_MANUAL = "manual"
SRC_CSV = "csv"
SRC_RECONCILE = "reconcile"


def now_ist() -> datetime:
    return datetime.now(IST)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat(timespec="seconds")


def _digest(*parts: object) -> str:
    canon = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _round(value: float | None, places: int = 4) -> float | None:
    """Round before hashing so float noise from a re-parse can't change an ID."""
    return None if value is None else round(float(value), places)


def make_trade(
    *,
    member: str,
    ts: datetime,
    side: str,
    isin: str | None,
    symbol: str,
    qty: float,
    price: float,
    name: str = "",
    exchange: str = "",
    charges: float = 0.0,
    trade_no: str | None = None,
    order_no: str | None = None,
    source: str = SRC_CONTRACT_NOTE,
    doc: str | None = None,
) -> dict:
    key = isin or symbol.upper()
    # A trade number is the broker's own unique key — the best dedupe anchor there
    # is. Without one we fall back to the natural key of the trade itself.
    if trade_no:
        ident = _digest("trade", member, key, trade_no, side, _round(qty), _round(price))
    else:
        ident = _digest("trade", member, key, ts.date(), side, _round(qty), _round(price))
    return {
        "id": ident,
        "type": TRADE,
        "ts": iso(ts),
        "member": member,
        "side": side.lower(),
        "isin": isin,
        "symbol": symbol.upper(),
        "name": name,
        "exchange": exchange,
        "qty": float(qty),
        "price": float(price),
        "charges": float(charges or 0.0),
        "trade_no": trade_no,
        "order_no": order_no,
        "source": source,
        "doc": doc,
        "ingested_at": iso(now_ist()),
    }


def make_snapshot(
    *,
    member: str,
    ts: datetime,
    holdings: list[dict],
    source: str,
    doc: str | None = None,
    has_cost: bool = False,
) -> dict:
    """`holdings` rows: {isin, symbol, name, qty, avg?}.

    has_cost says whether `avg` is trustworthy. Depository and settlement
    statements carry quantity but never what you paid, so they come in with
    has_cost=False and the replay preserves whatever cost basis it already knew.
    """
    rows = sorted(
        (
            {
                "isin": h.get("isin"),
                "symbol": (h.get("symbol") or "").upper(),
                "name": h.get("name", ""),
                "qty": float(h["qty"]),
                "avg": (float(h["avg"]) if h.get("avg") is not None else None),
            }
            for h in holdings
        ),
        key=lambda h: (h["isin"] or "", h["symbol"]),
    )
    fingerprint = _digest(
        "snapshot", member, ts.date(), source,
        json.dumps([(r["isin"], r["symbol"], _round(r["qty"]), _round(r["avg"])) for r in rows]),
    )
    return {
        "id": fingerprint,
        "type": SNAPSHOT,
        "ts": iso(ts),
        "member": member,
        "holdings": rows,
        "has_cost": bool(has_cost),
        "source": source,
        "doc": doc,
        "ingested_at": iso(now_ist()),
    }


def make_adjustment(
    *,
    member: str,
    ts: datetime,
    isin: str | None,
    symbol: str,
    qty_delta: float,
    reason: str,
    preserve_cost: bool = True,
    source: str = SRC_RECONCILE,
    doc: str | None = None,
) -> dict:
    """A correction. The log is never rewritten — divergence is a new entry.

    preserve_cost=True keeps total money invested unchanged while quantity moves,
    which is exactly right for a bonus issue or a split.
    """
    key = isin or symbol.upper()
    return {
        "id": _digest("adj", member, key, ts.date(), _round(qty_delta), reason),
        "type": ADJUSTMENT,
        "ts": iso(ts),
        "member": member,
        "isin": isin,
        "symbol": symbol.upper(),
        "qty_delta": float(qty_delta),
        "preserve_cost": bool(preserve_cost),
        "reason": reason,
        "source": source,
        "doc": doc,
        "ingested_at": iso(now_ist()),
    }


def shard_for(ts_iso: str) -> Path:
    return paths.EVENTS / f"{ts_iso[:7]}.jsonl"


def all_events() -> list[dict]:
    """Every event, ordered by business time.

    Ties break on type first so that a snapshot lands before same-timestamp
    trades, then on id for a stable total order across runs.
    """
    rows: list[dict] = []
    if paths.EVENTS.exists():
        for shard in sorted(paths.EVENTS.glob("*.jsonl")):
            rows.extend(read_jsonl(shard))
    order = {SNAPSHOT: 0, TRADE: 1, ADJUSTMENT: 2}
    rows.sort(key=lambda e: (e.get("ts", ""), order.get(e.get("type"), 9), e.get("id", "")))
    return rows


def existing_ids() -> set[str]:
    return {e["id"] for e in all_events() if "id" in e}


def append(events: Iterable[dict]) -> tuple[int, int]:
    """Append events, dropping any whose ID is already in the log.

    Returns (written, skipped_as_duplicate).
    """
    events = list(events)
    if not events:
        return 0, 0
    known = existing_ids()
    fresh, dupes = [], 0
    seen_now: set[str] = set()
    for ev in events:
        if ev["id"] in known or ev["id"] in seen_now:
            dupes += 1
            continue
        seen_now.add(ev["id"])
        fresh.append(ev)

    by_shard: dict[Path, list[dict]] = {}
    for ev in fresh:
        by_shard.setdefault(shard_for(ev["ts"]), []).append(ev)
    written = 0
    for shard, rows in by_shard.items():
        written += append_jsonl(shard, rows)
    return written, dupes
