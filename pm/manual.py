"""Manual entry and CSV import.

This is the escape hatch that keeps the dashboard useful no matter what the email
pipeline is doing. It is also how cost basis gets bootstrapped: depository
statements tell you what you own but never what you paid, so the first holdings
load for a member usually comes from a broker CSV export or a browser fetch.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path

from . import events as ev
from .events import IST, now_ist
from .parsers.common import find_isin, to_number

SYMBOL_KEYS = ("symbol", "scrip", "ticker", "stock", "instrument", "security", "company", "name")
ISIN_KEYS = ("isin",)
QTY_KEYS = ("quantity", "qty", "shares", "units", "holding", "balance")
AVG_KEYS = ("average", "avg", "buy price", "buy avg", "cost price", "purchase price", "cost")


def _at(when: date | None, hour: int, minute: int) -> datetime:
    when = when or now_ist().date()
    return datetime(when.year, when.month, when.day, hour, minute, tzinfo=IST)


def add_trade(
    *, member: str, symbol: str, side: str, qty: float, price: float,
    when: date | None = None, isin: str | None = None, charges: float = 0.0,
    name: str = "",
) -> dict:
    return ev.make_trade(
        member=member, ts=_at(when, 15, 30), side=side, isin=isin,
        symbol=symbol, name=name or symbol, qty=qty, price=price,
        charges=charges, source=ev.SRC_MANUAL,
        # Manual entries have no broker trade number, so two identical trades on
        # the same day would collapse into one. A counter keeps them distinct.
        trade_no=f"manual-{ev.now_ist().timestamp():.0f}",
    )


def set_holdings(
    *, member: str, rows: list[dict], when: date | None = None,
    source: str = ev.SRC_MANUAL, doc: str | None = None,
) -> dict:
    """Record a complete holdings snapshot for a member.

    `rows` need symbol and qty; avg is optional but strongly preferred, since
    without it no P&L can be computed for positions we've never seen bought.
    """
    has_cost = any(r.get("avg") for r in rows)
    return ev.make_snapshot(
        member=member, ts=_at(when, 23, 59), holdings=rows,
        source=source, doc=doc, has_cost=has_cost,
    )


def add_adjustment(
    *, member: str, symbol: str, qty_delta: float, reason: str,
    isin: str | None = None, when: date | None = None, preserve_cost: bool = True,
) -> dict:
    return ev.make_adjustment(
        member=member, ts=_at(when, 23, 58), isin=isin, symbol=symbol,
        qty_delta=qty_delta, reason=reason, preserve_cost=preserve_cost,
        source=ev.SRC_MANUAL,
    )


def _squash(text: str) -> str:
    text = re.sub(r"(?i)\b(limited|ltd|the|and|company|co|india|indian)\b", " ", text or "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_to_holdings(rows: list[dict], known: list[dict]) -> tuple[list[dict], list[str]]:
    """Give every CSV row an ISIN, so it lands on the position it belongs to.

    Broker exports name the company and leave the ISIN out, but positions are
    keyed by ISIN — everything else gets renamed sooner or later. Without this the
    snapshot would key on the name instead, which does not merely lose the link:
    it creates a *second* position for a company already held, and the holding it
    failed to recognise looks like one the statement omitted.

    NSE's list settles most of it. `known` — what the member already holds —
    settles the rest, and is the only thing that can: a recently listed or
    unlisted security is in nobody's master list but is sitting in the portfolio
    under a name a person would recognise.
    """
    from . import instruments

    by_name = {_squash(k.get("name", "")): k for k in known if k.get("isin")}
    by_symbol = {_squash(k.get("symbol", "")): k for k in known if k.get("isin")}

    matched_master = matched_held = 0
    unmatched: list[str] = []
    for row in rows:
        if row.get("isin"):
            continue
        found = instruments.isin_for(row.get("name", ""), row.get("symbol", ""))
        if found:
            row["isin"] = found
            matched_master += 1
            continue
        key = _squash(row.get("name", "")) or _squash(row.get("symbol", ""))
        hit = by_name.get(key) or by_symbol.get(key)
        if not hit and len(key) >= 6:
            hit = next((k for name, k in by_name.items()
                        if name and (name.startswith(key) or key.startswith(name))), None)
        if hit:
            row["isin"] = hit["isin"]
            row["symbol"] = hit.get("symbol") or row["symbol"]
            matched_held += 1
        else:
            unmatched.append(row.get("name") or row.get("symbol", "?"))

    notes = []
    if matched_master:
        notes.append(f"matched {matched_master} row(s) to an ISIN via NSE's list")
    if matched_held:
        notes.append(f"matched {matched_held} row(s) to a position already held")
    if unmatched:
        notes.append(
            f"no ISIN for {len(unmatched)}: {', '.join(unmatched[:6])}"
            + (f" (+{len(unmatched) - 6} more)" if len(unmatched) > 6 else "")
            + " — recorded under their names, so check they aren't duplicates of "
              "something already held")
    return rows, notes


def _column(header: list[str], keys: tuple[str, ...]) -> int | None:
    lowered = [(h or "").strip().lower() for h in header]
    for key in keys:
        for i, cell in enumerate(lowered):
            if key == cell:
                return i
    for key in keys:
        for i, cell in enumerate(lowered):
            if key in cell:
                return i
    return None


def read_holdings_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Read a holdings CSV exported from a broker. Returns (rows, notes).

    Column names are matched loosely because every broker names them differently.
    """
    notes: list[str] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = list(csv.reader(fh))
    if not reader:
        return [], ["file is empty"]

    # The header is not always line 1 — exports often start with a title block.
    header_idx, header = 0, reader[0]
    for i, row in enumerate(reader[:10]):
        if _column(row, QTY_KEYS) is not None and _column(row, SYMBOL_KEYS) is not None:
            header_idx, header = i, row
            break

    col_symbol = _column(header, SYMBOL_KEYS)
    col_qty = _column(header, QTY_KEYS)
    col_avg = _column(header, AVG_KEYS)
    col_isin = _column(header, ISIN_KEYS)

    if col_symbol is None or col_qty is None:
        return [], [f"could not find symbol and quantity columns in: {', '.join(header)}"]
    if col_avg is None:
        notes.append("no average-price column found — P&L will be unavailable "
                     "for these positions until cost is supplied")

    rows: list[dict] = []
    for line in reader[header_idx + 1:]:
        if not any((c or "").strip() for c in line):
            continue
        try:
            symbol = (line[col_symbol] or "").strip()
            qty = to_number(line[col_qty]) if col_qty < len(line) else None
        except IndexError:
            continue
        if not symbol or not qty or qty <= 0:
            continue
        avg = to_number(line[col_avg]) if (col_avg is not None and col_avg < len(line)) else None
        isin = None
        if col_isin is not None and col_isin < len(line):
            isin = find_isin(line[col_isin] or "")
        rows.append({
            "isin": isin,
            "symbol": symbol.upper(),
            "name": symbol,
            "qty": qty,
            "avg": avg if (avg and avg > 0) else None,
        })

    if not rows:
        notes.append("no usable rows found")
    return rows, notes
