"""Configuration: family members, their brokers, and how to reach their mail.

config.json at the project root is the store — a small filesystem database. The
app writes to it, so adding a person through the UI persists across restarts and
redeployments. It is gitignored and chmod 600 because it holds real names,
addresses and credentials.

.env is a seed, not a second source of truth. Anything it defines fills a gap in
config.json on load and is then persisted on the next save, which is what makes a
fresh deployment work with no manual setup. config.json always wins where it has
a value, so a credential entered in the UI is never overridden by a stale .env.

Point PM_ROOT at a mounted volume to keep both — plus the event log — outside the
checkout.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import env, paths
from .store import read_json, secure_write

# Kept only to recognise a PAN when someone types one, so we can also try the
# upper/lower spellings brokers vary on. Nothing requires a PAN any more.
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


@dataclass
class Mailbox:
    host: str = "imap.gmail.com"
    port: int = 993
    user: str = ""
    password: str = ""
    folder: str = "INBOX"

    @property
    def resolved_password(self) -> str:
        return os.environ.get("PM_MAIL_PASSWORD") or self.password

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.resolved_password)

    def to_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "user": self.user,
                "password": self.password, "folder": self.folder}


# What each broker sends and from where, verified against real inboxes.
#
# `senders` is the whole search criterion: only mail from these addresses is ever
# read. Brokers use a separate sending address per document type, which is a
# gift — we subscribe to the two that matter and never download the daily margin
# statements, retention statements or payout alerts at all.
#
# `subjects` is documentation and a classification hint. It is deliberately NOT
# used to search, because matching a subject line across all senders is how
# unrelated mail (another registrar, a forwarded newsletter) gets pulled in.
#
# `layouts` records which extraction strategy each broker's documents use. Once a
# broker is in this table it is handled by plain code forever — no model, no
# guessing, same output for the same input.
BROKER_PROFILES: dict[str, dict] = {
    "groww": {
        "label": "Groww",
        "senders": ["noreply@groww.in"],
        "subjects": ["contract note", "transaction and holding statement"],
        # SEBI's layout since Feb 2025; the older buy/sell-column and plain-text
        # forms before that. A five-year inbox contains all three.
        "layouts": {"contract_note": ["sebi-split-table", "buy-sell-column", "text"],
                    "holdings": "holdings-balance-table"},
        "verified": True,
    },
    "zerodha": {
        "label": "Zerodha",
        "senders": [
            "no-reply-contract-notes@reportsmailer.zerodha.net",
            "no-reply-transaction-with-holding-statement@reportsmailer.zerodha.net",
        ],
        "subjects": ["combined equity contract note", "transaction with holding statement"],
        # Two formats, and both are still in the archive. Notes up to mid-2025 are
        # ruled one row per fill with a Buy(B)/Sell(S) column; later ones use
        # SEBI's per-ISIN net obligation blocks. Declaring the newer one first
        # keeps it preferred — it states the obligation directly, so it cannot be
        # thrown off by a fill table breaking across pages.
        "layouts": {"contract_note": ["sebi-split-table", "buy-sell-column"],
                    "holdings": "holdings-balance-table"},
        "verified": True,
    },
    "dhan": {
        "label": "Dhan",
        # statements@ carries the contract notes and the monthly holding
        # statement. no-reply@ is deliberately excluded: in a real inbox it was
        # 265 messages of portfolio digests, fund-transfer receipts and
        # auto-pledge alerts, and one stray contract note.
        "senders": ["statements@dhan.co"],
        "subjects": ["contract note", "demat transaction and holding statement"],
        # Confirmed against real documents: the contract note is the same SEBI
        # split-table as Groww and Zerodha, and the monthly holding statement is a
        # balance table whose position column is "Tot Qty" (not "Free Bal", which
        # appears earlier and excludes pledged shares).
        "layouts": {"contract_note": "sebi-split-table",
                    "holdings": "holdings-balance-table"},
        "verified": True,
    },
    "icici": {
        "label": "ICICI Direct",
        "senders": ["service@icicisecurities.com"],
        "subjects": ["contract note", "holding statement"],
        "layouts": {}, "verified": False,
    },
    "upstox": {
        "label": "Upstox",
        "senders": ["donotreply@transactions.upstox.com"],
        "subjects": ["contract note", "holding statement"],
        # No contract note or holding statement has been seen from Upstox yet —
        # the only documents it sent were a Client Master Report and retention
        # reports, neither of which states a position.
        "layouts": {}, "verified": False,
    },
    "iifl": {
        "label": "IIFL",
        "senders": ["trade@iiflstatements.com"],
        "subjects": ["contract note"],
        "layouts": {}, "verified": False,
    },
    "other": {"label": "Other / manual", "senders": [], "subjects": [],
              "layouts": {}, "verified": False},
}


def _current_profiles() -> dict[str, dict]:
    """Built-ins plus any custom brokers in config.json.

    Re-read on every call — it is one small JSON read, and it means a broker
    onboarded from the UI applies to the very next document without a restart.
    """
    try:
        return load().broker_profiles()
    except Exception:
        return BROKER_PROFILES


def broker_for_sender(sender: str) -> str | None:
    """Which broker sent this. The sending address is the deterministic key.

    Every document that reaches the parsers came from an address we explicitly
    subscribed to, so the broker is known before the file is opened — which means
    the extraction strategy can be looked up rather than discovered by trying
    several and seeing which produces something.
    """
    low = (sender or "").lower()
    for key, profile in _current_profiles().items():
        if any(addr and addr in low for addr in profile["senders"]):
            return key
    return None


def layout_for(broker: str | None, kind: str) -> list[str]:
    """The declared extraction strategies for a broker's document of `kind`.

    A list, in the order to attempt them, because brokers change their formats:
    Groww's contract note before February 2025 looks nothing like the SEBI one
    after it, and a five-year mailbox contains both. Declaring both and trying
    them in order stays deterministic — the set and the order are fixed and
    written down — without pretending a broker only ever had one layout.
    """
    if not broker:
        return []
    declared = (_current_profiles().get(broker) or {}).get("layouts", {}).get(kind)
    if not declared:
        return []
    return [declared] if isinstance(declared, str) else list(declared)


def is_verified(broker: str | None) -> bool:
    return bool(broker and _current_profiles().get(broker, {}).get("verified"))


def broker_choices(cfg: "Config | None" = None) -> list[dict]:
    profiles = cfg.broker_profiles() if cfg else BROKER_PROFILES
    return [
        {"id": key, "label": profile["label"],
         "verified": profile.get("verified", False),
         "custom": bool(profile.get("custom"))}
        for key, profile in profiles.items()
    ]


@dataclass
class Llm:
    """Credential for the one model-assisted feature: onboarding a new broker.

    Stored in config.json next to the other secrets so a deployed copy can be
    set up entirely from the browser. Both fields are optional — when empty the
    Anthropic SDK falls back to whatever this machine has (ANTHROPIC_API_KEY or
    an `ant auth login` profile), and with nothing at all the broker scan still
    works, the user just ticks the senders themselves.
    """
    api_key: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {"api_key": self.api_key, "model": self.model}

    def is_empty(self) -> bool:
        return not (self.api_key or self.model)


@dataclass
class Sources:
    """Which messages are worth looking at, evaluated by the mail server.

    This is the difference between a sync that takes seconds and one that never
    finishes. Without it, an incremental read means downloading every message in
    the mailbox and deciding afterwards — tens of thousands of them on a personal
    Gmail. With it, the server returns the handful that match.
    """
    # Extras on top of whatever the members' brokers already imply.
    senders: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"senders": self.senders, "subjects": self.subjects}

    def is_empty(self) -> bool:
        return not (self.senders or self.subjects)


@dataclass
class Member:
    id: str
    name: str
    # Whatever unlocks this person's statement PDFs. Most brokers use the PAN,
    # some use a client code or a date of birth — the app doesn't care which, it
    # only cares that it opens the file and that no two members share one.
    doc_passwords: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    # A list, because people genuinely hold accounts at more than one broker and
    # each one mails a different set of documents from a different address.
    brokers: list[str] = field(default_factory=lambda: ["groww"])
    mailbox: Mailbox | None = None   # only for per-member mailbox mode
    active: bool = True
    # Which values .env supplied because config.json had none. Recorded so the
    # UI can show where a credential came from; they are still persisted on save.
    from_env: set[str] = field(default_factory=set, repr=False, compare=False)

    def to_dict(self) -> dict:
        """Everything about this member, secrets included.

        config.json is the store, not a checked-in file — it is gitignored and
        chmod 600. Persisting credentials here is what lets someone add a person
        through the UI on a deployed instance and have it survive a restart.
        """
        out = {"id": self.id, "name": self.name,
               "doc_passwords": self.doc_passwords,
               "emails": self.emails, "brokers": self.brokers,
               "active": self.active}
        if self.mailbox:
            out["mailbox"] = self.mailbox.to_dict()
        return out

    @property
    def pdf_passwords(self) -> list[str]:
        """Every spelling of this member's passwords worth trying.

        The password does double duty: it opens the PDF *and* identifies whose
        statement it is, because no two people share one. That is why there is no
        separate PAN field — a PAN was only ever being used as a password.

        Brokers disagree about case, so each entry is tried as given, uppercased
        and lowercased.
        """
        out: list[str] = []
        for raw in self.doc_passwords:
            value = (raw or "").strip()
            if value:
                out += [value, value.upper(), value.lower()]
        return list(dict.fromkeys(out))


@dataclass
class Config:
    members: list[Member] = field(default_factory=list)
    mailbox: Mailbox = field(default_factory=Mailbox)   # optional shared inbox
    sources: Sources = field(default_factory=Sources)
    # Brokers onboarded from the UI, stored in config.json under "brokers".
    # Same shape as BROKER_PROFILES entries; they extend the built-in table
    # rather than replacing it, so shipping a new built-in never conflicts.
    custom_brokers: dict[str, dict] = field(default_factory=dict)
    # Claude assist for broker onboarding, entered in the Setup tab.
    llm: Llm = field(default_factory=Llm)

    def broker_profiles(self) -> dict[str, dict]:
        """Built-in brokers plus custom ones, with "Other / manual" kept last."""
        merged = {k: v for k, v in BROKER_PROFILES.items() if k != "other"}
        merged.update(self.custom_brokers)
        merged["other"] = BROKER_PROFILES["other"]
        return merged

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)

    def active_members(self) -> list[Member]:
        return [m for m in self.members if m.active]

    def mailbox_for(self, member: Member) -> Mailbox:
        return member.mailbox or self.mailbox

    def mailboxes(self) -> list[tuple[Mailbox, list[Member]]]:
        """Group members by the mailbox they're read from.

        Shared-inbox setups collapse to a single connection; per-member app
        passwords fan out to one connection each.
        """
        groups: dict[tuple, tuple[Mailbox, list[Member]]] = {}
        for m in self.active_members():
            mb = self.mailbox_for(m)
            if not mb.is_configured():
                continue
            key = (mb.host, mb.port, mb.user, mb.folder)
            groups.setdefault(key, (mb, []))[1].append(m)
        return list(groups.values())

    def brokers_in_use(self) -> list[str]:
        return sorted({b for m in self.active_members() for b in m.brokers})

    def effective_sources(self, members: list[Member] | None = None) -> Sources:
        """What to actually ask the mail server for.

        The brokers in use imply most of it; `sources` only carries anything extra
        the user typed. Scoping by member matters when reading a personal inbox —
        there is no reason to search Alice's mail for Groww statements when
        her only broker is Zerodha.
        """
        pool = members if members is not None else self.active_members()
        senders: list[str] = []
        subjects: list[str] = []
        profiles = self.broker_profiles()
        for broker in sorted({b for m in pool for b in m.brokers}):
            profile = profiles.get(broker)
            if not profile:
                continue
            senders.extend(profile["senders"])
        senders.extend(self.sources.senders)
        # Only whatever the user explicitly added — never the brokers' subject
        # hints, which exist to classify a document, not to find one.
        subjects.extend(self.sources.subjects)
        return Sources(
            senders=list(dict.fromkeys(s.lower() for s in senders if s)),
            subjects=list(dict.fromkeys(s.lower() for s in subjects if s)),
        )

    def testable_mailboxes(self) -> list[tuple[Mailbox, list[Member]]]:
        """Like `mailboxes()`, but includes a configured shared inbox that no
        member is using yet.

        Connecting the mailbox is usually done before anyone is added, and a
        connection test that refuses to run until you have members is useless
        exactly when you need it.
        """
        groups = self.mailboxes()
        if self.mailbox.is_configured():
            key = (self.mailbox.host, self.mailbox.port, self.mailbox.user, self.mailbox.folder)
            already = {(mb.host, mb.port, mb.user, mb.folder) for mb, _ in groups}
            if key not in already:
                groups.append((self.mailbox, []))
        return groups

    def to_dict(self) -> dict:
        out = {
            "sources": self.sources.to_dict(),
            "mailbox": self.mailbox.to_dict(),
            "members": [m.to_dict() for m in self.members],
        }
        if self.custom_brokers:
            # The "custom" marker is derived on load, not stored.
            out["brokers"] = {
                key: {k: v for k, v in profile.items() if k != "custom"}
                for key, profile in self.custom_brokers.items()
            }
        if not self.llm.is_empty():
            out["llm"] = self.llm.to_dict()
        return out

    def save(self) -> None:
        secure_write(paths.CONFIG, self.to_dict())


def _mailbox_from(raw: dict | None) -> Mailbox | None:
    if not raw:
        return None
    return Mailbox(
        host=raw.get("host", "imap.gmail.com"),
        port=int(raw.get("port", 993)),
        user=raw.get("user", ""),
        password=raw.get("password", ""),
        folder=raw.get("folder", "INBOX"),
    )


def load() -> Config:
    # .env first, so its values are visible while members are assembled.
    env.load()
    raw = read_json(paths.CONFIG, default={}) or {}
    members = [
        Member(
            id=m["id"],
            name=m.get("name", m["id"]),

            emails=[e.strip().lower() for e in m.get("emails", []) if e.strip()],
            brokers=[b.strip().lower() for b in
                     (m.get("brokers") or ([m["broker"]] if m.get("broker") else ["groww"]))
                     if b.strip()],
            # A `pan` from an older config is just another password now.
            doc_passwords=[p for p in (
                list(m.get("doc_passwords", []))
                + ([m["pan"]] if m.get("pan") else [])
            ) if (p or "").strip()],
            mailbox=_mailbox_from(m.get("mailbox")),
            active=m.get("active", True),
        )
        for m in raw.get("members", [])
    ]

    # Overlay anything .env supplies, and remember that it came from there.
    for member in members:
        mail_user = env.mail_user(member.id)
        mail_password = env.mail_password(member.id)
        doc_passwords = env.doc_passwords(member.id)

        # Gaps only. config.json is authoritative, so a credential entered through
        # the UI is never silently overridden by a stale .env — and .env still
        # seeds a fresh deployment that has no config.json yet.
        if mail_user or mail_password:
            member.mailbox = member.mailbox or Mailbox()
            if mail_user and not member.mailbox.user:
                member.mailbox.user = mail_user
                member.from_env.add("mail_user")
            if mail_password and not member.mailbox.password:
                member.mailbox.password = mail_password
                member.from_env.add("mail_password")
        if doc_passwords and not member.doc_passwords:
            member.doc_passwords = doc_passwords
            member.from_env.add("doc_passwords")

    raw_sources = raw.get("sources") or {}
    sources = Sources(
        senders=[s.strip().lower() for s in raw_sources.get("senders", []) if s.strip()],
        subjects=[s.strip().lower() for s in raw_sources.get("subjects", []) if s.strip()],
    ) if raw_sources else Sources()

    custom_brokers = {}
    for key, profile in (raw.get("brokers") or {}).items():
        broker_id = slugify(key)
        if not broker_id or broker_id in BROKER_PROFILES or not isinstance(profile, dict):
            continue
        custom_brokers[broker_id] = {
            "label": str(profile.get("label") or broker_id),
            "senders": [s.strip().lower() for s in profile.get("senders", []) if str(s).strip()],
            "subjects": [s.strip().lower() for s in profile.get("subjects", []) if str(s).strip()],
            "layouts": profile.get("layouts") or {},
            "verified": bool(profile.get("verified", False)),
            "custom": True,
        }

    shared = _mailbox_from(raw.get("mailbox")) or Mailbox()
    shared_user, shared_password = env.shared_mailbox()
    if shared_user and not shared.user:
        shared.user = shared_user
    if shared_password and not shared.password:
        shared.password = shared_password

    raw_llm = raw.get("llm") or {}
    llm = Llm(
        api_key=str(raw_llm.get("api_key") or "").strip(),
        model=str(raw_llm.get("model") or "").strip(),
    )

    return Config(
        members=members,
        mailbox=shared,
        sources=sources,
        custom_brokers=custom_brokers,
        llm=llm,
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "member"
