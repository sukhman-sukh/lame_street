"""Prices from Yahoo Finance — free, no key, roughly 15 minutes delayed.

Prices are deliberately decoupled from holdings. Holdings change a few times a
month and cost real effort to fetch; prices change constantly and cost nothing.
So the dashboard can feel live while the broker side is touched rarely.

A failed fetch never clears a known price. The old value stays with its original
timestamp and gets marked stale, because a slightly old number that says how old
it is beats a blank cell.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import paths
from .events import IST, iso, now_ist
from .store import read_json, write_json

log = logging.getLogger(__name__)

STALE_AFTER_MINUTES = 30


def load_cache() -> dict:
    return read_json(paths.PRICES_LATEST, default={"quotes": {}, "fetched_at": None}) or {}


def _setup(yf) -> None:
    """Give yfinance its own cache directory.

    Its timezone cache is a SQLite file that several worker threads write to at
    once; on the shared default path that reliably produces 'database is locked'
    and silently drops tickers from the result.
    """
    try:
        cache = paths.STATE / "yf-cache"
        cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache))
    except Exception:
        pass


def _batch(tickers: list[str]) -> dict[str, dict]:
    """One batched Yahoo call for every ticker we hold."""
    import yfinance as yf

    out: dict[str, dict] = {}
    if not tickers:
        return out
    _setup(yf)

    frame = yf.download(
        tickers=" ".join(tickers),
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        return out

    single = len(tickers) == 1

    for ticker in tickers:
        try:
            closes = (frame["Close"] if single else frame[ticker]["Close"]).dropna()
            if closes.empty:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else last
            out[ticker] = {"price": last, "prev_close": prev}
        except Exception as exc:
            log.debug("no close data for %s: %s", ticker, exc)

    # Yahoo drops tickers from a batch fairly often. Retry the stragglers one at
    # a time rather than leaving a holding permanently unpriced.
    for ticker in [t for t in tickers if t not in out]:
        try:
            closes = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
            if closes.empty:
                continue
            last = float(closes.iloc[-1])
            out[ticker] = {
                "price": last,
                "prev_close": float(closes.iloc[-2]) if len(closes) > 1 else last,
            }
        except Exception as exc:
            log.debug("retry failed for %s: %s", ticker, exc)
    return out


def _intraday(tickers: list[str], quotes: dict[str, dict]) -> None:
    """Upgrade daily closes to a live-ish price where Yahoo offers one."""
    import yfinance as yf

    _setup(yf)
    try:
        handles = yf.Tickers(" ".join(tickers))
    except Exception:
        return
    for ticker in tickers:
        try:
            info = handles.tickers[ticker].fast_info
            live = info.get("last_price") if hasattr(info, "get") else getattr(info, "last_price", None)
            if live and float(live) > 0:
                quotes.setdefault(ticker, {})["price"] = float(live)
                prev = info.get("previous_close") if hasattr(info, "get") else getattr(info, "previous_close", None)
                if prev and float(prev) > 0:
                    quotes[ticker]["prev_close"] = float(prev)
        except Exception:
            continue


def refresh(tickers: list[str]) -> dict:
    """Fetch prices for `tickers`, merging into the cache. Returns the cache."""
    tickers = sorted({t for t in tickers if t})
    cache = load_cache()
    quotes: dict[str, dict] = dict(cache.get("quotes", {}))
    stamp = iso(now_ist())

    fresh: dict[str, dict] = {}
    if tickers:
        try:
            fresh = _batch(tickers)
            _intraday(tickers, fresh)
        except Exception as exc:
            log.warning("price refresh failed: %s", exc)

    for ticker in tickers:
        got = fresh.get(ticker)
        if got:
            quotes[ticker] = {
                "price": got["price"],
                "prev_close": got.get("prev_close", got["price"]),
                "fetched_at": stamp,
            }
        elif ticker not in quotes:
            quotes[ticker] = {"price": None, "prev_close": None, "fetched_at": None}

    cache = {
        "fetched_at": stamp,
        "requested": len(tickers),
        "updated": len(fresh),
        "quotes": quotes,
    }
    write_json(paths.PRICES_LATEST, cache)
    return cache


def quote_for(cache: dict, ticker: str | None) -> dict:
    if not ticker:
        return {"price": None, "prev_close": None, "fetched_at": None, "stale": True}
    quote = dict(cache.get("quotes", {}).get(ticker) or {})
    quote.setdefault("price", None)
    quote.setdefault("prev_close", None)
    quote["stale"] = _is_stale(quote.get("fetched_at"))
    return quote


def _is_stale(stamp: str | None) -> bool:
    if not stamp:
        return True
    try:
        age = (now_ist() - datetime.fromisoformat(stamp).astimezone(IST)).total_seconds()
    except Exception:
        return True
    return age > STALE_AFTER_MINUTES * 60
