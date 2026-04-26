"""Git helpers used by indexer and `changed` queries."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _run(args: list[str], cwd: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            args,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def is_git_repo(path: Path) -> bool:
    out = _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return out is not None and out.strip() == "true"


def repo_root(path: Path) -> Path:
    """Return git repo root for `path`, or `path` itself if not a git repo."""
    out = _run(["git", "rev-parse", "--show-toplevel"], path)
    if out:
        return Path(out.strip())
    return path


def last_commit_for_file(repo: Path, rel_path: str) -> tuple[Optional[str], Optional[str]]:
    """Return (short_sha, iso_date) of last commit touching the file, or (None, None)."""
    out = _run(
        ["git", "log", "-1", "--format=%h|%cI", "--", rel_path],
        repo,
    )
    if not out:
        return (None, None)
    line = out.strip()
    if not line or "|" not in line:
        return (None, None)
    sha, date = line.split("|", 1)
    return (sha or None, date or None)


def changed_files_since(repo: Path, ref: str) -> list[str]:
    """Return repo-relative paths changed in working tree relative to `ref`."""
    out = _run(["git", "diff", "--name-only", ref, "--"], repo)
    if out is None:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_tracked_files(repo: Path) -> list[str]:
    """List repo-tracked files (respects .gitignore)."""
    out = _run(["git", "ls-files"], repo)
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]
