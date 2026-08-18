"""End-to-end test over generated statements.

Runs against a throwaway PM_ROOT so it can never touch real data. Generates
password-protected PDFs shaped like the real documents, pushes them through the
same code path a mail sync uses, and checks the numbers that come out.

Run:  .venv/bin/python tests/test_pipeline.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORKDIR = tempfile.mkdtemp(prefix="pm-test-")
os.environ["PM_ROOT"] = WORKDIR

from pm import config as cfgmod  # noqa: E402
from pm import events as ev  # noqa: E402
from pm import ingest, manual, paths  # noqa: E402
from pm.config import Member  # noqa: E402
from pm.mailbox import Attachment  # noqa: E402
from pm.replay import replay  # noqa: E402

RAVI_PAN = "ABCDE1234F"
PRIYA_PAN = "PQRSX6789K"

passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32mpass\033[0m  {label}")
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"\n        {detail}" if detail else ""))


# ------------------------------------------------------------------ fixtures

def make_pdf(lines: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(orientation="L", format="A4")
    pdf.add_page()
    pdf.set_font("Courier", size=7)
    for line in lines:
        pdf.cell(0, 4, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def encrypt(data: bytes, password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader, writer = PdfReader(io.BytesIO(data)), PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


CONTRACT_NOTE = [
    "GROWW INVEST TECH PRIVATE LIMITED",
    "CONTRACT NOTE CUM TAX INVOICE",
    "Trade Date: 07-08-2026        Settlement No: 2026153",
    f"Client PAN: {RAVI_PAN}",
    "",
    "OrderNo OrderTime TradeNo TradeTime Security Description Buy/Sell Quantity WAP Brokerage NetRate NetTotal",
    "1000123456 09:20:11 55123301 09:20:12 RELIANCE INDUSTRIES LTD INE002A01018 B 10 1423.55 0.10 1423.65 14236.50",
    "1000123457 10:05:40 55123302 10:05:41 INFOSYS LIMITED INE009A01021 S 5 1180.25 0.10 1180.15 5901.25",
    "",
    "Net Total 8335.25",
]

HOLDING_STATEMENT = [
    "CENTRAL DEPOSITORY SERVICES (INDIA) LIMITED",
    "TRANSACTION CUM HOLDING STATEMENT",
    "Statement for the period 01-08-2026 to 31-08-2026",
    "Holdings as on 31-08-2026",
    f"PAN: {PRIYA_PAN}",
    "",
    "ISIN Security Name Current Balance Value",
    "INE002A01018 RELIANCE INDUSTRIES LTD 12 16800.00",
    "INE154A01025 ITC LTD 150 61890.00",
    "INE155A01022 TATA MOTORS LTD 66 22902.00",
]

FUNDS_STATEMENT = [
    "GROWW INVEST TECH PRIVATE LIMITED",
    "STATEMENT OF ACCOUNTS OF FUNDS",
    "Period: 01-08-2026 to 31-08-2026",
    f"Client PAN: {RAVI_PAN}",
    "",
    "Date Voucher Description Debit Credit Balance",
    "05-08-2026 R0012 Payment received 0.00 50000.00 50000.00",
    "07-08-2026 B0451 Purchase of securities 14236.50 0.00 35763.50",
]


# --------------------------------------------------------------------- setup

def setup_members() -> list[Member]:
    paths.ensure_dirs()
    cfg = cfgmod.Config(members=[
        Member(id="ravi", name="Ravi", doc_passwords=[RAVI_PAN], emails=["ravi@example.com"]),
        Member(id="priya", name="Priya", doc_passwords=[PRIYA_PAN], emails=["priya@example.com"]),
    ])
    cfg.save()
    return cfg.members


def ingest_pdf(data: bytes, subject: str, members, when: date,
               filename="statement.pdf", sender="noreply@groww.in"):
    """Push a document through the same entry point a mail sync uses.

    `sender` defaults to a real broker address on purpose. It is what makes these
    tests cover the wiring — sender to broker to declared layout to parser — and
    not merely the parsers in isolation. A parser can be perfectly correct and the
    pipeline still return nothing if the layout it is handed has the wrong shape.
    """
    att = Attachment(filename, "application/pdf", data)
    report, produced = ingest.process_attachment(
        att, subject=subject, mail_date=when, members=members,
        recipients=[], doc_ref=f"test-{filename}-{when}", sender=sender,
    )
    return report, produced


# --------------------------------------------------------------------- tests

def real_document_layouts() -> None:
    """Regressions taken from the actual documents Groww sends.

    These table shapes were learned the hard way against live statements, and
    each one previously produced wrong numbers rather than an obvious failure —
    the worst kind of bug for a ledger. They are asserted directly against the
    parsers so no PDF generation is involved.
    """
    from datetime import date as _date

    from pm.parsers import contract_note, holdings

    print("\n\033[1mReal Groww document layouts\033[0m")

    # Contract note: Buy and Sell are side-by-side column blocks under a two-row
    # header, with no buy/sell indicator anywhere.
    cn_tables = [[
        ["Security Description", "", "Buy", "", "", "", "", "Sell", "", "", "", "",
         "Net Obligation per ISIN (before levies)", ""],
        ["ISIN", "Security Name/ Symbol", "Quantity", "WAP per Share (Rs)",
         "Brokerage per Share (Rs)", "WAP per Share after brokerage (Rs)",
         "Total Value after brokerage (Rs)", "Quantity", "WAP per Share (Rs)",
         "Brokerage per Share (Rs)", "WAP per Share after brokerage (Rs)",
         "Total Value after brokerage (Rs)", "Net Quantity", "Net Amount (Rs)"],
        ["INE176B01034", "Havells India", "", "", "", "", "",
         "10", "462.00", "0.05", "461.95", "4619.50", "-10", "4619.50"],
        ["INE484J01027", "Sunteck Realty Limited", "25", "406.65", "0.04", "406.69",
         "10167.25", "", "", "", "", "", "25", "10167.25"],
    ]]
    result = contract_note.parse("CONTRACT NOTE\nTrade Date 13-07-2026", cn_tables, _date(2026, 7, 13))
    by_symbol = {r["side"]: r for r in result.rows}
    check("side-by-side Buy/Sell blocks read correctly",
          len(result.rows) == 2 and by_symbol.get("sell", {}).get("qty") == 10
          and by_symbol.get("buy", {}).get("qty") == 25,
          f"got {result.rows}")
    check("ISIN captured from the contract note",
          all(r["isin"] for r in result.rows), f"got {[r['isin'] for r in result.rows]}")
    check("sell price taken from the sell block, not the buy block",
          by_symbol.get("sell", {}).get("price") == 462.00,
          f"got {by_symbol.get('sell', {}).get('price')}")

    # Transaction-cum-holding statement: a transaction table first, then the real
    # holdings table. Quantity lives in "Current Bal", abbreviated.
    tchs_text = (
        "STATEMENT OF TRANSACTION\nFrom 01-07-2026 To 31-07-2026\n"
        "ISIN: INE176B01034 ISIN Name: HAVELLS INDIA-EQ\n"
        "13-07-2026 DEBIT On Market 10 10.00\n"
        "HOLDINGS BALANCE\nAs on 31-07-2026\n"
    )
    tchs_tables = [
        [["Date", "Ref No", "Trans Description", "Setl No", "Buy/Cr", "Sell/Dr", "Balance"],
         ["13-07-2026", "12345", "DEBIT On Market", "2026139", "", "10", "10"]],
        [["ISIN Code", "Company Name", "Current Bal", "Free Bal", "Pldg Bal", "DEMAT",
          "Safe Keep Bal", "REMAT", "Earmark Bal", "LockIn", "Rate", "Value"],
         ["INE176B01034", "HAVELLS INDIA-EQ", "10.00", "10.00", "0.00", "0.00",
          "0.00", "0.00", "0.00", "0.00", "460.25", "4602.50"],
         ["INE508G01029", "TIME TECHNOPLAST-EQ1", "20.00", "20.00", "0.00", "0.00",
          "0.00", "0.00", "0.00", "0.00", "206.89", "4137.80"]],
    ]
    result = holdings.parse(tchs_text, tchs_tables, _date(2026, 8, 3))
    quantities = {r["symbol"]: r["qty"] for r in result.rows}
    check("holdings read from the holdings table, not the transaction table",
          len(result.rows) == 2 and 10.0 in quantities.values() and 20.0 in quantities.values(),
          f"got {result.rows}")
    check("as-of date is the statement's, not the email's",
          result.as_of == _date(2026, 7, 31), f"got {result.as_of}")

    # A weekly settlement/payout advice lists ISINs and quantities too, but they
    # are movements. Read as a snapshot it would zero every other position.
    ros_text = (
        "Statement of Accounts of Securities for the period from '2026-07-27' To '2026-08-02'\n"
        "Transaction Date Execution Date Settlement No. ISIN Code Scrip Name "
        "Quantity Delivered (Qty.) Quantity Received (Qty.) Balance (Qty.) Purpose\n"
        "2026-07-30 2026-07-31 NSE Clearing EQ INE484J01027 SUNTECK REALTY LIMITED "
        "0 25 25 MKT PayOut\n"
        "Pending Obligations as on '2026-08-02'\n"
    )
    # Zerodha's holdings statement rules only the header, leaving the body as
    # aligned text. The header still tells us the column layout, and rows must be
    # read by counting in from the END — several security names contain digits.
    zerodha_header_only = [[
        ["ISIN Code", "Company Name", "Curr. Bal", "Free Bal", "Pldg. Bal", "Earmark Bal",
         "Demat", "Remat", "Lockin", "Rate", "Value"]
    ]]
    zerodha_text = (
        "Holding Statement\n"
        "ISIN Code Company Name Curr. Bal Free Bal Pldg. Bal Earmark Bal Demat Remat Lockin Rate Value\n"
        "INE855B01025 RAIN INDUSTRIES-2/- 4000.000 4000.000 0.000 0.000 0.000 0.000 0.000 148.35 593400.00\n"
        "INE522D01027 MANAPPURAM FIN RE2/- 350.000 350.000 0.000 0.000 0.000 0.000 0.000 265.10 92785.00\n"
        "As on 30-06-2026\n"
    )
    result = holdings.parse(zerodha_text, zerodha_header_only, _date(2026, 7, 1))
    got = {r["symbol"]: r["qty"] for r in result.rows}
    check("header-only table still yields the column layout",
          len(result.rows) == 2, f"got {result.rows}")
    check("digits inside a security name don't shift the quantity",
          got.get("RAININDUSTRIES2") == 4000.0 and got.get("MANAPPURAMFINRE2") == 350.0,
          f"got {got}")
    check("field labels stripped from the security name",
          all("symbol" not in r["name"].lower() for r in result.rows),
          f"got {[r['name'] for r in result.rows]}")

    # value / rate is an independent statement of the quantity, so a misread
    # column gets caught by the row's own arithmetic.
    shifted = holdings.parse(
        "Holding Statement\nAs on 30-06-2026\n"
        "INE522D01027 SOME NAME 14578057.000 265.10 92785.00\n", [], _date(2026, 7, 1))
    check("a value read as a quantity is corrected by value/rate",
          shifted.rows and shifted.rows[0]["qty"] == 350.0,
          f"got {[(r['symbol'], r['qty']) for r in shifted.rows]}")

    # Named document types that list securities but never state a position.
    for label, subject, body in [
        ("retention report", "Retention Report",
         "RETENTION REPORT\nClosing Balance\nINE522D01027 MANAPPURAM 80 265.10 21208.00"),
        ("daily margin statement", "Daily Equity Margin Statement",
         "DAILY MARGIN STATEMENT\nHoldings as on 05-08-2026\nINE522D01027 MANAPPURAM 80"),
    ]:
        check(f"{label} disqualified as a snapshot",
              holdings.looks_like_holdings(subject, "x.pdf", body) is False)

    # A broker declares which layouts its documents may use, in order. Declaring
    # only the SEBI layout must mean exactly that — never a quiet fall-through to
    # text scanning, which on a rich layout yields plausible, wrong numbers.
    strict = contract_note.parse("CONTRACT NOTE\nTrade Date 13-07-2026\n"
                                 "1001 09:20 INE176B01034 HAVELLS INDIA B 10 462.00 4620.00\n",
                                 [], _date(2026, 7, 13), layout="sebi-split-table")
    check("a broker that declares one layout does not fall through to another",
          not strict.rows, f"got {strict.rows}")

    permissive = contract_note.parse("CONTRACT NOTE\nTrade Date 13-07-2026\n"
                                     "1001 09:20 INE176B01034 HAVELLS INDIA B 10 462.00 4620.00\n",
                                     [], _date(2026, 7, 13),
                                     layout=["sebi-split-table", "text"])
    check("declaring an ordered list falls through only to what is declared",
          len(permissive.rows) == 1 and permissive.rows[0]["qty"] == 10,
          f"got {permissive.rows}")

    # Dhan's holdings table puts "Free Bal" before "Tot Qty". Reading the first
    # balance-shaped column would under-report every pledged holding.
    dhan_tables = [[
        ["Sr", "ISIN Code", "Company Name", "Free Bal", "Pldg Bal", "Earmark",
         "Demat", "Remat", "Lock In", "Tot Qty", "Rate", "Value"],
        ["1", "INE484J01027", "SUNTECK REALTY LTD", "800.00", "3000.00", "0.00",
         "0.00", "0.00", "0.00", "3800.00", "298.55", "1134490.00"],
    ]]
    result = holdings.parse("Holdings as on 30-06-2026", dhan_tables, _date(2026, 7, 1),
                            layout="holdings-balance-table")
    check("total quantity preferred over free balance",
          result.rows and result.rows[0]["qty"] == 3800.0,
          f"got {[(r['symbol'], r['qty']) for r in result.rows]}")

    # The other CDSL layout Dhan sends states no total at all, so the position is
    # the sum of the parts. Reading "Free Bal" alone under-reports every pledged
    # holding — and drops a fully-pledged one outright, since its free balance is
    # 0 and a zero quantity looks like a closed position.
    no_total_tables = [[
        ["Sr.", "ISIN Code", "Company Name", "Free Bal", "Pldg Bal",
         "Demat", "Remat", "Lockin", "Rate", "Value"],
        ["19", "INE900Z01010", "PARTLY PLEDGED LTD-EQ", "800.00", "3000.000", "", "", "",
         "298.55", "1134490.00"],
        ["20", "INE901Z01018", "FULLY PLEDGED LTD-EQ", "0.00", "1250.000", "", "", "",
         "260.25", "325312.50"],
    ]]
    result = holdings.parse("Holdings as on 30-04-2026", no_total_tables, _date(2026, 5, 1),
                            layout="holdings-balance-table")
    got = {r["symbol"]: r["qty"] for r in result.rows}
    check("with no total column, free + pledged are summed",
          got.get("PARTLYPLEDGEDEQ") == 3800.0, f"got {got}")
    check("a fully-pledged holding is not dropped as a zero balance",
          got.get("FULLYPLEDGEDEQ") == 1250.0, f"got {got}")

    # A holdings table that runs past the bottom of a page is extracted as one
    # table per page, each repeating the header. Reading only the biggest page
    # turns every position on the others into a phantom sale.
    page_header = ["Sr", "ISIN Code", "Company Name", "Free Bal", "Pldg Bal", "Earmark",
                   "Demat", "Remat", "Lock In", "Tot Qty", "Rate", "Value"]

    def _page(rows):
        return [page_header] + [
            [str(sr), isin, name, str(qty), "0", "0.0", "0", "0", "0", str(qty),
             "100.00", str(qty * 100)]
            for sr, isin, name, qty in rows
        ]

    paged = [
        _page([(1, "INE900Z01010", "PAGE ONE LTD-EQ", 800)]),
        _page([(2, "INE901Z01018", "PAGE TWO LTD-EQ", 640),
               (3, "INE902Z01016", "PAGE TWO OTHER-2/-", 120)]),
        _page([(4, "INE903Z01014", "PAGE THREE LTD-EQ1", 55)]),
    ]
    result = holdings.parse("Holdings as on 30-06-2026", paged, _date(2026, 7, 1),
                            layout="holdings-balance-table")
    got = {r["symbol"]: r["qty"] for r in result.rows}
    check("every page of a holdings table is read, not just the biggest",
          len(result.rows) == 4 and got.get("PAGETWOEQ") == 640.0
          and got.get("PAGEONEEQ") == 800.0 and got.get("PAGETHREEEQ1") == 55.0,
          f"got {got}")

    # Merging pages must key off the header, so an unrelated table that happens to
    # carry the same required columns is still left alone.
    unrelated = paged[:1] + [[
        ["ISIN", "Scrip", "Quantity", "Purpose"],
        ["INE904Z01012", "SOMETHING ELSE", "99", "Collateral"],
    ]]
    result = holdings.parse("Holdings as on 30-06-2026", unrelated, _date(2026, 7, 1),
                            layout="holdings-balance-table")
    check("a differently-headed table is not merged in as another page",
          len(result.rows) == 1 and result.rows[0]["qty"] == 800.0,
          f"got {[(r['symbol'], r['qty']) for r in result.rows]}")

    # A contract note's trade table breaks across pages too, and the continuation
    # sometimes carries no header at all — the rows just resume. Reading only the
    # first page loses trades, which is silent: the cost basis simply comes out low.
    cn_header = ["Order No.", "Order Time", "Trade No.", "Trade Time",
                 "Security / Contract Description", "Buy(B) / Sell(S)", "Exchange",
                 "Quantity", "Gross Rate/ Trade Price per Unit(₹)²", "Brokerage per Unit(₹)",
                 "Net Rate per Unit(₹)", "Closing Rate per Unit (₹)",
                 "Net Total (Before Levies)(₹)", "Remarks"]

    def _fill(order, trade, qty, price):
        return [order, "10:00:00", trade, "10:00:01", "PAGED CO-EQ/INE900Z01010", "B", "NSE",
                str(qty), f"{price:.2f}", "", f"{price:.2f}", "", f"({qty * price:.2f})", ""]

    paged_note = [
        [cn_header, _fill("A1", "T1", 10, 400.0)],
        # page two repeats the header — but without the footnote marker on it
        [[c.replace("²", "") for c in cn_header], _fill("A2", "T2", 20, 401.0)],
        # page three drops the header entirely
        [_fill("A3", "T3", 30, 402.0)],
    ]
    result = contract_note.parse("CONTRACT NOTE\nTrade Date 13-07-2026", paged_note,
                                 _date(2026, 7, 13), layout="buy-sell-column")
    by_trade = {r["trade_no"]: r for r in result.rows}
    check("a trade table repeating its header on the next page is read whole",
          "T2" in by_trade and by_trade["T2"]["qty"] == 20, f"got {sorted(by_trade)}")
    check("a footnote marker on the first page's header doesn't split the table",
          len(result.rows) >= 2, f"got {[r['trade_no'] for r in result.rows]}")
    check("a continuation page with no header at all is still read",
          "T3" in by_trade and by_trade["T3"]["qty"] == 30, f"got {sorted(by_trade)}")

    # ...but only when it really is a continuation. An unrelated block of the same
    # width must not be swept in as trades.
    unrelated_note = paged_note[:1] + [[
        ["Charges", "0.00", "GST", "0.00", "Stamp", "0.00", "SEBI", "0.00",
         "", "", "", "", "", ""]]]
    result = contract_note.parse("CONTRACT NOTE\nTrade Date 13-07-2026", unrelated_note,
                                 _date(2026, 7, 13), layout="buy-sell-column")
    check("a same-width block that isn't trades is left alone",
          len(result.rows) == 1, f"got {[(r['trade_no'], r['qty']) for r in result.rows]}")

    check("settlement/payout advice is not mistaken for a holdings snapshot",
          holdings.looks_like_holdings("Statement of Accounts of Securities", "ROS_123", ros_text)
          is False)
    check("funds ledger still ignored",
          holdings.looks_like_holdings("Report: Statement of Accounts of Funds", "x",
                                       "STATEMENT OF ACCOUNTS OF FUNDS\nPurchase of securities") is False)


def uploaded_statement_sets_holdings_and_rewinds() -> None:
    """Handing a statement over directly must do what finding one in mail does.

    The recovery path when a month never arrives: the document sets the position
    on its own date, and the mail cursor rewinds to that date so the next sync
    reads every contract note since and applies it on top.
    """
    from pm.config import Mailbox

    print("\n\033[1mStatement upload\033[0m")

    cfg = cfgmod.load()
    priya = cfg.member("priya")
    priya.mailbox = Mailbox(user="priya@example.com", password="secret")
    cfg.save()
    cfg = cfgmod.load()

    stmt = encrypt(make_pdf(HOLDING_STATEMENT), PRIYA_PAN)
    result = ingest.ingest_statement(cfg, stmt, filename="aug.pdf")
    check("owner identified from the statement password alone",
          result.get("ok") and result["member"] == "priya",
          f"got {result}")
    check("as-of date read from the document, not from today",
          result["as_of"] == "2026-08-31", f"got {result.get('as_of')}")
    check("every position recorded", result["positions"] == 3, f"got {result.get('positions')}")

    key = "priya@example.com@imap.gmail.com/INBOX"
    check("the member's inbox was rewound to the statement date",
          ingest.last_fetch_for(key).date() == date(2026, 8, 31),
          f"got {ingest.last_fetch_for(key)}")
    state = ingest.load_sync_state()["mailboxes"][key]
    check("the resume-by-UID cursor was cleared, so the rewind takes effect",
          state.get("last_uid") is None, f"got {state.get('last_uid')}")

    # Uploading the same file twice is the same document, not a second snapshot.
    again = ingest.ingest_statement(cfg, stmt, filename="aug.pdf")
    check("re-uploading the same statement is recognised, not duplicated",
          again.get("ok") and again["duplicate"] and not again["new"], f"got {again}")

    # Archived under the upload marker, so a re-parse keeps it even though it has
    # no broker sender to match on.
    from pm.mailbox import load_archived
    archived = [m for m, _ in load_archived(["noreply@groww.in"]) if m.get("upload")]
    check("an uploaded statement survives the archive's sender filter",
          len(archived) == 1, f"got {len(archived)} upload(s) in the archive")

    # Wrong document type must be refused rather than read as a snapshot, since a
    # snapshot supersedes the log.
    note = encrypt(make_pdf(CONTRACT_NOTE), RAVI_PAN)
    refused = ingest.ingest_statement(cfg, note, filename="note.pdf")
    check("a contract note is refused, not mistaken for a holdings statement",
          not refused.get("ok") and "holdings statement" in refused.get("detail", ""),
          f"got {refused}")

    # Naming the wrong person must fail rather than attribute the portfolio to them.
    misattributed = ingest.ingest_statement(cfg, stmt, filename="aug.pdf", member_id="ravi")
    check("a statement is never attributed to a member whose password it isn't",
          not misattributed.get("ok"), f"got {misattributed}")

    check("recording without a sync leaves the cursor alone",
          ingest.ingest_statement(cfg, stmt, filename="aug.pdf", rewind=False)["rewound"] == [],
          "cursor was rewound despite rewind=False")


def an_export_outranks_the_statements_that_follow_it() -> None:
    """Once an export states a book, the mail no longer overrides it.

    Depository statements carry quantity and nothing else, so they cannot say what
    anything cost. An uploaded export can. So the export anchors the book and the
    trades after it carry it forward; statements arriving later are checked against
    it and reported, never applied.
    """
    from datetime import datetime as _datetime

    from pm.events import IST

    print("\n\033[1mExport takes precedence over statements\033[0m")

    def snap(day, source, rows, has_cost=False):
        return ev.make_snapshot(
            member="nina", ts=_datetime(2026, day, 15, 23, 59, tzinfo=IST),
            holdings=[{"isin": i, "symbol": s, "name": s, "qty": q, "avg": a}
                      for i, s, q, a in rows],
            source=source, has_cost=has_cost)

    def buy(day, isin, sym, qty, price):
        return ev.make_trade(member="nina", ts=_datetime(2026, day, 20, 15, 30, tzinfo=IST),
                             side="buy", isin=isin, symbol=sym, qty=qty, price=price,
                             source=ev.SRC_CONTRACT_NOTE)

    A, B = "INE900Z01010", "INE901Z01018"

    # Export in March, then a statement in April that disagrees, then a trade.
    log = [
        snap(3, ev.SRC_CSV, [(A, "AAA", 100, 50.0), (B, "BBB", 200, 10.0)], has_cost=True),
        snap(4, ev.SRC_HOLDINGS_STATEMENT, [(A, "AAA", 900, None)]),
        buy(5, A, "AAA", 50, 60.0),
    ]
    held = {p["symbol"]: p for p in replay(log)["holdings"]["nina"]}
    check("a statement after an export does not overwrite its quantities",
          held["AAA"]["qty"] == 150, f"got {held['AAA']['qty']}")
    check("a holding the later statement omits is not retired",
          held.get("BBB", {}).get("qty") == 200, f"got {held.get('BBB')}")
    check("cost from the export survives, and the trade after it is applied",
          abs(held["AAA"]["cost"] - (100 * 50.0 + 50 * 60.0)) < 0.01,
          f"got {held['AAA']['cost']}")

    warned = [w for w in replay(log)["warnings"] if w["kind"] == "statement_disagrees"]
    check("the statement's disagreement is reported rather than hidden",
          len(warned) == 1 and "AAA" in warned[0]["detail"], f"got {warned}")

    # With no export, the statements are the only anchor and still apply.
    mail_only = [
        snap(3, ev.SRC_HOLDINGS_STATEMENT, [(A, "AAA", 100, 50.0)], has_cost=True),
        snap(4, ev.SRC_HOLDINGS_STATEMENT, [(A, "AAA", 900, None)]),
    ]
    held = {p["symbol"]: p for p in replay(mail_only)["holdings"]["nina"]}
    check("without an export, a later statement still corrects the quantity",
          held["AAA"]["qty"] == 900, f"got {held['AAA']['qty']}")

    # A newer export replaces an older one.
    two = [
        snap(3, ev.SRC_CSV, [(A, "AAA", 100, 50.0)], has_cost=True),
        snap(6, ev.SRC_CSV, [(A, "AAA", 400, 70.0)], has_cost=True),
    ]
    held = {p["symbol"]: p for p in replay(two)["holdings"]["nina"]}
    check("the latest export is the one in force",
          held["AAA"]["qty"] == 400 and abs(held["AAA"]["avg"] - 70.0) < 0.01,
          f"got qty={held['AAA']['qty']} avg={held['AAA']['avg']}")


def csv_rows_land_on_the_right_positions() -> None:
    """A broker export names companies; positions are keyed by ISIN.

    Without resolving one to the other, uploading an export does not merely fail
    to update the holdings — it creates a second position for every company
    already held, and the originals look like holdings the export omitted.
    """
    print("\n\033[1mCSV holdings sync\033[0m")

    held = [
        {"isin": "INE900Z01010", "symbol": "ORBITAL", "name": "Orbital Realty Limited"},
        {"isin": "INE901Z01018", "symbol": "ORBITEL", "name": "Orbitel Infra Solutions"},
    ]
    rows = [
        {"isin": None, "symbol": "ORBITAL REALTY", "name": "Orbital Realty", "qty": 100, "avg": 400.0},
        {"isin": None, "symbol": "ORBITEL INFRA SOLUTIONS", "name": "Orbitel Infra Solutions",
         "qty": 50, "avg": 33.0},
        {"isin": None, "symbol": "MYSTERY CORP", "name": "Mystery Corp", "qty": 5, "avg": 10.0},
    ]
    out, notes = manual.match_to_holdings([dict(r) for r in rows], held)
    by_name = {r["name"]: r for r in out}

    check("a company already held is matched to its ISIN, not duplicated",
          by_name["Orbital Realty"]["isin"] == "INE900Z01010",
          f"got {by_name['Orbital Realty']['isin']}")
    check("an unlisted holding is matched from the portfolio, not NSE's list",
          by_name["Orbitel Infra Solutions"]["isin"] == "INE901Z01018",
          f"got {by_name['Orbitel Infra Solutions']['isin']}")
    check("two similarly-named companies are not confused for each other",
          by_name["Orbital Realty"]["isin"] != by_name["Orbitel Infra Solutions"]["isin"])
    check("a row that matches nothing is reported rather than silently guessed",
          by_name["Mystery Corp"]["isin"] is None
          and any("Mystery Corp" in n for n in notes), f"notes: {notes}")

    # Re-uploading the same export must update in place, not accumulate.
    again, _ = manual.match_to_holdings([dict(r) for r in rows], held)
    check("re-running the match is stable",
          [r["isin"] for r in again] == [r["isin"] for r in out],
          f"{[r['isin'] for r in again]} vs {[r['isin'] for r in out]}")


def incomplete_statements_do_not_erase_positions() -> None:
    """A snapshot that could not be read whole must not retire the rest of a book.

    A snapshot supersedes the log, so a parser that loses a page produces a
    document indistinguishable from a liquidation: every position it failed to read
    gets its quantity *and* its cost basis zeroed. If a later statement lists the
    holding again there is no average left to price it with, so it comes back
    showing shares worth lakhs against zero invested — which is exactly what a
    dropped page did to this portfolio before these two guards existed.
    """
    from datetime import datetime as _datetime

    from pm.events import IST

    print("\n\033[1mIncomplete statements\033[0m")

    def snapshot(day: int, symbols: dict[str, float]) -> dict:
        return ev.make_snapshot(
            member="asha",
            ts=_datetime(2026, day, 28, 23, 59, tzinfo=IST),
            holdings=[{"isin": f"INE{code:09d}", "symbol": sym, "name": sym, "qty": qty}
                      for sym, (code, qty) in symbols.items()],
            source=ev.SRC_HOLDINGS_STATEMENT, has_cost=False,
        )

    def trade(month: int, sym: str, code: int, qty: float, price: float,
              day: int = 10) -> dict:
        return ev.make_trade(
            member="asha", ts=_datetime(2026, month, day, 15, 30, tzinfo=IST), side="buy",
            isin=f"INE{code:09d}", symbol=sym, qty=qty, price=price,
            source=ev.SRC_CONTRACT_NOTE,
        )

    book = {"AAA": (1, 100.0), "BBB": (2, 200.0), "CCC": (3, 300.0),
            "DDD": (4, 400.0), "EEE": (5, 500.0), "FFF": (6, 600.0)}
    buys = [trade(1, sym, code, qty, 50.0) for sym, (code, qty) in book.items()]

    # A statement that lost a page: one position of six, where the other five were
    # bought and never sold.
    partial = replay(buys + [snapshot(2, {"AAA": book["AAA"]})])
    held = {p["symbol"]: p for p in partial["holdings"]["asha"]}
    check("an incomplete statement does not retire the positions it omits",
          len(held) == 6, f"got {sorted(held)}")
    check("the omitted positions keep their cost basis",
          held.get("FFF", {}).get("cost") == 600.0 * 50.0,
          f"got {held.get('FFF', {}).get('cost')}")
    check("reading a statement as incomplete is reported, not silent",
          any(w["kind"] == "partial_snapshot" for w in partial["warnings"]),
          f"warnings: {[w['kind'] for w in partial['warnings']]}")

    # Bought on the statement's own date: T+1 settlement means the shares are not
    # in the demat account yet, so the statement omitting them is not a sale.
    # The trade and the statement share a calendar date: 28 March, 15:30 then 23:59.
    same_day = replay(buys + [
        snapshot(2, book),
        trade(3, "GGG", 7, 50.0, 20.0, day=28),
        snapshot(3, book),
    ])
    held = {p["symbol"]: p for p in same_day["holdings"]["asha"]}
    check("a purchase made on the statement's own date is not retired by it",
          held.get("GGG", {}).get("qty") == 50.0, f"got {held.get('GGG')}")
    check("and it keeps the cost it was bought at",
          held.get("GGG", {}).get("cost") == 50.0 * 20.0,
          f"got {held.get('GGG', {}).get('cost')}")

    # A real sale still gets through: below the threshold, an omission means sold.
    sold = replay(buys + [snapshot(2, {k: v for k, v in book.items() if k != "FFF"})])
    held = {p["symbol"]: p for p in sold["holdings"]["asha"]}
    check("a plausible number of omissions is still read as a sale",
          "FFF" not in held and len(held) == 5, f"got {sorted(held)}")

    # And when a position genuinely does go missing and come back, the average it
    # was retired at is what prices it — not zero.
    round_trip = replay(buys + [
        snapshot(2, {k: v for k, v in book.items() if k != "FFF"}),
        snapshot(3, book),
    ])
    held = {p["symbol"]: p for p in round_trip["holdings"]["asha"]}
    check("a position that reappears is priced at what it cost before",
          held.get("FFF", {}).get("cost") == 600.0 * 50.0,
          f"got {held.get('FFF', {}).get('cost')}")
    check("a reappearing position is not reported as cost-unknown",
          held.get("FFF", {}).get("cost_known") is True,
          f"got {held.get('FFF', {}).get('cost_known')}")


def hand_set_values_survive_a_rebuild() -> None:
    """Values typed into the dashboard must outrank what was derived — and stay.

    They exist for the cells no document can fill: a security no price feed
    carries, a position whose cost predates the mailbox. That makes them the one
    kind of state the pipeline must never overwrite, and the one that has to
    survive a rebuild, a sync and a host that wipes its disk.
    """
    from pm import build as buildmod
    from pm import notes, overrides

    print("\n\033[1mValues set by hand\033[0m")

    cfg = cfgmod.load()
    cfg.members.append(Member(id="kavi", name="Kavi", doc_passwords=["KAVIX0001Z"]))
    cfg.save()

    # Two positions with no NSE mapping in this throwaway root, so neither can be
    # priced — which is exactly the situation a hand-set value is for. One of them
    # also has no cost, the other does.
    UNLISTED, PRIVATE = "INE900A01019", "INE900A01027"
    ev.append([manual.set_holdings(member="kavi", rows=[
        {"isin": UNLISTED, "symbol": "UNLISTED", "name": "Unlisted Co", "qty": 200, "avg": None},
        {"isin": PRIVATE, "symbol": "PRIVATE", "name": "Private Co", "qty": 50, "avg": 300.0},
    ], when=date(2026, 8, 1), source=ev.SRC_CSV)])

    def row(symbol: str) -> dict:
        payload = buildmod.build()
        kavi = next(m for m in payload["members"] if m["id"] == "kavi")
        found = next(h for h in kavi["holdings"] if h["symbol"] == symbol)
        return {**found, "_payload": payload}

    before = row("UNLISTED")
    check("a position with no price feed starts unpriced and without cost",
          before["priced"] is False and before["cost_known"] is False,
          f"got priced={before['priced']} cost_known={before['cost_known']}")

    overrides.set_value("price", UNLISTED, "125.50")
    after = row("UNLISTED")
    check("a hand-set price prices the position",
          after["priced"] and after["price"] == 125.5 and after["value"] == 200 * 125.5,
          f"got {after['price']} / {after['value']}")
    check("and carries no day change, having no yesterday to compare against",
          after["day_change"] is None, f"got {after['day_change']}")
    check("the cell is marked as one that was typed in",
          after["manual"] == ["price"] and after["manual_price"] is True, f"got {after['manual']}")
    check("a hand-set price is never called stale",
          after["stale"] is False and after["_payload"]["as_of"]["prices_stale"] is False)

    overrides.set_value("avg", UNLISTED, "1,00.50", member="kavi")
    after = row("UNLISTED")
    check("a hand-set average supplies the cost the log never saw",
          after["cost_known"] and after["avg"] == 100.5 and after["cost"] == 200 * 100.5,
          f"got avg={after['avg']} cost={after['cost']}")
    check("and answers the “cost was never seen” warning it existed to fix",
          not [a for a in after["_payload"]["attention"]
               if a["kind"] == "cost_unknown" and a["symbol"] == "UNLISTED"],
          f"still warned: {[a for a in after['_payload']['attention'] if a['kind'] == 'cost_unknown']}")

    # Invested and average are the same fact twice, so setting one must retire the
    # other rather than leave the pair contradicting itself.
    overrides.set_value("cost", UNLISTED, "30000", member="kavi")
    after = row("UNLISTED")
    check("setting invested recomputes the average and drops the average override",
          after["cost"] == 30000 and after["avg"] == 150.0 and "avg" not in after["manual"],
          f"got cost={after['cost']} avg={after['avg']} manual={after['manual']}")

    # A corrected share count is a claim about what is held, not about what a
    # share cost, so the average holds and the money follows it.
    overrides.set_value("cost", UNLISTED, "", member="kavi")
    overrides.set_value("qty", PRIVATE, "60", member="kavi")
    after = row("PRIVATE")
    check("a hand-set quantity keeps the average and moves the money invested",
          after["qty"] == 60 and after["avg"] == 300.0 and after["cost"] == 60 * 300.0,
          f"got qty={after['qty']} avg={after['avg']} cost={after['cost']}")

    check("only what actually landed on a position is counted",
          after["_payload"]["manual"]["values"] == 2
          and after["_payload"]["manual"]["unapplied"] == 0,
          f"got {after['_payload']['manual']}")

    overrides.set_value("price", "INE000SOLD019", "99")
    check("a value stored against a position nobody holds counts as unapplied",
          buildmod.build()["manual"]["unapplied"] == 1,
          f"got {buildmod.build()['manual']}")

    overrides.set_value("qty", PRIVATE, "", member="kavi")
    after = row("PRIVATE")
    check("clearing a value goes back to what the log says",
          after["qty"] == 50 and after["manual"] == [], f"got qty={after['qty']} {after['manual']}")

    for field, value, why in (
        ("price", "not a number", "text where a number belongs"),
        ("price", "-5", "a negative price"),
        ("price", "0", "a zero price"),
        ("qty", "1e40", "an implausible quantity"),
        ("pnl", "5", "a field that is computed, not entered"),
    ):
        try:
            overrides.set_value(field, UNLISTED, value, member="kavi")
            check(f"{why} is refused", False, "it was accepted")
        except ValueError:
            check(f"{why} is refused", True)

    # A thesis: markdown on disk, carried in the payload so a statically hosted
    # copy shows it too.
    notes.write(UNLISTED, "## Why\n\nBecause of the *land bank*.\n")
    after = row("UNLISTED")
    stock = next(r for r in after["_payload"]["consolidated"] if r["key"] == UNLISTED)
    check("a thesis is stored and rides along in the dashboard payload",
          stock["thesis"] and "land bank" in stock["thesis"]["markdown"]
          and stock["thesis"]["words"] == 7,
          f"got {stock.get('thesis')}")
    check("the note is a plain readable .md file, named after the instrument",
          (paths.NOTES / f"{UNLISTED}.md").exists())
    check("writing nothing but whitespace deletes the note",
          notes.write(UNLISTED, "  \n ")["cleared"]
          and not (paths.NOTES / f"{UNLISTED}.md").exists())

    for key in ("../../etc/passwd", "a/b", "", "x" * 80):
        try:
            notes.write(key, "x")
            check(f"a note key of {key!r} is refused", False, "it was accepted")
        except ValueError:
            check(f"a note key of {key!r} is refused", True)

    # None of this is re-derivable, so it has to travel in the backup.
    import io
    import tarfile

    from pm import backup

    notes.write(PRIVATE, "keep me\n")
    names = set(tarfile.open(fileobj=io.BytesIO(backup._pack())).getnames())
    check("the backup carries the overrides and the notes",
          "data/manual/overrides.json" in names
          and f"data/manual/notes/{PRIVATE}.md" in names,
          f"got {sorted(n for n in names if 'manual' in n)}")

    # Leave the shared root as it was found, so later tests see the same numbers.
    overrides.clear_all()
    notes.write(PRIVATE, "")


def broker_profiles_are_wired() -> None:
    """Every layout a broker declares must be one its parser actually supports.

    This is the guard for a whole class of failure. `layout_for()` returns a list;
    a parser that expected a string once rejected every list it was given and
    returned no rows — no exception, no warning, just a silently empty holdings
    statement for every broker. Asserting the declaration round-trips through the
    real accessor catches that the moment it is introduced.
    """
    from pm.parsers import contract_note, holdings

    print("\n\033[1mBroker profiles wired to parsers\033[0m")
    supported = {
        "contract_note": set(contract_note.STRATEGIES),
        "holdings": set(holdings.SUPPORTED_LAYOUTS),
    }

    for broker in cfgmod.BROKER_PROFILES:
        for kind, known in supported.items():
            declared = cfgmod.layout_for(broker, kind)
            check(f"{broker}/{kind}: declared layouts are supported",
                  all(name in known for name in declared),
                  f"declared {declared}, parser knows {sorted(known)}")

    # And the accessor's own shape: a string declaration and a list declaration
    # must both survive the round trip into something the parser accepts.
    check("layout_for always returns a list",
          isinstance(cfgmod.layout_for("zerodha", "holdings"), list)
          and isinstance(cfgmod.layout_for("groww", "contract_note"), list))

    for shape in ("holdings-balance-table", ["holdings-balance-table"], None):
        rows = holdings.parse(
            "Holdings as on 30-06-2026\n"
            "INE484J01027 SUNTECK REALTY LTD 25.000 25.000 298.55 7463.75\n",
            [], date(2026, 7, 1), layout=shape).rows
        check(f"holdings parser accepts layout={shape!r}", len(rows) == 1, f"got {rows}")


def main() -> int:
    members = setup_members()
    print("\n\033[1mIdentification and parsing\033[0m")

    # A contract note encrypted with Ravi's PAN must be attributed to Ravi and
    # yield one buy and one sell.
    note = encrypt(make_pdf(CONTRACT_NOTE), RAVI_PAN)
    report, trades = ingest_pdf(note, "Contract Note for 07-08-2026", members, date(2026, 8, 7))
    check("contract note recognised", report.kind == "contract_note", f"got {report.kind}: {report.notes}")
    check("owner identified by statement password", report.member == "ravi", f"got {report.member}")
    check("two trades parsed", len(trades) == 2, f"got {len(trades)}: {report.notes}")

    if len(trades) == 2:
        buy = next((t for t in trades if t["side"] == "buy"), None)
        sell = next((t for t in trades if t["side"] == "sell"), None)
        check("buy row read correctly",
              buy and buy["qty"] == 10 and abs(buy["price"] - 1423.55) < 0.01
              and buy["isin"] == "INE002A01018",
              f"got {buy}")
        check("sell row read correctly",
              sell and sell["qty"] == 5 and abs(sell["price"] - 1180.25) < 0.01,
              f"got {sell}")
        check("trade date taken from the document, not the email",
              buy and buy["ts"].startswith("2026-08-07"), f"got {buy['ts'] if buy else None}")
        # The description sits among order numbers and timestamps; none of that
        # should leak into the displayed name.
        check("security name read cleanly out of the row",
              buy and buy["name"] == "RELIANCE INDUSTRIES LTD",
              f"got {buy['name']!r}" if buy else "no buy")

    # A holdings statement encrypted with Priya's PAN belongs to Priya, and
    # carries quantity but no cost.
    stmt = encrypt(make_pdf(HOLDING_STATEMENT), PRIYA_PAN)
    report, snaps = ingest_pdf(
        stmt, "Transaction cum Holding Statement", members, date(2026, 9, 1),
        sender="no-reply-transaction-with-holding-statement@reportsmailer.zerodha.net")
    check("holding statement recognised", report.kind == "holdings_statement",
          f"got {report.kind}: {report.notes}")
    check("owner identified as Priya", report.member == "priya", f"got {report.member}")
    check("three positions parsed", snaps and len(snaps[0]["holdings"]) == 3,
          f"got {snaps[0]['holdings'] if snaps else None}")
    check("marked as quantity-only (no cost basis)", snaps and snaps[0]["has_cost"] is False)
    check("as-of date read from the statement", snaps and snaps[0]["ts"].startswith("2026-08-31"),
          f"got {snaps[0]['ts'] if snaps else None}")

    # The funds ledger is a real document with nothing to log. It must not be
    # mistaken for holdings.
    funds = encrypt(make_pdf(FUNDS_STATEMENT), RAVI_PAN)
    report, produced = ingest_pdf(funds, "Report: Statement of Accounts of Funds",
                                  members, date(2026, 9, 1))
    check("funds ledger identified and ignored",
          report.kind == "funds_statement" and not produced,
          f"got kind={report.kind}, {len(produced)} events")

    # A statement whose PAN we don't hold must be flagged, never guessed at.
    stranger = encrypt(make_pdf(CONTRACT_NOTE), "ZZZZZ9999Z")
    report, produced = ingest_pdf(stranger, "Contract Note", members, date(2026, 8, 7))
    check("unknown password flagged rather than misattributed",
          report.member is None and report.status == "needs_attention" and not produced,
          f"got member={report.member} status={report.status}")

    print("\n\033[1mEvent log and replay\033[0m")

    ev.append([manual.set_holdings(
        member="ravi",
        rows=[{"isin": "INE002A01018", "symbol": "RELIANCE", "qty": 25, "avg": 1380.50},
              {"isin": "INE009A01021", "symbol": "INFY", "qty": 30, "avg": 1495.80}],
        when=date(2026, 8, 1), source=ev.SRC_CSV)])
    # Priya's opening comes from a broker's own holdings *report* — it carries cost,
    # but it arrived in the mail, so the depository statements that follow still
    # supersede it. That is the path taken by anyone who has never uploaded an
    # export, and it is what keeps bonus issues and splits being absorbed.
    ev.append([manual.set_holdings(
        member="priya",
        rows=[{"isin": "INE002A01018", "symbol": "RELIANCE", "qty": 12, "avg": 1402.10},
              {"isin": "INE154A01025", "symbol": "ITC", "qty": 150, "avg": 412.60},
              {"isin": "INE155A01022", "symbol": "TATAMOTORS", "qty": 60, "avg": 690.40}],
        when=date(2026, 8, 1), source=ev.SRC_HOLDINGS_STATEMENT)])

    written, _ = ev.append(trades)
    check("trades appended", written == 2, f"wrote {written}")

    again, dupes = ev.append(trades)
    check("re-ingesting the same contract note is a no-op",
          again == 0 and dupes == 2, f"wrote {again}, skipped {dupes}")

    ev.append(snaps)
    state = replay()

    ravi = {p["symbol"]: p for p in state["holdings"]["ravi"]}
    check("buy added to the opening snapshot (25 + 10)",
          ravi.get("RELIANCE", {}).get("qty") == 35, f"got {ravi.get('RELIANCE')}")
    check("sell subtracted (30 - 5)",
          ravi.get("INFY", {}).get("qty") == 25, f"got {ravi.get('INFY')}")
    check("average cost unchanged by a sell",
          abs(ravi["INFY"]["avg"] - 1495.80) < 0.01, f"got {ravi['INFY']['avg']}")

    expected_avg = (25 * 1380.50 + 10 * 1423.55) / 35
    check("buy blended into the weighted average",
          abs(ravi["RELIANCE"]["avg"] - expected_avg) < 0.5,
          f"got {ravi['RELIANCE']['avg']:.2f}, expected {expected_avg:.2f}")

    priya = {p["symbol"]: p for p in state["holdings"]["priya"]}
    check("quantity-only statement corrected the share count (60 → 66)",
          priya.get("TATAMOTORS", {}).get("qty") == 66, f"got {priya.get('TATAMOTORS')}")
    check("bonus shares kept total cost fixed and lowered the average",
          abs(priya["TATAMOTORS"]["cost"] - 60 * 690.40) < 1
          and priya["TATAMOTORS"]["avg"] < 690.40,
          f"cost={priya['TATAMOTORS']['cost']:.2f} avg={priya['TATAMOTORS']['avg']:.2f}")

    drift = [w for w in state["warnings"] if w["kind"] == "drift"]
    check("the discrepancy was surfaced, not silently applied", bool(drift),
          f"warnings: {state['warnings']}")

    uploaded_statement_sets_holdings_and_rewinds()
    an_export_outranks_the_statements_that_follow_it()
    csv_rows_land_on_the_right_positions()
    incomplete_statements_do_not_erase_positions()
    real_document_layouts()
    broker_profiles_are_wired()
    hand_set_values_survive_a_rebuild()

    print(f"\n\033[1m{passed} passed, {failed} failed\033[0m  ({WORKDIR})\n")
    shutil.rmtree(WORKDIR, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
