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

    check("settlement/payout advice is not mistaken for a holdings snapshot",
          holdings.looks_like_holdings("Statement of Accounts of Securities", "ROS_123", ros_text)
          is False)
    check("funds ledger still ignored",
          holdings.looks_like_holdings("Report: Statement of Accounts of Funds", "x",
                                       "STATEMENT OF ACCOUNTS OF FUNDS\nPurchase of securities") is False)


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
    ev.append([manual.set_holdings(
        member="priya",
        rows=[{"isin": "INE002A01018", "symbol": "RELIANCE", "qty": 12, "avg": 1402.10},
              {"isin": "INE154A01025", "symbol": "ITC", "qty": 150, "avg": 412.60},
              {"isin": "INE155A01022", "symbol": "TATAMOTORS", "qty": 60, "avg": 690.40}],
        when=date(2026, 8, 1), source=ev.SRC_CSV)])

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

    real_document_layouts()
    broker_profiles_are_wired()

    print(f"\n\033[1m{passed} passed, {failed} failed\033[0m  ({WORKDIR})\n")
    shutil.rmtree(WORKDIR, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
