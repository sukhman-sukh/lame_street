"""Setup endpoints — everything that used to require the CLI.

Adding a member, connecting the mailbox, bootstrapping cost basis and fixing
instrument mappings are all one-time-ish jobs, but they are exactly the jobs the
person running this shouldn't have to open a terminal for.

There is no auth in front of these — the server binds to localhost only, so
"can reach it" and "is sitting at this machine" are the same thing.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import auth
from . import build as buildmod
from . import config as cfgmod
from . import events as ev
from . import instruments, llm, manual
from .config import Llm, Mailbox, Member, slugify

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MAX_UPLOAD = 5 * 1024 * 1024


# ------------------------------------------------------------------- payloads

class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    doc_password: str = ""
    emails: list[str] = Field(default_factory=list)
    # Each person's own Gmail and app password, supplied when they're created.
    mail_user: str = ""
    mail_password: str = ""
    brokers: list[str] = Field(default_factory=list)


class MapIn(BaseModel):
    key: str
    symbol: str
    yahoo: str | None = None


class MemberMailIn(BaseModel):
    mail_user: str = ""
    mail_password: str = ""


class MemberDocsIn(BaseModel):
    brokers: list[str] = Field(default_factory=list)
    doc_password: str = ""


class SourcesIn(BaseModel):
    senders: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


class BrokerScanIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class BrokerIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    senders: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


class LlmIn(BaseModel):
    api_key: str = ""
    model: str = Field("", max_length=100)


# -------------------------------------------------------------------- helpers

def _state() -> dict:
    """Everything the setup screen needs, with no secrets in it."""
    cfg = cfgmod.load()
    registry = instruments.load_registry()
    profiles = cfg.broker_profiles()
    return {
        "members": [
            {"id": m.id, "name": m.name,
             "has_password": bool(m.doc_passwords),
             "password_count": len(m.doc_passwords),
             "emails": m.emails, "active": m.active,
             # Never send a password back — only whether one is stored.
             "mail_user": (m.mailbox.user if m.mailbox else ""),
             "mail_ready": bool(m.mailbox and m.mailbox.is_configured()),
             "brokers": m.brokers,
             "broker_labels": [profiles.get(b, {}).get("label", b)
                               for b in m.brokers],
            }
            for m in cfg.members
        ],
        "shared_mailbox": {
            "user": cfg.mailbox.user,
            "configured": cfg.mailbox.is_configured(),
        },
        "sources": cfg.sources.to_dict(),
        "effective_sources": cfg.effective_sources().to_dict(),
        "broker_choices": cfgmod.broker_choices(cfg),
        # Status only — the key itself never leaves the server.
        "llm": llm.status(),
        # Whether a login gates this server, so the viewer can offer a logout.
        "auth_enabled": auth.enabled(),
        "instruments": {
            "known": len(registry),
            "unmapped": [
                {"key": (e.get("isin") or e.get("symbol") or ""), "symbol": e.get("symbol"),
                 "name": e.get("name", "")}
                for e in instruments.unresolved(registry)
            ],
        },
    }


# --------------------------------------------------------------------- routes

@router.get("/setup")
def get_setup():
    return _state()


@router.post("/members", status_code=201)
def add_member(payload: MemberIn):
    cfg = cfgmod.load()
    member_id = slugify(payload.name)
    if cfg.member(member_id):
        raise HTTPException(409, f"“{payload.name}” already exists")

    password = payload.doc_password.strip()
    # Sharing a password would make it impossible to tell whose statement is
    # whose, since that is exactly what identifies the owner.
    if password and any(password in m.doc_passwords for m in cfg.members):
        raise HTTPException(409, "another member already uses that statement password — "
                                 "each person needs a distinct one")

    emails = [e.strip().lower() for e in payload.emails if e.strip()]
    mail_user = payload.mail_user.strip().lower()

    mailbox = None
    if mail_user:
        mailbox = Mailbox(
            user=mail_user,
            # Gmail displays app passwords in groups of four; people paste the spaces.
            password=payload.mail_password.replace(" ", ""),
        )
        if mail_user not in emails:
            emails.append(mail_user)

    brokers = [b.strip().lower() for b in payload.brokers
               if b.strip().lower() in cfg.broker_profiles()] or ["groww"]
    cfg.members.append(Member(
        id=member_id, name=payload.name.strip(),
        emails=emails, brokers=brokers, mailbox=mailbox,
        doc_passwords=[password] if password else [],
    ))
    cfg.save()
    buildmod.build()

    warnings = []
    if not password:
        warnings.append("no statement password — their PDFs are encrypted, so nothing "
                        "can be opened or attributed to them until you add one "
                        "(usually their PAN)")
    if mail_user and not payload.mail_password.strip():
        warnings.append("no app password — the inbox won't connect until you add one")
    if not mail_user:
        warnings.append("no inbox — nothing will sync for them automatically yet")

    return {"id": member_id, "warnings": warnings, "setup": _state()}


@router.patch("/members/{member_id}")
def update_member(member_id: str, payload: MemberIn):
    cfg = cfgmod.load()
    member = cfg.member(member_id)
    if not member:
        raise HTTPException(404, "no such member")
    member.name = payload.name.strip() or member.name
    if payload.doc_password.strip():
        member.doc_passwords = [payload.doc_password.strip()]
    member.emails = [e.strip().lower() for e in payload.emails if e.strip()]
    cfg.save()
    buildmod.build()
    return {"setup": _state()}


@router.delete("/members/{member_id}")
def remove_member(member_id: str):
    cfg = cfgmod.load()
    if not cfg.member(member_id):
        raise HTTPException(404, "no such member")
    cfg.members = [m for m in cfg.members if m.id != member_id]
    cfg.save()
    buildmod.build()
    # Their events stay in the log on purpose — re-adding the same name brings
    # the whole history back, and nothing is ever destroyed by a UI click.
    return {"removed": member_id, "setup": _state()}


@router.post("/members/{member_id}/mailbox")
def set_member_mailbox(member_id: str, payload: MemberMailIn):
    """Set or update one person's Gmail address and app password."""
    cfg = cfgmod.load()
    member = cfg.member(member_id)
    if not member:
        raise HTTPException(404, "no such member")

    member.mailbox = member.mailbox or Mailbox()
    if payload.mail_user.strip():
        member.mailbox.user = payload.mail_user.strip().lower()
        if member.mailbox.user not in member.emails:
            member.emails.append(member.mailbox.user)
    if payload.mail_password.strip():
        member.mailbox.password = payload.mail_password.replace(" ", "")

    if not member.mailbox.user:
        raise HTTPException(422, "an inbox address is required")
    cfg.save()
    return {"setup": _state()}


@router.post("/members/{member_id}/docs")
def set_member_docs(member_id: str, payload: MemberDocsIn):
    """Which brokers a person uses, and any non-PAN statement password."""
    cfg = cfgmod.load()
    member = cfg.member(member_id)
    if not member:
        raise HTTPException(404, "no such member")

    if payload.brokers:
        unknown = [b for b in payload.brokers if b.lower() not in cfg.broker_profiles()]
        if unknown:
            raise HTTPException(422, f"unknown broker(s): {', '.join(unknown)}")
        member.brokers = [b.lower() for b in payload.brokers]
    password = payload.doc_password.strip()
    if password:
        clash = next((m for m in cfg.members
                      if m.id != member_id and password in m.doc_passwords), None)
        if clash:
            raise HTTPException(409, f"{clash.name} already uses that password — "
                                     "each person needs a distinct one")
        if password not in member.doc_passwords:
            member.doc_passwords.append(password)
    cfg.save()
    return {"setup": _state()}


@router.post("/brokers/scan")
def scan_for_broker(payload: BrokerScanIn):
    """Look for a new broker's mail in the connected inboxes.

    Finds candidate sending addresses (with message counts, PDF counts and
    sample subjects), then asks Claude to pick the document senders out of the
    marketing noise. Nothing is saved — the caller reviews and posts to
    /api/brokers if the proposal looks right.
    """
    from datetime import timedelta

    from .mailbox import MailReader, friendly_error

    cfg = cfgmod.load()
    groups = cfg.testable_mailboxes()
    if not groups:
        raise HTTPException(400, "connect at least one inbox first — the scan "
                                 "works by looking at real mail from this broker")

    name = payload.name.strip()
    # IMAP FROM matches substrings, so search by name fragments: "Angel One"
    # scans for FROM "angelone" and FROM "angel", plus the name in subjects.
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    tokens = {compact} | {w for w in re.split(r"\s+", name.lower()) if len(w) >= 4}
    since = ev.now_ist() - timedelta(days=365)

    candidates: dict[str, dict] = {}
    errors: list[str] = []
    for mb, _members in groups:
        try:
            with MailReader(mb) as reader:
                uids = reader.search(since=since, senders=sorted(tokens),
                                     subjects=[name.lower()])
                # Newest few per mailbox — enough to see the sender pattern
                # without downloading a year of attachments.
                for uid in uids[-15:]:
                    doc = reader.fetch(uid)
                    if not doc:
                        continue
                    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", doc.sender or "")
                    addr = (match.group(0) if match else doc.sender or "?").lower()
                    entry = candidates.setdefault(
                        addr, {"sender": addr, "count": 0, "pdfs": 0, "subjects": []})
                    entry["count"] += 1
                    if any(a.is_pdf for a in doc.attachments):
                        entry["pdfs"] += 1
                    subject = doc.subject.strip()
                    if subject and subject not in entry["subjects"] and len(entry["subjects"]) < 5:
                        entry["subjects"].append(subject)
        except Exception as exc:
            errors.append(f"{mb.user}: {friendly_error(str(exc))}")

    ranked = sorted(candidates.values(), key=lambda c: (-c["pdfs"], -c["count"]))
    proposal, note = llm.propose_profile(name, ranked)
    return {"candidates": ranked, "proposal": proposal, "llm": note, "errors": errors}


@router.post("/brokers", status_code=201)
def add_broker(payload: BrokerIn):
    """Save a reviewed broker profile so it appears in every broker list."""
    cfg = cfgmod.load()
    broker_id = slugify(payload.name)
    if broker_id in cfg.broker_profiles():
        raise HTTPException(409, f"“{payload.name}” is already a known broker")

    senders = list(dict.fromkeys(
        s.strip().lower() for s in payload.senders if "@" in s and s.strip()))
    if not senders:
        raise HTTPException(422, "at least one sender address is required — that is "
                                 "the only thing that makes the broker's mail readable")

    cfg.custom_brokers[broker_id] = {
        "label": payload.name.strip(),
        "senders": senders,
        "subjects": [s.strip().lower() for s in payload.subjects if s.strip()],
        # Nearly every Indian broker uses SEBI's common formats, so start there;
        # `verified` stays False until a real document parses.
        "layouts": {"contract_note": "sebi-split-table",
                    "holdings": "holdings-balance-table"},
        "verified": False,
        "custom": True,
    }
    cfg.save()
    return {"id": broker_id, "setup": _state()}


@router.delete("/brokers/{broker_id}")
def remove_broker(broker_id: str):
    cfg = cfgmod.load()
    if broker_id in cfgmod.BROKER_PROFILES:
        raise HTTPException(409, "built-in brokers can't be removed")
    if broker_id not in cfg.custom_brokers:
        raise HTTPException(404, "no such broker")
    used_by = [m.name for m in cfg.members if broker_id in m.brokers]
    if used_by:
        raise HTTPException(409, f"{', '.join(used_by)} still use(s) this broker — "
                                 "change their brokers first")
    del cfg.custom_brokers[broker_id]
    cfg.save()
    return {"removed": broker_id, "setup": _state()}


@router.post("/llm")
def set_llm(payload: LlmIn):
    """The Anthropic key and model Claude assist uses, kept in config.json
    alongside the other secrets (chmod 600, gitignored, never sent back).

    An empty key keeps whatever is already stored, so the model can be changed
    without re-pasting the key.
    """
    cfg = cfgmod.load()
    if payload.api_key.strip():
        cfg.llm.api_key = payload.api_key.strip()
    cfg.llm.model = payload.model.strip()
    cfg.save()
    return {"setup": _state()}


@router.delete("/llm")
def clear_llm():
    """Forget the stored key and model. The environment fallback still applies."""
    cfg = cfgmod.load()
    cfg.llm = Llm()
    cfg.save()
    return {"setup": _state()}


@router.post("/llm/test")
def test_llm():
    """Validate the credential with a free Models API call — no tokens spent."""
    ok, detail = llm.test_credential()
    if not ok:
        raise HTTPException(502, detail)
    return {"ok": True, "detail": detail}


@router.post("/sources")
def set_sources(payload: SourcesIn):
    """Which senders and subjects the mail server should match."""
    cfg = cfgmod.load()
    senders = [s.strip().lower() for s in payload.senders if s.strip()]
    subjects = [s.strip().lower() for s in payload.subjects if s.strip()]
    if not senders and not subjects:
        raise HTTPException(
            422, "Give at least one sender or subject. With no filter, a sync would "
                 "have to download every message in the mailbox.")
    cfg.sources.senders = senders
    cfg.sources.subjects = subjects
    cfg.save()
    return {"setup": _state()}


@router.post("/sources/preview")
def preview_sources():
    """Count what the current filters match, without downloading anything.

    Worth having: it turns "did I get the sender right?" into a number, instead
    of a sync that silently finds nothing.
    """
    from datetime import timedelta

    from .mailbox import MailReader, friendly_error

    cfg = cfgmod.load()
    groups = cfg.testable_mailboxes()
    if not groups:
        raise HTTPException(400, "no inbox connected yet")
    if cfg.sources.is_empty():
        raise HTTPException(422, "no senders or subjects configured")

    since = ev.now_ist() - timedelta(days=365)
    results = []
    for mb, members in groups:
        try:
            with MailReader(mb) as reader:
                matched = reader.search(since=since, senders=cfg.sources.senders,
                                        subjects=cfg.sources.subjects)
                total = reader.count_since(since)
            results.append({
                "user": mb.user, "ok": True, "matched": len(matched), "total": total,
                "detail": f"{len(matched)} matching messages in the last year "
                          f"(out of {total:,} total)",
                "members": [m.name for m in members],
            })
        except Exception as exc:
            results.append({"user": mb.user, "ok": False, "matched": 0, "total": 0,
                            "detail": friendly_error(str(exc)),
                            "members": [m.name for m in members]})
    return {"results": results}


@router.post("/mailbox/test")
def test_mailbox():
    from .mailbox import MailReader

    cfg = cfgmod.load()
    groups = cfg.testable_mailboxes()
    if not groups:
        raise HTTPException(400, "no mailbox configured yet")

    results = []
    for mb, members in groups:
        try:
            with MailReader(mb) as reader:
                seen = reader.count_since(ev.now_ist().replace(day=1))
            results.append({"user": mb.user, "ok": True,
                            "detail": f"connected — {seen} messages this month",
                            "members": [m.name for m in members]})
        except Exception as exc:
            from .mailbox import friendly_error

            results.append({"user": mb.user, "ok": False,
                            "detail": friendly_error(str(exc)),
                            "members": [m.name for m in members]})
    return {"results": results}


@router.post("/members/{member_id}/holdings")
async def upload_holdings(member_id: str, file: UploadFile = File(...), as_of: str = Form("")):
    """Set a member's holdings — and their cost basis — from a broker CSV export.

    The one thing mail can never supply. Depository statements carry quantity but
    never what was paid, so for any position bought before the mailbox history
    begins there is no document that states its cost. A broker's own export does.
    """
    cfg = cfgmod.load()
    if not cfg.member(member_id):
        raise HTTPException(404, "no such member")

    blob = await file.read()
    if len(blob) > MAX_UPLOAD:
        raise HTTPException(413, "file is larger than 5 MB — that isn't a holdings export")
    if not blob.strip():
        raise HTTPException(422, "the file is empty")

    when = None
    if as_of:
        from datetime import datetime
        try:
            when = datetime.strptime(as_of, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(422, "date should look like 2026-08-10")

    result = manual.record_holdings_csv(
        member_id, blob, filename=file.filename or "upload.csv", when=when)
    if not result.get("ok"):
        raise HTTPException(422, result.get("detail") or "could not read that export")

    payload = buildmod.build()
    after = next((m for m in payload["members"] if m["id"] == member_id), {})
    return {
        **result,
        "invested": after.get("invested"),
        "cost_unknown": sum(1 for h in after.get("holdings", []) if not h.get("cost_known")),
        "setup": _state(),
    }


@router.post("/instruments/refresh")
def refresh_instruments():
    count, err = instruments.refresh_nse_master()
    if err:
        raise HTTPException(502, f"could not fetch NSE's equity list: {err}")
    registry = instruments.load_registry()
    for entry in list(registry.values()):
        if not entry.get("manual"):
            instruments.resolve(registry, isin=entry.get("isin"),
                                symbol=entry.get("symbol", ""), name=entry.get("name", ""))
    instruments.save_registry(registry)
    buildmod.build()
    return {"loaded": count, "setup": _state()}


@router.post("/instruments/map")
def map_instrument(payload: MapIn):
    registry = instruments.load_registry()
    if not payload.symbol.strip():
        raise HTTPException(422, "a symbol is required")
    instruments.set_override(registry, payload.key.strip(),
                             symbol=payload.symbol.strip(), yahoo=payload.yahoo)
    instruments.save_registry(registry)
    buildmod.build()
    return {"setup": _state()}
