"""File primitives: atomic JSON writes and append-only JSONL.

Nothing here knows about portfolios. It just guarantees that a crash mid-write
never leaves a half-written file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Write via a temp file in the same directory, then rename.

    rename() is atomic on the same filesystem, so readers either see the old
    file or the new one — never a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    """Same guarantee as write_json, for a file that is meant to stay readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, rows: list[dict]) -> int:
    """Append rows to a JSONL file and fsync. Returns how many were written."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(rows)


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield rows, skipping any corrupt trailing line (a crash mid-append)."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def secure_write(path: Path, payload: Any) -> None:
    """write_json, then lock the file down to owner-only (it holds secrets)."""
    write_json(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass
