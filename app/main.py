"""PUMA - Program Update Monitoring Assistant
FastAPI app: session auth, dashboard.

Run (from inside app/, after activating the venv):
    uvicorn main:app --host 127.0.0.1 --port 8123
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import re
import socket
import sys
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Make sibling modules (models, versioning, adapters) importable regardless
# of the process's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bleach
import markdown as markdown_lib
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from PIL import Image
from sqlmodel import Session, SQLModel, create_engine, select, func
from starlette.middleware.sessions import SessionMiddleware

import adapters
import backups
import checker
from emailer import SMTP_CONFIGURED, send_email
from models import (
    BackupRun,
    BackupStatus,
    BackupTrigger,
    CheckRun,
    CheckTrigger,
    EmailLog,
    MonitoredApp,
    Notification,
    ReleaseObservation,
    Setting,
    User,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DB_PATH = os.environ.get("PUMA_DB_PATH", "/var/lib/puma/appmonitor.db")
SECRET_KEY = os.environ.get("PUMA_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "PUMA_SECRET_KEY is not set. Add it to /etc/puma/puma.env "
        "(generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\")"
    )

# Uploaded app logos live under the same writable directory as the
# database (systemd's ProtectSystem=strict only allows writes under
# /var/lib/puma at runtime - the app's own static/ directory under
# /var/www is read-only in production), served through a dedicated route
# rather than the regular StaticFiles mount for that same reason.
APP_LOGOS_DIR = Path(DB_PATH).parent / "app_logos"
APP_LOGOS_DIR.mkdir(parents=True, exist_ok=True)
MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024  # 2MB
ALLOWED_LOGO_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}

# A browser can never re-populate a <input type="file"> after a page
# reload - it's a security restriction, not a bug we can work around
# client-side. So if the Add/Edit Application form gets re-rendered
# because of some OTHER validation error (duplicate name, untrusted
# host, a missing required source field...), a logo the user already
# picked would otherwise vanish with no indication, and saving on the
# retry silently goes out without it. To fix that, a logo submitted
# alongside a failed submission is staged here under a random token;
# the re-rendered form carries that token in a hidden field, and the
# retry picks the staged file back up automatically unless the user
# chooses a different one. Cleaned up once it's actually used, or
# opportunistically swept if abandoned (see cleanup_stale_pending_logos).
PENDING_LOGOS_DIR = Path(DB_PATH).parent / "pending_logos"
PENDING_LOGOS_DIR.mkdir(parents=True, exist_ok=True)
PENDING_LOGO_MAX_AGE_SECONDS = 60 * 60  # 1 hour

# Everything is stored/computed in UTC internally (datetime.now(timezone.utc)
# throughout). This is only for *display* - converting absolute timestamps
# to the team's actual timezone before formatting, so dates/times shown in
# the UI match the wall clock instead of raw UTC. Configurable via env var
# in case this ever needs to run for a team elsewhere.
DISPLAY_TZ = ZoneInfo(os.environ.get("PUMA_DISPLAY_TZ", "Asia/Manila"))

# When this worker process actually started - used for the "uptime" figure
# on System Info. Module-level so it's set once at import time, before any
# request handling begins.
APP_STARTED_AT = datetime.now(timezone.utc)

engine = create_engine(f"sqlite:///{DB_PATH}")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="PUMA")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Separate sub-app for the actual JSON API, given its own OpenAPI/Swagger
# docs. Deliberately not just adding docs to the main `app`: that would
# mix the one real API endpoint into a Swagger page alongside every HTML
# page route (dozens of them) with no clean way to hide them all short of
# marking each individually. A mounted sub-app gets its own independent
# route list and schema, so only endpoints actually meant to be an API
# ever show up here - future API endpoints just get added to api_app,
# no per-page bookkeeping required to keep them out of it.
#
# Mounted at /api/v1 specifically (not the shorter /api) because
# /api/backup-status and /api/email-events already exist as internal
# polling endpoints on the main app - Starlette's mount matches by path
# prefix, so mounting at the broader /api would have silently shadowed
# those two. /api/v1/dashboard keeps its exact existing external URL
# either way (mount prefix + the sub-app's own /dashboard route).
api_app = FastAPI(
    title="PUMA API",
    description=(
        "Read-only status API. Deliberately unauthenticated by design - "
        "relies entirely on network-level restriction (e.g. a firewall "
        "rule) rather than a login, so treat reachability to this port "
        "as the actual access control. Never returns credentials or "
        "tokens, even though those live in the same database rows this "
        "reads from."
    ),
    version="1.0.0",
)
app.mount("/api/v1", api_app)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

scheduler_logger = logging.getLogger("puma.scheduler")
logger = logging.getLogger("puma.main")


def _scheduled_check_due(
    last_full_check: Optional[datetime], interval_minutes: int, now: datetime
) -> bool:
    """Pure decision logic, kept separate from the loop/DB/asyncio plumbing
    so it can be tested directly without needing to run an actual
    background task. Deliberately looks at the last FULL check regardless
    of whether it was triggered manually or by this same scheduler - a
    manual Check All Now legitimately resets the clock too, no reason to
    run again five minutes later just because the scheduler doesn't know
    a human already did it."""
    if last_full_check is None:
        return True  # never checked at all - due immediately
    if last_full_check.tzinfo is None:
        last_full_check = last_full_check.replace(tzinfo=timezone.utc)
    elapsed_minutes = (now - last_full_check).total_seconds() / 60
    return elapsed_minutes >= interval_minutes


def _run_backup_sync(app_id: int, trigger: BackupTrigger) -> None:
    """Runs one app's backup with its own fresh DB session - never handed a
    Session that was opened on another thread or coroutine, since SQLAlchemy
    sessions aren't safe to share across threads. This is the one function
    both the manual-trigger route (via BackgroundTasks, off the request
    entirely) and the scheduler (via asyncio.to_thread, off the event loop)
    call, so a Moodle-sized backup can run for minutes without either
    hanging a browser tab (504) or freezing every other page load for
    every other user while it runs - a real backup is genuinely slow
    (tar-ing a data directory, pg_dump), and nothing about that should ever
    block anything other than itself."""
    with Session(engine) as session:
        try:
            checker.run_backup(session, app_id, trigger)
        except ValueError:
            logger.warning("Backup requested for app %s with no valid strategy", app_id)
        except Exception:
            # Mirrors checker.run_backup's own top-level catch - belt and
            # suspenders, since this also runs completely detached from
            # any request or the scheduler's own try/except once it's
            # handed off to a background thread.
            logger.exception("Backup failed for app %s", app_id)


async def scheduled_check_loop() -> None:
    """Runs for the lifetime of the process, checking once a minute
    whether a scheduled sweep is due. There was previously no scheduler
    at all despite the Auto Check toggle and interval setting existing in
    Settings - they were saved to the database but nothing ever read them
    to actually trigger anything."""
    while True:
        await asyncio.sleep(60)
        try:
            with Session(engine) as session:
                enabled = get_setting(session, "auto_check_enabled", "true") == "true"
                if not enabled:
                    continue
                interval_raw = get_setting(session, "auto_check_interval_minutes", "360")
                interval_minutes = int(interval_raw) if interval_raw.isdigit() else 360

                last_full_run = session.exec(
                    select(CheckRun)
                    .where(CheckRun.is_full_check == True)  # noqa: E712
                    .order_by(CheckRun.started_at.desc())
                    .limit(1)
                ).first()
                last_full_check = last_full_run.started_at if last_full_run else None

                if _scheduled_check_due(last_full_check, interval_minutes, datetime.now(timezone.utc)):
                    scheduler_logger.info(
                        "Scheduled check due (interval=%s min, last=%s) - running now",
                        interval_minutes, last_full_check,
                    )
                    checker.run_check(session, CheckTrigger.scheduled)
        except Exception:
            # A single bad iteration must never kill the loop for the rest
            # of the process's life - that would silently turn Auto Check
            # back off with no way to notice short of restarting the app.
            scheduler_logger.exception("Scheduled check loop iteration failed")


async def scheduled_backup_loop() -> None:
    """Runs for the lifetime of the process, checking once a minute for
    apps whose own backup schedule is due. Backups are scheduled per-app
    (backup_schedule_enabled + backup_interval_minutes on MonitoredApp),
    not by one global setting like Auto Check - different apps warrant
    very different backup cadences (a fast-moving app's daily database
    vs. a slow one's weekly snapshot), so there's no one sensible global
    interval to offer instead.

    The due-apps query runs on the event loop (fast, a single SELECT),
    but each actual backup runs via asyncio.to_thread - a real backup can
    take minutes (tar-ing a data directory, pg_dump), and that's a
    blocking, synchronous operation under the hood (subprocess.run).
    Calling it directly in this coroutine would freeze the entire app -
    every page load, every login, for every user - for as long as the
    backup takes. to_thread hands it to a worker thread and awaits there,
    so the event loop stays free to keep serving everyone else."""
    while True:
        await asyncio.sleep(60)
        try:
            with Session(engine) as session:
                due_apps = session.exec(
                    select(MonitoredApp)
                    .where(MonitoredApp.backup_schedule_enabled == True)  # noqa: E712
                    .where(MonitoredApp.backup_strategy.is_not(None))
                ).all()
                now = datetime.now(timezone.utc)
                due = [
                    (app_row.id, app_row.name)
                    for app_row in due_apps
                    if _scheduled_check_due(app_row.last_scheduled_backup_at, app_row.backup_interval_minutes, now)
                ]
            # session closed before any slow work starts - it must not sit
            # open (or be passed across threads) for however long these take
            for app_id, app_name in due:
                scheduler_logger.info("Scheduled backup due for %s - running now", app_name)
                try:
                    await asyncio.to_thread(_run_backup_sync, app_id, BackupTrigger.scheduled)
                except Exception:
                    # One app's backup failing (bad config, source
                    # temporarily unreachable, disk full) must never
                    # stop the rest of the due apps from getting
                    # their turn in the same sweep.
                    scheduler_logger.exception("Scheduled backup failed for %s", app_name)
        except Exception:
            scheduler_logger.exception("Scheduled backup loop iteration failed")


def render(
    request: Request,
    template_name: str,
    context: dict,
    status_code: int = 200,
) -> HTMLResponse:
    """Wraps templates.TemplateResponse and injects context every page
    needs regardless of which route renders it - right now just
    unread_notifications for the topbar bell badge, which was previously
    only computed inside the Dashboard route and silently missing
    everywhere else. Routing every render through here means adding a
    future "every page needs this" value can't be forgotten route by
    route the way this one was.

    status_code exists because Starlette's own TemplateResponse supports
    it (e.g. returning 401 on a failed login, 400 on a rejected form) -
    dropping it here would silently 500 on every call site that relies
    on it, which is exactly what happened until this was caught."""
    with Session(engine) as session:
        unread = session.exec(
            select(func.count()).select_from(Notification).where(
                Notification.read == False  # noqa: E712
            )
        ).one()
    merged = {**context, "unread_notifications": unread}
    return templates.TemplateResponse(
        request, template_name, merged, status_code=status_code
    )


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def get_setting(session: Session, key: str, default: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = Setting(key=key, value=value)
    session.add(row)


def get_trusted_hosts(session: Session) -> list[str]:
    raw = get_setting(session, "trusted_hosts", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def get_dashboard_hidden_app_ids(session: Session) -> set[int]:
    """The set of app IDs explicitly hidden from the Dashboard's table -
    a blocklist, not an allowlist. Anything NOT in this set shows by
    default, including apps added after this setting was last saved -
    the opposite of storing "visible" IDs, which would require someone
    to remember to go add every new app to the list or it silently
    never appears."""
    raw = get_setting(session, "dashboard_hidden_app_ids", "")
    if not raw.strip():
        return set()
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


SAFE_REDIRECT_PATHS = {"/", "/applications", "/backups", "/procedures"}
_SAFE_REDIRECT_PATH_PATTERNS = [
    re.compile(r"^/procedures/\d+$"),  # per-app Procedures detail page
]


def safe_redirect_target(next_value: Optional[str]) -> str:
    """Only ever redirect to a known internal page - never trust an
    arbitrary value from a form field as a redirect target (open-redirect
    risk). Allows a query string on top of a whitelisted path (e.g.
    "/backups?app_id=5" from the Backups page's app filter) as long as
    the path itself - not the query - is one of SAFE_REDIRECT_PATHS, and
    the value never specifies a scheme or host (which would make it an
    off-site redirect rather than a relative in-app one).

    A handful of pages are per-app (their path includes that app's own
    id, e.g. /procedures/5), so a fixed set of literal strings can never
    cover them - _SAFE_REDIRECT_PATH_PATTERNS matches those by shape
    instead, restricted to digits only so it can't be widened into an
    actual open redirect."""
    if not next_value:
        return "/"
    parsed = urlparse(next_value)
    if parsed.scheme or parsed.netloc:
        return "/"
    if parsed.path in SAFE_REDIRECT_PATHS:
        return next_value
    if any(p.match(parsed.path) for p in _SAFE_REDIRECT_PATH_PATTERNS):
        return next_value
    return "/"


_LOGO_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def save_uploaded_logo(upload, app_slug: str) -> str:
    """Validates an uploaded app-logo file and saves it under
    APP_LOGOS_DIR with a safe, collision-proof filename (never the
    user-supplied one - avoids any path weirdness entirely, not just
    sanitizing it). Returns the filename to store on the app. Raises
    ValueError with a plain, user-facing message on any validation
    failure - the caller turns that straight into a form error."""
    content_type = (upload.content_type or "").lower()
    if content_type not in _LOGO_EXT_BY_CONTENT_TYPE:
        raise ValueError(
            f"Unsupported image type ({content_type or 'unknown'}). "
            "Use PNG, JPEG, GIF, WebP, or SVG."
        )

    contents = upload.file.read()
    if not contents:
        raise ValueError("The uploaded logo file is empty.")
    if len(contents) > MAX_LOGO_SIZE_BYTES:
        raise ValueError(
            f"That logo is too large ({len(contents) // 1024}KB) - "
            f"max size is {MAX_LOGO_SIZE_BYTES // 1024}KB."
        )

    # Actually verify it's a genuine image rather than trusting the
    # content-type header, which the browser sets client-side and a
    # crafted request could spoof. PIL can't parse SVG (it's XML, not a
    # raster format), so that one's skipped here - the content-type
    # check above and the size cap are still real limits for SVGs.
    if content_type != "image/svg+xml":
        try:
            img = Image.open(io.BytesIO(contents))
            img.verify()
        except Exception:
            raise ValueError("That file doesn't appear to be a valid image.")

    safe_ext = _LOGO_EXT_BY_CONTENT_TYPE[content_type]
    safe_filename = f"{app_slug}-{uuid.uuid4().hex[:8]}{safe_ext}"
    (APP_LOGOS_DIR / safe_filename).write_bytes(contents)
    return safe_filename


def cleanup_stale_pending_logos() -> None:
    """Best-effort sweep of staged logos nobody ever came back to finish
    submitting (e.g. they fixed the error in a different tab, or just gave
    up). Cheap enough to run inline whenever we stage a new one."""
    cutoff = datetime.now(timezone.utc).timestamp() - PENDING_LOGO_MAX_AGE_SECONDS
    try:
        for f in PENDING_LOGOS_DIR.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def stage_pending_logo(upload) -> str:
    """Same validation as save_uploaded_logo, but parks the file under
    PENDING_LOGOS_DIR instead of committing it as a real app logo yet.
    Returns an opaque token (the staged filename) to round-trip through
    a hidden form field. Raises ValueError on the same conditions
    save_uploaded_logo does."""
    content_type = (upload.content_type or "").lower()
    if content_type not in _LOGO_EXT_BY_CONTENT_TYPE:
        raise ValueError(
            f"Unsupported image type ({content_type or 'unknown'}). "
            "Use PNG, JPEG, GIF, WebP, or SVG."
        )

    contents = upload.file.read()
    if not contents:
        raise ValueError("The uploaded logo file is empty.")
    if len(contents) > MAX_LOGO_SIZE_BYTES:
        raise ValueError(
            f"That logo is too large ({len(contents) // 1024}KB) - "
            f"max size is {MAX_LOGO_SIZE_BYTES // 1024}KB."
        )
    if content_type != "image/svg+xml":
        try:
            img = Image.open(io.BytesIO(contents))
            img.verify()
        except Exception:
            raise ValueError("That file doesn't appear to be a valid image.")

    cleanup_stale_pending_logos()
    safe_ext = _LOGO_EXT_BY_CONTENT_TYPE[content_type]
    token = f"{uuid.uuid4().hex}{safe_ext}"
    (PENDING_LOGOS_DIR / token).write_bytes(contents)
    return token


def load_pending_logo(token: str) -> Optional[tuple[bytes, str]]:
    """Reads back a staged logo by token. Returns (contents, content_type)
    or None if the token is missing/invalid/expired - callers should treat
    that the same as no logo having been provided at all, rather than as
    an error, since it just means the staged copy aged out."""
    if not token or "/" in token or "\\" in token or ".." in token:
        return None
    path = (PENDING_LOGOS_DIR / token).resolve()
    if PENDING_LOGOS_DIR.resolve() not in path.parents or not path.is_file():
        return None
    ext = path.suffix.lower()
    content_type = next(
        (ct for ct, e in _LOGO_EXT_BY_CONTENT_TYPE.items() if e == ext), None
    )
    if not content_type:
        return None
    return path.read_bytes(), content_type


def discard_pending_logo(token: Optional[str]) -> None:
    """Best-effort cleanup of a staged file once it's been used or is no
    longer needed - mirrors delete_logo_file's caution about never letting
    cleanup failure block the actual request."""
    if not token or "/" in token or "\\" in token or ".." in token:
        return
    try:
        path = PENDING_LOGOS_DIR / token
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def save_logo_bytes(contents: bytes, content_type: str, app_slug: str) -> str:
    """Commits already-validated logo bytes (e.g. loaded back from a
    staged pending upload) as a real app logo, the same way the tail end
    of save_uploaded_logo does for a fresh upload."""
    safe_ext = _LOGO_EXT_BY_CONTENT_TYPE[content_type]
    safe_filename = f"{app_slug}-{uuid.uuid4().hex[:8]}{safe_ext}"
    (APP_LOGOS_DIR / safe_filename).write_bytes(contents)
    return safe_filename


def delete_logo_file(filename: Optional[str]) -> None:
    """Best-effort cleanup - a failure here shouldn't block the actual
    request (a logo replace/removal/app delete) from completing."""
    if not filename:
        return
    try:
        path = APP_LOGOS_DIR / filename
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "app"


def extract_target_host(source_type: str, config: dict) -> Optional[str]:
    """Which external host would this source actually contact?
    github_releases/github_tags are hardcoded to api.github.com, so they
    need no check. Anything that takes a user-supplied URL does."""
    url = None
    if source_type in ("gitlab_releases", "glpi_rest_api", "stanza_api"):
        url = config.get("base_url")
    elif source_type in ("atom_feed", "json_api", "html_regex", "http_header"):
        url = config.get("url")
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def time_ago(value: Optional[datetime]) -> str:
    if not value:
        return "Never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Just Now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


templates.env.filters["timeago"] = time_ago


def seconds_ago(value: Optional[datetime]) -> str:
    """Plain integer seconds elapsed - used to drive the client-side live
    ticker via pure arithmetic (base + elapsed_ticks) instead of comparing
    a server date string against the browser's own clock on every tick.
    Comparing clocks client-side is exactly the kind of thing that can go
    wrong in hard-to-diagnose ways; this sidesteps that class of bug
    entirely by never parsing or comparing a Date in JS at all."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    return str(max(0, int(delta.total_seconds())))


templates.env.filters["secondsago"] = seconds_ago


def short_date(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TZ).strftime("%b %-d, %Y")


templates.env.filters["shortdate"] = short_date


def local_datetime(value: Optional[datetime]) -> str:
    """Full date + time, converted from UTC (how everything is stored) to
    the display timezone - without this, absolute timestamps render as
    raw UTC labeled as if they were local time, off by a fixed offset
    from what's actually on the wall clock (Manila is UTC+8, so this was
    showing times 8 hours behind)."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TZ).strftime("%b %d, %Y %I:%M %p")


templates.env.filters["localdatetime"] = local_datetime

# Colored badge per app row, mirroring the mockup's per-app icon colors.
# Known apps get a thoughtful, on-brand color; anything else gets a
# stable color chosen from its slug so it's at least consistent run to run.
_BADGE_COLORS = {
    "moodle": "bg-orange-500",
    "glpi": "bg-sky-600",
    "xwiki": "bg-indigo-500",
    "shelf": "bg-emerald-500",
    "gitlab": "bg-orange-600",
}
_BADGE_FALLBACK = [
    "bg-puma-primary", "bg-purple-500", "bg-blue-500",
    "bg-emerald-500", "bg-amber-500", "bg-slate-600",
]


def app_badge_color(slug: str) -> str:
    if slug in _BADGE_COLORS:
        return _BADGE_COLORS[slug]
    stable_index = zlib.crc32(slug.encode()) % len(_BADGE_FALLBACK)
    return _BADGE_FALLBACK[stable_index]


templates.env.filters["app_badge_color"] = app_badge_color

# Restore procedures are free-text Markdown the user writes, but they're
# then shown to any logged-in user (not just whoever wrote them), so the
# rendered HTML needs to be sanitized rather than trusted outright -
# Markdown itself passes raw HTML straight through unless something else
# strips it.
_MARKDOWN_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
    "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "a",
    "table", "thead", "tbody", "tr", "th", "td",
]
_MARKDOWN_ALLOWED_ATTRS = {"a": ["href", "title"]}


def render_markdown(text: Optional[str]) -> str:
    if not text:
        return ""
    html = markdown_lib.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists"]
    )
    return bleach.clean(
        html, tags=_MARKDOWN_ALLOWED_TAGS, attributes=_MARKDOWN_ALLOWED_ATTRS, strip=True
    )


templates.env.filters["markdown"] = render_markdown


def require_login(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    with Session(engine) as session:
        user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(user: User = Depends(require_login)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

# Module-level reference, deliberately kept alive for the process's whole
# lifetime. asyncio.create_task() only returns a weak-referenced handle -
# if nothing holds a strong reference to it, the event loop is free to
# garbage-collect the task mid-execution with no error and no warning at
# all. This bit exactly that way: the scheduler ran for zero iterations
# despite starting up cleanly and passing every logic test, because the
# task itself never survived past its own creation.
_background_tasks: set[asyncio.Task] = set()


@app.on_event("startup")
async def on_startup() -> None:
    SQLModel.metadata.create_all(engine)
    for coro in (scheduled_check_loop(), scheduled_backup_loop()):
        task = asyncio.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return render(
        request, "login.html", {"error": None}
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user or not verify_password(password, user.password_hash):
            return render(
                request,
                "login.html",
                {"error": "Invalid username or password."},
                status_code=401,
            )
        user.last_login_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
        request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------
# Add Application
# --------------------------------------------------------------------------

@app.get("/app-logos/{filename}")
def serve_app_logo(filename: str, user: User = Depends(require_login)):
    # Resolve fully and confirm the result is still inside APP_LOGOS_DIR
    # before serving anything - blocks any "../../etc/passwd"-style
    # traversal attempt regardless of what filename gets passed in.
    requested = (APP_LOGOS_DIR / filename).resolve()
    logos_root = APP_LOGOS_DIR.resolve()
    if requested != logos_root and logos_root not in requested.parents:
        raise HTTPException(status_code=404, detail="Logo not found")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(requested)


@app.get("/applications/new", response_class=HTMLResponse)
def new_application_form(request: Request, next: str = "/", user: User = Depends(require_admin)):
    return render(
        request,
        "add_application.html",
        {
            "user": user,
            "source_types": adapters.catalogue(),
            "backup_strategies": backups.catalogue(),
            "error": None,
            "form": {"next": safe_redirect_target(next)},
            "editing_app": None,
        },
    )


def parse_app_form(form) -> tuple[dict, list[str]]:
    """Parse and validate the Add/Edit Application form. Shared by both
    routes so a fix to one never silently misses the other."""
    errors: list[str] = []
    name = (form.get("name") or "").strip()
    source_type = form.get("source_type") or ""

    if not name:
        errors.append("Application name is required.")
    if source_type not in adapters.REGISTRY:
        errors.append("Choose a valid update source.")

    source_config: dict[str, str] = {}
    if source_type in adapters.REGISTRY:
        for field in adapters.REGISTRY[source_type].fields:
            value = (form.get(f"cfg__{source_type}__{field.name}") or "").strip()
            if field.required and not value:
                errors.append(f"'{field.label}' is required for this source.")
            if value:
                source_config[field.name] = value

    installed_probe = form.get("installed_probe") or ""
    installed_probe_config: dict[str, str] = {}
    if installed_probe and installed_probe not in adapters.REGISTRY:
        errors.append("Choose a valid installed-version probe.")
    elif installed_probe:
        for field in adapters.REGISTRY[installed_probe].fields:
            value = (form.get(f"probe__{installed_probe}__{field.name}") or "").strip()
            if field.required and not value:
                errors.append(f"'{field.label}' is required for the installed-version probe.")
            if value:
                installed_probe_config[field.name] = value

    backup_strategy = form.get("backup_strategy") or ""
    backup_config: dict[str, str] = {}
    if backup_strategy and backup_strategy not in backups.REGISTRY:
        errors.append("Choose a valid backup strategy.")
    elif backup_strategy:
        for field in backups.REGISTRY[backup_strategy].fields:
            value = (form.get(f"backup__{backup_strategy}__{field.name}") or "").strip()
            if field.required and not value:
                errors.append(f"'{field.label}' is required for this backup strategy.")
            if value:
                backup_config[field.name] = value

    fields = dict(
        name=name,
        category=(form.get("category") or "").strip() or None,
        source_type=source_type,
        source_config=source_config,
        version_pattern=(form.get("version_pattern") or "").strip() or None,
        track_branch=(form.get("track_branch") or "").strip() or None,
        include_prerelease=form.get("include_prerelease") == "on",
        installed_probe=installed_probe or None,
        installed_probe_config=installed_probe_config,
        instance_url=(form.get("instance_url") or "").strip() or None,
        installed_version=(form.get("installed_version") or "").strip() or None,
        check_interval_minutes=int(form.get("check_interval_minutes") or 360),
        backup_strategy=backup_strategy or None,
        backup_config=backup_config,
        backup_schedule_enabled=form.get("backup_schedule_enabled") == "on",
        backup_interval_minutes=int(form.get("backup_interval_minutes") or 1440),
        upgrade_procedure=(form.get("upgrade_procedure") or "").strip() or None,
        restore_procedure=(form.get("restore_procedure") or "").strip() or None,
    )
    return fields, errors


def validate_trusted_hosts(session: Session, fields: dict, errors: list[str]) -> None:
    """Shared trusted-host check for both create and edit."""
    if errors:
        return
    hosts_to_check = []
    h = extract_target_host(fields["source_type"], fields["source_config"])
    if h:
        hosts_to_check.append(h)
    if fields["installed_probe"]:
        h2 = extract_target_host(fields["installed_probe"], fields["installed_probe_config"])
        if h2:
            hosts_to_check.append(h2)
    if hosts_to_check:
        trusted = get_trusted_hosts(session)
        for h in hosts_to_check:
            if h.lower() not in trusted:
                errors.append(
                    f"'{h}' is not in Trusted Hosts. "
                    f"Add it under Settings before using this source."
                )


def app_to_form_dict(app_row: MonitoredApp) -> dict:
    """Flatten a saved MonitoredApp back into the same shape the Add/Edit
    form's fields use, so the edit form can be pre-filled."""
    d = {
        "name": app_row.name,
        "category": app_row.category or "",
        "instance_url": app_row.instance_url or "",
        "installed_version": app_row.installed_version or "",
        "source_type": app_row.source_type,
        "track_branch": app_row.track_branch or "",
        "check_interval_minutes": str(app_row.check_interval_minutes),
        "version_pattern": app_row.version_pattern or "",
        "include_prerelease": "on" if app_row.include_prerelease else "",
        "installed_probe": app_row.installed_probe or "",
        "backup_strategy": app_row.backup_strategy or "",
        "backup_schedule_enabled": "on" if app_row.backup_schedule_enabled else "",
        "backup_interval_minutes": str(app_row.backup_interval_minutes),
        "upgrade_procedure": app_row.upgrade_procedure or "",
        "restore_procedure": app_row.restore_procedure or "",
    }
    for k, v in (app_row.source_config or {}).items():
        d[f"cfg__{app_row.source_type}__{k}"] = v
    if app_row.installed_probe:
        for k, v in (app_row.installed_probe_config or {}).items():
            d[f"probe__{app_row.installed_probe}__{k}"] = v
    if app_row.backup_strategy:
        for k, v in (app_row.backup_config or {}).items():
            d[f"backup__{app_row.backup_strategy}__{k}"] = v
    return d


@app.post("/applications")
async def create_application(request: Request, user: User = Depends(require_admin)):
    form = await request.form()
    fields, errors = parse_app_form(form)
    logo_upload = form.get("logo_file")
    has_logo_upload = bool(logo_upload is not None and getattr(logo_upload, "filename", ""))
    pending_logo_token = (form.get("pending_logo_token") or "").strip() or None

    with Session(engine) as session:
        if fields["name"] and not errors:
            existing = session.exec(
                select(MonitoredApp).where(MonitoredApp.name == fields["name"])
            ).first()
            if existing:
                errors.append("An application with this name already exists.")

        validate_trusted_hosts(session, fields, errors)

        if errors:
            # A logo the user already picked can't be refilled into the
            # file input on this reload (browsers block that outright), so
            # stage it - or keep whatever was already staged from an
            # earlier retry - and carry the token through so it's picked
            # back up automatically once they fix the actual error above.
            carry_token = pending_logo_token
            if has_logo_upload:
                try:
                    carry_token = stage_pending_logo(logo_upload)
                    discard_pending_logo(pending_logo_token)
                except ValueError as e:
                    errors.append(str(e))
                    carry_token = pending_logo_token
            form_dict = dict(form)
            if carry_token:
                form_dict["pending_logo_token"] = carry_token
            else:
                form_dict.pop("pending_logo_token", None)
            return render(
                request,
                "add_application.html",
                {
                    "user": user,
                    "source_types": adapters.catalogue(),
                    "backup_strategies": backups.catalogue(),
                    "error": " ".join(errors),
                    "form": form_dict,
                    "editing_app": None,
                },
                status_code=400,
            )

        slug = slugify(fields["name"])
        base_slug, i = slug, 2
        while session.exec(select(MonitoredApp).where(MonitoredApp.slug == slug)).first():
            slug = f"{base_slug}-{i}"
            i += 1

        saved_logo_filename = None
        if has_logo_upload:
            try:
                saved_logo_filename = save_uploaded_logo(logo_upload, slug)
            except ValueError as e:
                return render(
                    request,
                    "add_application.html",
                    {
                        "user": user,
                        "source_types": adapters.catalogue(),
                        "backup_strategies": backups.catalogue(),
                        "error": str(e),
                        "form": dict(form),
                        "editing_app": None,
                    },
                    status_code=400,
                )
            discard_pending_logo(pending_logo_token)
        elif pending_logo_token and (pending := load_pending_logo(pending_logo_token)):
            contents, content_type = pending
            saved_logo_filename = save_logo_bytes(contents, content_type, slug)
            discard_pending_logo(pending_logo_token)

        row = MonitoredApp(slug=slug, custom_logo_filename=saved_logo_filename, **fields)
        session.add(row)
        session.commit()

    return RedirectResponse(safe_redirect_target(form.get("next")), status_code=303)


@app.get("/applications/{app_id}/edit", response_class=HTMLResponse)
def edit_application_form(
    app_id: int, request: Request, next: str = "/", user: User = Depends(require_admin)
):
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if not app_row:
            return RedirectResponse("/", status_code=303)
        form_dict = app_to_form_dict(app_row)
        form_dict["next"] = safe_redirect_target(next)

    return render(
        request,
        "add_application.html",
        {
            "user": user,
            "source_types": adapters.catalogue(),
            "backup_strategies": backups.catalogue(),
            "error": None,
            "form": form_dict,
            "editing_app": app_row,
        },
    )


@app.post("/applications/{app_id}/edit")
async def update_application(app_id: int, request: Request, user: User = Depends(require_admin)):
    form = await request.form()
    fields, errors = parse_app_form(form)
    logo_upload = form.get("logo_file")
    has_logo_upload = bool(logo_upload is not None and getattr(logo_upload, "filename", ""))
    remove_logo = form.get("remove_logo") == "on"
    pending_logo_token = (form.get("pending_logo_token") or "").strip() or None
    redirect_target = safe_redirect_target(form.get("next"))

    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if not app_row:
            return RedirectResponse(redirect_target, status_code=303)

        if fields["name"] and not errors:
            existing = session.exec(
                select(MonitoredApp).where(
                    MonitoredApp.name == fields["name"], MonitoredApp.id != app_id
                )
            ).first()
            if existing:
                errors.append("An application with this name already exists.")

        validate_trusted_hosts(session, fields, errors)

        if errors:
            # Same story as create_application: a chosen logo can't
            # survive this reload in the file input itself, so stage it
            # (or keep whatever was already staged) and carry the token
            # forward for the retry to pick up automatically.
            carry_token = pending_logo_token
            if has_logo_upload:
                try:
                    carry_token = stage_pending_logo(logo_upload)
                    discard_pending_logo(pending_logo_token)
                except ValueError as e:
                    errors.append(str(e))
                    carry_token = pending_logo_token
            form_dict = dict(form)
            if carry_token:
                form_dict["pending_logo_token"] = carry_token
            else:
                form_dict.pop("pending_logo_token", None)
            return render(
                request,
                "add_application.html",
                {
                    "user": user,
                    "source_types": adapters.catalogue(),
                    "backup_strategies": backups.catalogue(),
                    "error": " ".join(errors),
                    "form": form_dict,
                    "editing_app": app_row,
                },
                status_code=400,
            )

        old_logo_filename = app_row.custom_logo_filename
        if has_logo_upload:
            try:
                fields["custom_logo_filename"] = save_uploaded_logo(logo_upload, app_row.slug)
            except ValueError as e:
                return render(
                    request,
                    "add_application.html",
                    {
                        "user": user,
                        "source_types": adapters.catalogue(),
                        "backup_strategies": backups.catalogue(),
                        "error": str(e),
                        "form": dict(form),
                        "editing_app": app_row,
                    },
                    status_code=400,
                )
            delete_logo_file(old_logo_filename)
            discard_pending_logo(pending_logo_token)
        elif pending_logo_token and (pending := load_pending_logo(pending_logo_token)):
            contents, content_type = pending
            fields["custom_logo_filename"] = save_logo_bytes(contents, content_type, app_row.slug)
            delete_logo_file(old_logo_filename)
            discard_pending_logo(pending_logo_token)
        elif remove_logo:
            fields["custom_logo_filename"] = None
            delete_logo_file(old_logo_filename)
        # else: no upload, no staged pending logo, and not removing -
        # custom_logo_filename simply isn't in `fields`, so the setattr
        # loop below leaves it untouched.

        for key, value in fields.items():
            setattr(app_row, key, value)
        app_row.updated_at = datetime.now(timezone.utc)
        session.add(app_row)
        session.commit()

    return RedirectResponse(redirect_target, status_code=303)


@app.post("/applications/{app_id}/delete")
def delete_application(
    app_id: int,
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_admin),
):
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if app_row:
            delete_logo_file(app_row.custom_logo_filename)
            for obs in session.exec(
                select(ReleaseObservation).where(ReleaseObservation.app_id == app_id)
            ).all():
                session.delete(obs)
            for notif in session.exec(
                select(Notification).where(Notification.app_id == app_id)
            ).all():
                session.delete(notif)
            session.delete(app_row)
            session.commit()
    return RedirectResponse(safe_redirect_target(next), status_code=303)


@app.post("/applications/{app_id}/reset-notification")
def reset_notification(
    app_id: int,
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_admin),
):
    """Clears the 'already notified about this version' marker, so the
    next check treats the current latest version as new again - lets an
    admin deliberately re-trigger a notification (in-app and/or email)
    without waiting for an actual new upstream release, useful for
    testing the notification pipeline end to end."""
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if app_row:
            app_row.notified_version = None
            session.add(app_row)
            session.commit()
    return RedirectResponse(safe_redirect_target(next), status_code=303)


# --------------------------------------------------------------------------
# Check Updates# --------------------------------------------------------------------------
# Check Updates
# --------------------------------------------------------------------------

@app.post("/check-updates")
def check_all(user: User = Depends(require_login)):
    with Session(engine) as session:
        checker.run_check(session, CheckTrigger.manual)
    return RedirectResponse("/", status_code=303)


@app.post("/applications/{app_id}/check")
def check_one(
    app_id: int,
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        checker.run_check(session, CheckTrigger.manual, app_ids=[app_id])
    return RedirectResponse(safe_redirect_target(next), status_code=303)


@app.post("/applications/{app_id}/backup")
def backup_one(
    app_id: int,
    background_tasks: BackgroundTasks,
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_admin),
):
    # Runs off the request entirely - a real backup (tar-ing a data
    # directory, pg_dump) can take minutes, and the click that triggers
    # it deserves an instant response, not a page hung waiting on nginx's
    # gateway timeout. The BackupRun row appears (as "Running...") within
    # a moment on /backups; refresh to see it finish.
    background_tasks.add_task(_run_backup_sync, app_id, BackupTrigger.manual)
    return RedirectResponse(safe_redirect_target(next), status_code=303)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Public read-only status API (no login) - for other apps on the LAN to
# query PUMA's dashboard/backup status. Deliberately unauthenticated by
# request: this relies entirely on the network being restricted (the UFW
# rule scoping port 8123 to 10.0.0.0/23) rather than a login/API key, so
# ANY device that can reach this port can read it. Only ever return
# status/version fields here - never source_config/installed_probe_config/
# backup_config, which hold real credentials (DB passwords, API tokens)
# for the very same MonitoredApp rows.
# --------------------------------------------------------------------------

def _app_status(a: MonitoredApp) -> str:
    """Same four-state logic _applications_table.html uses, kept in one
    place so the API and the UI can never quietly disagree on what
    'up to date' means."""
    if not a.latest_version:
        return "not_checked"
    if not a.installed_version:
        return "unknown"
    if a.update_available:
        return "update_available"
    return "up_to_date"


class BackupInfoOut(BaseModel):
    configured: bool = PydanticField(description="Whether this app has a backup strategy configured at all.")
    schedule_enabled: Optional[bool] = PydanticField(default=None, description="Only present when configured=true.")
    last_backup_at: Optional[str] = PydanticField(default=None, description="ISO 8601. Any backup, manual or scheduled.")
    last_backup_status: Optional[str] = None
    last_successful_size_bytes: Optional[int] = PydanticField(default=None, description="Size of the most recent successful backup run.")


class AppStatusOut(BaseModel):
    name: str
    category: Optional[str] = None
    source_type: str = PydanticField(description="The adapter key used for this app's upstream version check.")
    instance_url: Optional[str] = None
    installed_version: Optional[str] = None
    latest_version: Optional[str] = None
    status: str = PydanticField(description="One of: not_checked, unknown, update_available, up_to_date.")
    last_checked_at: Optional[str] = PydanticField(default=None, description="ISO 8601.")
    last_error: Optional[str] = None
    backup: BackupInfoOut


class DashboardSummaryOut(BaseModel):
    apps_monitored: int
    up_to_date: int
    updates_available: int
    last_full_check_at: Optional[str] = PydanticField(default=None, description="ISO 8601, of the most recent scheduled full sweep.")


class DashboardResponse(BaseModel):
    summary: DashboardSummaryOut
    apps: list[AppStatusOut]


@api_app.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Dashboard & per-app status",
    description="KPI summary plus per-app version/status/backup info for every monitored app.",
)
def api_dashboard():
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()

        last_full_run = session.exec(
            select(CheckRun)
            .where(CheckRun.is_full_check == True)  # noqa: E712
            .order_by(CheckRun.started_at.desc())
            .limit(1)
        ).first()

        app_list = []
        for a in apps:
            backup_info = None
            if a.backup_strategy:
                last_run = session.exec(
                    select(BackupRun)
                    .where(BackupRun.app_id == a.id)
                    .where(BackupRun.status == BackupStatus.success)
                    .order_by(BackupRun.started_at.desc())
                    .limit(1)
                ).first()
                backup_info = {
                    "configured": True,
                    "schedule_enabled": a.backup_schedule_enabled,
                    "last_backup_at": a.last_backup_at.isoformat() if a.last_backup_at else None,
                    "last_backup_status": a.last_backup_status,
                    "last_successful_size_bytes": last_run.size_bytes if last_run else None,
                }
            else:
                backup_info = {"configured": False}

            app_list.append({
                "name": a.name,
                "category": a.category,
                "source_type": a.source_type,
                "instance_url": a.instance_url,
                "installed_version": a.installed_version,
                "latest_version": a.latest_version,
                "status": _app_status(a),
                "last_checked_at": a.last_checked_at.isoformat() if a.last_checked_at else None,
                "last_error": a.last_error,
                "backup": backup_info,
            })

        summary = {
            "apps_monitored": len(apps),
            "up_to_date": sum(1 for a in apps if a.installed_version and not a.update_available),
            "updates_available": sum(1 for a in apps if a.update_available),
            "last_full_check_at": last_full_run.started_at.isoformat() if last_full_run else None,
        }

    return {"summary": summary, "apps": app_list}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()

        total = len(apps)
        updates_available = sum(1 for a in apps if a.update_available)
        up_to_date = sum(
            1 for a in apps if a.installed_version and not a.update_available
        )
        # Only a full sweep (Check All Now or the scheduler) counts here -
        # checking a single app from its row shouldn't make this say
        # "Just Now" when the other six apps haven't been touched in hours.
        last_full_run = session.exec(
            select(CheckRun)
            .where(CheckRun.is_full_check == True)  # noqa: E712
            .order_by(CheckRun.started_at.desc())
            .limit(1)
        ).first()
        last_checked = last_full_run.started_at if last_full_run else None

        recent_runs = session.exec(
            select(CheckRun).order_by(CheckRun.started_at.desc()).limit(3)
        ).all()

        settings_overview = {
            "auto_check_enabled": get_setting(session, "auto_check_enabled", "true"),
            "auto_check_interval_minutes": get_setting(
                session, "auto_check_interval_minutes", "360"
            ),
            "update_source_timeout_seconds": get_setting(
                session, "update_source_timeout_seconds", "30"
            ),
            "notifications_enabled": get_setting(
                session, "notifications_enabled", "true"
            ),
            "trusted_hosts_count": len(
                get_setting(session, "trusted_hosts", "").split(",")
            ) if get_setting(session, "trusted_hosts", "") else 0,
        }

        show_apps_table = (
            get_setting(session, "dashboard_show_applications_table", "true")
            == "true"
        )

        hidden_ids = get_dashboard_hidden_app_ids(session)
        dashboard_table_apps = [a for a in apps if a.id not in hidden_ids]

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "apps": dashboard_table_apps,
            "total": total,
            "total_apps_count": total,
            "updates_available": updates_available,
            "up_to_date": up_to_date,
            "last_checked": last_checked,
            "recent_runs": recent_runs,
            "settings_overview": settings_overview,
            "show_apps_table": show_apps_table,
        },
    )


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()
    return render(
        request,
        "applications.html",
        {"user": user, "apps": apps},
    )


HISTORY_PAGE_SIZE = 20


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, page: int = 1, user: User = Depends(require_login)):
    with Session(engine) as session:
        total_runs = session.exec(select(func.count()).select_from(CheckRun)).one()
        total_pages = max(1, (total_runs + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        page = max(1, min(page, total_pages))

        runs = session.exec(
            select(CheckRun)
            .order_by(CheckRun.started_at.desc())
            .offset((page - 1) * HISTORY_PAGE_SIZE)
            .limit(HISTORY_PAGE_SIZE)
        ).all()

        run_ids = [r.id for r in runs]
        observations = (
            session.exec(
                select(ReleaseObservation)
                .where(ReleaseObservation.check_run_id.in_(run_ids))
                .order_by(ReleaseObservation.checked_at)
            ).all()
            if run_ids
            else []
        )
        apps_by_id = {a.id: a for a in session.exec(select(MonitoredApp)).all()}

        obs_by_run: dict[int, list] = {}
        for obs in observations:
            obs_by_run.setdefault(obs.check_run_id, []).append(obs)

    return render(
        request,
        "history.html",
        {
            "user": user,
            "runs": runs,
            "obs_by_run": obs_by_run,
            "apps_by_id": apps_by_id,
            "page": page,
            "total_pages": total_pages,
            "total_runs": total_runs,
        },
    )


@app.get("/backups", response_class=HTMLResponse)
def backups_page(
    request: Request,
    page: int = 1,
    app_id: int | None = None,
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        base_query = select(BackupRun)
        count_query = select(func.count()).select_from(BackupRun)
        selected_app = None
        if app_id is not None:
            selected_app = session.get(MonitoredApp, app_id)
            base_query = base_query.where(BackupRun.app_id == app_id)
            count_query = count_query.where(BackupRun.app_id == app_id)

        total_runs = session.exec(count_query).one()
        total_pages = max(1, (total_runs + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        page = max(1, min(page, total_pages))

        runs = session.exec(
            base_query
            .order_by(BackupRun.started_at.desc())
            .offset((page - 1) * HISTORY_PAGE_SIZE)
            .limit(HISTORY_PAGE_SIZE)
        ).all()

        # Apps with a strategy configured at all - the page's "Backup Now"
        # quick-action list, since an app with nothing configured has no
        # useful action to offer here beyond "go set one up".
        backupable_apps = session.exec(
            select(MonitoredApp)
            .where(MonitoredApp.backup_strategy.is_not(None))
            .order_by(MonitoredApp.name)
        ).all()

        # Apps that back up but have no documented way to actually use
        # that backup - the whole point of a backup is being able to
        # restore from it, so this gap is worth surfacing rather than
        # letting it sit unnoticed the way Moodle's did.
        apps_missing_restore_procedure = [
            a for a in backupable_apps if not a.restore_procedure
        ]

        # Only apps that actually have at least one backup run show up in
        # the filter dropdown - no point offering a filter that always
        # renders empty.
        apps_with_backups = session.exec(
            select(MonitoredApp)
            .where(MonitoredApp.id.in_(select(BackupRun.app_id).distinct()))
            .order_by(MonitoredApp.name)
        ).all()

    return render(
        request,
        "backups.html",
        {
            "user": user,
            "runs": runs,
            "backupable_apps": backupable_apps,
            "apps_with_backups": apps_with_backups,
            "apps_missing_restore_procedure": apps_missing_restore_procedure,
            "selected_app": selected_app,
            "page": page,
            "total_pages": total_pages,
            "total_runs": total_runs,
        },
    )


@app.get("/procedures", response_class=HTMLResponse)
def procedures_page(request: Request, user: User = Depends(require_login)):
    """Compact index - every monitored app, one line each, linking out to
    its own dedicated page for the full content. Deliberately not
    rendering every app's Markdown on this one page: even a handful of
    apps with real, filled-in procedures (which tend to run long - see
    Moodle's) makes a single combined page an unreasonably long scroll,
    and it only gets worse as more apps get documented. An index + a
    detail page per app scales to any number of apps and any amount of
    content, and gives a clean, bookmarkable link straight to one app's
    steps for handing off during an actual incident."""
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()

    return render(
        request,
        "procedures.html",
        {"user": user, "apps": apps},
    )


@app.get("/procedures/{app_id}", response_class=HTMLResponse)
def procedure_detail_page(app_id: int, request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if app_row is None:
            raise HTTPException(status_code=404, detail="No such application")

    return render(
        request,
        "procedure_detail.html",
        {"user": user, "app": app_row},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: int = 0,
    test_email: Optional[str] = None,
    test_email_error: Optional[str] = None,
    user: User = Depends(require_admin),
):
    with Session(engine) as session:
        current = {
            "auto_check_enabled": get_setting(session, "auto_check_enabled", "true"),
            "auto_check_interval_minutes": get_setting(
                session, "auto_check_interval_minutes", "360"
            ),
            "update_source_timeout_seconds": get_setting(
                session, "update_source_timeout_seconds", "30"
            ),
            "notifications_enabled": get_setting(
                session, "notifications_enabled", "true"
            ),
            "email_notifications_enabled": get_setting(
                session, "email_notifications_enabled", "false"
            ),
            "notification_email_recipients": get_setting(
                session, "notification_email_recipients", ""
            ),
            "trusted_hosts": get_trusted_hosts(session),
            "dashboard_show_applications_table": get_setting(
                session, "dashboard_show_applications_table", "true"
            ),
        }
        all_apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()
        hidden_ids = get_dashboard_hidden_app_ids(session)
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "current": current,
            "saved": bool(saved),
            "all_apps": all_apps,
            # Checkbox semantics stay "checked = visible" in the UI, even
            # though what's stored underneath is the hidden set - so a
            # checkbox is checked whenever its app ID is NOT in the
            # hidden set.
            "dashboard_visible_ids": {a.id for a in all_apps} - hidden_ids,
            "smtp_configured": SMTP_CONFIGURED,
            "test_email": test_email,
            "test_email_error": test_email_error,
        },
    )


@app.post("/settings")
async def update_settings(request: Request, user: User = Depends(require_admin)):
    form = await request.form()

    def as_int(raw, fallback: int, lo: int, hi: int) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return fallback
        return max(lo, min(hi, value))

    auto_check_enabled = "true" if form.get("auto_check_enabled") == "on" else "false"
    auto_check_interval_minutes = as_int(
        form.get("auto_check_interval_minutes"), 360, 15, 10080
    )
    update_source_timeout_seconds = as_int(
        form.get("update_source_timeout_seconds"), 30, 5, 300
    )
    notifications_enabled = (
        "true" if form.get("notifications_enabled") == "on" else "false"
    )
    email_notifications_enabled = (
        "true" if form.get("email_notifications_enabled") == "on" else "false"
    )
    recipients_raw = str(form.get("notification_email_recipients", ""))
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    dashboard_show_applications_table = (
        "true" if form.get("dashboard_show_applications_table") == "on" else "false"
    )
    # The checkbox list in the form is "checked = visible" (matches what
    # the person actually sees and clicks), but what we store is the
    # inverse - which ones are HIDDEN - so an app that doesn't exist yet
    # at save time is simply absent from the hidden set and shows up
    # automatically once it's added, no need to revisit this page.
    checked_ids = {
        int(x) for x in form.getlist("dashboard_visible_app_ids") if x.isdigit()
    }
    trusted_hosts_raw = str(form.get("trusted_hosts", ""))
    hosts = [h.strip().lower() for h in trusted_hosts_raw.splitlines() if h.strip()]
    # de-duplicate while preserving order, so repeated pastes don't grow forever
    seen: set[str] = set()
    deduped_hosts = [h for h in hosts if not (h in seen or seen.add(h))]

    with Session(engine) as session:
        set_setting(session, "auto_check_enabled", auto_check_enabled)
        set_setting(
            session, "auto_check_interval_minutes", str(auto_check_interval_minutes)
        )
        set_setting(
            session, "update_source_timeout_seconds", str(update_source_timeout_seconds)
        )
        set_setting(session, "notifications_enabled", notifications_enabled)
        set_setting(session, "email_notifications_enabled", email_notifications_enabled)
        set_setting(session, "notification_email_recipients", ",".join(recipients))
        set_setting(session, "trusted_hosts", ",".join(deduped_hosts))
        set_setting(
            session,
            "dashboard_show_applications_table",
            dashboard_show_applications_table,
        )
        all_app_ids = {
            a.id for a in session.exec(select(MonitoredApp)).all()
        }
        hidden_ids = all_app_ids - checked_ids
        set_setting(
            session,
            "dashboard_hidden_app_ids",
            ",".join(str(i) for i in sorted(hidden_ids)),
        )
        session.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/send-test-email")
async def send_test_email(request: Request, user: User = Depends(require_admin)):
    form = await request.form()
    recipients_raw = str(form.get("notification_email_recipients", ""))
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    success, error = send_email(
        recipients,
        "PUMA test email",
        "This is a test email from PUMA to confirm your SMTP settings are working correctly.\n\n"
        "If you're reading this, notifications are set up correctly.",
    )
    with Session(engine) as session:
        session.add(EmailLog(
            app_id=None,
            app_name="Test email",
            version="-",
            success=success,
            error=None if success else error,
        ))
        session.commit()
    if success:
        return RedirectResponse("/settings?test_email=sent", status_code=303)
    else:
        from urllib.parse import quote
        return RedirectResponse(
            f"/settings?test_email=failed&test_email_error={quote(error or 'Unknown error')}",
            status_code=303,
        )


NOTIFICATIONS_PAGE_SIZE = 20


@app.get("/api/email-events")
def email_events(since_id: int = 0, user: User = Depends(require_login)):
    """Polled by every page (see base.html) to drive the live email-sent
    toast. Returns anything newer than since_id, oldest first, plus the
    highest id seen so the client knows what to ask for next time."""
    with Session(engine) as session:
        rows = session.exec(
            select(EmailLog)
            .where(EmailLog.id > since_id)
            .order_by(EmailLog.id.asc())
            .limit(20)
        ).all()
    latest_id = rows[-1].id if rows else since_id
    return {
        "latest_id": latest_id,
        "events": [
            {
                "id": r.id,
                "app_name": r.app_name,
                "version": r.version,
                "success": r.success,
                "error": r.error,
            }
            for r in rows
        ],
    }


@app.get("/api/backup-status")
def backup_status(ids: str = "", user: User = Depends(require_login)):
    """Polled by the Backups page while any row is still 'running', so it
    updates to Success/Error on its own - same pattern as /api/email-events
    driving the live email toast. Only ever asked about a handful of ids
    the page already knows are in-flight, not a general listing."""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return {"runs": []}
    with Session(engine) as session:
        rows = session.exec(
            select(BackupRun).where(BackupRun.id.in_(id_list))
        ).all()
    return {
        "runs": [
            {
                "id": r.id,
                "status": r.status.value,
                "size_bytes": r.size_bytes,
                "size_display": _format_bytes(r.size_bytes) if r.size_bytes else None,
            }
            for r in rows
        ]
    }


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, page: int = 1, user: User = Depends(require_login)):
    with Session(engine) as session:
        total_notifs = session.exec(
            select(func.count()).select_from(Notification)
        ).one()
        total_pages = max(1, (total_notifs + NOTIFICATIONS_PAGE_SIZE - 1) // NOTIFICATIONS_PAGE_SIZE)
        page = max(1, min(page, total_pages))

        notifs = session.exec(
            select(Notification)
            .order_by(Notification.created_at.desc())
            .offset((page - 1) * NOTIFICATIONS_PAGE_SIZE)
            .limit(NOTIFICATIONS_PAGE_SIZE)
        ).all()

        app_ids = {n.app_id for n in notifs if n.app_id}
        apps_by_id = (
            {
                a.id: a
                for a in session.exec(
                    select(MonitoredApp).where(MonitoredApp.id.in_(app_ids))
                ).all()
            }
            if app_ids
            else {}
        )

    return render(
        request,
        "notifications.html",
        {
            "user": user,
            "notifs": notifs,
            "apps_by_id": apps_by_id,
            "page": page,
            "total_pages": total_pages,
            "total_notifs": total_notifs,
        },
    )


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    next: Optional[str] = Form(default="/notifications"),
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        notif = session.get(Notification, notification_id)
        if notif:
            notif.read = True
            session.add(notif)
            session.commit()
    return RedirectResponse(
        next if next and next.startswith("/notifications") else "/notifications",
        status_code=303,
    )


@app.post("/notifications/mark-all-read")
def mark_all_notifications_read(
    next: Optional[str] = Form(default="/notifications"),
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        unread = session.exec(
            select(Notification).where(Notification.read == False)  # noqa: E712
        ).all()
        for notif in unread:
            notif.read = True
            session.add(notif)
        session.commit()
    return RedirectResponse(
        next if next and next.startswith("/notifications") else "/notifications",
        status_code=303,
    )


def _format_duration(delta_seconds: float) -> str:
    seconds = int(delta_seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts[:2])


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["filesize"] = _format_bytes


@app.get("/system-info", response_class=HTMLResponse)
def system_info_page(request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        total_apps = session.exec(select(func.count()).select_from(MonitoredApp)).one()
        enabled_apps = session.exec(
            select(func.count()).select_from(MonitoredApp).where(
                MonitoredApp.enabled == True  # noqa: E712
            )
        ).one()
        total_runs = session.exec(select(func.count()).select_from(CheckRun)).one()
        total_notifications = session.exec(
            select(func.count()).select_from(Notification)
        ).one()

    try:
        db_size = _format_bytes(os.path.getsize(DB_PATH))
    except OSError:
        db_size = "unavailable"

    now_utc = datetime.now(timezone.utc)
    info = {
        "puma_version": "v1.0.0",
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "python_version": platform.python_version(),
        "db_path": DB_PATH,
        "db_size": db_size,
        "display_timezone": str(DISPLAY_TZ),
        "server_time_utc": now_utc.strftime("%b %d, %Y %H:%M:%S UTC"),
        "server_time_local": now_utc.astimezone(DISPLAY_TZ).strftime("%b %d, %Y %I:%M:%S %p"),
        "uptime": _format_duration((now_utc - APP_STARTED_AT).total_seconds()),
        "total_apps": total_apps,
        "enabled_apps": enabled_apps,
        "total_runs": total_runs,
        "total_notifications": total_notifications,
    }

    return render(request, "system_info.html", {"user": user, "info": info})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request, user: User = Depends(require_login)):
    return render(request, "about.html", {"user": user, "puma_version": "v1.0.0"})


@app.get("/game", response_class=HTMLResponse)
def game_page(request: Request, user: User = Depends(require_login)):
    return render(request, "game.html", {"user": user})


@app.post("/account/toggle-dark-mode")
def toggle_dark_mode(
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        row = session.get(User, user.id)
        row.dark_mode = not row.dark_mode
        session.add(row)
        session.commit()
    safe_next = next if next and next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(safe_next, status_code=303)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(require_admin)):
    with Session(engine) as session:
        users = session.exec(select(User).order_by(User.username)).all()
    return render(request, "users.html", {"user": user, "users": users})


@app.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, user: User = Depends(require_admin)):
    return render(
        request,
        "user_form.html",
        {"user": user, "editing_user": None, "error": None, "form": {}},
    )


@app.post("/users")
async def create_user(request: Request, user: User = Depends(require_admin)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    is_admin = form.get("is_admin") == "on"

    errors = []
    if not username:
        errors.append("Username is required.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm_password:
        errors.append("Passwords don't match.")

    with Session(engine) as session:
        if username and session.exec(
            select(User).where(User.username == username)
        ).first():
            errors.append(f"Username '{username}' is already taken.")

        if errors:
            return render(
                request,
                "user_form.html",
                {
                    "user": user,
                    "editing_user": None,
                    "error": " ".join(errors),
                    "form": {"username": username, "is_admin": is_admin},
                },
            )

        new_row = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        session.add(new_row)
        session.commit()

    return RedirectResponse("/users", status_code=303)


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(
    user_id: int, request: Request, user: User = Depends(require_admin)
):
    with Session(engine) as session:
        editing_user = session.get(User, user_id)
        if not editing_user:
            raise HTTPException(status_code=404, detail="User not found")
    return render(
        request,
        "user_form.html",
        {"user": user, "editing_user": editing_user, "error": None, "form": {}},
    )


@app.post("/users/{user_id}/edit")
async def update_user(
    user_id: int, request: Request, user: User = Depends(require_admin)
):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    is_admin = form.get("is_admin") == "on"

    with Session(engine) as session:
        editing_user = session.get(User, user_id)
        if not editing_user:
            raise HTTPException(status_code=404, detail="User not found")

        errors = []
        if not username:
            errors.append("Username is required.")
        existing = session.exec(
            select(User).where(User.username == username)
        ).first()
        if existing and existing.id != user_id:
            errors.append(f"Username '{username}' is already taken.")

        # Only validate password fields if the admin is actually trying to
        # change one - leaving both blank means "keep the current password."
        changing_password = bool(password or confirm_password)
        if changing_password:
            if len(password) < 8:
                errors.append("New password must be at least 8 characters.")
            if password != confirm_password:
                errors.append("Passwords don't match.")

        # Never allow the last remaining admin to be demoted - that would
        # leave the app with no one able to manage users at all, and
        # there's no other way back in without direct DB access.
        if editing_user.is_admin and not is_admin:
            other_admins = session.exec(
                select(func.count())
                .select_from(User)
                .where(User.is_admin == True, User.id != user_id)  # noqa: E712
            ).one()
            if other_admins == 0:
                errors.append(
                    "Can't remove admin from the last remaining admin account."
                )

        # Separately: never allow someone to remove their OWN admin access,
        # even when other admins exist elsewhere - doing this mid-session
        # immediately locks the person doing it out of everything admin-only,
        # with no way to undo it themselves. Same category of protection as
        # not being able to delete your own account.
        if user_id == user.id and editing_user.is_admin and not is_admin:
            errors.append(
                "You can't remove your own admin access. Ask another admin to do this for you."
            )

        if errors:
            return render(
                request,
                "user_form.html",
                {
                    "user": user,
                    "editing_user": editing_user,
                    "error": " ".join(errors),
                    "form": {"username": username, "is_admin": is_admin},
                },
            )

        editing_user.username = username
        editing_user.is_admin = is_admin
        if changing_password:
            editing_user.password_hash = hash_password(password)
        session.add(editing_user)
        session.commit()

    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int, user: User = Depends(require_admin)):
    if user_id == user.id:
        raise HTTPException(
            status_code=400, detail="You can't delete your own account while logged in."
        )
    with Session(engine) as session:
        target = session.get(User, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target.is_admin:
            other_admins = session.exec(
                select(func.count())
                .select_from(User)
                .where(User.is_admin == True, User.id != user_id)  # noqa: E712
            ).one()
            if other_admins == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Can't delete the last remaining admin account.",
                )
        session.delete(target)
        session.commit()
    return RedirectResponse("/users", status_code=303)
