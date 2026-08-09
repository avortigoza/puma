"""Version extraction, comparison and selection.

This is the module that decides "is there an update?", so it is where
most of the sharp edges live: v-prefixes, Moodle's trailing '+',
GitLab's 17.3.1-ee, XWiki's 16.10.2, etc.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from packaging.version import InvalidVersion, Version

# Default: first dotted-numeric run in the tag. Overridable per app.
_DEFAULT_EXTRACT = re.compile(r"v?(\d+(?:\.\d+)+)")


def extract(raw: str, pattern: Optional[str] = None) -> Optional[str]:
    """Pull a comparable version string out of a raw tag / title.

    'v17.3.1-ee'          -> '17.3.1'
    'MOODLE_502_STABLE'   -> None unless a custom pattern is supplied
    'glpi-10.0.18'        -> '10.0.18'
    """
    if not raw:
        return None
    rx = re.compile(pattern) if pattern else _DEFAULT_EXTRACT
    m = rx.search(raw)
    if not m:
        return None
    value = m.group(1) if m.groups() else m.group(0)
    return value.strip().lstrip("vV").rstrip("+")


def parse(value: str) -> Version:
    """Lenient parse — never raises, so one weird tag can't kill a check."""
    try:
        return Version(value)
    except InvalidVersion:
        nums = re.findall(r"\d+", value or "")
        return Version(".".join(nums[:4]) or "0")


def is_newer(candidate: str, installed: Optional[str]) -> bool:
    if not candidate:
        return False
    if not installed:
        return True
    return parse(candidate) > parse(installed)


def in_branch(version: str, branch: Optional[str]) -> bool:
    """branch='5.2' matches 5.2.x only. branch=None matches everything.

    This is what stops the dashboard screaming about Moodle 6.0 while you
    are deliberately sitting on the 5.2 line.
    """
    if not branch:
        return True
    want = [int(p) for p in re.findall(r"\d+", branch)]
    got = [int(p) for p in re.findall(r"\d+", version)]
    return got[: len(want)] == want


def looks_prerelease(raw: str) -> bool:
    return bool(re.search(r"(?i)\b(rc|beta|alpha|dev|pre|nightly|snapshot)\b", raw or ""))


def pick_latest(
    candidates: Iterable[tuple[str, bool]],
    *,
    pattern: Optional[str] = None,
    track_branch: Optional[str] = None,
    include_prerelease: bool = False,
) -> Optional[str]:
    """candidates: iterable of (raw_tag, prerelease_flag_from_source)."""
    best: Optional[Version] = None
    best_str: Optional[str] = None
    for raw, flagged in candidates:
        if (flagged or looks_prerelease(raw)) and not include_prerelease:
            continue
        v = extract(raw, pattern)
        if not v or not in_branch(v, track_branch):
            continue
        parsed = parse(v)
        if best is None or parsed > best:
            best, best_str = parsed, v
    return best_str
