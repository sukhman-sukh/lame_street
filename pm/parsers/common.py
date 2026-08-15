"""Shared parsing helpers for broker statements.

ISIN is the backbone of all of this. It appears in every SEBI-format contract
note and every depository holding statement, it survives ticker renames, and it
has a check digit — so it doubles as a reliable way to find the real data rows
and ignore headers, footers and disclaimers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

ISIN_RE = re.compile(r"\b(IN[A-Z0-9]{10})\b")

NUM_RE = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")

DATE_PATTERNS = (
    (re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"), "%d-%m-%Y"),
    (re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"), "%Y-%m-%d"),
    (re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3})[-\s](\d{4})\b"), "%d-%b-%Y"),
    (re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3})[-\s](\d{2})\b"), "%d-%b-%y"),
)


@dataclass
class ParseResult:
    """Whatever the parser found, plus why it found it.

    `notes` is not decoration — broker layouts differ and change, so when a
    document yields nothing the notes are what tell you which step gave up.
    """
    rows: list[dict] = field(default_factory=list)
    as_of: date | None = None
    method: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def declared_layouts(layout: str | list[str] | None) -> list[str]:
    """Normalise a declared layout into a list.

    Every parser must accept both shapes. A broker profile may declare one layout
    as a string or several as a list, and a parser that compares the raw value
    against a string silently rejects the list form — returning no rows, with no
    error, for every document of that kind.
    """
    if not layout:
        return []
    return [layout] if isinstance(layout, str) else [name for name in layout if name]


def valid_isin(code: str) -> bool:
    """ISIN check digit (letters expand to two digits, then Luhn)."""
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", code or ""):
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in code[:-1])
    total, double = 0, True
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return (10 - total % 10) % 10 == int(code[-1])


def find_isin(text: str) -> str | None:
    for candidate in ISIN_RE.findall(text or ""):
        if valid_isin(candidate):
            return candidate
    return None


def to_number(token: str) -> float | None:
    """Parse Indian-format numbers: 1,23,456.78, (500) for negative, trailing -."""
    if token is None:
        return None
    raw = str(token).strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").replace(",", "").replace("₹", "").strip()
    if raw.endswith("-"):
        negative, raw = True, raw[:-1]
    if raw.upper().endswith(("DR", "CR")):
        negative = negative or raw.upper().endswith("DR")
        raw = raw[:-2].strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return -value if negative else value


def numbers_in(text: str) -> list[float]:
    out = []
    for token in NUM_RE.findall(text or ""):
        value = to_number(token)
        if value is not None:
            out.append(value)
    return out


def find_date(text: str, *, keywords: tuple[str, ...] = ()) -> date | None:
    """Find the date a document refers to.

    Statements are full of dates — print date, settlement date, period start —
    so the keyword decides which one matters. Keywords are tried in priority
    order, most specific first, rather than taking whichever line comes first.

    On a line describing a range ("period 01-08-2026 to 31-08-2026") the closing
    date is the one that describes the position, so we take the last date there.
    """
    lines = (text or "").splitlines()
    for keyword in keywords:
        for line in lines:
            low = line.lower()
            if keyword not in low:
                continue
            found = _scan_dates(line)
            if not found:
                continue
            spans_a_range = any(word in low for word in (" to ", "period", "from"))
            return found[-1] if (spans_a_range and len(found) > 1) else found[0]
    return _scan_date(text or "")


def _scan_dates(text: str) -> list[date]:
    """Every date in a string, in the order they appear."""
    hits: list[tuple[int, date]] = []
    for pattern, fmt in DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                hits.append((match.start(), datetime.strptime("-".join(match.groups()), fmt).date()))
            except ValueError:
                continue
    seen, out = set(), []
    for _, value in sorted(hits):
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _scan_date(text: str) -> date | None:
    for pattern, fmt in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return datetime.strptime("-".join(match.groups()), fmt).date()
        except ValueError:
            continue
    return None


def header_index(header: list[str], *keywords: str) -> int | None:
    """Index of the first column whose name contains any keyword."""
    for i, cell in enumerate(header):
        low = (cell or "").lower()
        if any(kw in low for kw in keywords):
            return i
    return None


def pick_table(tables: list[list[list[str]]], *required: tuple[str, ...]) -> list[list[str]] | None:
    """Best table whose header row satisfies every group of alternatives.

    pick_table(tables, ("isin",), ("qty", "quantity")) wants a header with an
    ISIN-ish column and a quantity-ish column.
    """
    best, best_score = None, 0
    for table in tables:
        for idx, header_row in enumerate(table[:3]):  # header is not always row 0
            header = [(c or "").lower() for c in header_row]
            if all(any(any(kw in cell for kw in group) for cell in header) for group in required):
                score = len(table) - idx
                if score > best_score:
                    best, best_score = table[idx:], score
    return best


# Footnote markers ride along on a header cell, and a statement prints them on
# the first page only: "Gross Rate/ Trade Price per Unit(₹)²" on page one becomes
# "…per Unit(₹)" on page two. Left in, that one character makes the continuation
# look like a different table and its rows are dropped.
FOOTNOTE_MARK = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+|(?<=[)\]a-z])\d+$")


def _header_signature(header_row: list[str]) -> tuple[str, ...]:
    """A header reduced to what identifies the table, not how it was typeset.

    Whitespace goes entirely: the same header re-ruled on the next page comes back
    with "(before levies)(₹)" where the first page had "(before levies) (₹)", and
    that single space is otherwise enough to make the continuation look like a
    different table and lose every row on it.
    """
    cells = []
    for cell in header_row:
        text = FOOTNOTE_MARK.sub("", (cell or "").strip().lower())
        cells.append(re.sub(r"\s+", "", text))
    return tuple(cells)


def table_pages(
    tables: list[list[list[str]]],
    chosen: list[list[str]],
    is_row=None,
) -> list[list[str]]:
    """Every data row of `chosen`, gathered from all the pages it spans.

    A table that runs past the bottom of a page comes back from extraction as
    several tables, in one of two shapes: repeating its header, or — when the PDF
    rules the header only once — as a bare block of data rows. Keeping only the
    first page silently drops the rest, which on a contract note loses trades and
    on a holdings statement retires positions that were never sold.

    Header-repeating pages are matched on the header, which is what makes that
    half safe: an unrelated table carrying the same columns has a different header
    and is left alone. A headerless continuation has nothing to match on, so it is
    admitted only when `is_row` recognises every one of its rows as a real row of
    this table. With no `is_row` they are ignored — guessing would be worse than
    missing them.
    """
    if not chosen:
        return []
    signature = _header_signature(chosen[0])
    width = len(chosen[0])

    rows: list[list[str]] = []
    started = False
    for table in tables:
        if not table:
            continue
        page = next((table[idx:] for idx, header_row in enumerate(table[:3])
                     if _header_signature(header_row) == signature), None)
        if page is not None:
            started = True
            rows.extend(page[1:])
            continue
        if not (started and is_row):
            continue
        body = [r for r in table if any((c or "").strip() for c in r)]
        if body and len(table[0]) == width and all(is_row(r) for r in body):
            rows.extend(body)
    return rows or list(chosen[1:])


def clean_name(text: str) -> str:
    """Tidy a security description into something displayable."""
    name = re.sub(r"\s+", " ", (text or "")).strip(" -|")
    name = ISIN_RE.sub("", name).strip(" -|")
    return name[:80]


def name_around_isin(line: str, isin: str) -> str:
    """Pull the security description out of a statement row.

    The description sits next to the ISIN — before it in most contract notes,
    after it in most depository statements — surrounded by order numbers, times
    and amounts. So take both sides, drop every purely numeric token, and keep
    whichever side actually reads like a company name.
    """
    if not line or not isin or isin not in line:
        return clean_name(_words_only(line))
    cut = line.index(isin)
    before, after = _words_only(line[:cut]), _words_only(line[cut + len(isin):])
    best = max((before, after), key=lambda side: (len(side.split()), len(side)))
    return clean_name(best)


# Field labels that sit next to the value in these statements. Without dropping
# them, a security ends up displayed as "Symbol: HBL POWER EQ".
LABELS = re.compile(r"(?i)^(isin|symbol|name|scrip|security|company|code)\s*:?$")


def _words_only(text: str) -> str:
    """Keep tokens that contain a letter; drop serials, amounts and field labels."""
    tokens = [
        token for token in re.split(r"\s+", text or "")
        if token
        and re.search(r"[A-Za-z]", token)
        and not re.fullmatch(r"[BS]", token)
        and not LABELS.match(token.strip(":"))
    ]
    return " ".join(tokens)


def symbol_from_name(name: str) -> str:
    """Rough ticker guess from a security description.

    Only used for display until the ISIN is mapped to a real NSE symbol.
    """
    words = re.sub(r"[^A-Za-z0-9 ]", " ", name or "").split()
    drop = {"limited", "ltd", "ltd.", "the", "india", "co", "company", "equity",
            "shares", "of", "and", "inr", "rs"}
    keep = [w for w in words if w.lower() not in drop]
    return ("".join(keep)[:20] or "UNKNOWN").upper()
