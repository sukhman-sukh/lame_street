"""Encrypted state backup to a private GitHub repo, restore on boot.

Free container hosts wipe the disk on every restart. Almost everything here
survives that by design — raw mail lives in Gmail, prices come from Yahoo,
the dashboard is derived — but four things exist nowhere else: config.json,
the event log (which carries the one-time holdings imports and their cost
basis), the manual ISIN mappings in data/state, and any document handed to
the app by hand rather than found in a mailbox. Together they are a few
megabytes.

So: tar those, encrypt them, and PUT the single blob into a private GitHub
repo via the contents API after every successful sync. On boot, a server that
finds no config.json pulls the blob back and unpacks it. The host's disk
becomes a cache; GitHub becomes the durable copy.

Encryption is Fernet (AES-128-CBC + HMAC) under a key derived from
PM_BACKUP_KEY with scrypt and a random salt, so the repo's contents are
opaque even to someone holding the repo and the token. Needs:

    PM_BACKUP_REPO=owner/name        private repo, created empty
    PM_BACKUP_TOKEN=github_pat_...   fine-grained token, contents read/write
    PM_BACKUP_KEY=any long phrase    the encryption secret — losing it means
                                     the backup is unreadable, so keep it
"""
from __future__ import annotations

import base64
import io
import logging
import os
import tarfile
import time

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import env, paths
from .store import read_json

log = logging.getLogger(__name__)

BLOB_PATH = "lamestreet-backup.tar.gz.enc"
_SALT_LEN = 16

# What must survive; everything else re-derives from Gmail/NSE/Yahoo. Prices
# are re-fetchable but weigh a few KB, and carrying them means a restored host
# can render a complete dashboard before its first price refresh.
_INCLUDE = ("config.json", "data/events", "data/snapshots", "data/state", "data/prices")

# data/raw is left out because it is re-fetchable — it is a cache of Gmail, and
# ninety megabytes of it. That holds for everything the sync downloaded, and for
# nothing that was handed over by hand: an uploaded export never went through a
# mailbox, and a forwarded statement arrived from an address the sync filters do
# not match. Those exist on this disk and nowhere else, so they travel with the
# backup. They are the documents the holdings are anchored to, and they are
# kilobytes.
_RAW = "data/raw"


def settings() -> tuple[str, str, str]:
    return (env.get("PM_BACKUP_REPO"), env.get("PM_BACKUP_TOKEN"),
            env.get("PM_BACKUP_KEY"))


def enabled() -> bool:
    return all(settings())


# ------------------------------------------------------------------- crypto

def _fernet(passphrase: str, salt: bytes) -> Fernet:
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def _encrypt(passphrase: str, payload: bytes) -> bytes:
    # Pure ASCII on purpose: hex salt, dot, Fernet token (itself base64).
    # GitHub's raw content endpoint mangles binary files, text passes through.
    salt = os.urandom(_SALT_LEN)
    return salt.hex().encode() + b"." + _fernet(passphrase, salt).encrypt(payload)


def _decrypt(passphrase: str, blob: bytes) -> bytes:
    salt_hex, _, body = blob.partition(b".")
    try:
        salt = bytes.fromhex(salt_hex.decode())
    except ValueError:
        raise InvalidToken
    return _fernet(passphrase, salt).decrypt(body)


# ------------------------------------------------------------------ tarball

def uploaded_documents() -> list[str]:
    """Archived documents that came from a person rather than a mailbox.

    Recognised by the `upload` flag their meta.json carries, which is written when
    a statement or an export is handed to the app directly.
    """
    root = paths.ROOT / _RAW
    if not root.exists():
        return []
    found = []
    for meta in sorted(root.glob("*/*/meta.json")):
        try:
            if (read_json(meta) or {}).get("upload"):
                found.append(str(meta.parent.relative_to(paths.ROOT)))
        except Exception:
            continue
    return found


def _pack() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel in list(_INCLUDE) + uploaded_documents():
            target = paths.ROOT / rel
            if target.exists():
                tar.add(target, arcname=rel)
    return buffer.getvalue()


def _unpack(payload: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        # Refuse anything that would land outside ROOT (hostile tarball).
        root = paths.ROOT.resolve()
        for member in tar.getmembers():
            destination = (root / member.name).resolve()
            if not destination.is_relative_to(root):
                raise RuntimeError(f"backup contains unsafe path {member.name!r}")
        tar.extractall(paths.ROOT)


# ------------------------------------------------------------- GitHub calls

def _api(repo: str, path: str = "") -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}"


def _headers(token: str, raw: bool = False) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _current_sha(repo: str, token: str) -> str | None:
    """SHA of the existing blob, from the directory listing (which, unlike a
    direct GET, has no size ceiling)."""
    res = requests.get(_api(repo), headers=_headers(token), timeout=30)
    if res.status_code == 404:
        return None
    res.raise_for_status()
    for entry in res.json():
        if entry.get("name") == BLOB_PATH:
            return entry.get("sha")
    return None


def save() -> str:
    """Encrypt and push the current state. Returns a short human summary."""
    repo, token, key = settings()
    blob = _encrypt(key, _pack())
    body = {
        "message": f"lamestreet backup {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "content": base64.b64encode(blob).decode(),
    }
    sha = _current_sha(repo, token)
    if sha:
        body["sha"] = sha
    res = requests.put(_api(repo, BLOB_PATH), json=body,
                       headers=_headers(token), timeout=120)
    res.raise_for_status()
    return f"backed up {len(blob) / 1e6:.1f} MB to {repo}"


def restore() -> bool:
    """Pull and unpack the backup. Returns True if state was restored."""
    repo, token, key = settings()
    res = requests.get(_api(repo, BLOB_PATH),
                       headers=_headers(token, raw=True), timeout=120)
    if res.status_code == 404:
        log.info("no backup found in %s — starting fresh", repo)
        return False
    res.raise_for_status()
    try:
        payload = _decrypt(key, res.content)
    except InvalidToken:
        raise RuntimeError(
            "backup exists but PM_BACKUP_KEY cannot decrypt it — wrong key?")
    _unpack(payload)
    log.info("restored %.1f MB of state from %s", len(payload) / 1e6, repo)
    return True


def restore_if_fresh_host() -> bool:
    """Boot-time hook: only a machine with no config restores, so a backup
    can never overwrite live local state."""
    if not enabled() or paths.CONFIG.exists():
        return False
    return restore()
