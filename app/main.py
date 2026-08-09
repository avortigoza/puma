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


def get_trusted_hosts(session: Session) -> list[str]:
    raw = get_setting(session, "trusted_hosts", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "app"


def extract_target_host(source_type: str, config: dict) -> Optional[str]:
    """Which external host would this source actually contact?
    github_releases/github_tags are hardcoded to api.github.com, so they
    need no check. Anything that takes a user-supplied URL does."""
    url = None
    if source_type in ("gitlab_releases", "glpi_rest_api"):
        url = config.get("base_url")
    elif source_type in ("atom_feed", "json_api", "html_regex"):
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
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


templates.env.filters["timeago"] = time_ago


def short_date(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%b %-d, %Y")


templates.env.filters["shortdate"] = short_date

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
        },
    )


@app.post("/applications")
async def create_application(request: Request, user: User = Depends(require_login)):
    form = await request.form()
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

    # Installed-version probe is optional - "" means manual entry only.
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

    with Session(engine) as session:
        if name and not errors:
            existing = session.exec(
                select(MonitoredApp).where(MonitoredApp.name == name)
            ).first()
            if existing:
                errors.append("An application with this name already exists.")

        # Trusted-hosts check applies to BOTH the update source and the
        # installed-version probe, since either can be pointed at an
        # arbitrary URL (local_file_regex is exempt - it never leaves disk).
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

        if errors:
            return templates.TemplateResponse(
                request,
                "add_application.html",
                {
                    "user": user,
                    "source_types": adapters.catalogue(),
                    "error": " ".join(errors),
                    "form": dict(form),
                },
                status_code=400,
            )

        slug = slugify(name)
        base_slug, i = slug, 2
        while session.exec(select(MonitoredApp).where(MonitoredApp.slug == slug)).first():
            slug = f"{base_slug}-{i}"
            i += 1

        row = MonitoredApp(
            name=name,
            slug=slug,
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
        session.add(row)
        session.commit()

    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Check Updates
# --------------------------------------------------------------------------

@app.post("/check-updates")
def check_all(user: User = Depends(require_login)):
    with Session(engine) as session:
        checker.run_check(session, CheckTrigger.manual)
    return RedirectResponse("/", status_code=303)


@app.post("/applications/{app_id}/check")
def check_one(app_id: int, user: User = Depends(require_login)):
    with Session(engine) as session:
        checker.run_check(session, CheckTrigger.manual, app_ids=[app_id])
    return RedirectResponse("/", status_code=303)


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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "apps": apps,
            "total": total,
            "updates_available": updates_available,
            "up_to_date": up_to_date,
            "last_checked": last_checked,
            "unread_notifications": unread_notifications,
            "recent_runs": recent_runs,
            "settings_overview": settings_overview,
        },
    )
