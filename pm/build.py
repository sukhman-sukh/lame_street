"""Assemble data/public/dashboard.json — the only file the viewer reads.

Keeping this as a build step rather than computing on request is what lets the
viewer be a static page. It can be opened locally or hosted anywhere; it never
needs the collector running.
"""
from __future__ import annotations

from . import config as cfgmod
from . import instruments, paths, prices
from .events import iso, now_ist
from .replay import replay
from .store import read_json, write_json

LAST_SYNC = paths.STATE / "last_sync.json"


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

    members_out: list[dict] = []
    consolidated: dict[str, dict] = {}
    attention: list[dict] = []
    any_stale = False

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
            quote = prices.quote_for(price_cache, entry.get("yahoo"))
            price, prev = quote.get("price"), quote.get("prev_close")
            any_stale = any_stale or (quote.get("stale") and entry.get("yahoo"))

            qty, cost = pos["qty"], pos["cost"]
            value = qty * price if price else None
            pnl = (value - cost) if (value is not None and pos["cost_known"]) else None
            change = qty * (price - prev) if (price and prev) else None

            invested += cost
            if value is not None:
                current += value
                priced_cost += cost
            if change:
                day_change += change

            rows_out.append({
                "isin": pos.get("isin"),
                "symbol": entry.get("symbol") or pos["symbol"],
                "name": entry.get("name") or pos.get("name", ""),
                "qty": round(qty, 4),
                "avg": _money(pos["avg"]),
                "cost": _money(cost),
                "price": _money(price),
                "value": _money(value),
                "pnl": _money(pnl),
                "pnl_pct": _pct(pnl, cost),
                "day_change": _money(change),
                "cost_known": pos["cost_known"],
                "priced": price is not None,
                "stale": bool(quote.get("stale")),
            })

            key = (pos.get("isin") or entry.get("symbol") or pos["symbol"]).upper()
            agg = consolidated.setdefault(key, {
                "isin": pos.get("isin"),
                "symbol": entry.get("symbol") or pos["symbol"],
                "name": entry.get("name") or pos.get("name", ""),
                "qty": 0.0, "cost": 0.0, "value": 0.0, "day_change": 0.0,
                "price": _money(price), "priced": price is not None,
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
                "qty": round(qty, 4), "avg": _money(pos["avg"]),
                "value": _money(value), "pnl": _money(pnl),
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

    # Who holds what — the view that answers "everyone sell X".
    all_ids = [m["id"] for m in members_out]
    consolidated_out = []
    for agg in consolidated.values():
        holder_ids = [h["member"] for h in agg["holders"]]
        pnl = agg["value"] - agg["cost"] if agg["value"] else None
        agg["holders"].sort(key=lambda h: (h["value"] or 0), reverse=True)
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
        attention.append({
            "kind": "unmapped_instrument",
            "member": None,
            "symbol": entry.get("symbol"),
            "detail": f"{entry.get('isin') or entry.get('symbol')} is not in NSE's equity list — "
                      "no price until it is mapped",
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
        "members": members_out,
        "consolidated": consolidated_out,
        "activity": state["activity"][:200],
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
