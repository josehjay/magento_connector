#!/usr/bin/env python3
"""
Create a patch version-bump commit when one is needed before pushing.

Safe to call multiple times: if HEAD is already a local version-bump commit,
this is a no-op.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUMP_SCRIPT = ROOT / "scripts" / "bump_version.py"
VERSION_FILES = ["connector/__init__.py", "setup.py"]
BUMP_MSG_RE = re.compile(r"^chore: bump version to \d+\.\d+\.\d+$")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
	return subprocess.run(
		cmd,
		cwd=ROOT,
		text=True,
		capture_output=True,
		check=False,
		**kwargs,
	)


def git(*args: str) -> subprocess.CompletedProcess:
	return run(["git", *args])


def head_commit_message() -> str:
	result = git("log", "-1", "--pretty=%s")
	if result.returncode != 0:
		return ""
	return (result.stdout or "").strip()


def is_bump_commit(message: str | None = None) -> bool:
	return bool(BUMP_MSG_RE.match(message if message is not None else head_commit_message()))


def ensure_python() -> list[str]:
	# Prefer the current interpreter so Windows/pyenv/venv all work.
	return [sys.executable, str(BUMP_SCRIPT)]


def bump_and_commit() -> str | None:
	"""Bump patch version and commit. Returns new version, or None if skipped."""
	if os.environ.get("SKIP_VERSION_BUMP") == "1":
		return None

	if is_bump_commit():
		return None

	# Don't interfere with in-progress git operations.
	for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
		if (ROOT / ".git" / state).exists():
			return None

	status = git("status", "--porcelain")
	if status.returncode != 0:
		raise SystemExit(status.stderr or "git status failed")

	bump = run(ensure_python())
	if bump.returncode != 0:
		raise SystemExit(bump.stderr or bump.stdout or "bump_version.py failed")
	new_version = (bump.stdout or "").strip()
	if not new_version:
		raise SystemExit("bump_version.py returned an empty version")

	add = git("add", "--", *VERSION_FILES)
	if add.returncode != 0:
		raise SystemExit(add.stderr or "git add failed")

	diff = git("diff", "--cached", "--quiet")
	if diff.returncode == 0:
		return None

	commit = git(
		"commit",
		"--no-verify",
		"-m",
		f"chore: bump version to {new_version}",
	)
	if commit.returncode != 0:
		raise SystemExit(commit.stderr or commit.stdout or "git commit failed")

	return new_version


def main() -> int:
	try:
		new_version = bump_and_commit()
	except SystemExit as exc:
		code = exc.code
		if isinstance(code, str) and code:
			print(code, file=sys.stderr)
			return 1
		if isinstance(code, int):
			return code
		return 1

	if new_version:
		print(f"auto-version: bumped to {new_version}")
	else:
		print("auto-version: no bump needed")
	return 0


if __name__ == "__main__":
	sys.exit(main())
