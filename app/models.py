"""PUMA — Program Update Monitoring Assistant
SQLite schema (SQLModel / SQLAlchemy).

Server-apps scope: no local install-path tracking. "Local Version" comes
from either a manual field (always works) or an optional probe adapter
(installed_probe) hitting the instance's own API/file - same adapter
registry as the upstream-release check in adapters.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import Column, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CheckStatus(str, Enum):
    ok = "ok"
    not_modified = "not_modified"
    error = "error"


class CheckTrigger(str, Enum):
    manual = "manual"
    scheduled = "scheduled"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class User(SQLModel, table=True):
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    is_admin: bool = True
    created_at: datetime = Field(default_factory=_now)
    last_login_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Core: monitored applications
# ---------------------------------------------------------------------------

class MonitoredApp(SQLModel, table=True):
    __tablename__ = "monitored_app"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)          # "VLE (Moodle)"
    slug: str = Field(index=True, unique=True)          # "moodle"
    category: Optional[str] = None                      # "LMS", "Wiki", "ITSM"...
    icon: Optional[str] = None                           # lucide icon name or initial
    enabled: bool = True
    notes: Optional[str] = None

    # --- where the upstream (latest) version comes from -----------------
    source_type: str = Field(index=True)                # adapters.REGISTRY key
    source_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )                                                    # {"repo": "glpi-project/glpi"}
    version_pattern: Optional[str] = None                # regex, 1 capture group
    track_branch: Optional[str] = None                   # "5.2" -> ignore 6.x
    include_prerelease: bool = False

    # --- the running instance (server apps only) -------------------------
    instance_url: Optional[str] = None                   # https://vle.pstprd.one
    installed_version: Optional[str] = None              # manual, or probed
    installed_version_raw: Optional[str] = None           # original probe value, before regex extraction
    installed_probe: Optional[str] = None                # adapters key, optional
    installed_probe_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )

    # --- scheduling & polling state ---------------------------------------
    check_interval_minutes: int = 360
    last_checked_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0

    # HTTP conditional-request cache (cheap on GitHub's rate limit)
    etag: Optional[str] = None
    last_modified_header: Optional[str] = None

    # --- latest known upstream release -------------------------------------
    latest_version: Optional[str] = None
    latest_version_raw: Optional[str] = None             # original tag
    latest_published_at: Optional[datetime] = None
    latest_release_url: Optional[str] = None
    latest_seen_at: Optional[datetime] = None             # when WE first saw it

    # --- notification wiring -------------------------------------------------
    notify_channels: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )                                                    # ["ops-teams", "email"]
    notified_version: Optional[str] = None                # dedupe: don't re-alert

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def update_available(self) -> bool:
        from versioning import is_newer
        return is_newer(self.latest_version or "", self.installed_version)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class CheckRun(SQLModel, table=True):
    """One row per 'Check Updates' click or scheduled sweep - this is what
    the mockup's 'Recent Check History' panel lists (Date & Time / Type /
    Result / Details)."""

    __tablename__ = "check_run"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=_now, index=True)
    finished_at: Optional[datetime] = None
    trigger: CheckTrigger = CheckTrigger.scheduled
    apps_checked: int = 0
    updates_found: int = 0
    errors: int = 0


class ReleaseObservation(SQLModel, table=True):
    """One row per app per check - belongs to a CheckRun."""

    __tablename__ = "release_observation"
    __table_args__ = (UniqueConstraint("app_id", "check_run_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    check_run_id: int = Field(foreign_key="check_run.id", index=True)
    app_id: int = Field(foreign_key="monitored_app.id", index=True)
    checked_at: datetime = Field(default_factory=_now, index=True)
    status: CheckStatus = CheckStatus.ok
    version: Optional[str] = None
    version_raw: Optional[str] = None
    published_at: Optional[datetime] = None
    release_url: Optional[str] = None
    is_new: bool = False                                 # first sighting?
    duration_ms: Optional[int] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Notifications (in-app bell) + delivery (Apprise) are separate concerns
# ---------------------------------------------------------------------------

class Notification(SQLModel, table=True):
    """Feeds the bell icon / unread badge in the mockup header."""

    __tablename__ = "notification"

    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: Optional[int] = Field(default=None, foreign_key="monitored_app.id")
    message: str
    read: bool = False
    created_at: datetime = Field(default_factory=_now, index=True)


class NotificationChannel(SQLModel, table=True):
    """Apprise delivery targets (email, Teams, ntfy, webhook...)."""

    __tablename__ = "notification_channel"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)           # "ops-teams"
    enabled: bool = True
    apprise_url: str                                      # mailto://, msteams://, ntfy://
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Settings (mockup's "Settings Overview" panel: Auto Check, Timeout,
# Notifications, Trusted Hosts)
# ---------------------------------------------------------------------------

class Setting(SQLModel, table=True):
    """Simple key-value store. Keys used by the app:
    auto_check_enabled ('true'/'false'), auto_check_interval_minutes,
    update_source_timeout_seconds, notifications_enabled,
    trusted_hosts (JSON-encoded list, stored as text - see note below).

    Trusted Hosts matters most: it's the allowlist that stops the
    html_regex / json_api adapters (which accept an arbitrary URL) from
    being pointed at internal-only services once the app is admin-driven
    from the GUI.
    """

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=_now)
