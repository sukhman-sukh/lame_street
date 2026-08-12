# Family Portfolio Manager — v3 (email-sourced, event-log architecture)

## The design in one paragraph

Each family member's broker emails are the primary feed. A local **collector** reads those emails
daily, extracts trades, and appends them to an **append-only event log** (a WAL). Current holdings are
*derived* by replaying that log on top of a **snapshot** taken from the broker. Periodically — and on
demand — a headless browser logs into Groww and takes a fresh authoritative snapshot; any difference
between "what the log says you own" and "what Groww says you own" is recorded as a reconciliation
entry, never a silent overwrite. Everything lands as plain JSON/JSONL files. A separate **viewer**
reads only those files, so it can run locally or be hosted anywhere.

Eventually consistent, auditable, and it degrades gracefully — if email parsing breaks you still have
the last snapshot; if the browser sync breaks you still have the log.

---

## Two things in your plan that don't work as stated

### 1. Vercel cannot run this as one app

You asked for JSON files so it could be hosted on Vercel and work the same. It can't, and it's worth
being blunt about why:

- Vercel's filesystem is **read-only at runtime**. Writing to a JSON file throws `EROFS`. Only `/tmp`
  is writable and it's wiped between invocations — so an append-only log is impossible there.
- Serverless functions can't run a **persistent headless browser** with a saved Groww login, and
  can't hold an OTP flow open.
- There's no long-lived process to sit and watch a mailbox.

**The fix — and it actually serves your goal better: split the app in two.**

| | Runs where | Does what | Writes |
|---|---|---|---|
| **Collector** | Your Mac (daily cron + on-demand) | Reads email, parses trades, browser sync, reconciles | The JSON files |
| **Viewer** | Anywhere — `localhost`, Vercel, GitHub Pages | Reads the JSON files, renders the dashboard | Nothing |

The JSON files are the contract between them. The viewer is pure read-only, so it deploys to Vercel
unchanged — you just push the data files alongside it. You get the portability you wanted, and the
messy stateful half stays on a machine that can actually do it.

### 2. Email gives you *changes*, not *holdings*

A transaction feed starting today tells you nothing about shares bought three years ago. So the log
alone can never produce correct holdings or average cost — it needs a starting balance. You already
half-solved this ("first time it does this sync compulsorily"); making it explicit:

```
holdings_now  =  snapshot(t0)  +  replay(all events after t0)
```

And it must be re-snapshotted periodically, because some things never appear in a contract note:
**bonus issues, stock splits, dividends, buybacks, off-market transfers**. Those silently rot the
derived state. Periodic snapshots are what catch that drift — this is the main reason the browser
sync stays in the design rather than being a nice-to-have.

---

## What the emails actually contain (researched)

**Contract notes are the gold source, and they're better than I expected:**

- Emailed by Groww **at the end of every trading day** — a SEBI obligation, not a courtesy.
- Since **1 Feb 2025** the format is SEBI-standardised: all trades in a security consolidated into a
  **single row with a Weighted Average Price**. That's exactly the shape we want to append.
- Contains order number, trade number, quantity, price, and charges.
- **Password-protected: your PAN in capital letters.** One-time config per member.

So we parse contract-note PDFs, not marketing-flavoured "order executed" emails. Far more stable,
legally mandated to keep arriving, and self-describing.

**What contract notes don't cover:** mutual funds (those come as separate CAMS/KFintech emails — a
later phase) and corporate actions (handled by reconciliation).

---

## Email access — I'd change your approach here

You suggested a Google app password per member. Two problems:

- App passwords still work for personal `@gmail.com` with 2FA, but Google is **phasing them out
  through 2026**, and they are **already dead for Google Workspace accounts**. If any family member
  uses a Workspace address, it won't work at all.
- An app password grants **full mailbox access**. That's six people's entire personal email sitting
  behind one script on your Mac. Even with total family trust, that's more access than the job needs.

The Gmail API alternative isn't much better: `gmail.readonly` is a *restricted* scope, so in Testing
mode refresh tokens die every 7 days, and you'd have to flip the project to Production and click past
an "unverified app" warning to avoid that.

**Recommended instead — one dedicated inbox, fed by forwarding:**

Each member sets up a Gmail filter once: *from Groww → forward to `familyportfolio@gmail.com`*
(a new address you create). Gmail asks them to confirm the forward once, and that's it.

Why this is better:
- The app connects to **one** mailbox instead of six.
- That mailbox contains **only broker mail** — the app is structurally incapable of reading anyone's
  personal email. That's a much easier thing to ask your family to agree to.
- One credential to rotate instead of six.
- Members can revoke by deleting their filter, no password change needed.

Attachments (the contract-note PDFs) forward through intact. The app still needs each member's PAN to
open their PDFs, and it identifies whose note is whose from the PDF contents, not the sender.

I'd still build the connector as a small interface with an IMAP implementation, so a per-member app
password remains possible if you'd rather do it that way — but the shared inbox is the default.

---

## The event log

Append-only JSONL, sharded by month. Append is O(1) and crash-safe; a single rewritten JSON file is
neither.

```jsonc
// data/events/2026-08.jsonl
{"id":"...","ts":"2026-08-07T15:31:00+05:30","member":"ravi","type":"trade",
 "side":"buy","symbol":"RELIANCE","exchange":"NSE","qty":10,"price":1423.55,
 "charges":18.40,"source":"contract_note","trade_no":"5512331","doc":"<msg-id>"}

{"id":"...","ts":"2026-08-09T09:00:00+05:30","member":"ravi","type":"snapshot",
 "source":"groww_browser","holdings":[{"symbol":"RELIANCE","qty":10,"avg":1423.55}]}

{"id":"...","ts":"2026-08-09T09:00:01+05:30","member":"ravi","type":"adjustment",
 "symbol":"TATAMOTORS","qty_delta":10,"reason":"drift_vs_snapshot; likely 1:1 bonus"}
```

**Idempotency is the thing that makes this safe.** Emails get re-fetched; the same PDF may arrive
twice. Every event gets a deterministic ID hashed from `(member, trade_no, symbol, qty, price)` —
contract notes carry real trade numbers, which are perfect natural keys. Re-processing the same email
a hundred times produces the same IDs and changes nothing. That property is what lets the sync be
careless and still correct.

## File layout

```
data/
  members.json              # roster: name, email, PAN (for PDF passwords), broker
  events/2026-08.jsonl      # append-only WAL, month-sharded
  state/holdings.json       # derived current holdings (rebuildable from events)
  state/sync.json           # last_fetch_at per source, cursors, last error
  snapshots/<member>/…json  # raw authoritative broker snapshots, kept verbatim
  prices/latest.json        # Yahoo Finance cache
  public/dashboard.json     # the single file the viewer reads
profiles/<member>/          # Playwright browser sessions (gitignored)
```

`state/` is a **cache, not a source of truth** — deletable and rebuildable from `events/` at any time.
That's the property that makes the whole thing debuggable.

---

## Sync triggers

| Trigger | What runs |
|---|---|
| **Daily, fixed time** (local cron/launchd) | Read email since `last_fetch_at` → append events → rebuild → refresh prices |
| **New member added** | Compulsory full backfill: browser snapshot first, then all historical email |
| **"Sync holdings" button** | Browser snapshot + reconcile against derived state |
| **"Refresh prices" button** | Yahoo only — free, fast, safe to spam |
| **Weekly (automatic)** | Browser snapshot for every member, to catch corporate-action drift |

`last_fetch_at` is stored per source, so a missed day just means the next run covers a wider window.
Nothing is lost by the collector being offline.

## Reconciliation

After a browser snapshot, compare derived vs authoritative per symbol:

- **Match** → record a `verified` marker, show a green "reconciled" badge with the timestamp.
- **Differ** → append an `adjustment` event with the delta and a guessed reason (bonus, split, missed
  email), and surface it in the UI as *"Ravi · TATAMOTORS · +10 unexplained · review"*.

The log is never rewritten. Corrections are new entries, the way a ledger works. You can always answer
"why does it think I own this?" by replaying.

---

## Build order

**Step 1 — Log + viewer, fed by hand.** Event schema, JSONL append, replay engine, derived holdings,
Yahoo prices, and the full dashboard reading `public/dashboard.json`. Seed it by manually adding a few
trades. *End state: a working, useful dashboard with zero integrations.*

**Step 2 — Contract-note pipeline.** Connect the shared inbox, pull messages since last fetch, filter
transactional from promotional, open PDFs with each member's PAN, parse the SEBI table, append events.
Idempotent by trade number.

**Step 3 — Browser snapshot + reconciliation.** Per-member Playwright profiles, link flow with OTP,
headless holdings fetch, drift detection, adjustment events, reconciliation badges.

**Step 4 — Scheduling and deployment.** launchd daily job, sync status page with per-source last-run
and last-error, and the viewer deployed to Vercel reading committed JSON.

**Later:** mutual funds via CAMS/KFintech emails; CDSL easi / eCAS as a third independent cross-check;
Groww Trade API adapter if reliability ever justifies ₹499/account.

---

## The dashboard

Professional and plain: white ground, one accent colour, real tables, right-aligned numbers,
green/red only on P&L, no gradients or animation.

- **Overview** — family totals, a card per member, then a consolidated stock table showing per stock
  **who holds it and who doesn't** (the direct input to "everyone sell X"), expandable to per-member
  quantity, average cost and P&L.
- **Member view** — that person's holdings and totals.
- **Activity** — the event log as a human-readable feed. Free, since we're storing it anyway, and it's
  the audit trail.
- **Sync status** — per source: last run, next run, last error, reconciliation state.
- **Three separate timestamps**, always visible: *prices as of*, *holdings as of*, *last reconciled*.
  Three different data ages; conflating them is how someone acts on stale information.

## Security

- Collector runs locally; viewer is read-only and holds no credentials.
- If the viewer goes on Vercel, it exposes the family's entire net worth to anyone with the URL —
  so it needs at minimum a password, or keep it on the LAN. Flagging this now, not later.
- `profiles/`, `data/`, and mail credentials are gitignored; PANs stored locally only.
- No order-placing code exists anywhere in the app. Read-only by construction.

---

## Sources

- [Groww: where to get contract notes](https://groww.in/help/my-account/ma-others/where-can-i-get-the-contract-note) · [report password = PAN in caps](https://groww.in/help/stocks,-f&o-&-ipo/discoverable/what-is-the-password-to-open-my-report)
- [Google: transition from less secure apps to OAuth](https://support.google.com/a/answer/14114704) · [Gmail app password phase-out, 2026](https://www.getmailbird.com/gmail-oauth-changes-app-password-phase-out/)
- [Google OAuth refresh token 7-day limit in Testing mode](https://www.unipile.com/google-oauth-refresh-token/)
- [Vercel: read-only filesystem / EROFS](https://github.com/vercel/community/discussions/314) · [using files in Vercel Functions](https://vercel.com/kb/guide/how-can-i-use-files-in-serverless-functions)
- [Sparker0i/indian-stock-mcp-agent](https://github.com/Sparker0i/indian-stock-mcp-agent) · [Groww Trade API](https://groww.in/trade-api)
