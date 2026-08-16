"""Backup strategies.

Contract: a strategy takes a config dict plus a destination directory
(already created, empty) and does whatever's needed to leave a usable
backup inside it, returning a BackupResult. It knows nothing about the
database, the scheduler or HTML - same separation as adapters.py.

The `fields` classvar does the same double duty it does in adapters.py:
it's what the Add/Edit Application GUI renders for whichever strategy is
selected. Add a new strategy class here and its form appears with no
frontend changes.

Every strategy method that shells out logs the command and captures its
output into the `log` list it's given, so a failed backup leaves a real
trail in the Backups history page instead of a bare "it didn't work".
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional


# The puma service account is a locked-down system user with no real
# home directory (/nonexistent, /usr/sbin/nologin), so `ssh` can't rely
# on its usual $HOME/.ssh/{known_hosts,id_*} defaults - it needs to be
# told explicitly where to look. Reuse the same writable directory (and
# the same env var) main.py already uses for the DB/logos/etc, rather
# than introducing a second convention.
_DB_PATH = os.environ.get("PUMA_DB_PATH", "/var/lib/puma/appmonitor.db")
SSH_DIR = Path(_DB_PATH).parent / ".ssh"
SSH_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Data carried between layers
# --------------------------------------------------------------------------

@dataclass
class BackupResult:
    success: bool
    size_bytes: int = 0
    error: Optional[str] = None


class BackupFailed(RuntimeError):
    """Raised by a strategy's run() on any failure - caught in one place
    by whatever calls run_backup(), same shape as adapters.SourceUnavailable."""


@dataclass
class Field_:
    """One input in the generated GUI form - same shape as adapters.Field_."""
    name: str
    label: str
    type: str = "text"          # text | password | select | textarea | checkbox
    required: bool = True
    placeholder: str = ""
    help: str = ""
    choices: Optional[list[str]] = None


# --------------------------------------------------------------------------
# Base + registry
# --------------------------------------------------------------------------

REGISTRY: dict[str, type["BackupStrategy"]] = {}


def register(cls: type["BackupStrategy"]) -> type["BackupStrategy"]:
    REGISTRY[cls.key] = cls
    return cls


class BackupStrategy(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    fields: ClassVar[list[Field_]] = []
    hint: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        ...

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Fed to the GUI alongside the source-type catalogue."""
        return {
            "key": cls.key,
            "label": cls.label,
            "hint": cls.hint,
            "fields": [f.__dict__ for f in cls.fields],
        }

    # -- shared helpers, available to every strategy below ----------------

    def _run(
        self,
        cmd: list[str],
        log: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout: int = 1800,
        stdout_file: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        """Run a command, logging it (with any env overrides named but not
        valued, so passwords never land in the log) and its output."""
        redacted_env_names = sorted((env or {}).keys())
        shown = " ".join(shlex.quote(c) for c in cmd)
        if redacted_env_names:
            shown = f"[env: {', '.join(redacted_env_names)}] " + shown
        log.append("$ " + shown)

        full_env = {**os.environ, **(env or {})}
        if stdout_file is not None:
            with open(stdout_file, "wb") as fh:
                proc = subprocess.run(
                    cmd, cwd=cwd, env=full_env, stdout=fh,
                    stderr=subprocess.PIPE, timeout=timeout,
                )
        else:
            proc = subprocess.run(
                cmd, cwd=cwd, env=full_env,
                capture_output=True, timeout=timeout,
            )
        if proc.stdout and stdout_file is None:
            log.append(_tail(proc.stdout.decode("utf-8", "replace")))
        if proc.stderr:
            log.append(_tail(proc.stderr.decode("utf-8", "replace")))
        if proc.returncode != 0:
            raise BackupFailed(
                f"`{cmd[0]}` exited {proc.returncode} — see log for details"
            )
        return proc

    def _tar_paths(self, paths: list[str], out_path: Path, log: list[str]) -> None:
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise BackupFailed(
                f"path(s) not found or not readable by the puma user: {', '.join(missing)}"
            )
        log.append(f"Archiving {len(paths)} path(s) -> {out_path.name}")
        with tarfile.open(out_path, "w:gz") as tar:
            for p in paths:
                tar.add(p, arcname=os.path.basename(p.rstrip("/")))
        log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")

    def _tar_paths_as_root(self, paths: list[str], out_path: Path, log: list[str]) -> None:
        """Same job as _tar_paths, but shells out to `sudo tar` instead of
        reading the paths directly as the puma user. For a mix of paths
        owned by different accounts (e.g. a web app's www-data-owned data
        directory alongside root-owned webserver/cron config), granting
        puma piecemeal read access to each one via ACLs is fragile - a
        process resetting one directory's ACL mask (a bare chmod on any
        file inside it does this) silently caps every grant on that tree
        back down, which is exactly the kind of thing that breaks again
        later without warning. Root reads all of it unconditionally
        instead, so this sidesteps the whole category of problem - at
        the cost of needing a sudo grant for this exact command shape to
        already exist on the server (see this strategy's own hint text)."""
        log.append(f"Archiving {len(paths)} path(s) as root -> {out_path.name}")
        self._run(["sudo", "tar", "czf", str(out_path), *paths], log)
        log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")

    def _dump_db(
        self, engine: str, host: str, name: str, user: str, password: str,
        out_path: Path, log: list[str],
    ) -> None:
        if engine == "postgres":
            cmd = ["pg_dump", "-h", host or "localhost", "-U", user, "-d", name,
                   "-F", "c", "-f", str(out_path)]
            env = {"PGPASSWORD": password} if password else None
        elif engine == "mysql":
            cmd = ["mysqldump", "-h", host or "localhost", "-u", user, name]
            env = {"MYSQL_PWD": password} if password else None
            self._run(cmd, log, env=env, stdout_file=out_path)
            log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")
            return
        else:
            raise BackupFailed(f"unknown db_engine {engine!r}")
        self._run(cmd, log, env=env)
        log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")


def _tail(text: str, max_lines: int = 200) -> str:
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        lines = ["... ({} lines truncated) ...".format(len(lines) - max_lines)] + lines[-max_lines:]
    return "\n".join(lines)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------
# Concrete strategies
# --------------------------------------------------------------------------

@register
class FileAndDbDump(BackupStrategy):
    """Local files + an optional database dump - e.g. Moodle's web root
    and moodledata directories, plus its database. Needs the puma service
    account to have read access to the listed paths (and, if a DB is
    configured, working DB client tools + credentials) - both are things
    to confirm on the server the first time this strategy is set up for
    an app, not something PUMA can grant itself."""

    key = "file_and_db_dump"
    label = "Files + database dump"
    hint = ("For an app installed on this server outside Docker (e.g. "
            "Moodle). Tars the listed director(ies) and, optionally, dumps "
            "a MySQL/MariaDB or PostgreSQL database alongside them. "
            "Either the puma service account needs read access to every "
            "path listed, or check \"Run as root\" and grant "
            "`puma ALL=(root) NOPASSWD: /usr/bin/tar czf *` in a sudoers "
            "file - useful when the paths span more than one owner (e.g. "
            "app data plus a webserver config file), since piecemeal ACL "
            "grants across mixed ownership are fragile.")
    fields = [
        Field_("paths", "Paths to back up (one per line)", type="textarea",
               placeholder="/var/www/moodle\n/var/www/moodledata"),
        Field_("run_as_root", "Run as root (sudo)", type="checkbox", required=False,
               help="For paths owned by more than one account. Needs a matching sudoers grant on the server first - see the note above."),
        Field_("db_engine", "Database engine", type="select", required=False,
               choices=["none", "mysql", "postgres"]),
        Field_("db_host", "Database host", required=False, placeholder="localhost"),
        Field_("db_name", "Database name", required=False),
        Field_("db_user", "Database user", required=False),
        Field_("db_password", "Database password", type="password", required=False),
    ]

    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        paths = [p.strip() for p in self.config.get("paths", "").splitlines() if p.strip()]
        if not paths:
            raise BackupFailed("no paths configured")
        if self.config.get("run_as_root") == "on":
            self._tar_paths_as_root(paths, dest_dir / "files.tar.gz", log)
        else:
            self._tar_paths(paths, dest_dir / "files.tar.gz", log)

        engine = self.config.get("db_engine") or "none"
        if engine != "none":
            ext = "dump" if engine == "postgres" else "sql"
            self._dump_db(
                engine,
                self.config.get("db_host", ""), self.config.get("db_name", ""),
                self.config.get("db_user", ""), self.config.get("db_password", ""),
                dest_dir / f"database.{ext}", log,
            )
        return BackupResult(success=True, size_bytes=_dir_size(dest_dir))


@register
class GitCheckoutAndDbDump(BackupStrategy):
    """A plain `git clone` + `git pull` deployment (not Docker) - e.g.
    Shelf.nu. The checkout itself is reproducible from git history, so
    this records the exact commit it's on (in case an upgrade moves it
    forward and you need to know exactly where you were) plus, if
    configured, its database - usually the part that actually can't be
    reconstructed from git."""

    key = "git_checkout_and_db_dump"
    label = "Git checkout + database dump"
    hint = ("For an app deployed on this server as a plain `git clone` + "
            "`git pull` (not Docker) - e.g. Shelf.nu. Records the exact "
            "commit it's on and, optionally, dumps its database.")
    fields = [
        Field_("repo_path", "Repository path", placeholder="/var/www/shelf.nu"),
        Field_("db_engine", "Database engine", type="select", required=False,
               choices=["none", "mysql", "postgres"]),
        Field_("db_host", "Database host", required=False, placeholder="localhost"),
        Field_("db_name", "Database name", required=False),
        Field_("db_user", "Database user", required=False),
        Field_("db_password", "Database password", type="password", required=False),
    ]

    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        repo_path = self.config.get("repo_path", "")
        if not repo_path or not os.path.isdir(repo_path):
            raise BackupFailed(f"repository path not found: {repo_path!r}")
        proc = self._run(
            ["git", "-c", f"safe.directory={repo_path}", "-C", repo_path,
             "rev-parse", "HEAD"],
            log,
        )
        commit = proc.stdout.decode().strip() if proc.stdout else ""
        (dest_dir / "commit.txt").write_text(f"{commit}\n")
        log.append(f"Recorded commit {commit}")

        engine = self.config.get("db_engine") or "none"
        if engine != "none":
            ext = "dump" if engine == "postgres" else "sql"
            self._dump_db(
                engine,
                self.config.get("db_host", ""), self.config.get("db_name", ""),
                self.config.get("db_user", ""), self.config.get("db_password", ""),
                dest_dir / f"database.{ext}", log,
            )
        return BackupResult(success=True, size_bytes=_dir_size(dest_dir))


@register
class DockerVolumesAndDbDump(BackupStrategy):
    """A Docker-deployed app - e.g. XWiki. Tars the named volume(s) via a
    disposable helper container (no direct filesystem access needed), and
    optionally pg_dump/mysqldump's the database straight from inside its
    own container. Needs the puma service account to be in the `docker`
    group - worth knowing that's equivalent to root on this host, since
    the docker group can bind-mount anything."""

    key = "docker_volumes_and_db_dump"
    label = "Docker volumes + database dump"
    hint = ("For an app running in Docker (e.g. XWiki). Snapshots the "
            "named volume(s) via a disposable helper container, and can "
            "pg_dump/mysqldump the database directly from its own "
            "container. Needs the puma service account in the `docker` "
            "group - that's equivalent to root on this host, so grant it "
            "deliberately.")
    fields = [
        Field_("volumes", "Named volumes to snapshot (one per line)", type="textarea",
               required=False, placeholder="xwiki-docker_data"),
        Field_("db_container", "Database container name", required=False,
               placeholder="xwiki-docker-db-1"),
        Field_("db_engine", "Database engine", type="select", required=False,
               choices=["none", "postgres", "mysql"]),
        Field_("db_name", "Database name", required=False),
        Field_("db_user", "Database user", required=False),
        Field_("db_password", "Database password", type="password", required=False),
    ]

    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        volumes = [v.strip() for v in self.config.get("volumes", "").splitlines() if v.strip()]
        for vol in volumes:
            out_name = f"{vol}.tar.gz"
            self._run(
                ["docker", "run", "--rm",
                 "-v", f"{vol}:/vol:ro",
                 "-v", f"{dest_dir}:/backup",
                 "alpine", "sh", "-c",
                 f"tar czf /backup/{out_name} -C /vol ."],
                log,
            )
            size_path = dest_dir / out_name
            if size_path.exists():
                log.append(f"{out_name}: {_human_size(size_path.stat().st_size)}")

        engine = self.config.get("db_engine") or "none"
        container = self.config.get("db_container", "")
        if engine != "none":
            if not container:
                raise BackupFailed("db_container is required when db_engine is set")
            name = self.config.get("db_name", "")
            user = self.config.get("db_user", "")
            password = self.config.get("db_password", "")
            ext = "dump" if engine == "postgres" else "sql"
            out_path = dest_dir / f"database.{ext}"
            if engine == "postgres":
                cmd = ["docker", "exec"]
                if password:
                    cmd += ["-e", f"PGPASSWORD={password}"]
                cmd += [container, "pg_dump", "-U", user, "-Fc", "-d", name]
                self._run(cmd, log, stdout_file=out_path)
            else:
                cmd = ["docker", "exec"]
                if password:
                    cmd += ["-e", f"MYSQL_PWD={password}"]
                cmd += [container, "mysqldump", "-u", user, name]
                self._run(cmd, log, stdout_file=out_path)
            log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")

        if not volumes and engine == "none":
            raise BackupFailed("nothing configured to back up (no volumes, no database)")
        return BackupResult(success=True, size_bytes=_dir_size(dest_dir))


@register
class RunScript(BackupStrategy):
    """Escape hatch for an app with its own mature backup tool - e.g.
    GitLab's own `gitlab-backup create`. Runs a command, then copies
    whatever files match a glob pattern into this run's backup folder.
    Whatever the command needs (sudo, being run as a specific user) has
    to already be permitted for the puma service account on the server -
    PUMA does not attempt to elevate its own privileges."""

    key = "run_script"
    label = "Run a backup script"
    hint = ("For an app that already has its own backup tool (e.g. "
            "GitLab's gitlab-backup create). Runs a command, then copies "
            "whatever files match the given pattern into this run's "
            "folder. Whatever privilege the command needs (sudo, a "
            "specific user) must already be granted to the puma service "
            "account on the server.")
    fields = [
        Field_("command", "Command", placeholder="sudo gitlab-backup create"),
        Field_("working_dir", "Working directory", required=False),
        Field_("artifact_glob", "Where to find the result afterward", required=False,
               placeholder="/var/opt/gitlab/backups/*_gitlab_backup.tar",
               help="PUMA copies whatever matches this pattern into its own dated backup folder. Leave blank if the command's own console output is all you need captured."),
    ]

    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        command = self.config.get("command", "")
        if not command:
            raise BackupFailed("no command configured")
        self._run(shlex.split(command), log, cwd=self.config.get("working_dir") or None)

        pattern = self.config.get("artifact_glob", "")
        copied = 0
        if pattern:
            matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
            if not matches:
                raise BackupFailed(
                    f"command ran but nothing matched artifact pattern {pattern!r}"
                )
            newest = matches[0]
            dest = dest_dir / os.path.basename(newest)
            self._run(["cp", "-a", newest, str(dest)], log)
            copied = 1
            log.append(f"Copied {newest} -> {dest}")

        if copied == 0 and not pattern:
            log.append("No artifact pattern configured - only the command's own output was captured.")
        return BackupResult(success=True, size_bytes=_dir_size(dest_dir))


@register
class SshFilesAndDbDump(BackupStrategy):
    """Files on a *different* server than PUMA itself, reachable over
    SSH - e.g. GLPI running on its own VM. Streams the remote path(s)
    straight into a local tar.gz via `ssh ... tar czf -` (no separate
    remote-to-local copy step, no scratch space needed on the remote
    host). The database dump reuses the same local mysqldump/pg_dump
    path as FileAndDbDump - that only works if the database itself is
    reachable over the network from the PUMA host (common when both are
    on the same private LAN); it is not tunnelled through the SSH
    connection used for the files. Needs the puma service account to
    have passwordless (key-based) SSH access to the remote host/user -
    that's a prerequisite to set up on the servers themselves, the same
    way DockerVolumesAndDbDump needs the puma account in the `docker`
    group first."""

    key = "ssh_files_and_db_dump"
    label = "Remote (SSH) files + database dump"
    hint = ("For an app hosted on a *different* server than PUMA, e.g. "
            "GLPI on its own VM (Linux) or a Windows service like Stanza "
            "GMA. Streams the listed remote path(s) into a local tar.gz "
            "over SSH, and can optionally dump a MySQL/MariaDB or "
            "PostgreSQL database too - but only if that database is "
            "reachable directly over the network from the PUMA server "
            "(the dump isn't tunnelled through SSH). Needs a key at "
            "/var/lib/puma/.ssh/id_ed25519 (or id_ecdsa/id_rsa) "
            "authorized on the remote host/user - the puma account has "
            "no real home directory, so keys must live there specifically, "
            "not wherever `ssh-keygen`'s own default would be. On "
            "Windows, that means either the user's own "
            "C:\\Users\\<user>\\.ssh\\authorized_keys, or, if that account "
            "is a local administrator, the system-wide "
            "C:\\ProgramData\\ssh\\administrators_authorized_keys instead "
            "(with SYSTEM/Administrators-only permissions) - and the "
            "remote machine needs tar.exe reachable on PATH, which ships "
            "built into Windows 10 1803+ / Server 2019+.")
    fields = [
        Field_("ssh_host", "SSH host", placeholder="10.0.0.161"),
        Field_("ssh_user", "SSH user", placeholder="postmams"),
        Field_("ssh_port", "SSH port", required=False, placeholder="22"),
        Field_("remote_os", "Remote OS", type="select", required=False, choices=["linux", "windows"],
               help="How paths and exclude patterns get quoted for the remote shell. Linux paths go "
                    "through a POSIX shell (sh/bash); Windows paths go through cmd.exe, which quotes "
                    "arguments completely differently - in particular it doesn't understand the single "
                    "quotes a POSIX shell would use, and backslashes (which every Windows path has) "
                    "would otherwise force POSIX-style quoting to kick in on every single path."),
        Field_("paths", "Remote paths to back up (one per line)", type="textarea",
               placeholder="/var/www/glpi"),
        Field_("exclude", "Exclude patterns (one per line, optional)", type="textarea", required=False,
               placeholder="_sessions",
               help="Matched against any path component (GNU tar --exclude semantics), so a bare name like _sessions skips that directory wherever it appears. Use for transient data - session files, caches, tmp/lock dirs - that has no backup value and is often unreadable by the SSH backup account anyway since it's owned by the web server user."),
        Field_("db_engine", "Database engine", type="select", required=False,
               choices=["none", "mysql", "postgres"]),
        Field_("db_host", "Database host (reachable directly from PUMA)", required=False,
               placeholder="10.0.0.161"),
        Field_("db_name", "Database name", required=False),
        Field_("db_user", "Database user", required=False),
        Field_("db_password", "Database password", type="password", required=False),
    ]

    @staticmethod
    def _win_quote(s: str) -> str:
        """Quote a single argument the way cmd.exe / the standard Windows
        argv parser expects - wrap in double quotes and escape any literal
        quote characters. Deliberately NOT shlex.quote: POSIX quoting
        treats backslash as needing escaping, so it would wrap every
        Windows path in single quotes, which cmd.exe doesn't treat as
        quoting at all - the literal quote characters would end up as
        part of the path tar.exe tries to open. Doesn't attempt to
        escape cmd.exe's own metacharacters (&, |, %, ^, ...) since none
        of those are legal in a real Windows path anyway; this only needs
        to survive cmd.exe treating the argument as one token."""
        return '"' + s.replace('"', '\\"') + '"'

    def run(self, dest_dir: Path, log: list[str]) -> BackupResult:
        host = self.config.get("ssh_host", "")
        user = self.config.get("ssh_user", "")
        if not host or not user:
            raise BackupFailed("ssh_host and ssh_user are required")
        port = self.config.get("ssh_port") or "22"
        paths = [p.strip() for p in self.config.get("paths", "").splitlines() if p.strip()]
        if not paths:
            raise BackupFailed("no remote paths configured")
        is_windows = (self.config.get("remote_os") or "linux") == "windows"
        quote = self._win_quote if is_windows else shlex.quote

        remote_cmd = "tar czf -"
        excludes = [e.strip() for e in self.config.get("exclude", "").splitlines() if e.strip()]
        for e in excludes:
            remote_cmd += " --exclude=" + quote(e)
        remote_cmd += " " + " ".join(quote(p) for p in paths)
        known_hosts = SSH_DIR / "known_hosts"
        ssh_cmd = [
            "ssh", "-p", str(port),
            "-o", "BatchMode=yes",  # fail fast instead of hanging on a password prompt
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
        ]
        # puma's $HOME (/nonexistent) means ssh won't find a key on its
        # own either - point it at whichever of the usual key filenames
        # actually exist under SSH_DIR instead of guessing one.
        for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
            candidate = SSH_DIR / name
            if candidate.exists():
                ssh_cmd += ["-i", str(candidate)]
        ssh_cmd += [f"{user}@{host}", remote_cmd]
        out_path = dest_dir / "files.tar.gz"
        self._run(ssh_cmd, log, stdout_file=out_path, timeout=3600)
        log.append(f"{out_path.name}: {_human_size(out_path.stat().st_size)}")

        engine = self.config.get("db_engine") or "none"
        if engine != "none":
            ext = "dump" if engine == "postgres" else "sql"
            self._dump_db(
                engine,
                self.config.get("db_host", "") or host,
                self.config.get("db_name", ""),
                self.config.get("db_user", ""),
                self.config.get("db_password", ""),
                dest_dir / f"database.{ext}", log,
            )
        return BackupResult(success=True, size_bytes=_dir_size(dest_dir))


def catalogue() -> list[dict[str, Any]]:
    """Fed to the Add/Edit Application GUI, same role as adapters.catalogue()."""
    return [cls.describe() for cls in REGISTRY.values()]
