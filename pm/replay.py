"""Replay the event log into current holdings.

    holdings_now = snapshot(t0) + every event after t0

Cost basis uses the weighted-average method: a sell reduces quantity and removes
its proportional share of cost, leaving the average untouched.

The subtle case is a quantity-only snapshot (depository and settlement statements
give you ISIN and quantity but never what you paid). There we keep total money
invested fixed and let quantity move, so the average recomputes on its own. That
happens to be exactly the right accounting for a bonus issue or a stock split —
you own more shares, you paid nothing extra, your average falls. Those two events
are the main reason derived state drifts, and this rule absorbs them for free.

Which snapshot wins is decided by where it came from, not only by when:

    an uploaded export, then the trades after it
    — or, if there has never been one, the holdings statements from the mail

An export is the broker's own view and states cost; a statement is the
depository's and cannot. So an export anchors the book, and statements arriving
after it are checked against it and reported rather than applied. The cost of that
rule is that a bonus or split — which only ever shows up in a statement — stops
being absorbed automatically once an export is in force; it surfaces as a
`statement_disagrees` warning instead, and a fresh export clears it.
"""
from __future__ import annotations

from collections import defaultdict

from .events import (
    ADJUSTMENT,
    SNAPSHOT,
    SRC_BROWSER,
    SRC_CSV,
    SRC_MANUAL,
    TRADE,
    all_events,
)

EPS = 1e-6

# How much a snapshot is worth when two of them disagree.
#
# A broker's own export states quantity *and* what was paid. A depository
# statement states quantity alone — it is the custodian's record of what sits in
# the demat account, and it has no idea what any of it cost. So once an export has
# anchored a member's book, a statement arriving later no longer supersedes it:
# the book carries forward from the export and the trades after it.
#
# Deliberately narrow: only something a person handed over on purpose — an export,
# a browser fetch, a manual entry — outranks the mail. A statement that happens to
# carry cost is still just mail, and still gets superseded by the next one.
ANCHOR_SOURCES = (SRC_CSV, SRC_BROWSER, SRC_MANUAL)


def _rank(ev: dict) -> int:
    return 2 if ev.get("source") in ANCHOR_SOURCES else 1


def _key(isin: str | None, symbol: str) -> str:
    """ISIN when we have it — symbols get renamed, ISINs don't."""
    return (isin or "").strip().upper() or symbol.strip().upper()


def _new_position(isin, symbol, name) -> dict:
    return {
        "isin": isin,
        "symbol": (symbol or "").upper(),
        "name": name or "",
        "qty": 0.0,
        "cost": 0.0,          # total money in, for the quantity still held
        "cost_known": True,   # False once a snapshot hands us shares we never saw bought
        "first_seen": None,
        "last_event": None,
        # What the position cost per share the last time a snapshot retired it.
        # A statement that omits a holding and a later one that lists it again is
        # overwhelmingly a gap in the paperwork rather than a sale followed by a
        # repurchase, so remembering the average is what stops a round trip
        # through an incomplete statement erasing the cost basis for good.
        "retired_avg": None,
        "retired_cost_known": True,
    }


def _avg(pos: dict) -> float:
    return pos["cost"] / pos["qty"] if pos["qty"] > EPS else 0.0


def replay(events: list[dict] | None = None) -> dict:
    """Fold the log into per-member positions plus everything worth reporting."""
    events = all_events() if events is None else events

    books: dict[str, dict[str, dict]] = defaultdict(dict)
    warnings: list[dict] = []
    activity: list[dict] = []
    last_snapshot: dict[str, dict] = {}   # member -> {ts, source, has_cost}
    last_trade: dict[str, str] = {}
    anchored: dict[str, int] = {}         # member -> rank of the snapshot in force

    for ev in events:
        member = ev.get("member")
        if not member:
            continue
        book = books[member]
        etype = ev.get("type")

        if etype == TRADE:
            key = _key(ev.get("isin"), ev.get("symbol", ""))
            pos = book.setdefault(key, _new_position(ev.get("isin"), ev.get("symbol"), ev.get("name")))
            pos["first_seen"] = pos["first_seen"] or ev["ts"]
            pos["last_event"] = ev["ts"]
            if ev.get("name") and not pos["name"]:
                pos["name"] = ev["name"]
            qty, price = float(ev["qty"]), float(ev["price"])
            charges = float(ev.get("charges") or 0.0)

            if ev.get("side") == "buy":
                pos["qty"] += qty
                pos["cost"] += qty * price + charges
            else:
                if qty > pos["qty"] + EPS:
                    # Selling more than the log knows about: an earlier buy never
                    # reached us. Clamp rather than go negative, and say so.
                    warnings.append({
                        "member": member, "symbol": pos["symbol"], "kind": "oversell",
                        "detail": f"sold {qty:g} but log only had {pos['qty']:g}",
                        "ts": ev["ts"],
                    })
                    pos["qty"], pos["cost"] = 0.0, 0.0
                    pos["cost_known"] = False
                else:
                    unit = _avg(pos)
                    pos["qty"] -= qty
                    pos["cost"] = max(0.0, pos["cost"] - unit * qty)
                    if pos["qty"] <= EPS:
                        pos["qty"], pos["cost"] = 0.0, 0.0
            last_trade[member] = ev["ts"]
            activity.append({
                "ts": ev["ts"], "member": member, "kind": "trade",
                # The instrument this happened to, so the feed can be sliced per
                # company as well as read whole.
                "key": key, "symbol": pos["symbol"],
                "text": f"{ev.get('side','').upper()} {qty:g} {pos['symbol']} @ {price:,.2f}",
                "source": ev.get("source"),
            })

        elif etype == SNAPSHOT:
            rank = _rank(ev)
            if rank < anchored.get(member, 0):
                # Outranked: an export already states this book, cost and all, so a
                # quantity-only statement no longer supersedes it. It is still the
                # depository's own count though, so it gets checked against what we
                # hold and any disagreement is reported rather than dropped —
                # otherwise a missed contract note could drift unnoticed forever.
                disagreed = []
                listed = set()
                for row in ev.get("holdings", []):
                    key = _key(row.get("isin"), row.get("symbol", ""))
                    listed.add(key)
                    have = (book.get(key) or {}).get("qty", 0.0)
                    want = float(row["qty"])
                    if abs(have - want) > EPS:
                        disagreed.append(f"{row.get('symbol') or key} {have:g}→{want:g}")
                for key, pos in book.items():
                    if key not in listed and pos["qty"] > EPS:
                        disagreed.append(f"{pos['symbol']} {pos['qty']:g}→0")
                if disagreed:
                    warnings.append({
                        "member": member, "kind": "statement_disagrees", "ts": ev["ts"],
                        "symbol": None,
                        "detail": "a holdings statement disagrees with the uploaded export "
                                  "that supersedes it: " + ", ".join(disagreed[:8])
                                  + (f" (+{len(disagreed) - 8} more)" if len(disagreed) > 8 else "")
                                  + " — upload a fresh export if the statement is right",
                        "source": ev.get("source"),
                    })
                activity.append({
                    "ts": ev["ts"], "member": member, "kind": "snapshot",
                    "text": f"Holdings statement — {len(ev.get('holdings', []))} positions "
                            "(not applied; an uploaded export is in force)",
                    "source": ev.get("source"),
                })
                continue
            anchored[member] = rank

            has_cost = bool(ev.get("has_cost"))
            seen: set[str] = set()
            drift: list[str] = []

            for row in ev.get("holdings", []):
                key = _key(row.get("isin"), row.get("symbol", ""))
                seen.add(key)
                pos = book.get(key)
                if pos is None:
                    pos = _new_position(row.get("isin"), row.get("symbol"), row.get("name"))
                    pos["first_seen"] = ev["ts"]
                    book[key] = pos
                if row.get("name") and not pos["name"]:
                    pos["name"] = row["name"]

                new_qty = float(row["qty"])
                old_qty, old_avg = pos["qty"], _avg(pos)

                if has_cost and row.get("avg") is not None:
                    pos["qty"] = new_qty
                    pos["cost"] = new_qty * float(row["avg"])
                    pos["cost_known"] = True
                else:
                    pos["qty"] = new_qty
                    if old_qty > EPS:
                        # Hold total cost steady; the average moves. Correct for
                        # bonuses and splits, and a reasonable guess otherwise.
                        pass
                    elif old_avg > 0:
                        pos["cost"] = new_qty * old_avg
                    elif pos["retired_avg"]:
                        # Back from the dead: an earlier statement dropped it and
                        # this one has it again. Price it at what it cost before.
                        pos["cost"] = new_qty * pos["retired_avg"]
                        pos["cost_known"] = pos["retired_cost_known"]
                    else:
                        pos["cost"] = 0.0
                        pos["cost_known"] = False

                if abs(new_qty - old_qty) > EPS and old_qty > EPS:
                    drift.append(f"{pos['symbol']} {old_qty:g}→{new_qty:g}")
                pos["last_event"] = ev["ts"]

            # Anything the statement omits is no longer in the demat account —
            # but only if the statement was read whole. A parser that loses a page
            # produces a document that looks exactly like a liquidation, and acting
            # on it destroys the quantity and the cost basis of every position it
            # failed to read. Nobody sells most of a portfolio and keeps the rest in
            # one month, so past a threshold the likelier explanation is the
            # paperwork, and the safe move is to correct what the statement lists
            # and leave the rest alone.
            #
            # One class of omission is not a sale at all: Indian equity settles
            # T+1, so shares bought on the statement's own date are not in the
            # demat account when it is drawn up. The statement is stamped at
            # 23:59 precisely so it supersedes that day's trades, and for
            # everything already settled that is right — but for a purchase made
            # that same day it would retire a position the document could not
            # have seen.
            omitted = [(key, pos) for key, pos in book.items()
                       if key not in seen and pos["qty"] > EPS
                       and (pos["last_event"] or "")[:10] != ev["ts"][:10]]
            held = sum(1 for pos in book.values() if pos["qty"] > EPS)
            if omitted and len(omitted) > max(3, held * 0.5):
                warnings.append({
                    "member": member, "kind": "partial_snapshot", "ts": ev["ts"],
                    "symbol": None,
                    "detail": f"statement lists {len(ev.get('holdings', []))} positions but "
                              f"omits {len(omitted)} of {held} held — too much to be a sale, "
                              "so it was read as incomplete and the omitted positions were "
                              "left untouched",
                    "source": ev.get("source"),
                })
                omitted = []

            for key, pos in omitted:
                drift.append(f"{pos['symbol']} {pos['qty']:g}→0")
                pos["retired_avg"] = _avg(pos) or pos["retired_avg"]
                pos["retired_cost_known"] = pos["cost_known"]
                pos["qty"], pos["cost"] = 0.0, 0.0
                pos["last_event"] = ev["ts"]

            if drift:
                warnings.append({
                    "member": member, "kind": "drift", "ts": ev["ts"],
                    "detail": "snapshot disagreed with the log: " + ", ".join(drift[:8])
                              + (f" (+{len(drift) - 8} more)" if len(drift) > 8 else ""),
                    "source": ev.get("source"),
                })
            last_snapshot[member] = {
                "ts": ev["ts"], "source": ev.get("source"), "has_cost": has_cost,
                "positions": len(ev.get("holdings", [])),
            }
            activity.append({
                "ts": ev["ts"], "member": member, "kind": "snapshot",
                "text": f"Holdings snapshot — {len(ev.get('holdings', []))} positions"
                        + ("" if has_cost else " (quantity only)"),
                "source": ev.get("source"),
            })

        elif etype == ADJUSTMENT:
            key = _key(ev.get("isin"), ev.get("symbol", ""))
            pos = book.setdefault(key, _new_position(ev.get("isin"), ev.get("symbol"), ""))
            delta = float(ev["qty_delta"])
            if ev.get("preserve_cost", True):
                pos["qty"] = max(0.0, pos["qty"] + delta)
            else:
                unit = _avg(pos)
                pos["qty"] = max(0.0, pos["qty"] + delta)
                pos["cost"] = max(0.0, pos["cost"] + unit * delta)
            pos["last_event"] = ev["ts"]
            activity.append({
                "ts": ev["ts"], "member": member, "kind": "adjustment",
                "key": key, "symbol": pos["symbol"],
                "text": f"{pos['symbol']} {delta:+g} — {ev.get('reason','')}",
                "source": ev.get("source"),
            })

    # Drop closed positions; flag anything we can't price honestly.
    holdings: dict[str, list[dict]] = {}
    for member, book in books.items():
        live = []
        for key, pos in book.items():
            if pos["qty"] <= EPS:
                continue
            pos = dict(pos)
            # The book's own key, carried out so callers address a position the
            # same way the log does — anything derived from the symbol alone
            # drifts the moment a company is renamed.
            pos["key"] = key
            pos["avg"] = _avg(pos)
            live.append(pos)
            if not pos["cost_known"]:
                warnings.append({
                    "member": member, "symbol": pos["symbol"], "kind": "cost_unknown",
                    "detail": "quantity is known but purchase cost was never seen — "
                              "P&L for this position is not meaningful yet",
                    "ts": pos["last_event"],
                })
        live.sort(key=lambda p: p["symbol"])
        holdings[member] = live

    activity.sort(key=lambda a: a["ts"], reverse=True)
    return {
        "holdings": holdings,
        "warnings": warnings,
        "activity": activity,
        "last_snapshot": last_snapshot,
        "last_trade": last_trade,
        "event_count": len(events),
    }
