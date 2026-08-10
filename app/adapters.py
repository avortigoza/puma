"""Source adapters.

Contract: an adapter takes a config dict and returns a list of Release
objects. It knows nothing about the database, the scheduler or HTML.

The `fields` classvar is doing double duty: it validates config AND it is
what the GUI renders as the "Add application" form. Add a new adapter
class and the form for it appears with no frontend changes. That is the
whole trick behind "add an app from the GUI".
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Optional

import httpx


# --------------------------------------------------------------------------
# Data carried between layers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Release:
    raw_tag: str
    published_at: Optional[datetime] = None
    url: Optional[str] = None
    prerelease: bool = False
    notes: Optional[str] = None


@dataclass
class FetchResult:
    releases: list[Release] = field(default_factory=list)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False


@dataclass
class Field_:
    """One input in the generated GUI form."""
    name: str
    label: str
    type: str = "text"          # text | password | url | select | checkbox
    required: bool = True
    placeholder: str = ""
    help: str = ""
    choices: Optional[list[str]] = None


class SourceUnavailable(RuntimeError):
    """Transient upstream problem — retry, don't disable the app."""


# --------------------------------------------------------------------------
# Base + registry
# --------------------------------------------------------------------------

REGISTRY: dict[str, type["SourceAdapter"]] = {}


def register(cls: type["SourceAdapter"]) -> type["SourceAdapter"]:
    REGISTRY[cls.key] = cls
    return cls


class SourceAdapter(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    fields: ClassVar[list[Field_]] = []
    default_pattern: ClassVar[Optional[str]] = None

    def __init__(self, config: dict[str, Any], client: httpx.Client):
        self.config = config
        self.client = client

    @abstractmethod
    def fetch(
        self, etag: Optional[str] = None, last_modified: Optional[str] = None
    ) -> FetchResult:
        ...

    # shared helper: conditional GET, so 304s cost no rate limit
    def _get(self, url: str, etag=None, last_modified=None, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            r = self.client.get(url, headers=headers, timeout=20, **kw)
        except httpx.HTTPError as e:
            raise SourceUnavailable(str(e)) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise SourceUnavailable(f"{r.status_code} from {url}")
        return r

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Fed to the GUI at GET /api/source-types."""
        return {
            "key": cls.key,
            "label": cls.label,
            "default_pattern": cls.default_pattern,
            "fields": [f.__dict__ for f in cls.fields],
        }


# --------------------------------------------------------------------------
# Concrete adapters
# --------------------------------------------------------------------------

@register
class GitHubReleases(SourceAdapter):
    """GLPI, XWiki, Shelf — anything that cuts real GitHub Releases."""

    key = "github_releases"
    label = "GitHub — releases"
    fields = [
        Field_("repo", "Repository", placeholder="glpi-project/glpi",
               help="owner/name"),
        Field_("token", "API token", type="password", required=False,
               help="Optional. Raises the rate limit from 60/hr to 5000/hr."),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        repo = self.config["repo"]
        headers = {"Accept": "application/vnd.github+json"}
        if self.config.get("token"):
            headers["Authorization"] = f"Bearer {self.config['token']}"
        r = self._get(
            f"https://api.github.com/repos/{repo}/releases?per_page=30",
            etag=etag, last_modified=last_modified, headers=headers,
        )
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        rels = [
            Release(
                raw_tag=item["tag_name"],
                published_at=_iso(item.get("published_at")),
                url=item.get("html_url"),
                prerelease=bool(item.get("prerelease") or item.get("draft")),
                notes=(item.get("body") or "")[:4000],
            )
            for item in r.json()
        ]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


@register
class GitHubTags(SourceAdapter):
    """For projects that tag but don't publish Releases — e.g. moodle/moodle."""

    key = "github_tags"
    label = "GitHub — tags"
    default_pattern = r"v?(\d+\.\d+(?:\.\d+)?)"
    fields = [
        Field_("repo", "Repository", placeholder="moodle/moodle"),
        Field_("token", "API token", type="password", required=False),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        repo = self.config["repo"]
        headers = {"Accept": "application/vnd.github+json"}
        if self.config.get("token"):
            headers["Authorization"] = f"Bearer {self.config['token']}"
        r = self._get(f"https://api.github.com/repos/{repo}/tags?per_page=100",
                      etag=etag, last_modified=last_modified, headers=headers)
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        rels = [
            Release(raw_tag=t["name"],
                    url=f"https://github.com/{repo}/releases/tag/{t['name']}")
            for t in r.json()
        ]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


@register
class GitLabReleases(SourceAdapter):
    """GitLab itself, and any self-hosted GitLab project."""

    key = "gitlab_releases"
    label = "GitLab — releases"
    fields = [
        Field_("base_url", "Instance URL", type="url",
               placeholder="https://gitlab.com"),
        Field_("project", "Project path", placeholder="gitlab-org/gitlab"),
        Field_("token", "Private token", type="password", required=False),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        base = self.config["base_url"].rstrip("/")
        project = httpx.URL(self.config["project"]).path.strip("/")
        encoded = project.replace("/", "%2F")
        headers = {}
        if self.config.get("token"):
            headers["PRIVATE-TOKEN"] = self.config["token"]
        r = self._get(f"{base}/api/v4/projects/{encoded}/releases?per_page=30",
                      etag=etag, last_modified=last_modified, headers=headers)
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        rels = [
            Release(raw_tag=item["tag_name"],
                    published_at=_iso(item.get("released_at")),
                    url=(item.get("_links") or {}).get("self"),
                    prerelease=bool(item.get("upcoming_release")),
                    notes=(item.get("description") or "")[:4000])
            for item in r.json()
        ]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


@register
class AtomFeed(SourceAdapter):
    """No auth, no rate limit. Every GitHub repo exposes /releases.atom,
    and most projects have a release feed somewhere. Good default."""

    key = "atom_feed"
    label = "Atom / RSS feed"
    fields = [
        Field_("url", "Feed URL", type="url",
               placeholder="https://github.com/xwiki/xwiki-platform/releases.atom"),
        Field_("entry_field", "Read version from", type="select",
               choices=["title", "id", "link"], required=False),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        import feedparser
        r = self._get(self.config["url"], etag=etag, last_modified=last_modified)
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        which = self.config.get("entry_field") or "title"
        parsed = feedparser.parse(r.content)
        rels = [
            Release(raw_tag=str(e.get(which) or e.get("title") or ""),
                    published_at=_struct(e.get("published_parsed")),
                    url=e.get("link"))
            for e in parsed.entries
        ]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


@register
class LocalFileRegex(SourceAdapter):
    """Read a version straight off disk - e.g. Moodle's version.php on the
    same box PUMA runs on. No network call, no auth, nothing to rate-limit.
    Most reliable of all the adapters when it's an option at all."""

    key = "local_file_regex"
    label = "Local file (regex)"
    fields = [
        Field_("path", "File path",
               placeholder="/var/www/moodle/public/version.php"),
        Field_("pattern", "Regex (1 capture group)",
               placeholder=r"\$release\s*=\s*'([\d.]+)"),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        path = self.config["path"]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            raise SourceUnavailable(f"cannot read {path}: {e}") from e
        found = re.findall(self.config["pattern"], text)
        rels = [
            Release(raw_tag=m if isinstance(m, str) else m[0])
            for m in found
        ]
        return FetchResult(rels)


@register
class GitLocalTag(SourceAdapter):
    """Read the checked-out release straight off a local git clone - e.g.
    shelf.nu deployed as a bare `git clone` + `git pull` on this box, no
    Docker involved. Uses `git describe --tags` so it still resolves a base
    version even when HEAD is sitting N commits ahead of the last tag
    (tracking `main` instead of pinned to an exact release). The safe.directory
    override is passed inline (not written to global git config) so this
    works regardless of which OS user owns the checkout."""

    key = "git_local_tag"
    label = "Local git checkout"
    fields = [
        Field_("path", "Repository path",
               placeholder="/var/www/shelf.nu",
               help="Directory containing the .git folder"),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        import subprocess
        path = self.config["path"]
        try:
            proc = subprocess.run(
                ["git", "-c", f"safe.directory={path}", "-C", path,
                 "describe", "--tags"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except OSError as e:
            raise SourceUnavailable(f"git not available: {e}") from e
        if proc.returncode != 0:
            raise SourceUnavailable(
                f"git describe failed in {path}: {proc.stderr.strip()[:200]}"
            )
        tag = proc.stdout.strip()
        if not tag:
            return FetchResult([])
        return FetchResult([Release(raw_tag=tag)])


@register
class JsonApi(SourceAdapter):
    """Escape hatch #1: any endpoint returning JSON, plus a dotted path.
    Path supports list indexing: 'releases.0.version' or 'versions[]' ."""

    key = "json_api"
    label = "JSON endpoint"
    fields = [
        Field_("url", "URL", type="url"),
        Field_("path", "Path to version", placeholder="releases.0.version"),
        Field_("header_name", "Auth header name", required=False,
               placeholder="PRIVATE-TOKEN",
               help="Optional. e.g. PRIVATE-TOKEN for GitLab, Authorization for Bearer tokens."),
        Field_("header_value", "Auth header value", type="password", required=False),
        Field_("url_template", "Release URL template", required=False,
               placeholder="https://example.org/download/{version}"),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        headers = {}
        if self.config.get("header_name") and self.config.get("header_value"):
            headers[self.config["header_name"]] = self.config["header_value"]
        r = self._get(self.config["url"], etag=etag, last_modified=last_modified,
                      headers=headers)
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        values = _dig(r.json(), self.config["path"])
        tmpl = self.config.get("url_template")
        rels = [
            Release(raw_tag=str(v),
                    url=tmpl.format(version=v) if tmpl else None)
            for v in values
        ]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


@register
class GlpiRestApi(SourceAdapter):
    """GLPI's REST API needs a login step (initSession) before ANY other
    call works - unlike every other adapter here, a single plain GET can't
    do this. Logs in, reads cfg_glpi.version, then best-effort logs out."""

    key = "glpi_rest_api"
    label = "GLPI REST API"
    fields = [
        Field_("base_url", "GLPI base URL", type="url",
               placeholder="http://10.0.0.161:8765"),
        Field_("user_token", "User Token", type="password",
               help="GLPI: My Settings -> Remote access keys -> API token"),
        Field_("app_token", "App Token", type="password", required=False,
               help="Only needed if the matching API client requires one"),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        base = self.config["base_url"].rstrip("/")
        headers = {"Authorization": f"user_token {self.config['user_token']}"}
        if self.config.get("app_token"):
            headers["App-Token"] = self.config["app_token"]

        r = self._get(f"{base}/apirest.php/initSession", headers=headers)
        if r.status_code >= 400:
            raise SourceUnavailable(f"GLPI initSession failed: {r.text[:200]}")
        session_token = r.json().get("session_token")
        if not session_token:
            raise SourceUnavailable("GLPI initSession did not return a session_token")

        headers2 = {"Session-Token": session_token}
        if self.config.get("app_token"):
            headers2["App-Token"] = self.config["app_token"]
        try:
            r2 = self._get(f"{base}/apirest.php/getGlpiConfig", headers=headers2)
            if r2.status_code >= 400:
                raise SourceUnavailable(f"GLPI getGlpiConfig failed: {r2.text[:200]}")
            version = (r2.json().get("cfg_glpi") or {}).get("version")
        finally:
            try:
                self._get(f"{base}/apirest.php/killSession", headers=headers2)
            except SourceUnavailable:
                pass  # best-effort logout; don't fail the check over this

        if not version:
            return FetchResult([])
        return FetchResult([Release(raw_tag=version)])


@register
class HtmlRegex(SourceAdapter):
    """Escape hatch #2: last resort for projects that only announce on a
    download page. Brittle by nature — expect to fix these occasionally."""

    key = "html_regex"
    label = "Scrape page with regex"
    fields = [
        Field_("url", "Page URL", type="url"),
        Field_("pattern", "Regex (1 capture group)",
               placeholder=r"Moodle\s+(\d+\.\d+(?:\.\d+)?)"),
    ]

    def fetch(self, etag=None, last_modified=None) -> FetchResult:
        r = self._get(self.config["url"], etag=etag, last_modified=last_modified)
        if r.status_code == 304:
            return FetchResult(not_modified=True, etag=etag,
                               last_modified=last_modified)
        r.raise_for_status()
        found = re.findall(self.config["pattern"], r.text)
        rels = [Release(raw_tag=m if isinstance(m, str) else m[0],
                        url=self.config["url"]) for m in found]
        return FetchResult(rels, r.headers.get("ETag"),
                           r.headers.get("Last-Modified"))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _struct(value) -> Optional[datetime]:
    if not value:
        return None
    return datetime(*value[:6])


def _dig(data: Any, path: str) -> list[Any]:
    """'releases.0.version' -> ['17.3.1'];  'releases[].version' -> [...]"""
    current: list[Any] = [data]
    for part in path.split("."):
        nxt: list[Any] = []
        for node in current:
            if part.endswith("[]"):
                key = part[:-2]
                seq = node.get(key, []) if key else node
                nxt.extend(seq if isinstance(seq, list) else [])
            elif part.isdigit() and isinstance(node, list):
                idx = int(part)
                if idx < len(node):
                    nxt.append(node[idx])
            elif isinstance(node, dict) and part in node:
                nxt.append(node[part])
        current = nxt
    return [c for c in current if c is not None]


def build(source_type: str, config: dict[str, Any],
          client: httpx.Client) -> SourceAdapter:
    try:
        return REGISTRY[source_type](config, client)
    except KeyError:
        raise ValueError(f"unknown source type: {source_type}") from None


def catalogue() -> list[dict[str, Any]]:
    return [cls.describe() for cls in REGISTRY.values()]
