"""Command line interface: python -m pm <command>"""
from __future__ import annotations

import argparse
import os
import getpass
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import build as buildmod
from . import config as cfgmod
from . import events as ev
from . import ingest, instruments, manual, paths, prices
from .config import Mailbox, Member, slugify
from .store import read_json, write_json

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _rupees(value) -> str:
    if value is None:
        return "—"
    return f"{'-' if value < 0 else ''}₹{abs(value):,.2f}"


# ------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    paths.ensure_dirs()
    cfg = cfgmod.load()
    cfg.save()
    _say(f"{GREEN}Ready.{RESET} Config at {paths.CONFIG}, data under {paths.DATA}")
    _say("Next: python -m pm member add --name \"...\" --password <statement-pw> "
         "--broker zerodha --email someone@gmail.com")
    return 0


def cmd_member(args) -> int:
    cfg = cfgmod.load()

    if args.action == "list":
        if not cfg.members:
            _say("No members yet.")
            return 0
        for m in cfg.members:
            flag = "" if m.active else f" {DIM}(inactive){RESET}"
            pw = (f"{len(m.doc_passwords)} password(s)" if m.doc_passwords
                  else f"{YELLOW}no statement password{RESET}")
            _say(f"  {BOLD}{m.id}{RESET}  {m.name}  ·  {pw}  ·  "
                 f"{','.join(m.brokers)}  ·  {', '.join(m.emails) or 'no email'}{flag}")
        return 0

    if args.action == "add":
        member_id = args.id or slugify(args.name)
        if cfg.member(member_id):
            _say(f"{RED}Member {member_id} already exists.{RESET}")
            return 1
        password = (args.password or "").strip()
        if password and any(password in m.doc_passwords for m in cfg.members):
            _say(f"{RED}Another member already uses that statement password.{RESET} "
                 "Each person needs a distinct one — it is what identifies whose "
                 "statement is whose.")
            return 1
        if not password:
            _say(f"{YELLOW}No statement password given.{RESET} Broker PDFs are encrypted, "
                 "so nothing can be opened or attributed to this person until you add one "
                 "(usually their PAN).")
        cfg.members.append(Member(
            id=member_id, name=args.name,
            doc_passwords=[password] if password else [],
            brokers=[b.lower() for b in (args.broker or ["groww"])],
            emails=[e.strip().lower() for e in (args.email or [])],
        ))
        cfg.save()
        _say(f"{GREEN}Added{RESET} {args.name} ({member_id}).")
        _say("Bootstrap their cost basis with:  python -m pm import-csv "
             f"--member {member_id} --file holdings.csv")
        return 0

    if args.action == "remove":
        member = cfg.member(args.id)
        if not member:
            _say(f"{RED}No member {args.id}.{RESET}")
            return 1
        cfg.members = [m for m in cfg.members if m.id != args.id]
        cfg.save()
        _say(f"Removed {args.id}. Their events stay in the log — "
             f"re-add with the same id to bring them back.")
        return 0
    return 1


def cmd_mailbox(args) -> int:
    cfg = cfgmod.load()

    if args.action == "set":
        password = args.password
        if password is None:
            password = getpass.getpass("App password (input hidden): ")
        target = cfg.mailbox
        if args.member:
            member = cfg.member(args.member)
            if not member:
                _say(f"{RED}No member {args.member}.{RESET}")
                return 1
            member.mailbox = member.mailbox or Mailbox()
            target = member.mailbox
        target.host = args.host or target.host
        target.port = args.port or target.port
        target.user = args.user or target.user
        target.folder = args.folder or target.folder
        if password:
            target.password = password
        cfg.save()
        _say(f"{GREEN}Saved{RESET} mailbox {target.user} on {target.host} "
             f"({'member ' + args.member if args.member else 'shared'}).")
        _say(f"{DIM}config.json is chmod 600. To keep the password out of the file entirely, "
             f"clear it and set PM_MAIL_PASSWORD instead.{RESET}")
        return 0

    if args.action == "test":
        groups = cfg.testable_mailboxes()
        if not groups:
            _say(f"{RED}No mailbox configured.{RESET} Run: python -m pm mailbox set --user you@gmail.com")
            return 1
        failed = 0
        for mb, members in groups:
            names = ", ".join(m.name for m in members) or "no members added yet"
            try:
                from .mailbox import MailReader

                with MailReader(mb) as reader:
                    seen = reader.count_since(ev.now_ist().replace(day=1))
                _say(f"  {GREEN}ok{RESET}  {mb.user} — {seen} messages this month  {DIM}({names}){RESET}")
            except Exception as exc:
                from .mailbox import friendly_error

                failed += 1
                _say(f"  {RED}fail{RESET}  {mb.user}")
                _say(f"        {friendly_error(str(exc))}")
        return 1 if failed else 0
    return 1


def cmd_sync(args) -> int:
    paths.ensure_dirs()
    cfg = cfgmod.load()
    if not cfg.mailboxes():
        _say(f"{RED}No mailbox configured.{RESET}")
        return 1

    _say(f"Reading mail{' (full history)' if args.full else ' since last run'}…")
    report = ingest.sync_mail(cfg, full=args.full)
    write_json(buildmod.LAST_SYNC, report.to_dict())
    _print_sync(report)

    if not args.no_prices:
        cmd_prices(args)
    buildmod.build()
    _say(f"{GREEN}Dashboard rebuilt.{RESET}")

    # Same contract as a sync run from the UI: if an off-site backup is
    # configured, every successful sync updates it. Without this a scheduled
    # CLI sync would quietly drift from the copy a hosted instance restores.
    from . import backup as backupmod
    if backupmod.enabled():
        try:
            _say(f"{GREEN}{backupmod.save()}{RESET}")
        except Exception as exc:
            _say(f"{YELLOW}Backup failed:{RESET} {exc}")

    return 1 if report.errors else 0


def cmd_statement(args) -> int:
    """Set holdings from a statement PDF, then read the mail that came after it."""
    cfg = cfgmod.load()
    path = Path(args.file)
    if not path.exists():
        _say(f"{RED}No such file:{RESET} {path}")
        return 1

    _say(f"Reading {path.name}…")
    result = ingest.ingest_statement(
        cfg, path.read_bytes(), filename=path.name,
        member_id=args.member, rewind=not args.no_sync,
    )
    for note in result.get("notes", []):
        _say(f"  {DIM}{note}{RESET}")
    if not result.get("ok"):
        _say(f"{RED}✗{RESET} {result.get('detail')}")
        return 1

    state = ("new snapshot" if result["new"]
             else "identical to one already recorded" if result["duplicate"] else "recorded")
    _say(f"{GREEN}✓{RESET} {result['member']} · {result['positions']} positions "
         f"as of {result['as_of']} {DIM}({state}){RESET}")

    if args.no_sync:
        buildmod.build()
        _say(f"{GREEN}Dashboard rebuilt.{RESET} {DIM}Mail cursor left where it was.{RESET}")
        return 0

    if result["rewound"]:
        _say(f"  rewound to {result['as_of']}: {', '.join(result['rewound'])}")
    _say("Reading everything that came after it…")
    report = ingest.sync_mail(cfg)
    write_json(buildmod.LAST_SYNC, report.to_dict())
    _print_sync(report)
    cmd_prices(args)
    buildmod.build()
    _say(f"{GREEN}Dashboard rebuilt.{RESET}")
    return 1 if report.errors else 0


def cmd_reparse(args) -> int:
    cfg = cfgmod.load()
    if args.rebuild:
        _say("Rebuilding the log from archived documents "
             f"{DIM}(manual entries kept, earlier interpretations set aside){RESET}…")
    elif args.snapshots:
        _say("Rebuilding holdings snapshots from archived statements "
             f"{DIM}(trades and manual entries left untouched){RESET}…")
    else:
        _say("Re-parsing archived documents…")
    report = ingest.reparse_archive(cfg, rebuild=args.rebuild,
                                    snapshots=args.snapshots, force=args.all)
    _print_sync(report)
    cmd_prices(args)
    buildmod.build()
    return 0


def _print_sync(report: ingest.SyncReport) -> None:
    _say(f"  messages scanned : {report.messages_seen}")
    _say(f"  documents        : {len(report.documents)}")
    _say(f"  new events       : {GREEN}{report.events_written}{RESET}"
         f"   {DIM}(skipped {report.duplicates} already-known){RESET}")
    parsed = [d for d in report.documents if d.status == "parsed"]
    for doc in parsed:
        _say(f"    {GREEN}✓{RESET} {doc.member or '?'} · {doc.kind} · {doc.events} events "
             f"{DIM}{doc.filename[:40]}{RESET}")
    for doc in report.needs_attention:
        _say(f"    {YELLOW}!{RESET} {doc.filename[:40]} — {'; '.join(doc.notes[-2:])}")
    for err in report.errors:
        _say(f"    {RED}✗{RESET} {err}")


def cmd_prices(args) -> int:
    tickers = buildmod.collect_tickers()
    if not tickers:
        _say(f"{DIM}No priceable holdings yet.{RESET}")
        return 0
    _say(f"Fetching {len(tickers)} prices from Yahoo…")
    cache = prices.refresh(tickers)
    got = cache.get("updated", 0)
    colour = GREEN if got == len(tickers) else YELLOW
    _say(f"  {colour}{got}/{len(tickers)} updated{RESET}")
    return 0


def cmd_build(args) -> int:
    payload = buildmod.build()
    _say(f"{GREEN}Built{RESET} {paths.DASHBOARD} — {payload['totals']['members']} members, "
         f"{payload['totals']['positions']} positions")
    return 0


def cmd_refresh(args) -> int:
    cmd_prices(args)
    return cmd_build(args)


def cmd_import_csv(args) -> int:
    cfg = cfgmod.load()
    if not cfg.member(args.member):
        _say(f"{RED}No member {args.member}.{RESET}")
        return 1
    rows, notes = manual.read_holdings_csv(Path(args.file))
    if rows:
        from .replay import replay
        rows, match_notes = manual.match_to_holdings(
            rows, replay()["holdings"].get(args.member, []))
        notes += match_notes
    for note in notes:
        _say(f"  {YELLOW}note{RESET}: {note}")
    if not rows:
        return 1
    when = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    snapshot = manual.set_holdings(member=args.member, rows=rows, when=when,
                                   source=ev.SRC_CSV, doc=Path(args.file).name)
    written, dupes = ev.append([snapshot])
    _say(f"{GREEN}Imported{RESET} {len(rows)} positions for {args.member} "
         f"({'new snapshot' if written else 'identical to an existing snapshot'}).")
    buildmod.build()
    return 0


def cmd_add_trade(args) -> int:
    when = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    event = manual.add_trade(
        member=args.member, symbol=args.symbol, side=args.side,
        qty=args.qty, price=args.price, when=when, charges=args.charges or 0.0,
    )
    ev.append([event])
    _say(f"{GREEN}Logged{RESET} {args.side} {args.qty} {args.symbol} @ {args.price}")
    buildmod.build()
    return 0


def cmd_instruments(args) -> int:
    registry = instruments.load_registry()
    if args.action == "refresh":
        count, err = instruments.refresh_nse_master()
        if err:
            _say(f"{RED}NSE master refresh failed:{RESET} {err}")
            return 1
        _say(f"{GREEN}Loaded{RESET} {count} NSE instruments.")
        # Re-resolve anything previously unmatched.
        for key, entry in list(registry.items()):
            if not entry.get("manual"):
                instruments.resolve(registry, isin=entry.get("isin"),
                                    symbol=entry.get("symbol", ""), name=entry.get("name", ""))
        instruments.save_registry(registry)
        buildmod.build()
        return 0

    if args.action == "list":
        pending = instruments.unresolved(registry)
        _say(f"{len(registry)} known, {len(pending)} unmapped")
        for entry in pending:
            _say(f"  {YELLOW}?{RESET} {entry.get('isin') or '—'}  {entry.get('symbol')}  "
                 f"{DIM}{entry.get('name','')[:40]}{RESET}")
        return 0

    if args.action == "map":
        instruments.set_override(registry, args.key, symbol=args.symbol, yahoo=args.yahoo)
        instruments.save_registry(registry)
        _say(f"{GREEN}Mapped{RESET} {args.key} → {args.symbol}")
        buildmod.build()
        return 0
    return 1


def cmd_inspect(args) -> int:
    """Dump what the parsers see in a PDF. The tool for fixing a broken parse."""
    from .pdfutil import PdfLocked, extract_tables, extract_text, unlock

    cfg = cfgmod.load()
    data = Path(args.file).read_bytes()
    candidates = [pw for m in cfg.active_members() for pw in m.pdf_passwords]
    if args.password:
        candidates.insert(0, args.password)

    try:
        password = unlock(data, candidates)
    except PdfLocked as exc:
        _say(f"{RED}Could not open:{RESET} {exc}")
        _say(f"{DIM}Tried {len(candidates)} candidate password(s) from the configured "
             f"members.{RESET}")
        return 1

    owner = next((m.name for m in cfg.active_members() if password in m.pdf_passwords), None)
    _say(f"{GREEN}Opened{RESET}" + (f" — belongs to {BOLD}{owner}{RESET}" if owner else " (not encrypted)"))

    text = extract_text(data, password)
    tables = extract_tables(data, password)
    _say(f"{len(text)} characters of text, {len(tables)} tables")

    from .parsers import contract_note, holdings

    kind = ("contract note" if contract_note.looks_like_contract_note("", args.file, text)
            else "holdings statement" if holdings.looks_like_holdings("", args.file, text)
            else "unrecognised")
    _say(f"Classified as: {BOLD}{kind}{RESET}")

    parser = contract_note if kind == "contract note" else holdings if kind == "holdings statement" else None
    if parser:
        result = parser.parse(text, tables, ev.now_ist().date())
        _say(f"as-of date: {result.as_of}   method: {result.method or 'none'}")
        for note in result.notes:
            _say(f"  {DIM}·{RESET} {note}")
        for row in result.rows[:25]:
            _say(f"  {row}")
        if len(result.rows) > 25:
            _say(f"  {DIM}… and {len(result.rows) - 25} more{RESET}")

    if args.redact:
        from .redact import redact, summarise_tables

        _say(f"\n{DIM}{'─' * 24} table structure (values masked) {'─' * 24}{RESET}")
        for line in summarise_tables(tables) or ["  (no tables — this is a text-layout PDF)"]:
            _say(line)
        _say(f"\n{DIM}{'─' * 26} text layout (values masked) {'─' * 26}{RESET}")
        _say(redact(text[:6000]))
        _say(f"\n{DIM}Digits are replaced with 9 and identifiers removed; only the layout "
             f"remains. Safe to share.{RESET}")
    elif args.text:
        _say(f"\n{DIM}{'─' * 60} raw text {'─' * 60}{RESET}")
        _say(text[:8000])
        _say(f"\n{YELLOW}This is unredacted — it contains real identifiers and holdings. "
             f"Use --redact if you plan to share it.{RESET}")
    return 0


def cmd_status(args) -> int:
    payload = read_json(paths.DASHBOARD, default=None)
    if not payload:
        _say("No dashboard built yet. Run: python -m pm build")
        return 0
    t = payload["totals"]
    share = f" ({t['pnl_pct']:+.2f}%)" if t.get("pnl_pct") is not None else ""
    _say(f"{BOLD}Family{RESET}  invested {_rupees(t['invested'])}  ·  now {_rupees(t['current'])}  "
         f"·  P&L {_rupees(t['pnl'])}{share}")
    if t.get("unpriced"):
        _say(f"{YELLOW}{t['unpriced']} holding(s) have no price — excluded from P&L.{RESET}")
    _say(f"{DIM}prices as of {payload['as_of']['prices'] or 'never'} · "
         f"last mail sync {payload['as_of']['last_mail_sync'] or 'never'} · "
         f"{payload['sync']['events']} events{RESET}")
    _say("")
    for m in payload["members"]:
        _say(f"  {m['name']:<16} {m['positions']:>3} positions  {_rupees(m['current']):>16}  "
             f"P&L {_rupees(m['pnl']):>14}")
    if payload["attention"]:
        _say(f"\n{YELLOW}Needs attention ({len(payload['attention'])}){RESET}")
        for item in payload["attention"][:10]:
            _say(f"  · {item['detail'][:100]}")
    return 0


def cmd_export(args) -> int:
    from .export import export

    try:
        info = export(Path(args.out))
    except FileNotFoundError as exc:
        _say(f"{RED}{exc}{RESET}")
        return 1
    _say(f"{GREEN}Exported{RESET} to {info['path']} — {info['members']} members, "
         f"{info['positions']} positions, as of {info['generated_at']}")
    _say(f"{DIM}Deploy with:  cd {info['path']} && vercel deploy --prod{RESET}")
    _say(f"{YELLOW}Note:{RESET} this is a read-only snapshot. Anyone with the URL sees the "
         "family's full net worth — put it behind access control or keep it private.")
    return 0


def cmd_schedule(args) -> int:
    from .schedule import LABEL, write_plist

    try:
        hour, minute = (int(x) for x in args.time.split(":"))
    except ValueError:
        _say(f"{RED}--time must look like 20:30{RESET}")
        return 1
    path = write_plist(hour, minute)
    _say(f"{GREEN}Wrote{RESET} {path}")
    _say(f"A daily sync at {args.time} is described there. Turn it on with:")
    _say(f"  launchctl load {path}")
    _say(f"{DIM}Turn it off later with: launchctl unload {path}{RESET}")
    _say(f"{DIM}Logs go to data/state/sync.log{RESET}")
    return 0


def cmd_secrets(args) -> int:
    """Move secrets out of config.json into .env, or report where they come from."""
    from . import env as envmod

    cfg = cfgmod.load()

    if args.action == "check":
        _say(f"{BOLD}Where each secret comes from{RESET}")
        for m in cfg.members:
            box = m.mailbox
            src = lambda field: (f"{GREEN}.env{RESET}" if field in m.from_env
                                 else f"{YELLOW}config.json{RESET}")
            _say(f"  {BOLD}{m.name}{RESET}  ({m.id})")
            _say(f"      inbox            {(box.user if box else '—') or '—'}  "
                 f"[{src('mail_user')}]")
            _say(f"      app password     {'set' if (box and box.resolved_password) else RED + 'missing' + RESET}  "
                 f"[{src('mail_password')}]")
            _say(f"      statement pw     {len(m.doc_passwords) or RED + 'missing' + RESET}  "
                 f"[{src('doc_passwords')}]")
        seeded = [m.name for m in cfg.members if m.from_env]
        missing = [m.name for m in cfg.members if not m.doc_passwords]
        _say("")
        _say(f"{DIM}config.json is the store — credentials live there and persist across "
             f"restarts. .env only fills gaps on a fresh deployment.{RESET}")
        if seeded:
            _say(f"{YELLOW}Currently taken from .env:{RESET} {', '.join(seeded)}")
            _say("Run `python -m pm secrets adopt` to write them into config.json for good.")
        if missing:
            _say(f"{YELLOW}No statement password yet:{RESET} {', '.join(missing)} — "
                 "their PDFs cannot be opened until one is set.")
        return 0

    if args.action == "adopt":
        # Pull whatever .env is currently supplying into config.json, so the store
        # is self-contained and .env can be deleted.
        adopted = [m.name for m in cfg.members if m.from_env]
        cfg.save()
        if adopted:
            _say(f"{GREEN}Persisted{RESET} .env-supplied credentials into {paths.CONFIG} "
                 f"for: {', '.join(adopted)}")
            _say(f"{DIM}config.json is the store now — .env is no longer needed for these.{RESET}")
        else:
            _say("Nothing came from .env; config.json already holds everything.")
        return 0

    if args.action == "export":
        if envmod.ENV_FILE.exists() and not args.force:
            _say(f"{RED}{envmod.ENV_FILE} already exists.{RESET} "
                 "Pass --force to overwrite it (its current contents would be lost).")
            return 1

        rows = [(m.id, m.name, (m.mailbox.user if m.mailbox else ""), list(m.doc_passwords))
                for m in cfg.members]
        body = envmod.render(rows, shared=(cfg.mailbox.user, ""))
        # App passwords are written in as well — the point is that config.json
        # stops holding them, not that they get retyped.
        for m in cfg.members:
            if m.mailbox and m.mailbox.password:
                key = f"PM_MAIL_PASSWORD_{envmod.key_for(m.id)}="
                body = body.replace(key + "\n", key + m.mailbox.password + "\n")
        if cfg.mailbox.password:
            body = body.replace("PM_MAIL_PASSWORD=\n", f"PM_MAIL_PASSWORD={cfg.mailbox.password}\n")

        envmod.ENV_FILE.write_text(body, encoding="utf-8")
        envmod.ENV_FILE.chmod(0o600)

        _say(f"{GREEN}Wrote{RESET} {envmod.ENV_FILE} (chmod 600, gitignored)")
        _say(f"{DIM}This is a seed file for deploying elsewhere. config.json remains the "
             f"store and still holds these values — both are gitignored.{RESET}")
        return 0
    return 1


def cmd_serve(args) -> int:
    from .server import run

    run(host=args.host, port=args.port, reload=getattr(args, 'reload', False))
    return 0


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm", description="LameStreet — one dashboard for a family's holdings")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config and data folders").set_defaults(fn=cmd_init)

    m = sub.add_parser("member", help="manage family members").add_subparsers(dest="action", required=True)
    ma = m.add_parser("add"); ma.add_argument("--name", required=True)
    ma.add_argument("--password", help="what opens their statement PDFs (usually the PAN)")
    ma.add_argument("--broker", action="append", help="repeatable: groww, zerodha, dhan, …")
    ma.add_argument("--email", action="append"); ma.add_argument("--id")
    ma.set_defaults(fn=cmd_member, action="add")
    m.add_parser("list").set_defaults(fn=cmd_member, action="list")
    mr = m.add_parser("remove"); mr.add_argument("id"); mr.set_defaults(fn=cmd_member, action="remove")

    mb = sub.add_parser("mailbox", help="configure the inbox to read").add_subparsers(dest="action", required=True)
    ms = mb.add_parser("set")
    ms.add_argument("--host", default=None); ms.add_argument("--port", type=int, default=None)
    ms.add_argument("--user"); ms.add_argument("--password", default=None)
    ms.add_argument("--folder", default=None); ms.add_argument("--member", default=None)
    ms.set_defaults(fn=cmd_mailbox, action="set")
    mb.add_parser("test").set_defaults(fn=cmd_mailbox, action="test")

    s = sub.add_parser("sync", help="read new mail, log trades, rebuild")
    s.add_argument("--full", action="store_true", help="ignore last-run marker and read everything")
    s.add_argument("--no-prices", action="store_true")
    s.set_defaults(fn=cmd_sync)

    st = sub.add_parser("statement",
                        help="set holdings from a statement PDF, then sync the mail after it")
    st.add_argument("file", help="the holdings/demat statement PDF")
    st.add_argument("--member", help="who it belongs to; by default the statement "
                                     "password decides, as it does for mail")
    st.add_argument("--no-sync", action="store_true",
                    help="record the statement only — don't rewind the mail cursor "
                         "or read anything after it")
    st.add_argument("--no-prices", action="store_true")
    st.set_defaults(fn=cmd_statement)

    rp = sub.add_parser("reparse", help="re-run parsers over archived mail")
    rp.add_argument("--all", action="store_true",
                    help="re-read every archived document, even ones this parser "
                         "version has already read")
    rp.add_argument("--rebuild", action="store_true",
                    help="also retire events from earlier parses (keeps manual entries; "
                         "use when a parser was reading a document wrongly)")
    rp.add_argument("--snapshots", action="store_true",
                    help="retire and re-derive holdings snapshots only, leaving trades "
                         "alone (use after a fix to the holdings parser)")
    rp.set_defaults(fn=cmd_reparse)
    sub.add_parser("prices", help="refresh prices only").set_defaults(fn=cmd_prices)
    sub.add_parser("build", help="rebuild dashboard.json").set_defaults(fn=cmd_build)
    sub.add_parser("refresh", help="prices + rebuild").set_defaults(fn=cmd_refresh)
    sub.add_parser("status", help="summary in the terminal").set_defaults(fn=cmd_status)

    ic = sub.add_parser("import-csv", help="bootstrap holdings from a broker CSV export")
    ic.add_argument("--member", required=True); ic.add_argument("--file", required=True)
    ic.add_argument("--date"); ic.set_defaults(fn=cmd_import_csv)

    at = sub.add_parser("add-trade", help="log a trade by hand")
    at.add_argument("--member", required=True); at.add_argument("--symbol", required=True)
    at.add_argument("--side", choices=["buy", "sell"], required=True)
    at.add_argument("--qty", type=float, required=True); at.add_argument("--price", type=float, required=True)
    at.add_argument("--charges", type=float, default=0.0); at.add_argument("--date")
    at.set_defaults(fn=cmd_add_trade)

    ins = sub.add_parser("instruments", help="ISIN → symbol mapping").add_subparsers(dest="action", required=True)
    ins.add_parser("refresh").set_defaults(fn=cmd_instruments, action="refresh")
    ins.add_parser("list").set_defaults(fn=cmd_instruments, action="list")
    im = ins.add_parser("map"); im.add_argument("key"); im.add_argument("symbol")
    im.add_argument("--yahoo"); im.set_defaults(fn=cmd_instruments, action="map")

    insp = sub.add_parser("inspect", help="show what the parser sees in a PDF")
    insp.add_argument("file"); insp.add_argument("--password")
    insp.add_argument("--text", action="store_true", help="dump the raw extracted text")
    insp.add_argument("--redact", action="store_true",
                      help="dump the layout with all values masked — safe to share")
    insp.set_defaults(fn=cmd_inspect)

    ex = sub.add_parser("export", help="write a static copy you can host anywhere")
    ex.add_argument("--out", default="dist"); ex.set_defaults(fn=cmd_export)

    sc = sub.add_parser("schedule", help="set up the daily automatic sync (macOS)")
    sc.add_argument("--time", default="20:00", help="24h local time, e.g. 20:00")
    sc.set_defaults(fn=cmd_schedule)

    sec = sub.add_parser("secrets", help="manage credentials in .env").add_subparsers(
        dest="action", required=True)
    sec.add_parser("check", help="show where each secret is read from").set_defaults(
        fn=cmd_secrets, action="check")
    sec.add_parser("adopt", help="persist .env-supplied credentials into config.json").set_defaults(
        fn=cmd_secrets, action="adopt")
    se = sec.add_parser("export", help="move secrets from config.json into .env")
    se.add_argument("--force", action="store_true", help="overwrite an existing .env")
    se.set_defaults(fn=cmd_secrets, action="export")

    # Defaults come from .env so a deployment sets them once, in one place.
    from . import env as _env
    _env.load()
    default_host = os.environ.get("PM_HOST", "127.0.0.1")
    # PORT is what hosting platforms inject and expect to be honoured; PM_PORT
    # still wins so a local .env keeps working.
    default_port = int(os.environ.get("PM_PORT") or os.environ.get("PORT") or 3002)

    sv = sub.add_parser("serve", help="run the dashboard locally")
    sv.add_argument("--host", default=default_host)
    sv.add_argument("--port", type=int, default=default_port)
    sv.add_argument("--reload", action="store_true",
                    help="restart when Python under pm/ changes (development; "
                         "turns off scheduled syncs)")
    sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    paths.ensure_dirs()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
