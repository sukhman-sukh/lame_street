"""Optional login in front of the dashboard.

The server is loginless by design when it listens on 127.0.0.1 — the OS user
already owns the machine. The moment it listens on a network interface, every
route (holdings, admin API, stored mail credentials) is one HTTP request away
from anyone on the LAN. Setting PM_AUTH_USER and PM_AUTH_PASSWORD in .env turns
on a session gate; `pm serve` refuses to bind a non-loopback host without it.

Sessions are an HMAC-signed cookie: `expiry.signature`, keyed by a random
secret persisted under data/state/. No dependency, nothing to configure, and
restarting the server does not log anyone out. Credentials stay in .env, which
is chmod 600 and gitignored — same treatment as the mail passwords.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import env, paths

SECRET_FILE = paths.STATE / "auth-secret"
SESSION_TTL = 30 * 24 * 3600  # seconds; re-login once a month
COOKIE = "pm_session"

# Failed-login throttle, per client address. Five misses buys a minute out —
# enough to make brute force pointless, invisible to a person mistyping twice.
_MAX_FAILURES = 5
_LOCKOUT = 60.0
_failures: dict[str, list[float]] = {}


def credentials() -> tuple[str, str]:
    return env.get("PM_AUTH_USER"), env.get("PM_AUTH_PASSWORD")


def enabled() -> bool:
    user, password = credentials()
    return bool(user and password)


def _secret() -> bytes:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    paths.ensure_dirs()
    value = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(value)
    SECRET_FILE.chmod(0o600)
    return value


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def issue() -> str:
    """A session token: expiry timestamp, signed."""
    expires = str(int(time.time()) + SESSION_TTL)
    return f"{expires}.{_sign(expires)}"


def verify(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, _, signature = token.partition(".")
    if not hmac.compare_digest(signature, _sign(expires)):
        return False
    try:
        return int(expires) > time.time()
    except ValueError:
        return False


def check_login(user: str, password: str) -> bool:
    want_user, want_password = credentials()
    # Compare both fields unconditionally, in constant time — the response
    # must not reveal which of the two was wrong, even by timing.
    user_ok = hmac.compare_digest(user.encode(), want_user.encode())
    password_ok = hmac.compare_digest(password.encode(), want_password.encode())
    return enabled() and user_ok and password_ok


def throttled(client: str) -> int:
    """Seconds this client must still wait, 0 if allowed to try."""
    now = time.time()
    recent = [t for t in _failures.get(client, []) if now - t < _LOCKOUT]
    _failures[client] = recent
    if len(recent) >= _MAX_FAILURES:
        return int(_LOCKOUT - (now - recent[0])) + 1
    return 0


def record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.time())


def clear_failures(client: str) -> None:
    _failures.pop(client, None)
