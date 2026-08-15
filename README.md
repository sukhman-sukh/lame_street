# LameStreet

One dashboard for a family's stock holdings, running on your own machine.

Indian brokers already email you everything you need — a contract note after every
trading day, a holding statement every month. LameStreet reads those, and turns
them into a live view of who owns what, at what cost, and what it's worth now.

No paid APIs. No cloud service holding anyone's credentials. Nothing leaves your
machine except a price lookup.

```
┌─ broker email ──┐
│ contract notes  │──┐
│ holding stmts   │  │   ┌───────────────┐   ┌────────────┐   ┌─────────┐
└─────────────────┘  ├──▶│  event log    │──▶│ dashboard  │──▶│ browser │
┌─ Yahoo Finance ─┐  │   │  (JSONL)      │   │ .json      │   │         │
│ prices, free    │──┘   └───────────────┘   └────────────┘   └─────────┘
└─────────────────┘
```

Built for the Indian market: NSE/BSE equities, ISIN-keyed, rupee formatting,
SEBI statement formats. **Verified against real accounts at Groww, Zerodha and
Dhan.**

---

## Why it works this way

**Holdings and prices are separated.** What someone owns changes a few times a
month and is slow to fetch. What it's worth changes every second and is free. So
prices refresh whenever you like, and the broker side gets touched rarely.

**Nothing is overwritten.** Every trade and statement becomes an entry in an
append-only log. Current holdings are *derived* by replaying it:

```
holdings now  =  latest holding statement  +  every trade since
```

If a number ever looks wrong, the log says exactly why. Corrections are new
entries, the way a ledger works.

**Re-reading the same email changes nothing.** Every event's ID is a hash of the
broker's own trade number, so ingestion can be repeated freely — including
re-parsing years of archived mail after fixing a parser.

**Bonus issues and splits handle themselves.** A holding statement gives quantity
but never cost. When it reports more shares than the log expected, total money
invested is held fixed and the average recomputes — exactly right for a bonus or
a split, and flagged for review either way.

**No LLM in the data path.** Parsing is regex, ISIN check digits and column-header
matching. Deterministic: same document in, same numbers out, every time. A model
is only ever used to help *read the layout* of a broker nobody has profiled yet —
never to extract a number from a broker that is already profiled.

---

## Install

Requires Python 3.10+.

```bash
git clone <your-fork> lamestreet && cd lamestreet
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pm init
.venv/bin/python -m pm serve
```

Open **http://localhost:3002** and use the **Setup** tab. Everything below can be
done there; the CLI equivalents are listed at the end.

### 1. Add each person

Name, statement password, brokers, their Gmail address, a Gmail app password.

Two fields matter more than they look:

- **Statement password** — whatever opens that person's broker PDFs. Usually their
  PAN in capitals; some brokers use a client code or date of birth. It does double
  duty: it opens the file *and* identifies whose it is, so **it must be unique per
  person**. If two people share one, the app refuses to guess rather than risk
  attributing a portfolio to the wrong person.
- **App password** — a 16-character code from
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
  (turn on 2-Step Verification first). A Google account password will **not** work;
  Gmail stopped accepting those for IMAP.

Press **Test all inboxes** to confirm each connects.

### 2. Sync holdings from a CSV — per person

The one thing email cannot give you. Broker and depository statements state what
you own but never what you paid, so cost basis has to come from the broker.
In Groww: **Reports → Holdings → download**. Zerodha: **Console → Portfolio →
Holdings → download**. Dhan: **Portfolio → Holdings → export**. Then upload the
CSV in the Setup tab. Column names are matched loosely, so most brokers' exports
work as-is.

Contract notes keep cost current for everything bought since the mailbox history
begins. For anything bought *before* it, no document you have states the price —
so re-upload an export whenever P&L looks wrong. It is not a one-time step.

Rows are matched to existing positions by ISIN, resolved from the company name
through NSE's list and through what that person already holds. That second
lookup is what catches recently listed or unlisted securities that appear in no
master list. Without the matching, an upload would key on the name and create a
*second* position for every company already held; anything still unmatched is
reported rather than guessed at.

**An export takes precedence over the mail.** Once you upload one for someone,
their holdings are that export plus the trades after it — depository statements
arriving later no longer override it. They are still read and compared, and any
disagreement is raised under **Needs a look**, but they cannot overwrite what the
broker itself reported. If nobody has uploaded an export, the holdings statements
from the mail are the anchor, exactly as before.

The tradeoff is worth knowing: a bonus issue or a split only ever appears in a
depository statement, so once an export is in force those stop being absorbed
automatically. They show up as a `statement_disagrees` warning instead, and
uploading a fresh export clears it.

### 3. Refresh the NSE list

One click. Maps ISINs to trading symbols and prices using NSE's published equity
list (~2,400 instruments).

### 4. Sync

Press **Sync holdings**, or for the first full historical read:

```bash
.venv/bin/python -m pm sync --full
```

---

## Configuration and secrets

**`config.json` is the store** — a small filesystem database. The app writes to
it, so people and credentials added through the UI persist across restarts and
redeployments. It is **gitignored** and `chmod 600` because it holds real names,
addresses and credentials. `config.example.json` shows the shape.

**`.env` is an optional seed** for standing up a new instance without typing
anything in. Copy `.env.example`, fill it, and the values fill any gaps in
`config.json` on load:

```bash
cp .env.example .env
# edit, then persist into config.json and delete .env if you like:
.venv/bin/python -m pm secrets adopt
```

`config.json` always wins where it has a value, so a stale `.env` can never
override a credential entered in the UI. `pm secrets check` shows where each
credential is currently read from.

**Deploying elsewhere:** point `PM_ROOT` at a mounted volume and everything
persistent — `config.json`, the event log, the raw archive — lives there, outside
the checkout.

```bash
PM_ROOT=/var/lib/lamestreet .venv/bin/python -m pm serve
```

### Never committed

`.gitignore` excludes all of it, and `pm secrets check` will tell you if anything
drifts:

```
.env, config.json          credentials, names, addresses
data/                      archived broker email, statement PDFs, the event log,
                           derived holdings, price cache, built dashboard
profiles/                  saved browser sessions
dist/                      static export — contains real positions
*.pdf *.csv *.xlsx         statements and exports anywhere in the tree
```

---

## Daily use

Two buttons, deliberately different:

| | What it does | Cost | How often |
|---|---|---|---|
| **Refresh prices** | Yahoo Finance quotes | free, instant | as often as you like |
| **Sync holdings** | reads new mail, re-derives everything | slower | daily is plenty; rate-limited to once per 5 min |

Three timestamps are always on screen — *prices*, *holdings*, *mail synced* —
because they are three different data ages, and conflating them is how someone
acts on stale information.

**Automatic daily sync** (macOS):

```bash
.venv/bin/python -m pm schedule --time 20:00
launchctl load ~/Library/LaunchAgents/com.lamestreet.sync.plist
```

launchd rather than cron, so it catches up after the machine has been asleep.

### Syncs stay cheap

Two mechanisms, because opening PDFs is by far the most expensive part:

- **Server-side mail filtering.** Only mail from the brokers' own sending
  addresses is fetched. On a real inbox that is 85 messages instead of 29,174.
- **A parsed-document ledger.** Each document is read once and recorded against a
  parser version. A re-parse that has nothing new to do takes **9 seconds instead
  of 406**. Bumping the parser version invalidates the archive on purpose, so a
  parser fix re-derives everything.

---

## Brokers

| Broker | Contract notes | Holding statements | Status |
|---|---|---|---|
| Groww | ✅ | ✅ | verified |
| Zerodha | ✅ | ✅ | verified |
| Dhan | ✅ | ✅ | verified |
| Upstox | — | — | profile only |
| ICICI Direct, IIFL | — | — | profile only |

Each broker is a profile in [`pm/config.py`](pm/config.py) recording its sending
addresses and which extraction strategy its documents use. SEBI standardised the
contract note in February 2025, so **one parser reads Groww, Zerodha and Dhan** —
adding a broker is usually a profile entry, not code.

Layouts are declared as an *ordered list*, so a broker that changed format (Groww
did) can declare both eras without the parser guessing. A broker declaring only
one layout will never quietly fall through to a weaker strategy, because a weak
strategy on a rich layout produces plausible, wrong numbers.

### Documents that are deliberately ignored

Several broker documents list ISINs with quantities and look like holdings, but
are not positions. Reading one as a snapshot is *destructive*, since a snapshot
supersedes the log and every position it omits gets zeroed:

- **Retention / margin statements** — shares pledged as collateral
- **Payout advices** ("Statement of Accounts of Securities", SOF-SOS) — shares
  that moved on a settlement
- **Client master reports** — account particulars
- **Funds ledgers** — no ISINs at all

A snapshot is only accepted from a document that explicitly states a holdings
balance.

### Reading a holdings table

Three rules, each earned from a real misparse:

1. **Recover the grid from whitespace** when a statement rules only its header
   (Zerodha does exactly this).
2. **Count columns in from the end of the row.** Security names contain digits
   more often than you'd expect — `MANAPPURAM FIN RE2/-`, `RAIN INDUSTRIES-2/-` —
   and counting from the front lets one shift every column along.
3. **Check the row's own arithmetic.** These rows end with a rate and a market
   value, so `value ÷ rate` independently states the quantity. When they disagree,
   the arithmetic wins. Without this guard, a market value once became a holding
   of 14,578,057 shares.

Quantity column preference is explicit: total before free balance. `Free Bal`
excludes pledged shares, so reading the first balance-shaped column silently
under-reports anyone using margin — and drops a *fully* pledged holding
altogether, since its free balance reads `0.00` and a zero quantity looks like a
closed position. Where a layout states no total at all (one of the two CDSL
formats Dhan sends), the position is the sum of the parts: free + pledged +
earmarked + demat + remat + lock-in.

### Tables that span pages

Both parsers gather every page of a table, because extraction returns one table
per page and reading only the first is silent: on a contract note it loses
trades, and on a holdings statement it retires positions that were never sold.

Continuation pages arrive in two shapes. Most repeat the header, which is matched
on — an unrelated table with the same columns has a different header and is left
alone. Some repeat nothing at all and the rows simply resume; those are admitted
only when *every* row on them is recognisable as a row of that table, since there
is no header to match on and guessing would be worse than missing them.

Header matching ignores whitespace and footnote markers. A statement prints
`Gross Rate/ Trade Price per Unit(₹)²` on page one and `…per Unit(₹)` on page
two, and `(before levies)(₹)` against `(before levies) (₹)` — either difference
is otherwise enough to make the continuation look like a different table.

### One broker, two formats

A broker may change its layout and keep both in your archive. Zerodha's contract
notes are ruled one row per fill with a `Buy(B)/Sell(S)` column up to mid-2025,
and as SEBI per-ISIN net-obligation blocks after. Profiles therefore declare an
ordered *list* of layouts, tried in order, so old and new documents both read
without either being guessed at.

---

## When a statement doesn't parse

Anything the parsers can't read is listed under **Needs a look** rather than
silently dropped, and the original file is kept under `data/raw/`.

```bash
.venv/bin/python -m pm inspect data/raw/2026-08/<...>/00-statement.pdf
```

That prints which password opened it, how it was classified, which extraction
strategy ran, what it found, and where it gave up.

**To share a layout for help, redact it first:**

```bash
.venv/bin/python -m pm inspect <file.pdf> --redact
```

Every digit becomes a `9`, PANs and emails are replaced, labelled personal fields
are dropped — while column headers, date formats and number formats survive
intact. Company names are kept, since seeing them is how you confirm the right
column was read. That output is safe to paste anywhere. `--text` gives the
unredacted version; don't share that one.

Once a parser improves:

```bash
.venv/bin/python -m pm reparse              # read anything not yet read
.venv/bin/python -m pm reparse --all        # re-read everything
.venv/bin/python -m pm reparse --snapshots  # re-derive holdings snapshots only
.venv/bin/python -m pm reparse --rebuild    # also retire earlier interpretations
```

Both rebuild modes parse first and only retire the old log if the result holds up,
so a re-parse that can read nothing (a missing password, say) leaves your data
intact. Retired shards move to `data/events/superseded-<timestamp>/` rather than
being deleted.

Prefer `--snapshots` after a fix to the holdings parser. A misread snapshot has to
be retired because it supersedes the log, but trades are the part nothing else can
reconstruct — and a full `--rebuild` would retire every contract note the parsers
cannot read today along with it.

---

## Resetting holdings from a statement

When a month's statement never arrives — or the numbers have drifted and you want
them set against the depository rather than inferred — hand the statement over
directly:

```bash
.venv/bin/python -m pm statement ~/Downloads/holding-statement-jun.pdf
```

Quantities become exactly what the document says on **its own** date, the mail
cursor rewinds to that date, and every contract note since is read and applied on
top. Encrypted statements need no extra argument: the password that opens the file
is what identifies the owner, the same rule the mail path uses. Add `--member` to
restrict it to one person, or `--no-sync` to record the statement and leave the
mail cursor where it is.

The same thing lives in the Setup tab under **Sync from a statement**, and the
uploaded file is archived under `data/raw/` like any other document, so a later
re-parse keeps it.

Uploading the same statement twice is recognised as the same document rather than
recorded again, and anything that isn't a holdings statement is refused — reading a
contract note or a margin report as a snapshot would zero every position it
doesn't happen to mention.

---

## Hosting the view elsewhere

```bash
.venv/bin/python -m pm export --out dist
```

A working dashboard with no server: the page falls back from the API to a
`dashboard.json` beside it and hides the action buttons. Deployable to any static
host.

It is a **snapshot**, not a live app — collection needs a filesystem and a
mailbox. Re-export to update.

> That URL exposes the family's entire net worth to anyone who has it, and the
> exported page has no login. Put it behind the host's access control, or don't
> deploy it.

---

## Commands

```
serve           run the dashboard — everything below is also in its Setup tab
init            create config and data folders
member          add / list / remove people
mailbox         set / test an inbox connection
secrets         check / adopt / export credentials
sync            read new mail, log trades, rebuild      (--full for all history)
statement       set holdings from a statement PDF, then sync the mail after it
reparse         re-run parsers over archived mail  (--all, --rebuild, --snapshots)
refresh         prices + rebuild
prices          prices only
build           rebuild dashboard.json from the log
status          summary in the terminal
import-csv      bootstrap holdings from a broker CSV
add-trade       log a trade by hand
instruments     refresh / list / map ISIN → symbol
inspect         show what the parser sees in a PDF       (--redact to share)
export          write a static copy you can host
schedule        set up the daily automatic sync
```

## Layout

```
config.json            the store: people, brokers, credentials   (gitignored)
.env                   optional seed for a fresh deployment      (gitignored)
data/
  events/YYYY-MM.jsonl the append-only log — the only source of truth
  raw/                 every original email + attachment, kept for re-parsing
  state/               derived caches — safe to delete, rebuilt by `pm build`
  public/dashboard.json what the viewer reads
pm/                    collector, parsers, server
viewer/                static HTML/CSS/JS, no build step
tests/test_pipeline.py 57 tests over generated and real-world layouts
```

`data/state/` is a cache, not truth. Delete it and `pm build` reconstructs it.

```bash
.venv/bin/python tests/test_pipeline.py
```

---

## Honest limitations

- **Cost basis must be bootstrapped once per person.** No mandatory statement
  contains your purchase price.
- **Holding statements are monthly.** Between them, accuracy depends on contract
  notes parsing correctly.
- **Contract notes cover equity and F&O only.** Mutual funds arrive as separate
  CAMS/KFintech statements — not yet parsed.
- **Pre-2025 layouts are patchy.** Groww's older contract notes are read on a
  best-effort basis, so cost basis for very long-held positions may be incomplete.
  Quantities are still correct, because the latest holding statement is
  authoritative.
- **Prices are ~15 minutes delayed** and depend on Yahoo Finance remaining free.
- **No authentication.** Safe because it binds to localhost; not safe the moment
  you pass `--host 0.0.0.0` or deploy the export publicly.
- **Credentials are stored in plain text** in `config.json` (chmod 600,
  gitignored). Revoke an app password at
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
  if a machine goes missing.
- **Read-only by construction.** There is no order-placing code anywhere in it.

## Legal

This reads statements a broker emailed to you, from your own mailbox, for your own
records. It uses no broker API and does not automate any broker website. You are
responsible for your own accounts and for complying with your brokers' terms.

Not investment advice. No warranty.
