# PUMA — Program Update Monitoring Assistant

PUMA is an in-house web app that monitors update availability for the
team's self-hosted applications, and can take a safety backup of any
of them before you go run an upgrade by hand.

It answers two questions for every app you point it at:

1. **Is a new version available?** (checked against the app's own
   upstream source — GitHub Releases, GitLab Releases, a vendor API,
   etc.)
2. **If I need to roll this back, do I have a recent, working backup
   — and do I actually know how to use it?**

---

## Contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Server layout](#server-layout)
- [Installation](#installation)
- [Configuration](#configuration)
- [First-time setup](#first-time-setup)
- [Adding a monitored app](#adding-a-monitored-app)
- [Backups](#backups)
- [Procedures](#procedures)
- [Deploying an update to PUMA itself](#deploying-an-update-to-puma-itself)
- [API](#api)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Update monitoring** — a pluggable adapter system checks each app's
  upstream source on a schedule (or on demand) and compares it against
  the installed version, with full check history retained per app.
- **Email notifications** when a new version is detected, with a
  "send test email" option in Settings and a log of every email sent.
- **Backups** — pluggable backup strategies matching how each app is
  actually deployed (see [Backups](#backups)), triggered manually or
  on their own fixed schedule, with automatic retention (last 5 runs
  per app) and a dedicated history page.
- **Procedures** — per-app, user-written Markdown documentation for
  how to actually upgrade and restore each app, rendered and
  sanitized, with an index page and a dedicated page per app.
- **A read-only status API** (`/api/v1/dashboard`, with interactive
  Swagger docs at `/api/v1/docs`) for other internal tools to query
  app/backup status programmatically.
- **A "Trusted Hosts" allowlist** — any admin-supplied URL used by an
  adapter is checked against this list first, so a misconfigured or
  malicious source can't be used to probe internal-only addresses.
- Dashboard, Applications, History, Backups, Procedures,
  Notifications, Settings, and System Info pages, with light/dark mode.

---

## Tech stack

- **Python 3.12**, **FastAPI** served by **uvicorn**
- **SQLModel** (SQLAlchemy + Pydantic) over a **SQLite** database
- **Jinja2** templates, **Tailwind CSS v4** (built via the standalone
  `tailwindcss` CLI binary — no Node/npm build step)
- Session-based auth (`itsdangerous` + Starlette sessions), passwords
  hashed with `passlib` + `bcrypt`
- `httpx` for outbound HTTP, `feedparser` for Atom/RSS
- `markdown` + `bleach` for rendering and sanitizing Procedures content
- No background job framework (Celery, etc.) — scheduling is two
  plain `asyncio` loops started on FastAPI's startup event, each
  waking up once a minute to check what's due

**Known pinned dependency — do not change without testing login
first:** `bcrypt` must stay at `4.0.1`. Newer `bcrypt` versions throw
a spurious "password cannot be longer than 72 bytes" error against
the version of `passlib` this app uses, which breaks login entirely.
This has bitten the project twice already from `requirements.txt`
drifting to `5.0.0`.

---

## Server layout

| Path | Purpose |
|---|---|
| `/var/www/puma` | Application code (this repo) |
| `/var/www/puma/.venv` | Python virtual environment |
| `/var/lib/puma/appmonitor.db` | SQLite database |
| `/var/lib/puma/backups/` | Local backup storage (`<app-slug>/<timestamp>-<run-id>/`) |
| `/var/lib/puma/.ssh/` | SSH keypair used by remote (SSH) backup strategies and probes |
| `/etc/puma/puma.env` | Environment file (secrets, paths, SMTP config) |
| `/etc/systemd/system/puma.service` | systemd unit |

The app runs as a dedicated, unprivileged `puma` system user.

---

## Installation

These steps assume a fresh Ubuntu/Debian-family server.

### 1. Create the service account and directories

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin puma
sudo mkdir -p /var/lib/puma /etc/puma
sudo chown -R puma:puma /var/lib/puma
```

### 2. Get the code and install dependencies

```bash
sudo git clone <this-repo-url> /var/www/puma
cd /var/www/puma
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Build the CSS

```bash
./tailwindcss -i app/static/css/input.css -o app/static/css/output.css --minify
```

(the `tailwindcss` binary itself and the built `output.css` are not
committed to the repo — both are produced/regenerated at deploy time)

### 4. Configure the environment

Create `/etc/puma/puma.env` — see [Configuration](#configuration)
below for the full list of variables.

### 5. Seed an admin user

```bash
sudo -u puma bash -c 'source /etc/puma/puma.env; exec /var/www/puma/.venv/bin/python app/seed_admin.py'
```

### 6. Set file ownership

```bash
sudo chown -R puma:puma /var/www/puma
```

(or keep the code owned by your deploy user with the `puma` group, if
you want to `git pull` without switching users — just make sure
`puma` has read access to everything under `app/`)

### 7. Install the systemd unit

```ini
# /etc/systemd/system/puma.service
[Unit]
Description=PUMA - Program Update Monitoring Assistant
After=network.target

[Service]
Type=simple
User=puma
Group=puma
WorkingDirectory=/var/www/puma/app
EnvironmentFile=/etc/puma/puma.env
ExecStart=/var/www/puma/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8124
Restart=on-failure
RestartSec=5
ProtectSystem=strict
ReadWritePaths=/var/lib/puma

[Install]
WantedBy=multi-user.target
```

`uvicorn` binds to `127.0.0.1` only — it is not reachable from outside
the box directly. Put a reverse proxy (nginx, etc.) in front to
terminate the public-facing port and forward to `127.0.0.1:8124`.

**A note on `ProtectSystem=strict` / `ReadWritePaths` / the absence of
`NoNewPrivileges`:** the unit above is deliberately hardened — the
filesystem is read-only to this process except for the paths listed.
Some backup strategies (see below) need `sudo` access to do their job
(e.g. `tar` running as root to read files owned by more than one
account), which required removing `NoNewPrivileges=true` from an
earlier, stricter version of this unit. If you don't need those
backup strategies, consider adding `NoNewPrivileges=true` back for a
meaningfully smaller attack surface. Any additional path a backup
strategy needs to write to (e.g. a shared `/backup` directory used by
an existing script) needs to be added to `ReadWritePaths` too, or that
strategy will fail with a read-only-filesystem error.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now puma.service
sudo systemctl status puma.service --no-pager
```

---

## Configuration

Environment variables read from `/etc/puma/puma.env`:

| Variable | Required | Purpose |
|---|---|---|
| `PUMA_SECRET_KEY` | Yes | Session signing key — generate a long random string and never change it after go-live (invalidates all sessions if changed) |
| `PUMA_DB_PATH` | No (defaults to `/var/lib/puma/appmonitor.db`) | SQLite database location |
| `PUMA_BACKUP_ROOT` | No (defaults to `/var/lib/puma/backups`) | Where local backup files are stored |
| SMTP settings | No | Required only if you want email notifications — check the Settings page in the app for the exact fields it expects |

---

## First-time setup

1. Log in with the admin account created by `seed_admin.py`.
2. Go to **Settings → Trusted Hosts** and add the hostname/IP of any
   app whose adapter or probe will take a URL you supply (this is
   enforced — an untrusted host is refused, by design).
3. Go to **Add Application** and add your first app (see below).

---

## Adding a monitored app

Each app needs an **update source** (how PUMA finds the latest
upstream version) and, optionally, an **installed-version probe**
(how PUMA finds out what version is actually running). The Add
Application form shows a short explanation for each option as you
select it.

**Update sources / probes available:**

| Key | What it's for |
|---|---|
| `github_releases` | GitHub Releases — the default choice for most self-hosted OSS |
| `github_tags` | GitHub tags, for projects without a Releases feed |
| `gitlab_releases` | GitLab Releases |
| `atom_feed` | A project's Atom/RSS feed |
| `json_api` | Any JSON endpoint, with a dotted path to the version field and an optional auth header |
| `html_regex` | Scraping a web page with a regex — last resort, brittle |
| `local_file_regex` | Reading a version string out of a local file (e.g. a `version.php`) |
| `git_local_tag` | `git describe --tags` on a local checkout |
| `http_header` | Reading the version from an HTTP response header |
| `glpi_rest_api` | GLPI-specific, logs in via GLPI's own API |
| `stanza_api` | Stanza (Telestream)-specific |
| `ssh_command_regex` | Runs a command over SSH on a *different* server and extracts the version with a regex — for an app whose own API is impractical to check directly (e.g. requires admin-tier permissions) |

---

## Backups

PUMA can take a pre-upgrade safety snapshot of an app, either on
demand (**Backup Now**) or on its own fixed schedule (a manual backup
never pushes a scheduled one's next run further out — they're tracked
independently). The last 5 runs per app are kept; older ones are
pruned automatically, files and history together.

**Strategies available:**

| Key | For an app that's... |
|---|---|
| `file_and_db_dump` | Installed directly on this server, outside Docker |
| `git_checkout_and_db_dump` | Deployed as a plain `git clone` on this server |
| `docker_volumes_and_db_dump` | Running in Docker, on this same server |
| `run_script` | Backed up by its own existing tool/script (e.g. GitLab's `gitlab-backup create`) |
| `ssh_files_and_db_dump` | Hosted on a **different** server than PUMA — files and/or a database dump are pulled over SSH; can also dump a database via `docker exec` on the remote host if that database has no port exposed to the network |

**Server-side prerequisites vary by strategy** and by exactly how a
given app is deployed — expect to grant one or more of:

- Read access to the app's files (an ACL grant is usually cleaner
  than adding `puma` to another group)
- `puma` in the `docker` group, for local Docker strategies —
  **this is equivalent to root on the host**, grant deliberately
- A narrowly scoped `sudo` rule for one exact command, for
  `run_script` (e.g. `puma ALL=(root) NOPASSWD: /usr/bin/gitlab-backup create`)
- An SSH keypair at `/var/lib/puma/.ssh/id_ed25519` (or `id_ecdsa` /
  `id_rsa`), with the public key authorized on the remote host/user,
  for any SSH-based strategy or probe

None of these are granted automatically — PUMA never elevates its own
privileges.

---

## Procedures

Each app can have a written **Upgrade Procedure** and **Restore
Procedure**, in Markdown, entered from that app's Edit page. These
are:

- Rendered (not raw) wherever they're shown, and sanitized before
  display — **Procedures pages are viewable by any logged-in user,
  not just admins**, so don't paste real credentials into the text
  itself; reference "see this app's Backup config" instead, since
  that page is admin-only.
- Shown inline on the Backups page when viewing one app's history,
  and on their own page at `/procedures/<app-id>`.
- Flagged with a warning banner on the Backups page if an app has a
  backup strategy configured but no restore procedure written yet.

**A Markdown formatting note that matters in practice:** keep
multi-line commands in their own standalone fenced code block
(triple backticks), never indented under a numbered list item. The
renderer needs exact leading whitespace to keep a code block attached
to a list item, and that whitespace is easy to lose when copying
through a browser and a clipboard — losing it makes the renderer end
the list early and restart numbering at 1 for whatever follows.
Standalone blocks between plain paragraphs don't have this problem.

---

## Deploying an update to PUMA itself

```bash
cd /var/www/puma
sudo -u <deploy-user> git pull
git log -1 --oneline

# Run any new migration scripts (see below) - safe to run even if
# there's nothing new to add
sudo -u puma .venv/bin/python app/migrate_backup_columns.py "$PUMA_DB_PATH"
sudo -u puma .venv/bin/python app/migrate_procedure_columns.py "$PUMA_DB_PATH"

sudo find /var/www/puma -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
./tailwindcss -i app/static/css/input.css -o app/static/css/output.css --minify

sudo -u puma bash -c 'source /etc/puma/puma.env; exec .venv/bin/python -m py_compile app/main.py' && echo "syntax OK"
sudo systemctl restart puma.service
sleep 2
sudo systemctl status puma.service --no-pager | head -5
```

**There is no migration framework (no Alembic).** `SQLModel.metadata.create_all()`
runs automatically on startup, but it only ever *creates tables that
don't exist yet* — it never adds a column to a table that already
exists. Any change that adds a column to an existing table needs its
own small, idempotent migration script (see the two already in
`app/`, which check `PRAGMA table_info` before altering anything, so
they're safe to run repeatedly). If you add a new column to
`MonitoredApp` or another existing table, write one.

If a change adds a new Python dependency, install it before
restarting:

```bash
sudo -u puma bash -c 'source /etc/puma/puma.env; exec .venv/bin/pip install -r /var/www/puma/requirements.txt'
```

---

## API

**`GET /api/v1/dashboard`** — unauthenticated, read-only. Returns a
KPI summary and per-app status/version/backup info. Relies entirely on
network-level restriction (e.g. a firewall rule) rather than a login —
this is the one part of PUMA reachable without authentication, by
design choice, and returns status/version fields only. It never
returns credentials or tokens, even though those live in the same
database rows.

**Interactive docs (Swagger UI):** `/api/v1/docs` — a dedicated
sub-app mounted at `/api/v1`, kept separate from the main app so only
actual API endpoints show up here, not PUMA's HTML pages. The raw
OpenAPI schema is at `/api/v1/openapi.json`. Add any future API
endpoint to `api_app` in `main.py` (not `app`) and it appears here
automatically, fully typed, with no extra bookkeeping needed to keep
HTML routes out of it.

---

## Troubleshooting

**Login broken after a dependency update** — check `bcrypt`'s pinned
version in `requirements.txt` first (see [Tech stack](#tech-stack)).

**A backup fails with "Permission denied" on a file that looks
readable** — check whether the *directory's* ACL was actually granted,
not just the file's; Linux requires execute/traverse permission on
every parent directory to reach a file at all, separate from read
permission on the file itself. Also watch for a directory's ACL
**mask** getting silently reset by an unrelated `chmod` on any file
inside it (a classic offender: a running app rewriting its own
session/cache files) — this quietly caps every ACL grant on that tree
back down without any error until the next thing tries to read it.

**A backup or check fails with a `sudo`/permission error even though
the sudoers rule looks right** — check `NoNewPrivileges` in
`puma.service` first (see the note under Installation above) and
whether the account in question is a local Windows administrator
being hit by token filtering on a remote/non-interactive logon (for
Windows-based apps specifically).

**"No such column" error right after a deploy** — a migration script
wasn't run. See [Deploying an update to PUMA itself](#deploying-an-update-to-puma-itself).
