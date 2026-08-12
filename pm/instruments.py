"""ISIN → trading symbol → Yahoo ticker.

Statements identify securities by ISIN and a free-text company name, neither of
which Yahoo Finance understands. NSE publishes its full equity list as a CSV with
both the ISIN and the trading symbol, so we use that as the authority and fall
back to a name-derived guess only for things it doesn't cover.

The registry is a plain JSON file. Anything it gets wrong can be corrected by
hand, and a manual override is never overwritten by a later refresh.
"""
from __future__ import annotations

import csv
import io
import logging
import re

import requests

from . import paths
from .events import iso, now_ist
from .store import read_json, write_json

log = logging.getLogger(__name__)

NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_MASTER = paths.STATE / "nse_master.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/csv,*/*",
}


def refresh_nse_master(timeout: int = 30) -> tuple[int, str | None]:
    """Download NSE's equity master. Returns (rows, error)."""
    try:
        resp = requests.get(NSE_EQUITY_LIST, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        table: dict[str, dict] = {}
        for row in reader:
            clean = { (k or "").strip().upper(): (v or "").strip() for k, v in row.items() }
            isin, symbol = clean.get("ISIN NUMBER", ""), clean.get("SYMBOL", "")
            if isin and symbol:
                table[isin] = {"symbol": symbol, "name": clean.get("NAME OF COMPANY", "")}
        if not table:
            return 0, "NSE returned no usable rows"
        write_json(NSE_MASTER, {"fetched_at": iso(now_ist()), "instruments": table})
        return len(table), None
    except Exception as exc:
        log.warning("NSE master refresh failed: %s", exc)
        return 0, str(exc)


def _master() -> dict:
    return (read_json(NSE_MASTER, default={}) or {}).get("instruments", {})


def load_registry() -> dict:
    return read_json(paths.INSTRUMENTS, default={}) or {}


def save_registry(registry: dict) -> None:
    write_json(paths.INSTRUMENTS, registry)


def resolve(registry: dict, *, isin: str | None, symbol: str, name: str = "") -> dict:
    """Resolve one security, updating `registry` in place. Returns its entry."""
    key = (isin or symbol or "").upper()
    if not key:
        return {"symbol": symbol, "name": name, "yahoo": None, "source": "unknown"}

    entry = registry.get(key, {})
    if entry.get("manual"):
        return entry

    master = _master()
    hit = master.get(isin or "")
    if hit:
        entry = {
            "isin": isin,
            "symbol": hit["symbol"],
            "name": hit["name"] or name,
            "yahoo": f"{hit['symbol']}.NS",
            "source": "nse_master",
            "resolved": True,
        }
    elif not entry.get("resolved"):
        # No ISIN match — keep the statement's own symbol and flag it for review.
        entry = {
            "isin": isin,
            "symbol": (symbol or "").upper(),
            "name": name,
            "yahoo": None,
            "source": "unresolved",
            "resolved": False,
        }
    registry[key] = entry
    return entry


def isin_for(name: str, symbol: str = "") -> str | None:
    """Reverse-lookup an ISIN from a company name or trading symbol.

    Older or text-extracted documents sometimes yield a name but no ISIN. Since a
    holdings statement always carries the ISIN, a trade that lacks one would key
    to a different instrument than the snapshot covering it — and the snapshot
    would then appear to say the position had gone to zero.
    """
    master = _master()
    if not master:
        return None

    wanted_symbol = re.sub(r"[^A-Z0-9]", "", (symbol or "").upper())
    if wanted_symbol:
        for isin, entry in master.items():
            if entry["symbol"].upper() == wanted_symbol:
                return isin

    def squash(text: str) -> str:
        text = re.sub(r"(?i)\b(limited|ltd|the|and|company|co)\b", " ", text or "")
        return re.sub(r"[^a-z0-9]", "", text.lower())

    wanted = squash(name)
    if len(wanted) < 5:
        return None
    for isin, entry in master.items():
        candidate = squash(entry.get("name", ""))
        if candidate and (candidate == wanted
                          or candidate.startswith(wanted)
                          or wanted.startswith(candidate)):
            return isin
    return None


def unresolved(registry: dict) -> list[dict]:
    return [e for e in registry.values() if not e.get("resolved")]


def set_override(registry: dict, key: str, *, symbol: str, yahoo: str | None = None) -> dict:
    entry = {
        "isin": registry.get(key, {}).get("isin"),
        "symbol": symbol.upper(),
        "name": registry.get(key, {}).get("name", ""),
        "yahoo": (yahoo or f"{symbol.upper()}.NS"),
        "source": "manual",
        "manual": True,
        "resolved": True,
    }
    registry[key.upper()] = entry
    return entry
