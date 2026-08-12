"""Generate a launchd job so the collector runs itself every day.

launchd rather than cron because it catches up after the Mac has been asleep,
which matters for a job that runs once a day on a laptop that gets closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import paths

LABEL = "com.lamestreet.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>pm</string>
    <string>sync</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <!-- Run as soon as the Mac wakes if it was asleep at the scheduled time. -->
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{root}/data/state/sync.log</string>
  <key>StandardErrorPath</key><string>{root}/data/state/sync.log</string>
</dict>
</plist>
"""


def write_plist(hour: int, minute: int) -> Path:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(TEMPLATE.format(
        label=LABEL,
        python=sys.executable,
        root=paths.ROOT,
        hour=hour,
        minute=minute,
    ))
    return PLIST_PATH
