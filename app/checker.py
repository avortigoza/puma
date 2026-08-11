"""The actual update-checking engine.

Nothing has called adapters.py since an app gets saved - this is that
missing piece. check_app() runs both halves for one app (latest upstream
release, and optionally the installed-version probe); run_check() does it
for a whole batch and rolls the results into a CheckRun for history.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlmodel import Session, select

import adapters
import versioning
from models import (
    CheckRun,
    CheckStatus,
    CheckTrigger,
    MonitoredApp,
    Notification,
    ReleaseObservation,
    Setting,
)


def _notifications_enabled(session: Session) -> bool:
    row = session.get(Setting, "notifications_enabled")
    return row.value == "true" if row else True  # same default as main.py's get_setting


def check_app(app_row: MonitoredApp, client: httpx.Client) -> ReleaseObservation:
    """Check one app. Mutates app_row in place; caller commits.
    Returns an observation not yet attached to a CheckRun (caller sets
    check_run_id)."""
    started = time.monotonic()
    obs = ReleaseObservation(check_run_id=0, app_id=app_row.id or 0)

    try:
        adapter = adapters.build(app_row.source_type, app_row.source_config, client)
        result = adapter.fetch(etag=app_row.etag, last_modified=app_row.last_modified_header)

        if not result.not_modified:
            candidates = [(r.raw_tag, r.prerelease) for r in result.releases]
            best = versioning.pick_latest(
                candidates,
                pattern=app_row.version_pattern,
                track_branch=app_row.track_branch,
                include_prerelease=app_row.include_prerelease,
            )
            if best:
                match = next(
                    (r for r in result.releases
                     if versioning.extract(r.raw_tag, app_row.version_pattern) == best),
                    None,
                )
                is_new_latest = best != app_row.latest_version
                app_row.latest_version = best
                app_row.latest_version_raw = match.raw_tag if match else best
                app_row.latest_published_at = match.published_at if match else None
                app_row.latest_release_url = match.url if match else None
                # Only stamp "first seen" on a genuine change - this is the
                # fallback used for display when the source has no real
                # published_at (e.g. Stanza's page scrape). Updating it on
                # every check regardless of change defeats the point: it
                # would always just show "whenever I last looked."
                if is_new_latest or not app_row.latest_seen_at:
                    app_row.latest_seen_at = datetime.now(timezone.utc)
                obs.version = best
                obs.version_raw = app_row.latest_version_raw
                obs.published_at = app_row.latest_published_at
                obs.release_url = app_row.latest_release_url

        app_row.etag = result.etag or app_row.etag
        app_row.last_modified_header = result.last_modified or app_row.last_modified_header
        app_row.last_error = None
        app_row.consecutive_failures = 0
        obs.status = CheckStatus.not_modified if result.not_modified else CheckStatus.ok

        # Optional: probe the actually-installed version. A probe failure
        # doesn't fail the whole check - keep the last-known value.
        if app_row.installed_probe:
            try:
                probe = adapters.build(
                    app_row.installed_probe, app_row.installed_probe_config, client
                )
                presult = probe.fetch()
                if presult.releases:
                    raw = presult.releases[0].raw_tag
                    probe_published_at = presult.releases[0].published_at
                    installed = versioning.extract(raw, app_row.version_pattern)
                    if installed:
                        is_new_installed = installed != app_row.installed_version
                        app_row.installed_version = installed
                        app_row.installed_version_raw = raw
                        # Real signal where the probe supports it (local
                        # file mtime, git commit date); "first detected"
                        # fallback otherwise. Only stamped on a genuine
                        # version change, not every check - same reasoning
                        # as latest_seen_at above.
                        if is_new_installed or not app_row.installed_version_changed_at:
                            app_row.installed_version_changed_at = (
                                probe_published_at or datetime.now(timezone.utc)
                            )
            except adapters.SourceUnavailable:
                pass

    except adapters.SourceUnavailable as e:
        app_row.last_error = str(e)
        app_row.consecutive_failures += 1
        obs.status = CheckStatus.error
        obs.error = str(e)

    app_row.last_checked_at = datetime.now(timezone.utc)
    if obs.status != CheckStatus.error:
        app_row.last_success_at = app_row.last_checked_at
    obs.duration_ms = int((time.monotonic() - started) * 1000)
    return obs


def run_check(
    session: Session,
    trigger: CheckTrigger,
    app_ids: Optional[list[int]] = None,
    timeout_seconds: int = 20,
) -> CheckRun:
    """Check all enabled apps, or a specific subset by id (used by the
    per-row 'check now' button)."""
    query = select(MonitoredApp).where(MonitoredApp.enabled == True)  # noqa: E712
    if app_ids:
        query = query.where(MonitoredApp.id.in_(app_ids))
    app_rows = list(session.exec(query).all())

    run = CheckRun(trigger=trigger, is_full_check=not bool(app_ids))
    session.add(run)
    session.commit()
    session.refresh(run)

    prior_versions = {a.id: a.latest_version for a in app_rows}
    updates_found = 0
    errors = 0
    notifications_enabled = _notifications_enabled(session)

    with httpx.Client(follow_redirects=True, timeout=timeout_seconds) as client:
        for app_row in app_rows:
            try:
                obs = check_app(app_row, client)
            except Exception as e:
                # check_app() already catches SourceUnavailable internally -
                # this is the last line of defense against something totally
                # unexpected (a malformed response causing a KeyError, etc.)
                # in one app's adapter. Without this, one broken app takes
                # down the whole "Check All Now" / per-row check request for
                # every other app too - happened twice now (GitLab's Releases
                # API 403, GitHub's rate-limit 403) from two different causes,
                # so this is a general safety net, not a fix for either
                # specific case.
                obs = ReleaseObservation(check_run_id=0, app_id=app_row.id or 0)
                obs.status = CheckStatus.error
                obs.error = f"{type(e).__name__}: {e}"[:500]
                app_row.last_error = obs.error
                app_row.consecutive_failures += 1
                app_row.last_checked_at = datetime.now(timezone.utc)

            obs.check_run_id = run.id
            obs.app_id = app_row.id

            is_new = (
                obs.status == CheckStatus.ok
                and obs.version
                and obs.version != prior_versions.get(app_row.id)
            )
            obs.is_new = bool(is_new)

            if obs.status == CheckStatus.error:
                errors += 1

            if is_new and app_row.installed_version and versioning.is_newer(
                obs.version, app_row.installed_version
            ):
                updates_found += 1

            # Independent of is_new (which only means "this run's result
            # differs from last run's") - this instead asks "is there a
            # newer version than what's installed that we haven't actually
            # notified about yet." Keeping these separate matters: if
            # notifications were off when an update was first found,
            # is_new would already be false on every later check (nothing
            # about the result has changed since), so gating on is_new
            # here would mean re-enabling the setting never catches up on
            # what was missed. This runs the same check on every pass
            # regardless, so the very next check after re-enabling queues
            # the notification that was skipped while off.
            if (
                obs.status == CheckStatus.ok
                and obs.version
                and app_row.installed_version
                and versioning.is_newer(obs.version, app_row.installed_version)
                and app_row.notified_version != obs.version
                and notifications_enabled
            ):
                session.add(Notification(
                    app_id=app_row.id,
                    message=f"{app_row.name} has a new release available: {obs.version}",
                ))
                app_row.notified_version = obs.version

            session.add(obs)
            session.add(app_row)

    run.finished_at = datetime.now(timezone.utc)
    run.apps_checked = len(app_rows)
    run.updates_found = updates_found
    run.errors = errors
    session.add(run)
    session.commit()
    return run
