"""Assemble data/public/dashboard.json — the only file the viewer reads.

Keeping this as a build step rather than computing on request is what lets the
viewer be a static page. It can be opened locally or hosted anywhere; it never
needs the collector running.
"""
from __future__ import annotations

from . import config as cfgmod
from . import instruments, notes, overrides, paths, prices
from .events import iso, now_ist
from .replay import replay
from .store import read_json, write_json

LAST_SYNC = paths.STATE / "last_sync.json"

# Bumped whenever the viewer starts needing a field this file did not write
# before. dashboard.json is a build artefact that only a sync or a refresh
# replaces, so after an upgrade the page would otherwise keep being handed a
# payload missing the fields it now reads — and fail quietly, which is the worst
# way to fail. The server compares this and re-derives when it does not match.
SCHEMA = 3

# How much history rides along in the payload. Both feeds are paginated, and
# /api/activity serves any page from the whole log — so these only decide how
# deep a *statically hosted* copy can be read, and how much a page that is
# re-fetched every couple of minutes has to carry. Six years of trades is half a
# megabyte; the first page or two is a few kilobytes.
ACTIVITY_IN_PAYLOAD = 100
TRADES_PER_STOCK = 50


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def collect_tickers() -> list[str]:
    """Yahoo tickers for everything currently held."""
    state = replay()
    registry = instruments.load_registry()
    tickers = set()
    for positions in state["holdings"].values():
        for pos in positions:
            entry = instruments.resolve(
                registry, isin=pos.get("isin"), symbol=pos["symbol"], name=pos.get("name", "")
            )
            if entry.get("yahoo"):
                tickers.add(entry["yahoo"])
    instruments.save_registry(registry)
    return sorted(tickers)


def build() -> dict:
    cfg = cfgmod.load()
    state = replay()
    registry = instruments.load_registry()
    price_cache = prices.load_cache()
    # Everything a person typed in: values that outrank what was derived, and the
    # thesis behind each holding.
    fixed = overrides.load()
    theses = notes.load_all()

    members_out: list[dict] = []
    consolidated: dict[str, dict] = {}
    attention: list[dict] = []
    any_stale = False
    # (member, symbol) pairs whose cost the replay could not find but a person
    # has since supplied. Their "cost was never seen" warning is answered.
    cost_by_hand: set[tuple[str, str]] = set()
    # Which hand-set values actually landed on a position. A stored value for
    # something nobody holds any more changes nothing, and counting it would put
    # a number in the header that the page cannot account for anywhere.
    applied: set[tuple[str, ...]] = set()

    member_names = {m.id: m.name for m in cfg.members}
    # A member with no rows yet should still show up on the dashboard.
    member_ids = [m.id for m in cfg.active_members()] or sorted(state["holdings"])

    for member_id in member_ids:
        name = member_names.get(member_id, member_id)
        rows_out = []
        invested = current = day_change = 0.0
        priced_cost = 0.0   # cost of only the positions we could price

        for pos in state["holdings"].get(member_id, []):
            entry = instruments.resolve(
                registry, isin=pos.get("isin"), symbol=pos["symbol"], name=pos.get("name", "")
            )
            # The log's own key for this position, so a typed-in value stays
            # attached to the company through a rename.
            key = (pos.get("key") or pos.get("isin") or entry.get("symbol")
                   or pos["symbol"]).upper()
            typed_stock = overrides.for_instrument(fixed, key)
            typed_pos = overrides.for_position(fixed, member_id, key)

            quote = prices.quote_for(price_cache, entry.get("yahoo"))
            price, prev = quote.get("price"), quote.get("prev_close")
            manual_price = "price" in typed_stock
            if manual_price:
                # A hand-set price has no yesterday to be compared against, so
                # the day's change is left blank rather than invented — and a
                # failed fetch is no longer worth calling stale.
                price, prev = typed_stock["price"], None
            any_stale = any_stale or bool(quote.get("stale") and entry.get("yahoo")
                                          and not manual_price)

            qty, cost, avg = pos["qty"], pos["cost"], pos["avg"]
            cost_known = pos["cost_known"]
            if "qty" in typed_pos:
                # A corrected share count says what is held, not what a share
                # cost — so the average holds and the money invested follows it.
                qty = typed_pos["qty"]
                cost = qty * avg
            if "avg" in typed_pos:
                avg = typed_pos["avg"]
                cost = qty * avg
                cost_known = True
            elif "cost" in typed_pos:
                cost = typed_pos["cost"]
                avg = cost / qty if qty else None
                cost_known = True
            if cost_known and not pos["cost_known"]:
                cost_by_hand.add((member_id, pos["symbol"]))

            name_out = typed_stock.get("name") or entry.get("name") or pos.get("name", "")
            value = qty * price if price else None
            pnl = (value - cost) if (value is not None and cost_known) else None
            change = qty * (price - prev) if (price and prev) else None
            typed_fields = sorted(set(typed_stock) | set(typed_pos))
            applied.update(("instrument", key, f) for f in typed_stock)
            applied.update(("position", member_id, key, f) for f in typed_pos)

            invested += cost
            if value is not None:
                current += value
                priced_cost += cost
            if change:
                day_change += change

            rows_out.append({
                "key": key,
                "isin": pos.get("isin"),
                "symbol": entry.get("symbol") or pos["symbol"],
                "name": name_out,
                "qty": round(qty, 4),
                "avg": _money(avg),
                "cost": _money(cost),
                "price": _money(price),
                "value": _money(value),
                "pnl": _money(pnl),
                "pnl_pct": _pct(pnl, cost),
                "day_change": _money(change),
                "cost_known": cost_known,
                "priced": price is not None,
                "stale": bool(quote.get("stale")) and not manual_price,
                # Which of these numbers were typed in rather than derived, so
                # the viewer can mark them and offer to clear them.
                "manual": typed_fields,
                "manual_price": manual_price,
            })

            agg = consolidated.setdefault(key, {
                "key": key,
                "isin": pos.get("isin"),
                "symbol": entry.get("symbol") or pos["symbol"],
                "name": name_out,
                "yahoo": entry.get("yahoo"),
                "qty": 0.0, "cost": 0.0, "value": 0.0, "day_change": 0.0,
                "price": _money(price), "priced": price is not None,
                "manual_price": manual_price,
                "manual": sorted(typed_stock),
                "holders": [],
            })
            agg["qty"] += qty
            agg["cost"] += cost
            if value is not None:
                agg["value"] += value
            if change:
                agg["day_change"] += change
            agg["holders"].append({
                "member": member_id, "member_name": name,
                "qty": round(qty, 4), "avg": _money(avg),
                "cost": _money(cost), "value": _money(value), "pnl": _money(pnl),
                "cost_known": cost_known,
                "manual": sorted(typed_pos),
            })

        rows_out.sort(key=lambda r: (r["value"] or 0), reverse=True)
        pnl_total = current - priced_cost if priced_cost else None
        snap = state["last_snapshot"].get(member_id)
        members_out.append({
            "id": member_id,
            "name": name,
            "positions": len(rows_out),
            "invested": _money(invested),
            # Cost of only the positions we could price. P&L must be computed
            # against this, never against total invested — otherwise a single
            # unpriced holding shows up as a huge phantom loss.
            "invested_priced": _money(priced_cost),
            "unpriced": sum(1 for r in rows_out if not r["priced"]),
            "current": _money(current),
            "pnl": _money(pnl_total),
            "pnl_pct": _pct(pnl_total, priced_cost),
            "day_change": _money(day_change),
            "day_change_pct": _pct(day_change, current - day_change if current else None),
            "holdings": rows_out,
            "holdings_as_of": (snap or {}).get("ts"),
            "snapshot_source": (snap or {}).get("source"),
            "last_trade": state["last_trade"].get(member_id),
        })

    # Every trade and correction, grouped by the company it happened to — this is
    # what a single stock's own page shows, and the family-wide feed is truncated
    # far too early to serve it.
    # Only the four fields the page renders: the same events are already in the
    # family feed, and copying them whole once per holding would double the size
    # of a file the viewer re-fetches every couple of minutes.
    by_stock: dict[str, list[dict]] = {}
    for act in state["activity"]:
        if act.get("key"):
            by_stock.setdefault(act["key"], []).append({
                "ts": act["ts"],
                "member_name": member_names.get(act["member"], act["member"]),
                "text": act["text"],
                "source": act.get("source"),
            })

    # Who holds what — the view that answers "everyone sell X".
    all_ids = [m["id"] for m in members_out]
    consolidated_out = []
    for agg in consolidated.values():
        holder_ids = [h["member"] for h in agg["holders"]]
        pnl = agg["value"] - agg["cost"] if agg["value"] else None
        agg["holders"].sort(key=lambda h: (h["value"] or 0), reverse=True)
        history = by_stock.get(agg["key"], [])
        consolidated_out.append({
            **agg,
            "qty": round(agg["qty"], 4),
            "cost": _money(agg["cost"]),
            "value": _money(agg["value"]),
            "day_change": _money(agg["day_change"]),
            "avg": _money(agg["cost"] / agg["qty"]) if agg["qty"] else None,
            "pnl": _money(pnl),
            "pnl_pct": _pct(pnl, agg["cost"]),
            "holder_ids": holder_ids,
            "held_by_count": len(holder_ids),
            "not_held_by": [
                {"id": mid, "name": member_names.get(mid, mid)}
                for mid in all_ids if mid not in holder_ids
            ],
            # Why it is held, in the owner's own words. Carried in the payload so
            # a statically hosted copy of the dashboard shows it too.
            "thesis": theses.get(agg["key"]),
            "trades": history[:TRADES_PER_STOCK],
            "trade_count": len(history),
            "first_seen": history[-1]["ts"] if history else None,
        })
    consolidated_out.sort(key=lambda r: (r["value"] or 0), reverse=True)

    # Only surface what is still actionable. A holdings statement supersedes the
    # log, so a disagreement recorded *before* the latest snapshot has already
    # been settled by it — the quantity is right, and re-litigating five years of
    # history buries the handful of warnings that still matter. They stay in the
    # log either way; this only decides what gets shown.
    anchors = {m: (state["last_snapshot"].get(m) or {}).get("ts")
               for m in state["holdings"]}
    historical = 0
    for warning in state["warnings"]:
        if (warning["kind"] == "cost_unknown"
                and (warning.get("member"), warning.get("symbol")) in cost_by_hand):
            # Answered: the cost the log never saw was typed in.
            continue
        anchor = anchors.get(warning.get("member"))
        stamp = warning.get("ts")
        if anchor and stamp and stamp < anchor and warning["kind"] in (
                "drift", "oversell", "partial_snapshot"):
            historical += 1
            continue
        attention.append({
            "kind": warning["kind"],
            "member": member_names.get(warning.get("member"), warning.get("member")),
            "symbol": warning.get("symbol"),
            "detail": warning["detail"],
            "ts": stamp,
        })
    for entry in instruments.unresolved(registry):
        by_hand = "price" in overrides.for_instrument(
            fixed, (entry.get("isin") or entry.get("symbol") or ""))
        attention.append({
            "kind": "unmapped_instrument",
            "member": None,
            "symbol": entry.get("symbol"),
            "detail": f"{entry.get('isin') or entry.get('symbol')} is not in NSE's equity list — "
                      + ("it is valued at the price you set by hand, which nothing will "
                         "refresh" if by_hand else "no price until it is mapped"),
            "ts": None,
        })

    # A held position with no price silently drags every total. Say so loudly.
    for row in consolidated_out:
        if not row["priced"]:
            attention.append({
                "kind": "no_price",
                "member": None,
                "symbol": row["symbol"],
                "detail": f"{row['symbol']} has no price yet, so its "
                          f"{_money(row['cost'])} of cost is excluded from P&L — "
                          "retry the price refresh, or map it with "
                          f"`pm instruments map {row['isin'] or row['symbol']} <NSE-SYMBOL>`",
                "ts": None,
            })

    last_sync = read_json(LAST_SYNC, default={}) or {}
    for doc in last_sync.get("documents", []):
        if doc.get("status") in ("needs_attention", "error"):
            attention.append({
                "kind": "document",
                "member": member_names.get(doc.get("member"), doc.get("member")),
                "symbol": None,
                "detail": f"{doc.get('subject') or doc.get('filename')}: "
                          + "; ".join(doc.get("notes", [])[-2:]),
                "ts": doc.get("date"),
            })

    totals_current = sum(m["current"] or 0 for m in members_out)
    totals_invested = sum(m["invested"] or 0 for m in members_out)
    totals_priced_cost = sum(m["invested_priced"] or 0 for m in members_out)
    totals_day = sum(m["day_change"] or 0 for m in members_out)
    totals_unpriced = sum(m["unpriced"] for m in members_out)
    totals_pnl = totals_current - totals_priced_cost if totals_priced_cost else None

    payload = {
        "schema": SCHEMA,
        "generated_at": iso(now_ist()),
        "currency": "INR",
        "as_of": {
            "prices": price_cache.get("fetched_at"),
            "prices_stale": any_stale,
            "last_mail_sync": (read_json(paths.SYNC_STATE, default={}) or {}).get("last_mail_sync"),
        },
        "totals": {
            "invested": _money(totals_invested),
            "current": _money(totals_current),
            "invested_priced": _money(totals_priced_cost),
            "unpriced": totals_unpriced,
            "pnl": _money(totals_pnl),
            "pnl_pct": _pct(totals_pnl, totals_priced_cost),
            "day_change": _money(totals_day),
            "day_change_pct": _pct(totals_day, totals_current - totals_day if totals_current else None),
            "members": len(members_out),
            "positions": len(consolidated_out),
        },
        # What was typed in by hand rather than derived. Surfaced so a number
        # someone set months ago is never mistaken for one a document supplied,
        # and so a note written about a position since sold does not simply
        # disappear from the page. `values` counts only what is in force; the
        # rest is stored against something nobody holds.
        "manual": {
            "values": len(applied),
            "fields": {f: sum(1 for a in applied if a[-1] == f)
                       for f in sorted({a[-1] for a in applied})},
            "stored": overrides.count(fixed),
            "unapplied": max(0, overrides.count(fixed) - len(applied)),
            "notes": len(theses),
            "notes_unheld": sorted(set(theses) - {r["key"] for r in consolidated_out}),
        },
        "members": members_out,
        "consolidated": consolidated_out,
        "activity": state["activity"][:ACTIVITY_IN_PAYLOAD],
        # The real length, so the feed can say what it is a page of and offer to
        # walk the rest rather than pretending the log ends here.
        "activity_total": len(state["activity"]),
        "attention": attention,
        "attention_historical": historical,
        "sync": {
            "events": state["event_count"],
            "last_run": last_sync.get("finished_at"),
            "messages_seen": last_sync.get("messages_seen"),
            "events_written": last_sync.get("events_written"),
            "errors": last_sync.get("errors", []),
            "mailboxes": (read_json(paths.SYNC_STATE, default={}) or {}).get("mailboxes", {}),
        },
    }

    instruments.save_registry(registry)
    write_json(paths.DASHBOARD, payload)
    write_json(paths.HOLDINGS_CACHE, {"generated_at": payload["generated_at"],
                                      "holdings": state["holdings"]})
    return payload
