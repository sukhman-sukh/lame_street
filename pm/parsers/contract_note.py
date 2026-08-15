"""Parse a daily contract note into trade events.

SEBI standardised the layout on 1 Feb 2025: trades in a security are consolidated
into one row carrying a weighted average price, which is exactly the granularity
the event log wants.

Two strategies, tried in order. Extracted tables are used when the PDF has real
table structure; otherwise we fall back to scanning text lines anchored on ISINs.
The fallback is deliberately strict — it would rather return nothing and say so
than invent a trade.
"""
from __future__ import annotations

import re
from datetime import date

from .common import (
    ParseResult,
    declared_layouts,
    clean_name,
    find_date,
    find_isin,
    header_index,
    name_around_isin,
    numbers_in,
    pick_table,
    symbol_from_name,
    table_pages,
    to_number,
)

BUY_TOKENS = re.compile(r"\b(b|buy|bought|purchase)\b", re.I)
SELL_TOKENS = re.compile(r"\b(s|sell|sold|sale)\b", re.I)
SKIP_ROW = re.compile(r"\b(total|net|sub[- ]?total|grand|closing|brought forward)\b", re.I)

TRADE_DATE_KEYWORDS = ("trade date", "dated", "date of trade", "trading date", "for the day")


def looks_like_contract_note(subject: str, filename: str, text: str) -> bool:
    blob = f"{subject} {filename} {text[:3000]}".lower()
    if "contract note" in blob or "contractnote" in blob:
        return True
    return "ecn" in blob and "trade" in blob


def _side_from(cell: str, qty: float | None) -> str | None:
    token = (cell or "").strip()
    if token:
        if re.fullmatch(r"b|buy", token, re.I):
            return "buy"
        if re.fullmatch(r"s|sell", token, re.I):
            return "sell"
        if BUY_TOKENS.search(token) and not SELL_TOKENS.search(token):
            return "buy"
        if SELL_TOKENS.search(token) and not BUY_TOKENS.search(token):
            return "sell"
    # Some layouts skip the column entirely and sign the quantity instead.
    if qty is not None and qty < 0:
        return "sell"
    return None


def _from_split_table(tables, result: ParseResult) -> None:
    """The SEBI post-Feb-2025 layout: Buy and Sell as side-by-side column blocks.

    The header spans two rows — a banner row grouping the columns, then the real
    names underneath:

        Security Description |     | Buy      |     | ... | Sell     | ...
        ISIN | Security Name | Quantity | WAP | ... | Quantity | WAP | ...

    So there is no buy/sell indicator to read; which side a number sits on is
    what makes it a purchase or a sale. One row can hold both.
    """
    for table in tables:
        if len(table) < 3:
            continue
        banner = [(c or "").lower() for c in table[0]]
        header = [(c or "").lower() for c in table[1]]

        buy_at = next((i for i, c in enumerate(banner) if re.fullmatch(r"buy\b.*", c)), None)
        sell_at = next((i for i, c in enumerate(banner) if re.fullmatch(r"sell\b.*", c)), None)
        if buy_at is None or sell_at is None or sell_at <= buy_at:
            continue
        if header_index(header, "isin") is None:
            continue

        end_at = next((i for i, c in enumerate(banner) if "net obligation" in c), len(header))
        col_isin = header_index(header, "isin")
        col_name = header_index(header, "security name", "symbol", "name")

        def block(lo: int, hi: int) -> dict:
            """Locate quantity / price / brokerage inside one side's columns."""
            window = header[lo:hi]
            def find(*words):
                idx = header_index(window, *words)
                return None if idx is None else lo + idx
            return {
                "qty": find("quantity", "qty"),
                "price": find("wap", "weighted average") or find("rate", "price"),
                "brokerage": find("brokerage"),
            }

        sides = {"buy": block(buy_at, sell_at), "sell": block(sell_at, end_at)}
        if sides["buy"]["qty"] is None or sides["sell"]["qty"] is None:
            continue

        result.method = "split-table"
        for row in table[2:]:
            joined = " ".join(c or "" for c in row)
            if SKIP_ROW.search(joined):
                continue
            isin = find_isin(row[col_isin] if col_isin < len(row) else "") or find_isin(joined)
            if not isin:
                continue
            name = clean_name(row[col_name]) if (col_name is not None and col_name < len(row)) else ""

            for side, cols in sides.items():
                qty = to_number(row[cols["qty"]]) if cols["qty"] < len(row) else None
                if not qty:
                    continue
                price = (to_number(row[cols["price"]])
                         if cols["price"] is not None and cols["price"] < len(row) else None)
                if not price or price <= 0:
                    result.note(f"{name or isin}: {side} quantity {qty:g} but no readable price")
                    continue
                brokerage = (to_number(row[cols["brokerage"]])
                             if cols["brokerage"] is not None and cols["brokerage"] < len(row) else 0)
                result.rows.append({
                    "isin": isin,
                    "name": name,
                    "symbol": symbol_from_name(name),
                    "side": side,
                    "qty": abs(qty),
                    "price": abs(price),
                    # Brokerage is quoted per share in this layout.
                    "charges": abs((brokerage or 0) * abs(qty)),
                    "trade_no": None,
                    "order_no": None,
                })
        if result.rows:
            return


def _from_tables(tables, result: ParseResult) -> None:
    table = pick_table(
        tables,
        ("quantity", "qty"),
        ("buy", "b/s", "sell", "b(buy)"),
    )
    if not table:
        result.note("no table with quantity + buy/sell columns")
        return

    header = [(c or "").lower() for c in table[0]]
    col_qty = header_index(header, "quantity", "qty")
    col_side = header_index(header, "buy(b)", "buy/", "b/s", "buy", "sell")
    col_price = (
        header_index(header, "weighted average", "wap")
        or header_index(header, "net rate", "net price")
        or header_index(header, "gross rate", "trade price")
        or header_index(header, "rate", "price")
    )
    col_desc = header_index(header, "security", "description", "scrip", "contract", "name")
    col_trade = header_index(header, "trade no", "trade number")
    col_order = header_index(header, "order no", "order number")
    col_brok = header_index(header, "brokerage")

    if col_qty is None or col_price is None:
        result.note(f"table found but missing columns (qty={col_qty}, price={col_price})")
        return

    def _is_trade_row(row: list[str]) -> bool:
        """A row of this same table, on a page that repeated no header.

        Deliberately strict: it must carry a buy/sell marker, a positive quantity
        and a price. A block of data that satisfies all three is a page of trades
        and nothing else in these documents looks like it.
        """
        if col_side is None or col_side >= len(row) or col_qty >= len(row) or col_price >= len(row):
            return False
        if _side_from(row[col_side] or "", 1) is None:
            return False
        qty, price = to_number(row[col_qty]), to_number(row[col_price])
        return bool(qty and price and qty > 0 and price > 0)

    body = table_pages(tables, table, is_row=_is_trade_row)
    if len(body) > len(table) - 1:
        result.note(f"trade table continues past its first page — "
                    f"read {len(body)} rows in total")

    result.method = "table"
    for row in body:
        joined = " ".join(c or "" for c in row)
        if not joined.strip() or SKIP_ROW.search(joined):
            continue

        qty = to_number(row[col_qty]) if col_qty < len(row) else None
        price = to_number(row[col_price]) if col_price < len(row) else None
        if qty is None or price is None or qty == 0 or price <= 0:
            continue

        side = _side_from(row[col_side] if col_side is not None and col_side < len(row) else "", qty)
        if side is None:
            result.note(f"skipped row, could not tell buy from sell: {joined[:70]}")
            continue

        desc = row[col_desc] if col_desc is not None and col_desc < len(row) else joined
        isin = find_isin(joined)
        brokerage = to_number(row[col_brok]) if col_brok is not None and col_brok < len(row) else 0.0
        name = clean_name(desc)

        result.rows.append({
            "isin": isin,
            "name": name,
            "symbol": symbol_from_name(name),
            "side": side,
            "qty": abs(qty),
            "price": abs(price),
            # Brokerage columns are per-unit in the SEBI layout.
            "charges": abs((brokerage or 0.0) * abs(qty)),
            "trade_no": (row[col_trade].strip() if col_trade is not None and col_trade < len(row) else None) or None,
            "order_no": (row[col_order].strip() if col_order is not None and col_order < len(row) else None) or None,
        })


def _from_text(text: str, result: ParseResult) -> None:
    """Line scan anchored on ISINs, for PDFs with no extractable table grid."""
    hits = 0
    for line in text.splitlines():
        isin = find_isin(line)
        if not isin:
            continue
        hits += 1
        if SKIP_ROW.search(line):
            continue

        # Anchor on the buy/sell marker rather than the start of the line. Order
        # numbers and timestamps sit to its left and would otherwise be read as
        # quantities; in the SEBI layout quantity is the first number to its
        # right, followed by the rates. The marker must stand alone — a B or S
        # wedged between digits is part of an ISIN, not a side indicator.
        marker = re.search(r"(?<![A-Za-z0-9])(B|S|Buy|Sell)(?![A-Za-z0-9])", line)
        if marker is None:
            result.note(f"ISIN row with no buy/sell marker: {line.strip()[:70]}")
            continue
        side = _side_from(marker.group(0), None)
        if side is None:
            continue

        values = [v for v in numbers_in(line[marker.end():]) if v != 0]
        if len(values) < 2:
            result.note(f"ISIN row with too few numbers after the marker: {line.strip()[:70]}")
            continue

        qty = values[0]
        # Quantity is whole; a rate carries paise. Prefer the first fractional
        # value after it, falling back to simply the next column.
        price = next((v for v in values[1:] if not float(v).is_integer()), values[1])
        if qty is None or price is None:
            result.note(f"could not separate qty from price: {line.strip()[:70]}")
            continue

        name = name_around_isin(line, isin)
        result.rows.append({
            "isin": isin,
            "name": name,
            "symbol": symbol_from_name(name),
            "side": side,
            "qty": abs(qty),
            "price": abs(price),
            "charges": 0.0,
            "trade_no": None,
            "order_no": None,
        })

    if hits and not result.rows:
        result.note(f"found {hits} ISIN rows but could not read any of them")
    elif not hits:
        result.note("no ISIN found anywhere in the document")
    if result.rows:
        result.method = "text"


# Strategy by name, so a broker profile can pin one instead of the parser
# discovering it per document.
STRATEGIES = {
    "sebi-split-table": _from_split_table,   # Buy/Sell as side-by-side blocks
    "buy-sell-column": _from_tables,         # a single Buy/Sell indicator column
    "text": _from_text,
}


DEFAULT_ORDER = ["sebi-split-table", "buy-sell-column", "text"]


def parse(text: str, tables, fallback_date: date,
          layout: str | list[str] | None = None) -> ParseResult:
    """Extract trades, using the layouts the broker declares, in declared order.

    Deterministic in the sense that matters: which strategies may run, and in what
    order, is fixed per broker and written down in the profile — not rediscovered
    per document. A broker that declares only the SEBI layout will never quietly
    fall through to text scanning, because a weak strategy on a rich layout
    produces plausible, wrong numbers.
    """
    result = ParseResult()
    result.as_of = find_date(text, keywords=TRADE_DATE_KEYWORDS) or fallback_date

    declared = declared_layouts(layout) or DEFAULT_ORDER
    unknown = [name for name in declared if name not in STRATEGIES]
    if unknown:
        result.note(f"unknown layout(s) declared for this broker: {', '.join(unknown)}")

    for name in declared:
        strategy = STRATEGIES.get(name)
        if strategy is None:
            continue
        strategy((text or "") if name == "text" else (tables or []), result)
        if result.rows:
            result.note(f"read with the {name!r} layout")
            return result

    result.note("none of the declared layouts (" + ", ".join(declared) + ") could read this")
    return result


