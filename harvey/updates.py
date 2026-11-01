"""Update check: compare release tags on origin, install only signed ones.

Harvey runs from a git checkout, so git is the whole mechanism. `check` lists
origin's version tags. `apply` installs one only when its tag signature verifies
against your own git/gpg trust, the tree is clean, and the agent loop is idle:
an update never lands mid-send.
"""

import re
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable

from harvey.paths import PROJECT_ROOT
from harvey.pidfile import loop_lock_held

TAG_RE = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
GIT_TIMEOUT_SECONDS = 30


class UpdateError(Exception):
    """An update check or install refused, with a reason someone can act on."""


@dataclass(frozen=True)
class UpdateStatus:
    current: str
    latest: str
    available: bool


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"git {args[0]} did not finish: {e}") from e
    if done.returncode != 0:
        raise UpdateError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise UpdateError(f"Unrecognized version: {version!r}")
    return tuple(int(part) for part in match.groups())


def latest_tag(ls_remote_output: str) -> str | None:
    """Highest vX.Y.Z tag in `git ls-remote --tags` output; other tags are ignored."""
    versions = [
        tuple(int(part) for part in match.groups())
        for line in ls_remote_output.splitlines()
        if (match := TAG_RE.search(line.strip()))
    ]
    return "v{}.{}.{}".format(*max(versions)) if versions else None


def check_for_update(git: Callable[..., str] = _git, current: str = "") -> UpdateStatus:
    current = current or metadata.version("harvey")
    latest = latest_tag(git("ls-remote", "--tags", "origin"))
    if latest is None:
        return UpdateStatus(current, current, False)
    return UpdateStatus(current, latest, _version_tuple(latest) > _version_tuple(current))


def apply_update(
    tag: str,
    git: Callable[..., str] = _git,
    loop_running: Callable[[], bool] = loop_lock_held,
) -> None:
    """Fast-forward to a signed tag. Every refusal leaves the checkout untouched."""
    if not TAG_RE.search(f"refs/tags/{tag}"):
        raise UpdateError(f"Not a release tag: {tag!r}")
    if loop_running():
        raise UpdateError("Harvey is running. Stop it first; updates install only when idle.")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError("You have uncommitted changes. Commit or stash them first.")
    git("fetch", "origin", "tag", tag, "--no-tags")
    try:
        git("verify-tag", tag)
    except UpdateError as e:
        raise UpdateError(
            f"{tag} has no signature you trust, so it was not installed. {e}"
        ) from e
    git("merge", "--ff-only", f"refs/tags/{tag}")
