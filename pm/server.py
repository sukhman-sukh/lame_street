"""Local web server: serves the viewer and runs syncs on demand.

There is no authentication. The server binds to 127.0.0.1 by default, so it is
reachable only from this machine — which is what makes that safe. Passing
`--host 0.0.0.0` opens it to every device on the network with no login in front
of it, so only do that on a network you trust.

Syncs run on a background thread because reading a year of mail and parsing PDFs
takes longer than a browser will wait. The page polls /api/job while it runs.
Only one runs at a time, and the mail sync has a cooldown — a refresh button
anyone can hammer is how you get rate-limited by a mail provider.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, backup
from . import build as buildmod
from . import config as cfgmod
from . import ingest, manual, paths, prices
from .api_admin import router as admin_router
from .api_edit import router as edit_router
from .events import iso, now_ist
from .replay import replay
from .store import read_json, write_json

log = logging.getLogger(__name__)

# Slim containers ship no /etc/mime.types, and Python's builtin table predates
# web fonts — without these the icon fonts go out as application/octet-stream.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

MAIL_SYNC_COOLDOWN = timedelta(minutes=5)

# Generous on purpose: a year's consolidated account statement is a big PDF, and
# the mail path already accepts attachments this size.
MAX_STATEMENT = 25 * 1024 * 1024

# An export is a text file; anything much larger is not one.
MAX_UPLOAD = 5 * 1024 * 1024

app = FastAPI(title="LameStreet", docs_url=None, redoc_url=None)
app.include_router(admin_router)
app.include_router(edit_router)

# Mounted here rather than inside run(), so that importing `pm.server:app` yields a
# complete app. The reloader — and any external ASGI server — imports the module in
# a worker process and never calls run(), so anything wired up only there is simply
# missing: with this mount inside run(), a reloading server served no assets at all.
if (paths.VIEWER / "assets").exists():
    app.mount("/assets", StaticFiles(directory=paths.VIEWER / "assets"), name="assets")


# ------------------------------------------------------------------ auth gate

# Reachable without a session: the login page, the login call itself, the
# health probe, and the static assets — those are public code (the repo is
# public), never data.
_OPEN_PATHS = {"/login", "/api/login", "/healthz"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    if (not auth.enabled()
            or request.url.path in _OPEN_PATHS
            or request.url.path.startswith("/assets/")
            or auth.verify(request.cookies.get(auth.COOKIE))):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.middleware("http")
async def revalidate_viewer(request: Request, call_next):
    """Never let a browser run last week's viewer against this week's data.

    The page and its assets keep the same filenames as the code changes under
    them, so a cached copy is how someone ends up looking at a dashboard whose
    JS predates the fields it is being handed — which shows up as a feature
    simply not working, with nothing in the console to say why. `no-cache` still
    allows the ETag round-trip, so an unchanged file is a 304 and costs nothing.
    """
    response = await call_next(request)
    if request.url.path.startswith("/assets/") or request.url.path in ("/", "/login"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not auth.enabled() or auth.verify(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=302)
    return FileResponse(paths.VIEWER / "login.html")


@app.post("/api/login")
def login(payload: LoginIn, request: Request):
    client = request.client.host if request.client else "?"
    wait = auth.throttled(client)
    if wait:
        raise HTTPException(status_code=429,
                            detail=f"too many attempts — try again in {wait}s")
    if not auth.check_login(payload.username.strip(), payload.password):
        auth.record_failure(client)
        raise HTTPException(status_code=401, detail="wrong username or password")
    auth.clear_failures(client)
    response = JSONResponse({"ok": True})
    response.set_cookie(auth.COOKIE, auth.issue(), max_age=auth.SESSION_TTL,
                        httponly=True, samesite="lax", path="/",
                        secure=request.url.scheme == "https")
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response

_lock = threading.Lock()
_job: dict = {"state": "idle", "kind": None, "started_at": None,
              "finished_at": None, "message": "", "error": None}
_last_mail_sync: datetime | None = None


# ------------------------------------------------------------------ job runner

def _run_job(kind: str, fn) -> None:
    def worker():
        global _last_mail_sync
        try:
            _job.update(state="running", kind=kind, started_at=iso(now_ist()),
                        finished_at=None, message=f"{kind} started", error=None)
            message = fn()
            if kind == "sync":
                _last_mail_sync = now_ist()
                if backup.enabled():
                    # Best-effort: a failed backup must not fail the sync.
                    try:
                        message += f" · {backup.save()}"
                    except Exception as exc:
                        log.warning("backup after sync failed: %s", exc)
                        message += " · backup failed (see server log)"
            _job.update(state="done", finished_at=iso(now_ist()), message=message)
        except Exception as exc:
            log.exception("%s job failed", kind)
            _job.update(state="error", finished_at=iso(now_ist()),
                        message=f"{kind} failed", error=str(exc))
        finally:
            _lock.release()

    if not _lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=f"a {_job.get('kind')} is already running")
    threading.Thread(target=worker, daemon=True, name=f"pm-{kind}").start()


def _do_refresh() -> str:
    tickers = buildmod.collect_tickers()
    cache = prices.refresh(tickers)
    buildmod.build()
    return f"Prices updated for {cache.get('updated', 0)} of {len(tickers)} holdings"


def _do_sync(full: bool) -> str:
    cfg = cfgmod.load()
    if not cfg.mailboxes():
        # Otherwise this reports "0 new events" and looks like a working sync
        # that simply found nothing.
        raise RuntimeError("No inbox connected yet — add a Gmail address and app "
                           "password for at least one person in the Setup tab.")
    report = ingest.sync_mail(cfg, full=full)
    write_json(buildmod.LAST_SYNC, report.to_dict())
    try:
        prices.refresh(buildmod.collect_tickers())
    except Exception as exc:
        log.warning("price refresh after sync failed: %s", exc)
    buildmod.build()
    attention = len(report.needs_attention)
    parts = [f"{report.events_written} new events from {report.messages_seen} messages"]
    if attention:
        parts.append(f"{attention} document(s) need attention")
    if report.errors:
        parts.append(f"{len(report.errors)} error(s)")
    return "; ".join(parts)


# --------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    """Liveness probe for hosting platforms — point their health check here so
    a rolling deploy only takes traffic once the app actually answers."""
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(paths.VIEWER / "index.html")


@app.get("/api/dashboard")
def dashboard():
    payload = read_json(paths.DASHBOARD, default=None)
    if payload is None:
        return JSONResponse(
            {"empty": True,
             "message": "No data yet. Add someone in the Setup tab and upload their "
                        "holdings, or run a mail sync."},
            status_code=200,
        )
    if payload.get("schema") != buildmod.SCHEMA:
        # Written by an older version of this app, so it is missing fields the
        # viewer now reads — and nothing would replace it until the next sync,
        # which could be tomorrow. Re-derive it now: the event log is the source
        # of truth, so this is cheap, safe to repeat, and happens exactly once.
        log.info("dashboard.json predates schema %s — rebuilding", buildmod.SCHEMA)
        try:
            payload = buildmod.build()
        except Exception as exc:
            log.warning("could not rebuild the dashboard: %s", exc)
    return payload


@app.get("/api/activity")
def activity(offset: int = 0, limit: int = 25, key: str = "", member: str = ""):
    """One page of the log, newest first — for the feeds, which are paginated.

    dashboard.json carries only the first page or two. It is re-fetched every
    couple of minutes, and half a megabyte of history nobody has scrolled to is a
    poor trade; paging deeper comes here instead, where the whole log is.

    `key` narrows it to one company (that is what a company page's history is),
    `member` to one person.
    """
    limit = max(1, min(200, limit))
    offset = max(0, offset)

    rows = replay()["activity"]
    if key:
        rows = [a for a in rows if a.get("key") == key.strip().upper()]
    if member:
        rows = [a for a in rows if a.get("member") == member.strip()]

    names = {m.id: m.name for m in cfgmod.load().members}
    return {
        "offset": offset,
        "limit": limit,
        "total": len(rows),
        "rows": [{"ts": a.get("ts"),
                  "member": a.get("member"),
                  "member_name": names.get(a.get("member"), a.get("member")),
                  "kind": a.get("kind"),
                  "key": a.get("key"),
                  "text": a.get("text"),
                  "source": a.get("source")}
                 for a in rows[offset:offset + limit]],
    }


@app.get("/api/job")
def job():
    return _job


@app.post("/api/refresh")
def refresh():
    """Prices only: free, fast, safe to press often."""
    _run_job("refresh", _do_refresh)
    return {"started": True, "kind": "refresh"}


@app.post("/api/statement")
async def upload_statement(
    file: UploadFile = File(...),
    member: str = Form(""),
    sync_after: bool = Form(True),
):
    """Set holdings from a statement PDF, then read the mail that came after it.

    The recovery path for a month the sync missed: the document fixes the position
    on its own date, and the follow-up sync walks forward from there so every
    contract note since is applied on top.
    """
    blob = await file.read()
    if len(blob) > MAX_STATEMENT:
        raise HTTPException(413, "that file is larger than 25 MB")
    if not blob.strip():
        raise HTTPException(422, "the file is empty")

    cfg = cfgmod.load()
    result = ingest.ingest_statement(
        cfg, blob, filename=file.filename or "statement.pdf",
        member_id=member.strip() or None,
        # Rewinding without reading afterwards would leave the next ordinary sync
        # to re-download weeks of mail unannounced.
        rewind=sync_after,
    )
    if not result.get("ok"):
        raise HTTPException(422, result.get("detail") or "could not read that statement")

    buildmod.build()

    started = False
    if sync_after and cfg.mailboxes():
        try:
            # Deliberate action, so it skips the refresh-button cooldown.
            _run_job("sync", lambda: _do_sync(False))
            started = True
        except HTTPException:
            result["notes"] = list(result.get("notes", [])) + [
                "another job is already running — press Sync when it finishes"]
    return {**result, "sync_started": started}


@app.post("/api/sync-holdings")
async def sync_holdings(
    file: UploadFile | None = File(None),
    member: str = Form(""),
):
    """Sync holdings, optionally re-anchoring on a broker export first.

    With an export attached it becomes that person's latest anchor, and the mail
    sync that follows contributes only the trades after it. With nothing attached
    it is an ordinary sync, which — because an export outranks the statements that
    follow it — is already "the diffs since the last export".
    """
    cfg = cfgmod.load()
    result: dict = {}

    blob = await file.read() if file is not None else b""
    if blob:
        member_id = member.strip()
        if not member_id:
            raise HTTPException(422, "choose whose export this is")
        if not cfg.member(member_id):
            raise HTTPException(404, "no such member")
        if len(blob) > MAX_UPLOAD:
            raise HTTPException(413, "that file is larger than 5 MB — it isn't a holdings export")
        result = manual.record_holdings_csv(
            member_id, blob, filename=(file.filename or "upload.csv"))
        if not result.get("ok"):
            raise HTTPException(422, result.get("detail") or "could not read that export")
        buildmod.build()

    if not cfg.mailboxes():
        if not result:
            raise HTTPException(400, "No inbox connected yet — add a Gmail address and app "
                                     "password for at least one person in the Setup tab.")
        return {**result, "sync_started": False,
                "notes": list(result.get("notes", [])) + ["no inbox connected, so nothing "
                                                          "was read after the export"]}

    # An attached export makes this a deliberate action, so it skips the cooldown
    # that exists to stop the refresh button being hammered.
    if not blob and _last_mail_sync and now_ist() - _last_mail_sync < MAIL_SYNC_COOLDOWN:
        wait = MAIL_SYNC_COOLDOWN - (now_ist() - _last_mail_sync)
        raise HTTPException(
            status_code=429,
            detail=f"mail was synced {int(wait.total_seconds() // 60) + 1} minute(s) ago — "
                   "holdings change slowly, prices are the thing worth refreshing")
    _run_job("sync", lambda: _do_sync(False))
    return {**result, "sync_started": True}


@app.post("/api/sync")
def sync(full: bool = False):
    """Read mail and re-derive holdings. Rate-limited."""
    if _last_mail_sync and now_ist() - _last_mail_sync < MAIL_SYNC_COOLDOWN and not full:
        wait = MAIL_SYNC_COOLDOWN - (now_ist() - _last_mail_sync)
        raise HTTPException(
            status_code=429,
            detail=f"mail was synced {int(wait.total_seconds() // 60) + 1} minute(s) ago — "
                   "holdings change slowly, prices are the thing worth refreshing",
        )
    _run_job("sync", lambda: _do_sync(full))
    return {"started": True, "kind": "sync"}


# -------------------------------------------------------------- self-syncing

def _start_scheduler() -> None:
    """Sync without cron: PM_SYNC_ON_START=1 reads mail as the server comes
    up, PM_SYNC_EVERY_HOURS=24 repeats it. Meant for containers and hosted
    machines that have no launchd/cron of their own — on a Mac, `pm schedule`
    is still the better tool. A failed run is logged and retried at the next
    interval; the UI buttons keep working throughout."""
    on_start = os.environ.get("PM_SYNC_ON_START", "").strip().lower() in ("1", "true", "yes")
    try:
        every = float(os.environ.get("PM_SYNC_EVERY_HOURS", "").strip() or 0)
    except ValueError:
        log.warning("PM_SYNC_EVERY_HOURS is not a number; periodic sync disabled")
        every = 0.0
    if not on_start and every <= 0:
        return

    def fire():
        try:
            _run_job("sync", lambda: _do_sync(False))
        except HTTPException:
            log.info("scheduled sync skipped — another job is already running")

    def beat():
        if on_start:
            time.sleep(3)  # let uvicorn come up so /healthz answers during the sync
            fire()
        while every > 0:
            time.sleep(every * 3600)
            fire()

    threading.Thread(target=beat, daemon=True, name="pm-scheduler").start()


def run(host: str = "127.0.0.1", port: int = 3002, reload: bool = False) -> None:
    """Serve the viewer.

    `reload` restarts the workers whenever a Python file under pm/ changes. It is
    for development only: it costs a file watcher, and the scheduler is left off
    under it because the supervising process does not serve requests — a timed sync
    firing there would write to the same data directory as the worker.

    Static assets are always read from disk per request, so the viewer's JS and CSS
    are live either way; it is only Python that a running server holds in memory.
    """
    import uvicorn

    if backup.enabled():
        try:
            restored = backup.restore_if_fresh_host()
        except Exception as exc:
            # A fresh host that cannot restore should say so loudly and stop:
            # silently starting empty would look like the data is gone.
            raise SystemExit(f"backup restore failed: {exc}")
        if restored:
            # The dashboard is derived, so it is not in the backup. Rebuild it
            # from the restored events now — otherwise the page reads "no data
            # yet" until the first sync finishes, which looks like the restore
            # failed when it actually worked.
            try:
                paths.ensure_dirs()
                buildmod.build()
                log.info("rebuilt dashboard from restored events")
            except Exception as exc:
                log.warning("could not rebuild dashboard after restore: %s", exc)

    paths.ensure_dirs()
    if not reload:
        _start_scheduler()
    if host not in ("127.0.0.1", "localhost", "::1") and not auth.enabled():
        raise SystemExit(
            f"refusing to bind {host}: that exposes the dashboard, the admin API and "
            "the stored mail credentials to the whole network with no login in front "
            "of them. Set PM_AUTH_USER and PM_AUTH_PASSWORD in .env first, or serve "
            "on 127.0.0.1.")

    print("\n  LameStreet")
    print(f"  → http://localhost:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠ bound to {host} — reachable from other devices on the network; "
              "login required (PM_AUTH_USER)")
    if not auth.enabled():
        print("  · no login configured (PM_AUTH_USER / PM_AUTH_PASSWORD unset) — "
              "fine on 127.0.0.1")
    if reload:
        print("  · reloading on changes under pm/ — scheduled syncs are off")
    print()

    if reload:
        # The reloader runs the app in a worker it starts itself, so it needs an
        # import string; handed the app object it can only warn and carry on
        # without reloading, which looks like it worked.
        uvicorn.run("pm.server:app", host=host, port=port, log_level="warning",
                    reload=True, reload_dirs=[str(Path(__file__).resolve().parent)])
    else:
        uvicorn.run(app, host=host, port=port, log_level="warning")
