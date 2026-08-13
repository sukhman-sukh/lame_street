"""Reading broker mail over IMAP.

Two things worth knowing about the design:

1. IMAP's SINCE search only has day granularity, so an incremental fetch always
   re-reads a day or two of overlap. That is fine — ingestion is idempotent, so
   re-reading the same message changes nothing.

2. Every message we care about is archived verbatim under data/raw/ before any
   parsing happens. Parsers for broker PDFs will need tuning against real
   documents; keeping the originals means that tuning is a re-parse of local
   files rather than another round-trip to the mail server.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import io
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from . import paths
from .config import Mailbox
from .events import IST, now_ist
from .store import write_json

log = logging.getLogger(__name__)

# Senders worth opening. Broad on purpose — classification happens after.
BROKER_DOMAINS = (
    "groww.in", "groww.com",
    "nsdl.co.in", "nsdl.com", "cdslindia.com", "cdslindia.co.in",
    "camsonline.com", "kfintech.com", "karvy.com",
    "zerodha.com", "zerodha.net", "upstox.com", "angelone.in", "angelbroking.com",
    "dhan.co", "fyers.in", "icicidirect.com", "hdfcsec.com", "kotaksecurities.com",
    "5paisa.com", "motilaloswal.com", "sharekhan.com", "paytmmoney.com",
)

MAX_BYTES = 25 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def is_pdf(self) -> bool:
        return self.filename.lower().endswith(".pdf") or "pdf" in self.content_type.lower()


@dataclass
class MailDoc:
    uid: str
    message_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: datetime
    body: str
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def slug(self) -> str:
        digest = hashlib.sha256((self.message_id or self.uid).encode()).hexdigest()[:16]
        return f"{self.date:%Y-%m-%d}-{digest}"

    @property
    def raw_dir(self):
        return paths.RAW / f"{self.date:%Y-%m}" / self.slug


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _addresses(msg: Message, *headers: str) -> list[str]:
    found: list[str] = []
    for header in headers:
        for raw in msg.get_all(header, []):
            found.extend(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", raw or ""))
    return [a.lower() for a in dict.fromkeys(found)]


def _body_text(msg: Message) -> str:
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        if part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_subtype() == "html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
        chunks.append(text)
    return re.sub(r"[ \t]+", " ", "\n".join(chunks)).strip()


def _attachments(msg: Message) -> list[Attachment]:
    out: list[Attachment] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode(part.get_filename())
        ctype = part.get_content_type()
        if not filename and "pdf" not in ctype:
            continue
        # Broker mail carries logos and banners as named attachments. Only
        # statements are of any use, so don't archive the artwork.
        if filename and not filename.lower().endswith((".pdf", ".zip")) and "pdf" not in ctype:
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not data or len(data) > MAX_BYTES:
            continue

        # Some depositories mail the statement inside a zip.
        if filename.lower().endswith(".zip") or "zip" in ctype:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        if info.is_dir() or info.file_size > MAX_BYTES:
                            continue
                        out.append(Attachment(info.filename, "application/pdf", zf.read(info)))
            except Exception as exc:
                log.debug("could not open zip %s: %s", filename, exc)
            continue

        out.append(Attachment(filename or "inline.pdf", ctype, data))
    return out


def _or_terms(field: str, values: list[str]) -> str:
    """Build an IMAP OR chain: OR takes exactly two arguments, so n terms nest.

        ['a', 'b', 'c']  ->  (OR FROM "a" (OR FROM "b" FROM "c"))
    """
    terms = [f'{field} "{v}"' for v in values if v]
    if not terms:
        return ""
    while len(terms) > 1:
        right = terms.pop()
        left = terms.pop()
        terms.append(f"(OR {left} {right})")
    return terms[0]


def friendly_error(raw: str) -> str:
    """Turn an IMAP failure into something actionable.

    Gmail's own answer to a wrong password is the bare string
    "[AUTHENTICATIONFAILED] Invalid credentials", which doesn't hint at the
    actual cause — which is almost always an account password being used where
    an app password is required.
    """
    low = raw.lower()
    if "authenticationfailed" in low or "invalid credentials" in low:
        return ("Gmail rejected the login. This needs a 16-character Google "
                "app password, not your account password — Gmail stopped accepting "
                "account passwords for IMAP. Turn on 2-Step Verification, then create "
                "one at myaccount.google.com/apppasswords.")
    if "application-specific password" in low:
        return "Gmail wants an app password here, not the account password."
    if "certificate" in low:
        return f"TLS problem talking to the server: {raw}"
    if "getaddrinfo" in low or "name or service" in low or "timed out" in low:
        return f"Could not reach the mail server. Check the host and your connection. ({raw})"
    if "folder" in low or "nonexistent" in low:
        return f'{raw} — for Gmail try INBOX, or "[Gmail]/All Mail".'
    return raw


def is_broker_mail(doc: MailDoc) -> bool:
    blob = f"{doc.sender} {doc.subject}".lower()
    if any(domain in doc.sender.lower() for domain in BROKER_DOMAINS):
        return True
    # Forwarded mail arrives from the member, so fall back to what it looks like.
    return any(
        kw in blob
        for kw in ("contract note", "holding statement", "statement of holding",
                   "consolidated account statement", "statement of account",
                   "transaction cum holding", "demat", "cas ")
    )


class MailReader:
    """Thin IMAP wrapper. Use as a context manager."""

    def __init__(self, mailbox: Mailbox):
        self.mb = mailbox
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "MailReader":
        self.conn = imaplib.IMAP4_SSL(self.mb.host, self.mb.port)
        self.conn.login(self.mb.user, self.mb.resolved_password)
        status, _ = self.conn.select(self.mb.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot open folder {self.mb.folder!r}")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.conn:
                self.conn.close()
                self.conn.logout()
        except Exception:
            pass

    def count_since(self, since: datetime) -> int:
        """How many messages exist in the window at all — for diagnostics."""
        stamp = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = self.conn.uid("search", None, f'(SINCE "{stamp}")')
        return len(data[0].split()) if status == "OK" and data and data[0] else 0

    def search(
        self,
        *,
        since: datetime | None = None,
        senders: list[str] | None = None,
        subjects: list[str] | None = None,
        min_uid: int | None = None,
    ) -> list[bytes]:
        """UIDs of messages matching the configured senders or subjects.

        The filtering happens on the server. That matters more than it sounds:
        a personal Gmail can hold twenty thousand messages in a year, and
        downloading them all to decide which are broker mail is the difference
        between a sync that takes seconds and one that never finishes.

        Senders and subjects are ORed — a broker mail from an unexpected address
        still gets caught by its subject, and vice versa.
        """
        window: list[str] = []
        if min_uid:
            # Everything newer than the last message we processed. Far cheaper
            # than a date window, and exact.
            window.append(f"UID {min_uid}:*")
        elif since:
            # A day of overlap absorbs IMAP's day-granularity SINCE and anything
            # that landed while a previous run was mid-flight.
            window.append(f'SINCE "{(since - timedelta(days=1)).strftime("%d-%b-%Y")}"')

        found: list[bytes] = []
        criteria: list[tuple[str, list[str]]] = [("FROM", list(senders or []))]
        # Subject matching is opt-in. By default only known broker addresses are
        # read, so a subject line can never drag in unrelated mail.
        if subjects:
            criteria.append(("SUBJECT", list(subjects)))

        for field, values in criteria:
            if not values:
                continue
            query = " ".join(window + [_or_terms(field, values)])
            try:
                status, data = self.conn.uid("search", None, f"({query})")
            except Exception as exc:
                log.warning("search failed for %s: %s", field, exc)
                continue
            if status == "OK" and data and data[0]:
                found.extend(data[0].split())

        # A message can match more than one criterion; fetch it once.
        return list(dict.fromkeys(found))

    def fetch(self, uid: bytes) -> MailDoc | None:
        # PEEK so reading never marks anyone's mail as seen.
        status, data = self.conn.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        msg = email.message_from_bytes(data[0][1])
        try:
            when = parsedate_to_datetime(msg.get("Date"))
            when = when.astimezone(IST) if when.tzinfo else when.replace(tzinfo=IST)
        except Exception:
            when = now_ist()
        return MailDoc(
            uid=uid.decode(),
            message_id=_decode(msg.get("Message-ID")) or f"uid-{uid.decode()}",
            subject=_decode(msg.get("Subject")),
            sender=_decode(msg.get("From")),
            # Delivered-To / X-Forwarded-To survive Gmail forwarding, which is how
            # we tell whose mail it was in a shared inbox when the PDF is open.
            recipients=_addresses(msg, "To", "Cc", "Delivered-To", "X-Forwarded-To"),
            date=when,
            body=_body_text(msg),
            attachments=_attachments(msg),
        )


def archive(doc: MailDoc) -> None:
    """Persist the message and its attachments so parsing can be replayed."""
    target = doc.raw_dir
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "meta.json", {
        "message_id": doc.message_id,
        "subject": doc.subject,
        "from": doc.sender,
        "recipients": doc.recipients,
        "date": doc.date.isoformat(),
        "body": doc.body[:20000],
        "attachments": [a.filename for a in doc.attachments],
    })
    for i, att in enumerate(doc.attachments):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", att.filename) or f"attachment-{i}"
        path = target / f"{i:02d}-{safe}"
        if not path.exists():
            path.write_bytes(att.data)


def load_archived(senders: list[str] | None = None) -> Iterator[tuple[dict, list[Attachment]]]:
    """Re-read everything under data/raw/ for offline re-parsing.

    `senders` filters to the currently-configured sources. The archive can hold
    messages fetched under older, looser filters — a mutual-fund statement from a
    registrar, say — and those shouldn't quietly come back on a re-parse.

    Yields one message at a time so peak memory stays flat: a few years of
    statements is hundreds of megabytes of PDF, and holding them all at once
    is what puts a re-parse over the limit on a small host.
    """
    if not paths.RAW.exists():
        return

    wanted = [s.lower() for s in (senders or [])]
    for meta_path in sorted(paths.RAW.glob("*/*/meta.json")):
        from .store import read_json

        meta = read_json(meta_path)
        if not meta:
            continue
        if wanted:
            sender = (meta.get("from") or "").lower()
            if not any(s in sender for s in wanted):
                continue

        attachments = []
        for path in sorted(meta_path.parent.iterdir()):
            if path.name == "meta.json" or not path.is_file():
                continue
            # Infer the type from the name; assuming PDF made every logo and
            # banner in the archive look like an unopenable statement.
            name = path.name.split("-", 1)[-1]
            if not name.lower().endswith(".pdf"):
                continue
            attachments.append(Attachment(name, "application/pdf", path.read_bytes()))
        yield meta, attachments
