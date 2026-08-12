"""Export a self-contained static copy of the dashboard.

The viewer already falls back from /api/dashboard to a dashboard.json sitting
beside it, so a plain folder of files is a working dashboard — deployable to
Vercel, Netlify, GitHub Pages or a USB stick, with no server involved.

What it deliberately cannot do is refresh itself. Collection needs a real
filesystem, a browser session and a mailbox; serverless hosts give you none of
those. So the export is a snapshot: the collector runs at home, and this is the
read-only view of what it found.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import paths
from .store import read_json, write_json

VERCEL_CONFIG = {
    "cleanUrls": True,
    "headers": [
        {
            "source": "/dashboard.json",
            # The data is only as fresh as the last collector run; don't let a
            # CDN serve a stale copy for longer than that.
            "headers": [{"key": "Cache-Control", "value": "public, max-age=0, must-revalidate"}],
        }
    ],
}


def export(out_dir: Path, *, include_vercel: bool = True) -> dict:
    """Copy viewer + dashboard.json into `out_dir`. Returns a small summary."""
    payload = read_json(paths.DASHBOARD, default=None)
    if payload is None:
        raise FileNotFoundError(
            "no dashboard.json yet — run `python -m pm build` first"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(paths.VIEWER / "index.html", out_dir / "index.html")
    assets_src, assets_dst = paths.VIEWER / "assets", out_dir / "assets"
    if assets_dst.exists():
        shutil.rmtree(assets_dst)
    shutil.copytree(assets_src, assets_dst)

    write_json(out_dir / "dashboard.json", payload)
    if include_vercel:
        write_json(out_dir / "vercel.json", VERCEL_CONFIG)

    return {
        "path": str(out_dir),
        "generated_at": payload.get("generated_at"),
        "members": len(payload.get("members", [])),
        "positions": len(payload.get("consolidated", [])),
    }
