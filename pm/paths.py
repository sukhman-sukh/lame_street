"""Every path the app uses, in one place."""
from __future__ import annotations

import os
from pathlib import Path

# Where the code lives, and where data lives. PM_ROOT relocates the data — the
# viewer and other shipped files always come from the installation itself.
INSTALL = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("PM_ROOT", INSTALL))

CONFIG = ROOT / "config.json"

DATA = ROOT / "data"
EVENTS = DATA / "events"          # append-only log, one JSONL per month
STATE = DATA / "state"            # derived caches — safe to delete and rebuild
SNAPSHOTS = DATA / "snapshots"    # authoritative broker/depository holdings, verbatim
RAW = DATA / "raw"                # original email bodies + attachments, kept forever
PRICES = DATA / "prices"
PUBLIC = DATA / "public"          # the only thing the viewer reads

HOLDINGS_CACHE = STATE / "holdings.json"
SYNC_STATE = STATE / "sync.json"
INSTRUMENTS = STATE / "instruments.json"
PRICES_LATEST = PRICES / "latest.json"
DASHBOARD = PUBLIC / "dashboard.json"

VIEWER = INSTALL / "viewer"        # shipped with the code, not the data
PROFILES = ROOT / "profiles"       # playwright browser sessions (bootstrap only)


def ensure_dirs() -> None:
    for d in (DATA, EVENTS, STATE, SNAPSHOTS, RAW, PRICES, PUBLIC, PROFILES):
        d.mkdir(parents=True, exist_ok=True)
