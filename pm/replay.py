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
"""
from __future__ import annotations

from collections import defaultdict

from .events import ADJUSTMENT, SNAPSHOT, TRADE, all_events

EPS = 1e-6


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
                "text": f"{ev.get('side','').upper()} {qty:g} {pos['symbol']} @ {price:,.2f}",
                "source": ev.get("source"),
            })

        elif etype == SNAPSHOT:
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
                    else:
                        pos["cost"] = 0.0
                        pos["cost_known"] = False

                if abs(new_qty - old_qty) > EPS and old_qty > EPS:
                    drift.append(f"{pos['symbol']} {old_qty:g}→{new_qty:g}")
                pos["last_event"] = ev["ts"]

            # Anything the statement omits is no longer in the demat account.
            for key, pos in book.items():
                if key not in seen and pos["qty"] > EPS:
                    drift.append(f"{pos['symbol']} {pos['qty']:g}→0")
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
                "text": f"{pos['symbol']} {delta:+g} — {ev.get('reason','')}",
                "source": ev.get("source"),
            })

    # Drop closed positions; flag anything we can't price honestly.
    holdings: dict[str, list[dict]] = {}
    for member, book in books.items():
        live = []
        for pos in book.values():
            if pos["qty"] <= EPS:
                continue
            pos = dict(pos)
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
