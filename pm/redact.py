"""Strip identifying data out of statement text while keeping its shape.

Tuning a parser needs the *layout* of a document — which columns exist, in what
order, how dates and amounts are formatted, where the ISIN sits. It does not need
anyone's PAN, account number, address, or actual holdings.

So this masks values and preserves structure: every digit becomes a 9, so
`1,423.55` stays a comma-grouped two-decimal number and `07-08-2026` stays a
dd-mm-yyyy date, but neither says anything real. Company names are left alone —
they're public, and seeing them is how you confirm the right column was read.
"""
from __future__ import annotations

import re

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
ISIN = re.compile(r"\bIN[A-Z0-9]{10}\b")
LABELLED = re.compile(
    r"(?i)\b(name|address|client\s*name|holder|mobile|phone|email|e-mail|"
    r"bank|account\s*name|nominee)\b\s*[:\-]\s*(.+)"
)


def redact(text: str) -> str:
    """Mask values, keep formatting. Safe to paste somewhere public."""
    out = EMAIL.sub("someone@example.com", text or "")
    out = PAN.sub("ABCDE1234F", out)
    # Keep the ISIN prefix so it still looks like (and validates as) an ISIN.
    out = ISIN.sub("INE000A01000", out)

    # Anything explicitly labelled as a person or contact detail goes entirely.
    out = LABELLED.sub(lambda m: f"{m.group(1)}: [redacted]", out)

    # Finally flatten every remaining digit. Lengths, separators and decimal
    # places survive; the values do not.
    return re.sub(r"\d", "9", out)


def summarise_tables(tables: list[list[list[str]]], limit: int = 3) -> list[str]:
    """Header rows plus one redacted sample row per table.

    Usually this alone is enough to fix a parser — the header names are what the
    column matching keys off.
    """
    lines: list[str] = []
    for i, table in enumerate(tables[:limit]):
        lines.append(f"table {i + 1}: {len(table)} rows x {max(len(r) for r in table)} cols")
        for label, row in (("header", table[0]), ("sample", table[1] if len(table) > 1 else [])):
            if row:
                cells = " | ".join(redact(cell or "") for cell in row)
                lines.append(f"  {label}: {cells}")
    return lines
