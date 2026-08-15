"""Turn mail into events.

Identifying the account holder is the interesting part. In a shared inbox every
message arrives from the same address, so the headers tell you little. But these
statements are encrypted with the holder's PAN — so the password that opens the
file *is* the identity. We try each member's PAN and whoever's works, owns the
document. Header matching is only a fallback for unencrypted mail.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

from . import events as ev
from . import config as cfgmod
from . import instruments, paths
from .config import Config, Member
from .events import IST, now_ist
from .mailbox import Attachment, MailDoc, MailReader, archive, load_archived
from .parsers import contract_note, holdings
from .pdfutil import PdfLocked, extract_tables, extract_text, unlock
from .store import append_jsonl, read_json, read_jsonl, write_json

log = logging.getLogger(__name__)

# A contract note describes the trading day; stamp it at the close. A holdings
# statement describes the end of its date, so it must land *after* that day's
# trades to supersede them.
TRADE_TIME = time(15, 30)
SNAPSHOT_TIME = time(23, 59)

# Bumped whenever a parser changes what it would extract. Documents are recorded
# against it, so a routine run re-reads nothing, while a parser fix invalidates
# the whole archive on purpose and re-derives it.
PARSER_VERSION = "2026-08-14.1"

PARSED_LEDGER = paths.STATE / "parsed.json"


@dataclass
class DocReport:
    slug: str
    subject: str
    date: str
    filename: str = ""
    member: str | None = None
    kind: str = "unknown"
    broker: str | None = None
    status: str = "skipped"          # parsed | skipped | needs_attention | error
    events: int = 0
    duplicates: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    started_at: str = ""
    finished_at: str = ""
    matched: int = 0          # messages the server matched against the filters
    messages_seen: int = 0    # of those, how many we actually read
    documents: list[DocReport] = field(default_factory=list)
    events_written: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> list[DocReport]:
        return [d for d in self.documents if d.status in ("needs_attention", "error")]

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "matched": self.matched,
            "messages_seen": self.messages_seen,
            "events_written": self.events_written,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "documents": [d.__dict__ for d in self.documents],
        }


# ------------------------------------------------------- parsed-document ledger

def load_parsed() -> dict:
    return read_json(PARSED_LEDGER, default={}) or {}


def save_parsed(ledger: dict) -> None:
    write_json(PARSED_LEDGER, ledger)


def already_parsed(ledger: dict, key: str) -> bool:
    """True when this document was read by the parsers as they stand now.

    Opening and parsing a PDF is the expensive part of a sync — orders of
    magnitude more than anything else — so a document is read once and remembered.
    Deterministic event IDs already stop duplicates; this stops the wasted work.
    """
    entry = ledger.get(key)
    return bool(entry and entry.get("parser_version") == PARSER_VERSION)


def record_parsed(ledger: dict, key: str, reports: list[DocReport], events: int) -> None:
    ledger[key] = {
        "parser_version": PARSER_VERSION,
        "parsed_at": ev.iso(now_ist()),
        "events": events,
        "kinds": sorted({r.kind for r in reports if r.kind != "unknown"}),
    }


# ---------------------------------------------------------------- sync state

def load_sync_state() -> dict:
    return read_json(paths.SYNC_STATE, default={}) or {}


def save_sync_state(state: dict) -> None:
    write_json(paths.SYNC_STATE, state)


def last_fetch_for(mailbox_key: str) -> datetime:
    state = load_sync_state().get("mailboxes", {}).get(mailbox_key, {})
    stamp = state.get("last_fetch_at")
    if stamp:
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            pass
    # First run: reach back far enough to pick up a monthly holdings statement.
    return now_ist().replace(year=now_ist().year - 1)


# ------------------------------------------------------------ identification

def _identify(
    att: Attachment, members: list[Member], recipients: list[str]
) -> tuple[Member | None, str, str]:
    """Return (member, password, how) for a PDF attachment.

    The statement password does double duty: it opens the file *and* says whose
    the file is, since no two people share one. Everything else is a fallback for
    documents that aren't encrypted at all.
    """
    try:
        unlock(att.data, [])          # unencrypted?
        encrypted = False
    except PdfLocked:
        encrypted = True

    if encrypted:
        opened: list[tuple[Member, str]] = []
        for member in members:
            for pw in member.pdf_passwords:
                try:
                    if unlock(att.data, [pw]) is not None:
                        opened.append((member, pw))
                        break
                except PdfLocked:
                    continue

        if len(opened) == 1:
            return opened[0][0], opened[0][1], "statement password"
        if len(opened) > 1:
            # Two people sharing a password makes ownership unknowable. Refusing
            # to guess is the only safe answer — attributing a portfolio to the
            # wrong person would be silently wrong for good.
            names = ", ".join(m.name for m, _ in opened)
            return None, "", (f"ambiguous: the same statement password is set for {names} — "
                              "give each person a distinct one")
        return None, "", ("encrypted, and none of the configured passwords opened it — "
                          "add this broker's password for the right person in Setup")

    # Unencrypted: an identifier printed in the document still names the owner.
    text = extract_text(att.data, "")
    for member in members:
        for pw in member.pdf_passwords:
            if len(pw) >= 6 and re.search(re.escape(pw), text, re.I):
                return member, "", "identifier found in the document"

    for member in members:
        if any(addr in recipients for addr in member.emails):
            return member, "", "recipient address"

    if len(members) == 1:
        return members[0], "", "only member on this mailbox"
    return None, "", "could not tell which member this belongs to"


# ------------------------------------------------------------------ ingestion

def process_attachment(
    att: Attachment,
    *,
    subject: str,
    mail_date: date,
    members: list[Member],
    recipients: list[str],
    doc_ref: str,
    sender: str = "",
) -> tuple[DocReport, list[dict]]:
    report = DocReport(slug=doc_ref, subject=subject, date=mail_date.isoformat(), filename=att.filename)

    # The sending address identifies the broker before the file is even opened,
    # so the extraction strategy is looked up rather than guessed at.
    broker = cfgmod.broker_for_sender(sender)
    if broker:
        report.broker = broker
        report.notes.append(f"broker: {broker}"
                            + ("" if cfgmod.is_verified(broker)
                               else " (layout not yet confirmed against a real document)"))

    if not att.is_pdf:
        report.notes.append("not a PDF")
        return report, []

    member, password, how = _identify(att, members, recipients)
    report.notes.append(f"identity: {how}")
    if member is None:
        report.status = "needs_attention"
        return report, []
    report.member = member.id

    text = extract_text(att.data, password)
    if not text.strip():
        report.status = "needs_attention"
        report.notes.append("no text could be extracted (scanned image?)")
        return report, []
    tables = extract_tables(att.data, password)

    if contract_note.looks_like_contract_note(subject, att.filename, text):
        report.kind = "contract_note"
        result = contract_note.parse(
            text, tables, mail_date,
            layout=cfgmod.layout_for(broker, "contract_note"))
        report.notes.extend(result.notes)
        if not result.ok:
            report.status = "needs_attention"
            return report, []
        stamp = datetime.combine(result.as_of or mail_date, TRADE_TIME, tzinfo=IST)
        # Back-fill any missing ISIN before the event is written. A trade keyed by
        # symbol and a snapshot keyed by ISIN are two different instruments as far
        # as the replay is concerned, and the snapshot would zero the position.
        for row in result.rows:
            if not row.get("isin"):
                row["isin"] = instruments.isin_for(row.get("name", ""), row.get("symbol", ""))
                if row["isin"]:
                    report.notes.append(f"matched {row['symbol']} to {row['isin']} by name")
        out = [
            ev.make_trade(
                member=member.id, ts=stamp, side=row["side"], isin=row["isin"],
                symbol=row["symbol"], name=row["name"], qty=row["qty"], price=row["price"],
                charges=row.get("charges", 0.0), trade_no=row.get("trade_no"),
                order_no=row.get("order_no"), source=ev.SRC_CONTRACT_NOTE, doc=doc_ref,
            )
            for row in result.rows
        ]
        report.status = "parsed"
        return report, out

    if holdings.looks_like_holdings(subject, att.filename, text):
        report.kind = "holdings_statement"
        result = holdings.parse(
            text, tables, mail_date,
            layout=cfgmod.layout_for(broker, "holdings"))
        report.notes.extend(result.notes)
        if not result.ok:
            report.status = "needs_attention"
            return report, []
        stamp = datetime.combine(result.as_of or mail_date, SNAPSHOT_TIME, tzinfo=IST)
        snapshot = ev.make_snapshot(
            member=member.id, ts=stamp, holdings=result.rows,
            source=ev.SRC_HOLDINGS_STATEMENT, doc=doc_ref,
            has_cost=holdings.has_cost(result.rows),
        )
        report.status = "parsed"
        return report, [snapshot]

    blob = f"{subject} {att.filename}".lower()
    if "fund" in blob or "ledger" in blob or "balance" in blob:
        # The cash ledger. Real document, just nothing to do with holdings.
        report.kind = "funds_statement"
        report.notes.append("cash ledger — no securities in it, nothing to log")
    else:
        report.notes.append("did not match any known statement type")
    return report, []


def process_document(
    *, subject: str, mail_date: date, attachments: Iterable[Attachment],
    recipients: list[str], members: list[Member], doc_ref: str, sender: str = "",
) -> tuple[list[DocReport], list[dict]]:
    reports, out = [], []
    for att in attachments:
        report, produced = process_attachment(
            att, subject=subject, mail_date=mail_date, members=members,
            recipients=recipients, doc_ref=doc_ref, sender=sender,
        )
        report.events = len(produced)
        reports.append(report)
        out.extend(produced)
    return reports, out


# -------------------------------------------------------------------- drivers

def sync_mail(cfg: Config, *, full: bool = False) -> SyncReport:
    """Fetch new mail from every configured mailbox and ingest it."""
    report = SyncReport(started_at=ev.iso(now_ist()))
    state = load_sync_state()
    state.setdefault("mailboxes", {})
    pending: list[dict] = []
    # Shared with reparse: a document read here is not read again later.
    ledger = load_parsed()

    if cfg.effective_sources().is_empty():
        report.errors.append(
            "Nothing to look for: no member has a broker set and no extra senders "
            "were added. Without a filter a sync would have to download the whole "
            "mailbox — set each person's broker in the Setup tab."
        )
        report.finished_at = ev.iso(now_ist())
        return report

    for mb, members in cfg.mailboxes():
        key = f"{mb.user}@{mb.host}/{mb.folder}"
        # Only this inbox's own brokers — searching one person's mail for another
        # broker's statements is wasted work.
        sources = cfg.effective_sources(members)
        entry = state["mailboxes"].setdefault(key, {})
        since = datetime(2000, 1, 1, tzinfo=IST) if full else last_fetch_for(key)
        # Resume from the last message we processed rather than re-scanning a
        # date window. Ignored on a full run.
        min_uid = None if full else entry.get("last_uid")
        highest = int(min_uid or 0)

        try:
            with MailReader(mb) as reader:
                uids = reader.search(
                    since=since, senders=sources.senders, subjects=sources.subjects,
                    min_uid=(int(min_uid) + 1) if min_uid else None,
                )
                report.matched += len(uids)
                for uid in uids:
                    doc = reader.fetch(uid)
                    highest = max(highest, int(uid))
                    if doc is None:
                        continue
                    report.messages_seen += 1
                    if not doc.attachments:
                        report.documents.append(DocReport(
                            slug=doc.slug, subject=doc.subject,
                            date=doc.date.date().isoformat(), status="skipped",
                            notes=["matched the filters but has no attachment — "
                                   "probably a notification with a download link"],
                        ))
                        continue
                    archive(doc)
                    reports, produced = process_document(
                        subject=doc.subject, mail_date=doc.date.date(),
                        attachments=doc.attachments, recipients=doc.recipients,
                        members=members, doc_ref=doc.slug, sender=doc.sender,
                    )
                    report.documents.extend(reports)
                    pending.extend(produced)
                    record_parsed(ledger, doc.message_id, reports, len(produced))
            entry.update({
                "last_fetch_at": ev.iso(now_ist()),
                "last_status": "ok",
                "last_error": None,
                "last_uid": highest or entry.get("last_uid"),
                "messages_seen": report.messages_seen,
            })
        except Exception as exc:
            log.exception("mailbox sync failed for %s", key)
            entry.update({"last_status": "error", "last_error": str(exc)})
            report.errors.append(f"{key}: {exc}")

    written, dupes = ev.append(pending)
    save_parsed(ledger)
    report.events_written, report.duplicates = written, dupes
    report.finished_at = ev.iso(now_ist())
    state["last_mail_sync"] = report.finished_at
    save_sync_state(state)
    return report


def rewind_mailboxes(cfg: Config, member_id: str, when: date) -> list[str]:
    """Point every inbox serving this member back at `when`.

    A statement states the position on its date, so everything that matters after
    it is a trade — and the incremental cursor normally resumes from the last
    message already seen, which skips precisely that stretch. Rewinding lets the
    next sync walk forward from the statement's own date instead.

    Two people can share one inbox, so a rewind can pull a little extra mail for
    somebody else. That costs time and nothing else: ingestion is idempotent.
    """
    state = load_sync_state()
    state.setdefault("mailboxes", {})
    rewound: list[str] = []
    for mb, members in cfg.mailboxes():
        if not any(m.id == member_id for m in members):
            continue
        key = f"{mb.user}@{mb.host}/{mb.folder}"
        entry = state["mailboxes"].setdefault(key, {})
        # last_uid beats any date window in the IMAP search, so it has to go —
        # leaving it set would make the rewind silently do nothing.
        entry["last_uid"] = None
        entry["last_fetch_at"] = ev.iso(datetime.combine(when, time(0, 0), tzinfo=IST))
        rewound.append(mb.user)
    save_sync_state(state)
    return rewound


def ingest_statement(
    cfg: Config, data: bytes, *, filename: str, member_id: str | None = None,
    rewind: bool = True,
) -> dict:
    """Ingest a holdings statement handed over directly instead of found in mail.

    The point of it is recovery. When a month's statement never arrives — or
    arrives somewhere the sync cannot see it — this sets the position to what the
    document says and rewinds the mail cursor to the document's own date, so the
    next sync reads every contract note after it and brings the numbers forward.

    Naming a member restricts identification to their statement password, which is
    what makes an unencrypted document attributable at all. Without one, the
    password decides the owner exactly as it does for mail.
    """
    members = cfg.active_members()
    if member_id:
        member = cfg.member(member_id)
        if not member:
            return {"ok": False, "detail": f"no member called {member_id!r}"}
        members = [member]
    if not members:
        return {"ok": False, "detail": "no active members to attribute this to"}

    att = Attachment(filename=filename or "statement.pdf",
                     content_type="application/pdf", data=data)
    if not att.is_pdf:
        return {"ok": False, "detail": "that is not a PDF"}

    # Keyed by content, so uploading the same file twice is recognised as the same
    # document rather than archived again under a new name.
    doc_ref = f"<upload-{hashlib.sha256(data).hexdigest()[:16]}@lamestreet.local>"
    report, produced = process_attachment(
        att, subject=f"Uploaded statement: {filename}", mail_date=now_ist().date(),
        members=members, recipients=[], doc_ref=doc_ref,
    )

    if report.member is None:
        return {"ok": False, "detail": "; ".join(report.notes) or "could not identify the owner",
                "notes": report.notes}
    if report.kind != "holdings_statement" or not produced:
        looked_like = report.kind.replace("_", " ") if report.kind != "unknown" else "nothing known"
        return {"ok": False, "notes": report.notes,
                "detail": f"this reads as {looked_like}, not a holdings statement — "
                          "upload the demat/holdings statement that lists every "
                          "position with its quantity"}

    snapshot = produced[0]
    as_of = date.fromisoformat(snapshot["ts"][:10])

    # Archive before appending, so the document behind the snapshot is always
    # recoverable — a re-parse can then re-derive it like any other statement.
    archive(MailDoc(
        uid="", message_id=doc_ref, subject=f"Uploaded statement: {filename}",
        sender="upload", recipients=[], body="",
        date=datetime.combine(as_of, time(12, 0), tzinfo=IST),
        attachments=[att], uploaded=True,
    ))

    written, dupes = ev.append(produced)
    ledger = load_parsed()
    record_parsed(ledger, doc_ref, [report], len(produced))
    save_parsed(ledger)

    registry = instruments.load_registry()
    for row in snapshot["holdings"]:
        instruments.resolve(registry, isin=row.get("isin"), symbol=row["symbol"],
                            name=row.get("name", ""))
    instruments.save_registry(registry)

    return {
        "ok": True,
        "member": report.member,
        "as_of": as_of.isoformat(),
        "positions": len(snapshot["holdings"]),
        "new": bool(written),
        "duplicate": bool(dupes and not written),
        "rewound": rewind_mailboxes(cfg, report.member, as_of) if rewind else [],
        "notes": report.notes,
    }


def _supersede_derived_events(types: tuple[str, ...] | None = None) -> tuple[int, int, Path | None]:
    """Set aside every event that came from a document, keeping manual entries.

    Needed when a parser was wrong rather than merely incomplete. Deterministic
    IDs make re-parsing safe against *duplicates*, but they can't retract an
    event that shouldn't have existed — and a wrong snapshot is destructive,
    because a snapshot supersedes the log.

    `types` narrows it to certain event types. A fix to the holdings parser has no
    business retiring years of trades it cannot re-derive, and the trades are the
    part of the log that nothing else can reconstruct.

    Nothing is deleted: the old shards move into a timestamped folder so the
    original interpretation stays auditable.
    """
    shards = sorted(paths.EVENTS.glob("*.jsonl"))
    if not shards:
        return 0, 0, None

    kept: list[dict] = []
    dropped = 0
    for shard in shards:
        for row in read_jsonl(shard):
            derived = row.get("source") not in (ev.SRC_MANUAL, ev.SRC_CSV)
            in_scope = types is None or row.get("type") in types
            if derived and in_scope:
                dropped += 1
            else:
                kept.append(row)

    backup = paths.EVENTS / f"superseded-{now_ist():%Y%m%d-%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    for shard in shards:
        shard.rename(backup / shard.name)

    by_shard: dict[Path, list[dict]] = {}
    for row in kept:
        by_shard.setdefault(ev.shard_for(row["ts"]), []).append(row)
    for shard, rows in by_shard.items():
        append_jsonl(shard, rows)

    return len(kept), dropped, backup


def reparse_archive(cfg: Config, *, rebuild: bool = False, snapshots: bool = False,
                    force: bool = False) -> SyncReport:
    """Re-run every archived document through the parsers.

    Deterministic IDs make this safe to run as often as you like — after a parser
    fix it back-fills whatever previously failed and changes nothing else.

    `rebuild` additionally retires the events that earlier parses produced, which
    is what you want when a parser was reading a document *wrongly* rather than
    failing to read it.

    `snapshots` does the same for holdings snapshots alone. That is the mode a
    holdings-parser fix wants: a misread snapshot has to be retired, because it
    supersedes the log and no amount of re-parsing can undo it, while the trades
    stay exactly as they are. A whole-log rebuild would also retire every trade,
    and any contract note the parsers cannot read today would be lost with it.
    """
    report = SyncReport(started_at=ev.iso(now_ist()))
    members = cfg.active_members()
    pending: list[dict] = []
    ledger = load_parsed()
    # A rebuild retires the existing events, so every document has to be read
    # again regardless of what the ledger says.
    force = force or rebuild or snapshots
    skipped = 0

    # A statement forwarded by hand arrives from the member's own address, not the
    # broker's, so filtering the archive on broker senders alone hides it from a
    # re-parse — and with it whatever month the automatic feed missed.
    senders = cfg.effective_sources().senders + [
        email.lower() for member in members for email in member.emails if email
    ]
    for meta, attachments in load_archived(senders):
        if not attachments:
            continue
        key = meta.get("message_id") or meta.get("date", "")
        if not force and already_parsed(ledger, key):
            skipped += 1
            continue
        report.messages_seen += 1
        try:
            when = datetime.fromisoformat(meta["date"]).date()
        except Exception:
            when = now_ist().date()
        reports, produced = process_document(
            subject=meta.get("subject", ""), mail_date=when, attachments=attachments,
            recipients=meta.get("recipients", []), members=members,
            doc_ref=meta.get("message_id", "archived"),
            sender=meta.get("from", ""),
        )
        report.documents.extend(reports)
        pending.extend(produced)
        record_parsed(ledger, key, reports, len(produced))

    if skipped:
        report.documents.append(DocReport(
            slug="skipped", subject=f"{skipped} document(s) already read by this parser version",
            date=now_ist().date().isoformat(), status="skipped",
            notes=[f"parser version {PARSER_VERSION}; pass --all to read them again"],
        ))

    # Retire the old interpretation only once the new one is in hand. Doing it
    # the other way round means a re-parse that can read nothing — a missing
    # statement password, say — empties the log and takes the dashboard with it.
    if rebuild or snapshots:
        scope = (ev.SNAPSHOT,) if snapshots else None
        in_scope = lambda e: scope is None or e.get("type") in scope   # noqa: E731
        derived = [e for e in ev.all_events()
                   if e.get("source") not in (ev.SRC_MANUAL, ev.SRC_CSV) and in_scope(e)]
        replacements = [e for e in pending if in_scope(e)]
        if derived and len(replacements) < len(derived) * 0.5:
            what = "snapshot(s)" if snapshots else "event(s)"
            report.errors.append(
                f"Refusing to rebuild: re-parsing produced {len(replacements)} {what} "
                f"where the log already holds {len(derived)}. Something is stopping "
                "the documents being read — most likely a missing statement "
                "password. The log has been left untouched."
            )
            report.finished_at = ev.iso(now_ist())
            return report

        kept, dropped, backup = _supersede_derived_events(scope)
        if backup:
            report.documents.append(DocReport(
                slug="rebuild", subject="Retired earlier interpretations",
                date=now_ist().date().isoformat(), status="parsed",
                notes=[f"kept {kept} event(s), retired {dropped} "
                       f"document-derived {'snapshot' if snapshots else 'event'}(s); "
                       f"previous log saved in {backup.name}"],
            ))

    written, dupes = ev.append(pending)
    save_parsed(ledger)
    report.events_written, report.duplicates = written, dupes
    report.finished_at = ev.iso(now_ist())
    return report
