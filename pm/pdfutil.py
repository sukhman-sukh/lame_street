"""Opening and reading broker PDFs.

Contract notes and holding statements are password-protected with the account
holder's PAN in capitals. That turns the password into an identity check: in a
shared inbox, whichever member's PAN opens the file is whose statement it is.
No need to trust the From or To headers.
"""
from __future__ import annotations

import io
import logging
import warnings

log = logging.getLogger(__name__)

# pdfminer (under pdfplumber) is chatty about malformed-but-readable PDFs.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*CropBox.*")


class PdfLocked(Exception):
    """None of the candidate passwords opened the document."""


def unlock(data: bytes, passwords: list[str]) -> str | None:
    """Return the password that opens this PDF ("" if unencrypted), else raise."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # not a PDF at all
        raise PdfLocked(f"unreadable pdf: {exc}") from exc

    if not reader.is_encrypted:
        return ""

    for pw in passwords:
        if not pw:
            continue
        try:
            if reader.decrypt(pw):
                return pw
        except Exception:
            continue
    raise PdfLocked("encrypted and no candidate password worked")


def extract_text(data: bytes, password: str = "") -> str:
    """Full text of the PDF. pdfplumber first — it keeps columns closer to
    reading order, which matters for the row-per-security statement tables."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        if text:
            return text
    except Exception as exc:
        log.debug("pdfplumber failed, falling back to pypdf: %s", exc)

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and password:
            reader.decrypt(password)
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as exc:
        log.warning("could not extract text: %s", exc)
        return ""


TEXT_GRID = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def extract_tables(data: bytes, password: str = "") -> list[list[list[str]]]:
    """Every table pdfplumber can find, as lists of rows of cells.

    Two passes per page. The default one follows ruled lines, which is precise
    when a statement draws them. Many don't — they rule the header and leave the
    body as aligned text — and that pass then returns a one-row "table" holding
    only column names. When that happens, the whitespace-alignment strategy
    recovers the real grid.

    This matters a lot: falling back to scanning raw text means guessing which
    number in a row is the quantity, and on a wrapped line that guess picks the
    market value instead. Recovering actual columns removes the guess.
    """
    tables: list[list[list[str]]] = []
    try:
        import pdfplumber

        def clean(tbl) -> list[list[str]]:
            return [
                [(cell or "").replace("\n", " ").strip() for cell in row]
                for row in tbl
                if any((c or "").strip() for c in row)
            ]

        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            for page in pdf.pages:
                ruled = [clean(t) for t in (page.extract_tables() or [])]
                ruled = [t for t in ruled if t]
                tables.extend(ruled)

                if not any(len(t) > 1 for t in ruled):
                    try:
                        loose = [clean(t) for t in (page.extract_tables(TEXT_GRID) or [])]
                        tables.extend(t for t in loose if len(t) > 1)
                    except Exception as exc:
                        log.debug("text-grid extraction failed: %s", exc)
    except Exception as exc:
        log.debug("table extraction failed: %s", exc)
    return tables
