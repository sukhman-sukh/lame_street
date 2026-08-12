"""Read secrets from a .env file instead of committing them.

Everything sensitive — mailbox addresses, Gmail app passwords, statement
passwords — lives here, keyed by member id. `config.json` then holds only names,
brokers and settings, which makes it safe to commit and safe to share.

The rule that makes this work: a secret loaded from the environment is never
written back to config.json. Provenance is tracked per field, so saving config
from the UI can't quietly copy a .env secret onto disk.

No dependency needed — the format is simple enough to parse honestly.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import paths

ENV_FILE = paths.ROOT / ".env"

# Member ids become env suffixes: "mary-jane" -> "MARY_JANE".
_SLUG = re.compile(r"[^A-Z0-9]+")


def key_for(member_id: str) -> str:
    return _SLUG.sub("_", (member_id or "").upper()).strip("_")


def load(path: Path | None = None) -> dict[str, str]:
    """Parse .env and put anything new into os.environ.

    A variable already present in the real environment always wins, so a
    deployment can override the file without editing it.
    """
    target = path or ENV_FILE
    values: dict[str, str] = {}
    if not target.exists():
        return values

    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        # Strip matching quotes; leave inner content alone.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not name:
            continue
        values[name] = value
        os.environ.setdefault(name, value)
    return values


def get(*names: str) -> str:
    """First non-empty value among these environment variable names."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def mail_user(member_id: str) -> str:
    return get(f"PM_MAIL_USER_{key_for(member_id)}")


def mail_password(member_id: str) -> str:
    # Falls back to a single shared password for a shared-inbox setup.
    return get(f"PM_MAIL_PASSWORD_{key_for(member_id)}", "PM_MAIL_PASSWORD")


def doc_passwords(member_id: str) -> list[str]:
    """Statement passwords for a member. Comma-separated for brokers that differ."""
    raw = get(f"PM_DOC_PASSWORD_{key_for(member_id)}",
              f"PM_DOC_PASSWORDS_{key_for(member_id)}")
    return [part.strip() for part in raw.split(",") if part.strip()]


def shared_mailbox() -> tuple[str, str]:
    return get("PM_MAIL_USER"), get("PM_MAIL_PASSWORD")


def render(members: list[tuple[str, str, str, list[str]]],
           shared: tuple[str, str] = ("", "")) -> str:
    """Build a .env body from (member_id, name, mail_user, doc_passwords) rows."""
    lines = [
        "# LameStreet secrets. Never commit this file — .gitignore already excludes it.",
        "# Anything set in the real environment overrides what is written here.",
        "",
    ]
    user, password = shared
    if user or password:
        lines += ["# Shared inbox (only if every member reads the same mailbox)",
                  f"PM_MAIL_USER={user}", f"PM_MAIL_PASSWORD={password}", ""]

    for member_id, name, mailbox_user, passwords in members:
        suffix = key_for(member_id)
        lines.append(f"# {name}  (member id: {member_id})")
        lines.append(f"PM_MAIL_USER_{suffix}={mailbox_user}")
        lines.append(f"PM_MAIL_PASSWORD_{suffix}=")
        lines.append(f"PM_DOC_PASSWORD_{suffix}={','.join(passwords)}")
        lines.append("")

    lines += [
        "# Where the local server listens. 127.0.0.1 keeps it to this machine;",
        "# 0.0.0.0 exposes it to the network, with no login in front of it.",
        "PM_HOST=127.0.0.1",
        "PM_PORT=3002",
        "",
        "# Optional. Only used to help read a broker layout nobody has profiled yet;",
        "# never used to extract numbers from a known broker.",
        "# ANTHROPIC_API_KEY=",
        "",
    ]
    return "\n".join(lines)
