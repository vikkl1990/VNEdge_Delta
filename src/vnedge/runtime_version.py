"""Build identity helpers shared by long-running VNEDGE processes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def code_version(repo: Path | None = None) -> str:
    """Return the checked-out commit plus an explicit dirty-worktree marker."""

    configured = os.environ.get("VNEDGE_BUILD_SHA", "").strip()
    if configured:
        return configured
    root = repo or Path.cwd()
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
        )
    except (OSError, subprocess.SubprocessError):
        return "local-unversioned"
    return f"{commit}{'+dirty' if dirty else ''}"
