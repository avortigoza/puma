"""PUMA - Program Update Monitoring Assistant
FastAPI app: session auth, dashboard.

Run (from inside app/, after activating the venv):
    uvicorn main:app --host 127.0.0.1 --port 8123
"""

from __future__ import annotations

import os
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Make sibling modules (models, versioning, adapters) importable regardless
# of the process's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlmodel import Session, SQLModel, create_engine, select, func
from starlette.middleware.sessions import SessionMiddleware

import adapters
import checker
from models import (
    CheckRun,
    CheckTrigger,
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

# Everything is stored/computed in UTC internally (datetime.now(timezone.utc)
# throughout). This is only for *display* - converting absolute timestamps
# to the team's actual timezone before formatting, so dates/times shown in
# the UI match the wall clock instead of raw UTC. Configurable via env var
# in case this ever needs to run for a team elsewhere.
DISPLAY_TZ = ZoneInfo(os.environ.get("PUMA_DISPLAY_TZ", "Asia/Manila"))

engine = create_engine(f"sqlite:///{DB_PATH}")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="PUMA")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

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


def get_dashboard_visible_app_ids(session: Session) -> Optional[set[int]]:
    """None means 'show all' (the default, and also what an empty setting
    means) - a real set means only those app IDs should appear in the
    Dashboard's Applications table. Doesn't affect the KPI counts or the
    standalone /applications page, which always reflect every app."""
    raw = get_setting(session, "dashboard_visible_app_ids", "")
    if not raw.strip():
        return None
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


SAFE_REDIRECT_PATHS = {"/", "/applications"}


def safe_redirect_target(next_value: Optional[str]) -> str:
    """Only ever redirect to a known internal page - never trust an
    arbitrary value from a form field as a redirect target (open-redirect
    risk), even though today's callers only ever send "/" or
    "/applications"."""
    if next_value in SAFE_REDIRECT_PATHS:
        return next_value
    return "/"


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


def require_login(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    with Session(engine) as session:
        user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    SQLModel.metadata.create_all(engine)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
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
            return templates.TemplateResponse(
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

def parse_application_form(
    form, session: Session, exclude_app_id: Optional[int] = None
) -> tuple[dict, list[str]]:
    """Shared validation for both create and edit - so the two paths can
    never quietly drift apart. Returns (kwargs_for_MonitoredApp, errors)."""
    name = (form.get("name") or "").strip()
    source_type = form.get("source_type") or ""

    errors: list[str] = []
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

    if name and not errors:
        query = select(MonitoredApp).where(MonitoredApp.name == name)
        if exclude_app_id is not None:
            query = query.where(MonitoredApp.id != exclude_app_id)
        if session.exec(query).first():
            errors.append("An application with this name already exists.")

    hosts_to_check = []
    if not errors:
        h = extract_target_host(source_type, source_config)
        if h:
            hosts_to_check.append(h)
        if installed_probe:
            h2 = extract_target_host(installed_probe, installed_probe_config)
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

    kwargs = dict(
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
    )
    return kwargs, errors


def app_to_form_dict(app_row: MonitoredApp) -> dict:
    """Reconstruct a form dict from a saved app, for pre-filling the edit form -
    the mirror image of what the browser would submit."""
    form: dict = {
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
    }
    if app_row.source_type in adapters.REGISTRY:
        for field in adapters.REGISTRY[app_row.source_type].fields:
            form[f"cfg__{app_row.source_type}__{field.name}"] = (
                app_row.source_config.get(field.name, "")
            )
    if app_row.installed_probe and app_row.installed_probe in adapters.REGISTRY:
        for field in adapters.REGISTRY[app_row.installed_probe].fields:
            form[f"probe__{app_row.installed_probe}__{field.name}"] = (
                app_row.installed_probe_config.get(field.name, "")
            )
    return form


@app.get("/applications/new", response_class=HTMLResponse)
def new_application_form(request: Request, user: User = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "add_application.html",
        {
            "user": user,
            "source_types": adapters.catalogue(),
            "error": None,
            "form": {},
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
    }
    for k, v in (app_row.source_config or {}).items():
        d[f"cfg__{app_row.source_type}__{k}"] = v
    if app_row.installed_probe:
        for k, v in (app_row.installed_probe_config or {}).items():
            d[f"probe__{app_row.installed_probe}__{k}"] = v
    return d


@app.post("/applications")
async def create_application(request: Request, user: User = Depends(require_login)):
    form = await request.form()
    fields, errors = parse_app_form(form)

    with Session(engine) as session:
        if fields["name"] and not errors:
            existing = session.exec(
                select(MonitoredApp).where(MonitoredApp.name == fields["name"])
            ).first()
            if existing:
                errors.append("An application with this name already exists.")

        validate_trusted_hosts(session, fields, errors)

        if errors:
            return templates.TemplateResponse(
                request,
                "add_application.html",
                {
                    "user": user,
                    "source_types": adapters.catalogue(),
                    "error": " ".join(errors),
                    "form": dict(form),
                    "editing_app": None,
                },
                status_code=400,
            )

        slug = slugify(fields["name"])
        base_slug, i = slug, 2
        while session.exec(select(MonitoredApp).where(MonitoredApp.slug == slug)).first():
            slug = f"{base_slug}-{i}"
            i += 1

        row = MonitoredApp(slug=slug, **fields)
        session.add(row)
        session.commit()

    return RedirectResponse("/", status_code=303)


@app.get("/applications/{app_id}/edit", response_class=HTMLResponse)
def edit_application_form(app_id: int, request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if not app_row:
            return RedirectResponse("/", status_code=303)
        form_dict = app_to_form_dict(app_row)

    return templates.TemplateResponse(
        request,
        "add_application.html",
        {
            "user": user,
            "source_types": adapters.catalogue(),
            "error": None,
            "form": form_dict,
            "editing_app": app_row,
        },
    )


@app.post("/applications/{app_id}/edit")
async def update_application(app_id: int, request: Request, user: User = Depends(require_login)):
    form = await request.form()
    fields, errors = parse_app_form(form)

    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if not app_row:
            return RedirectResponse("/", status_code=303)

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
            return templates.TemplateResponse(
                request,
                "add_application.html",
                {
                    "user": user,
                    "source_types": adapters.catalogue(),
                    "error": " ".join(errors),
                    "form": dict(form),
                    "editing_app": app_row,
                },
                status_code=400,
            )

        for key, value in fields.items():
            setattr(app_row, key, value)
        app_row.updated_at = datetime.now(timezone.utc)
        session.add(app_row)
        session.commit()

    return RedirectResponse("/", status_code=303)


@app.post("/applications/{app_id}/delete")
def delete_application(
    app_id: int,
    next: Optional[str] = Form(default="/"),
    user: User = Depends(require_login),
):
    with Session(engine) as session:
        app_row = session.get(MonitoredApp, app_id)
        if app_row:
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


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()

        total = len(apps)
        updates_available = sum(1 for a in apps if a.update_available)
        up_to_date = sum(
            1 for a in apps if a.installed_version and not a.update_available
        )
        checked_times = [a.last_checked_at for a in apps if a.last_checked_at]
        last_checked = max(checked_times) if checked_times else None

        unread_notifications = session.exec(
            select(func.count()).select_from(Notification).where(
                Notification.read == False  # noqa: E712
            )
        ).one()

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

        visible_ids = get_dashboard_visible_app_ids(session)
        dashboard_table_apps = (
            apps if visible_ids is None else [a for a in apps if a.id in visible_ids]
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "apps": dashboard_table_apps,
            "total": total,
            "updates_available": updates_available,
            "up_to_date": up_to_date,
            "last_checked": last_checked,
            "unread_notifications": unread_notifications,
            "recent_runs": recent_runs,
            "settings_overview": settings_overview,
            "show_apps_table": show_apps_table,
        },
    )


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, user: User = Depends(require_login)):
    with Session(engine) as session:
        apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()
    return templates.TemplateResponse(
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

    return templates.TemplateResponse(
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


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, user: User = Depends(require_login)):
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
            "trusted_hosts": get_trusted_hosts(session),
            "dashboard_show_applications_table": get_setting(
                session, "dashboard_show_applications_table", "true"
            ),
        }
        all_apps = session.exec(select(MonitoredApp).order_by(MonitoredApp.name)).all()
        visible_ids = get_dashboard_visible_app_ids(session)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "current": current,
            "saved": bool(saved),
            "all_apps": all_apps,
            # None means "show all" - every checkbox should render checked
            "dashboard_visible_ids": (
                {a.id for a in all_apps} if visible_ids is None else visible_ids
            ),
        },
    )


@app.post("/settings")
async def update_settings(request: Request, user: User = Depends(require_login)):
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
    dashboard_show_applications_table = (
        "true" if form.get("dashboard_show_applications_table") == "on" else "false"
    )
    # Whatever's checked at save time, stored literally - if every app is
    # checked that's functionally "show all" today, but a NEW app added
    # later won't auto-appear until it's explicitly checked here too.
    # Predictable > magic: no special-casing "all checked" back to an
    # empty/show-all sentinel.
    dashboard_visible_app_ids = ",".join(
        form.getlist("dashboard_visible_app_ids")
    )
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
        set_setting(session, "trusted_hosts", ",".join(deduped_hosts))
        set_setting(
            session,
            "dashboard_show_applications_table",
            dashboard_show_applications_table,
        )
        set_setting(session, "dashboard_visible_app_ids", dashboard_visible_app_ids)
        session.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)
