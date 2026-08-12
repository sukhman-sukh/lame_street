"""Parse a holdings statement into a snapshot.

Covers the family of statements that all carry the same essential shape — ISIN,
security name, quantity:

  * NSDL / CDSL transaction-cum-holding statement (monthly when the account has
    activity) — the authoritative one, and broker-independent
  * Consolidated Account Statement (CAS)
  * the securities section of a broker's monthly settlement statement
  * a broker's own holdings report

Only the last of these usually carries an average buy price. When cost is absent
the snapshot goes in with has_cost=False and the replay keeps whatever cost basis
it already had, so quantity gets corrected without destroying P&L.
"""
from __future__ import annotations

import re
from datetime import date

from .common import (
    ISIN_RE,
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
    to_number,
    valid_isin,
)

SKIP_ROW = re.compile(r"\b(total|grand total|sub[- ]?total|summary|page \d)\b", re.I)

# A real holdings statement says, somewhere, "here is what you hold". Requiring
# one of these is what separates it from the several other broker documents that
# also list securities with quantities but mean something completely different.
HOLDINGS_MARKERS = (
    "holdings balance", "holding balance", "balance of holdings",
    "statement of holding", "holdings as on", "holding as on", "holdings as at",
    "current bal", "closing balance", "consolidated account statement",
    "portfolio holding", "demat holding",
)

# Per-transaction vocabulary. A settlement or payout advice — Groww's weekly
# "Statement of Accounts of Securities", a retention statement, a margin
# statement — lists ISINs and quantities that are *movements*, not positions.
# Reading one as a snapshot silently zeroes everything it doesn't mention.
TRANSACTION_MARKERS = (
    "quantity delivered", "quantity receive", "counterparty demat",
    "mkt payout", "mkt payin", "pending obligations", "purpose",
    "statement of accounts of securities", "transaction type",
    "trans description", "buy/cr", "sell/dr", "daily margin", "retention",
)

# Document types that list securities with quantities but never state a position.
# Named types with known meanings, so this is a hard reject rather than a hint:
#   retention / margin  — shares pledged as collateral
#   payout advice       — shares that moved on a settlement
#   client master       — account particulars
# Any of these read as a snapshot would zero every position it omits.
DISQUALIFIERS = (
    "retention report", "retention statement", "retention account statement",
    "margin statement", "daily margin", "collateral statement",
    "statement of accounts of securities", "mkt payout", "mkt payin",
    "pending obligations", "client master report", "delivery obligation",
    "statement of funds and securities", "sof-sos",
)

TRANSACTION_COLUMNS = (
    "delivered", "receive", "counterparty", "purpose", "transaction type",
    "trans description", "buy/cr", "sell/dr", "settlement no", "setl no",
)

# Where the quantity actually lives, most specific first.
#
# Order is the whole point. A statement lists several quantity-shaped columns and
# only one is the position:
#
#   Dhan:    Sr | ISIN | Company | Free Bal | Pldg Bal | … | Tot Qty | Rate | Value
#   Groww:   ISIN | Company | Current Bal | Free Bal | Pldg Bal | … | Rate | Value
#
# "Free Bal" excludes pledged shares, so matching a bare "bal" first would
# silently under-report anyone using margin. The total always wins.
QTY_COLUMNS = (
    ("tot qty", "total qty", "total quantity"),
    ("current bal", "closing bal", "total bal"),
    ("holding", "quantity", "qty", "units"),
    ("bal",),
)

# Most specific first — find_date takes the first keyword that matches, and
# "Holdings as on 31-08-2026" is a better answer than the period line above it.
AS_OF_KEYWORDS = (
    "holdings as on", "holding as on", "closing balance as", "balance as on",
    "position as on", "as on", "as at", "as of",
    "period ended", "statement for the period", "statement as", "statement for",
)

SUBJECT_HINTS = (
    "holding statement", "statement of holding", "transaction cum holding",
    "consolidated account statement", "cas for", "demat account statement",
    "statement of accounts", "holdings statement", "portfolio statement",
    "statement of securities",
)


def looks_like_holdings(subject: str, filename: str, text: str) -> bool:
    """Decide whether a document states what someone currently holds.

    Being strict here matters more than being generous. Brokers send several
    documents that all list ISINs with quantities next to them:

      * a transaction-cum-holding statement — positions, what we want
      * a weekly settlement/payout advice — shares that moved that week
      * a retention or margin statement — shares held as collateral
      * a funds ledger — no ISINs at all

    Only the first is a snapshot. Mistaking any of the others for one is worse
    than ignoring it, because a snapshot supersedes the log: every position the
    document doesn't happen to mention gets zeroed. So we require an explicit
    holdings marker, and reject anything that reads as per-transaction.
    """
    blob = f"{subject} {filename} {text or ''}".lower()
    if "contract note" in blob:
        return False
    if any(marker in blob for marker in DISQUALIFIERS):
        return False

    distinct_isins = {code for code in ISIN_RE.findall(text or "") if valid_isin(code)}
    if not distinct_isins:
        return False

    has_holdings = any(marker in blob for marker in HOLDINGS_MARKERS)
    if not has_holdings:
        return False

    # A statement can legitimately contain both — a transaction list followed by
    # a holdings balance. Only reject when it is transactions and nothing else.
    if any(marker in blob for marker in TRANSACTION_MARKERS):
        return _holdings_offset(text) is not None

    return True


def _header_line_offset(text: str) -> int | None:
    """Offset of the holdings table's header line, if it appears in the text.

    More precise than a keyword: the line that names both an ISIN column and a
    balance column *is* where the position rows begin.
    """
    cursor = 0
    for line in (text or "").splitlines(keepends=True):
        low = line.lower()
        if "isin" in low and any(kw in low for group in QTY_COLUMNS for kw in group):
            return cursor
        cursor += len(line)
    return None


def _holdings_offset(text: str) -> int | None:
    """Character offset where the holdings section starts, if there is one."""
    header = _header_line_offset(text)
    if header is not None:
        return header
    low = (text or "").lower()
    hits = [low.find(m) for m in HOLDINGS_MARKERS if low.find(m) != -1]
    return min(hits) if hits else None


def _column_plan(tables) -> tuple[int, int, str] | None:
    """Learn the column layout from a header row, even one with no data under it.

    Statements routinely rule the header and leave the body as plain text, so the
    extracted "table" is a single row of column names. That row is still valuable:
    it says how many numeric columns each position has and which one is the
    balance — and that is enough to read the text rows positionally instead of
    guessing which number is the quantity.

    Returns (numeric column count, index of the quantity among them, its name).
    """
    for table in tables or []:
        header = [(c or "").lower() for c in table[0]]
        if header_index(header, "isin") is None:
            continue
        qty_at = next(
            (idx for group in QTY_COLUMNS if (idx := header_index(header, *group)) is not None),
            None,
        )
        if qty_at is None:
            continue
        name_at = header_index(header, "company", "security", "scrip", "name", "description")
        first_numeric = (name_at + 1) if name_at is not None else 1
        numeric = len(header) - first_numeric
        if numeric >= 1 and qty_at >= first_numeric:
            return numeric, qty_at - first_numeric, header[qty_at]
    return None


def holdings_section(text: str) -> str:
    """Just the holdings part of a mixed statement.

    A transaction-cum-holding statement lists every movement first and the
    balances second. Scanning the whole document would read transaction
    quantities as positions, so cut to the holdings heading before parsing.
    """
    offset = _holdings_offset(text)
    return text if offset is None else text[offset:]


def _is_transaction_table(header: list[str]) -> bool:
    joined = " ".join(header)
    return any(col in joined for col in TRANSACTION_COLUMNS)


def _from_tables(tables, result: ParseResult) -> bool:
    # "Current Bal" is abbreviated in real statements, so match on "bal" too —
    # looking only for the word "balance" is why this table was missed before.
    candidates = [
        t for t in (tables or [])
        if not _is_transaction_table([(c or "").lower() for c in t[0]])
    ]
    if len(candidates) < len(tables or []):
        result.note(f"ignored {len(tables) - len(candidates)} transaction-level table(s)")

    table = pick_table(candidates, ("isin",), ("qty", "quantity", "bal", "holding", "units"))
    if not table:
        result.note("no table with ISIN + a balance column")
        return False

    header = [(c or "").lower() for c in table[0]]
    col_isin = header_index(header, "isin")
    col_qty = next(
        (idx for group in QTY_COLUMNS if (idx := header_index(header, *group)) is not None),
        None,
    )
    col_name = header_index(header, "company", "security", "scrip", "name", "description")
    col_avg = header_index(header, "average", "avg", "buy price", "cost")

    if col_isin is None or col_qty is None:
        result.note(f"table found but missing columns (isin={col_isin}, qty={col_qty})")
        return False
    result.note(f"reading quantity from column {col_qty!r}: {header[col_qty]!r}")

    result.method = "table"
    for row in table[1:]:
        joined = " ".join(c or "" for c in row)
        if SKIP_ROW.search(joined):
            continue
        isin = find_isin(row[col_isin] if col_isin < len(row) else "") or find_isin(joined)
        if not isin:
            continue
        qty = to_number(row[col_qty]) if col_qty < len(row) else None
        if qty is None or qty <= 0:
            continue
        avg = to_number(row[col_avg]) if col_avg is not None and col_avg < len(row) else None
        name = clean_name(row[col_name] if col_name is not None and col_name < len(row) else joined)
        result.rows.append({
            "isin": isin,
            "name": name,
            "symbol": symbol_from_name(name),
            "qty": qty,
            "avg": avg if (avg and avg > 0) else None,
        })
    return bool(result.rows)


def _reconcile_quantity(qty: float | None, numbers: list[float]) -> tuple[float | None, str]:
    """Check a quantity against the row's own arithmetic.

    Every one of these layouts ends each position row with a rate and a market
    value, so `value / rate` is an independent statement of the quantity. When the
    two disagree, the column was misread — usually a digit inside the security
    name shifting everything along — and the arithmetic is the one to trust.

    This is what stops a market value being recorded as a share count, which is
    the single most destructive misparse possible here: it survives into the
    snapshot and silently rewrites the position.
    """
    if len(numbers) < 3:
        return qty, ""
    rate, value = numbers[-2], numbers[-1]
    if rate <= 0 or value <= 0:
        return qty, ""

    implied = value / rate
    if implied <= 0:
        return qty, ""
    # Snap to a whole share count when it is obviously one.
    if abs(implied - round(implied)) < 0.01:
        implied = float(round(implied))

    if qty is not None and abs(qty - implied) <= max(0.01, 0.02 * implied):
        return qty, ""
    return implied, (f"quantity {qty:g} disagreed with value/rate ({implied:g}); "
                     "used the arithmetic" if qty is not None else "")


def _from_text(text: str, result: ParseResult, plan: tuple[int, int, str] | None = None) -> None:
    hits = 0
    if plan:
        count, index, label = plan
        result.note(f"reading {label!r} as column {index + 1} of the {count} "
                    "numeric columns on each row")

    for line in text.splitlines():
        isin = find_isin(line)
        if not isin:
            continue
        hits += 1
        if SKIP_ROW.search(line):
            continue
        # Anything left of the ISIN is a serial or account number, never a holding.
        tail = line[line.index(isin) + len(isin):]
        numbers = numbers_in(tail)

        qty = None
        if plan:
            count, index, _ = plan
            if len(numbers) >= count:
                # Count in from the END of the row. Security names contain digits
                # more often than you would expect — "MANAPPURAM FIN RE2/-",
                # "RAIN INDUSTRIES-2/-" — and counting from the front lets one of
                # those shift every column, which silently turns a market value
                # into a share count.
                qty = numbers[-count:][index]
        if qty is None:
            positive = [v for v in numbers if v > 0]
            qty = positive[0] if positive else None

        qty, complaint = _reconcile_quantity(qty, numbers)
        if complaint:
            result.note(f"{isin}: {complaint}")
        if qty is None:
            result.note(f"ISIN row with no quantity: {line.strip()[:70]}")
            continue
        if qty <= 0:
            continue
        name = name_around_isin(line, isin)
        result.rows.append({
            "isin": isin,
            "name": name,
            "symbol": symbol_from_name(name),
            "qty": qty,
            "avg": None,
        })

    if hits and not result.rows:
        result.note(f"found {hits} ISIN rows but read none of them")
    elif not hits:
        result.note("no ISIN found anywhere in the document")
    if result.rows:
        result.method = "text"


SUPPORTED_LAYOUTS = ("holdings-balance-table",)


def parse(text: str, tables, fallback_date: date,
          layout: str | list[str] | None = None) -> ParseResult:
    """Extract positions. `layout` is the broker's declared strategy, if known.

    One strategy exists today — find the balance table, or read text rows
    positionally using the layout its header describes — so a declared layout is
    validated rather than dispatched on. It still earns its place: a broker whose
    layout has never been confirmed gets flagged rather than silently trusted.
    """
    declared = declared_layouts(layout)
    unknown = [name for name in declared if name not in SUPPORTED_LAYOUTS]
    if unknown:
        result = ParseResult()
        result.note("unknown holdings layout(s) declared for this broker: "
                    + ", ".join(unknown))
        return result
    return _parse(text, tables, fallback_date)


def _parse(text: str, tables, fallback_date: date) -> ParseResult:
    result = ParseResult()
    result.as_of = find_date(text, keywords=AS_OF_KEYWORDS) or fallback_date

    if not _from_tables(tables or [], result):
        # Only the holdings half — transaction rows above it would otherwise be
        # read as positions.
        section = holdings_section(text or "")
        if len(section) < len(text or ""):
            result.note("scanned only the holdings section of a mixed statement")
        # Even a header-only table tells us the column layout to read text rows by.
        _from_text(section, result, _column_plan(tables))

    # Collapse duplicate ISINs (free vs pledged vs locked rows) into one position.
    if result.rows:
        merged: dict[str, dict] = {}
        for row in result.rows:
            existing = merged.get(row["isin"])
            if existing:
                existing["qty"] += row["qty"]
                existing["avg"] = existing["avg"] or row["avg"]
            else:
                merged[row["isin"]] = dict(row)
        collapsed = len(result.rows) - len(merged)
        result.rows = sorted(merged.values(), key=lambda r: r["symbol"])
        if collapsed:
            result.note(f"merged {collapsed} duplicate ISIN rows")
        result.note(f"{len(result.rows)} positions via {result.method} extraction")

    return result


def has_cost(rows: list[dict]) -> bool:
    return any(r.get("avg") for r in rows)
