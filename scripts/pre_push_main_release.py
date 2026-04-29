#!/usr/bin/env python3
"""Pre-push release helper for dekart-cli main branch.

Behavior on push to main:
1) Bump minor version in pyproject.toml and commit
2) Build + publish to PyPI via twine
3) Exit non-zero so user re-runs push with the new commit
4) On immediate retry push with same HEAD, allow push through
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PENDING_PUSH_MARKER = ROOT / ".git" / "dekart_release_pending_push"
PACKAGE_NAME = "dekart"


def run(cmd, check=True, capture=False):
    kwargs = {"cwd": str(ROOT), "text": True}
    if capture:
        kwargs["capture_output"] = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def git_output(cmd):
    result = run(cmd, capture=True)
    return (result.stdout or "").strip()


def has_changes(paths):
    out = git_output(["git", "status", "--porcelain", "--", *paths])
    return bool(out)


def commit_if_needed(paths, message):
    if not has_changes(paths):
        return False
    run(["git", "add", "--", *paths])
    run(["git", "commit", "-m", message])
    return True


def bump_minor_version():
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"\s*$', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find [project].version in pyproject.toml")
    major, minor, _patch = map(int, match.groups())
    new_version = f"{major}.{minor + 1}.0"
    updated = re.sub(
        r'^version\s*=\s*"\d+\.\d+\.\d+"\s*$',
        f'version = "{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    PYPROJECT.write_text(updated, encoding="utf-8")
    return new_version


def read_version():
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"\s*$', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read [project].version from pyproject.toml")
    return match.group(1)


def current_branch():
    return git_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])


def current_head_sha():
    return git_output(["git", "rev-parse", "HEAD"])


def pending_push_head():
    if not PENDING_PUSH_MARKER.exists():
        return ""
    return PENDING_PUSH_MARKER.read_text(encoding="utf-8").strip()


def mark_pending_push(head_sha):
    PENDING_PUSH_MARKER.write_text(f"{head_sha}\n", encoding="utf-8")


def clear_pending_push():
    if PENDING_PUSH_MARKER.exists():
        PENDING_PUSH_MARKER.unlink()


def publish_to_pypi():
    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "build", "twine"])

    dist_dir = ROOT / "dist"
    if dist_dir.exists():
        for path in dist_dir.glob("*"):
            if path.is_file():
                path.unlink()

    run([sys.executable, "-m", "build"])
    version = read_version()
    dist_files = sorted(glob.glob(str(dist_dir / f"{PACKAGE_NAME}-{version}*")))
    if not dist_files:
        raise RuntimeError(f"No dist artifacts found for {PACKAGE_NAME}-{version}")

    token = (os.environ.get("PYPI_API_TOKEN") or "").strip()
    cmd = [sys.executable, "-m", "twine", "upload", *dist_files]
    if token:
        cmd.extend(["-u", "__token__", "-p", token])
    run(cmd)


def main():
    try:
        branch = current_branch()
    except Exception:
        return 0

    if branch != "main":
        return 0

    pending = pending_push_head()
    head = current_head_sha()
    if pending and pending == head:
        clear_pending_push()
        print("[pre-push] release already prepared for current HEAD; allowing push.")
        return 0
    if pending and pending != head:
        clear_pending_push()

    print("[pre-push] main branch detected: running release steps...")

    new_version = bump_minor_version()
    version_committed = commit_if_needed(["pyproject.toml"], f"chore(release): bump version to {new_version}")
    if version_committed:
        print(f"[pre-push] committed version bump -> {new_version}")

    publish_to_pypi()
    print("[pre-push] published to PyPI")

    mark_pending_push(current_head_sha())
    print("[pre-push] release commit created. Re-run 'git push' to push the new commit.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"[pre-push] command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except Exception as exc:
        print(f"[pre-push] error: {exc}", file=sys.stderr)
        raise SystemExit(1)
