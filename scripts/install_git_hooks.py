#!/usr/bin/env python3
"""Point this repo at .githooks and enable one-step versioned pushes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = ".githooks"
# Shell function so `git push origin main` forwards args to the wrapper.
PUSH_ALIAS = '!f() { python scripts/git_push_with_version.py "$@"; }; f'


def git_config(key: str, value: str) -> int:
	result = subprocess.run(
		["git", "config", key, value],
		cwd=ROOT,
		text=True,
		capture_output=True,
		check=False,
	)
	if result.returncode != 0:
		print(result.stderr or f"git config {key} failed", file=sys.stderr)
	return result.returncode


def main() -> int:
	hooks_dir = ROOT / HOOKS_PATH
	pre_push = hooks_dir / "pre-push"
	push_wrapper = ROOT / "scripts" / "git_push_with_version.py"
	if not pre_push.exists():
		print(f"Missing {pre_push}", file=sys.stderr)
		return 1
	if not push_wrapper.exists():
		print(f"Missing {push_wrapper}", file=sys.stderr)
		return 1

	if git_config("core.hooksPath", HOOKS_PATH) != 0:
		return 1
	if git_config("alias.push", PUSH_ALIAS) != 0:
		return 1

	# Best-effort executable bit for non-Windows clones.
	try:
		pre_push.chmod(pre_push.stat().st_mode | 0o111)
	except OSError:
		pass

	print(f"Configured core.hooksPath={HOOKS_PATH}")
	print("Configured alias.push -> scripts/git_push_with_version.py")
	print("Automatic version bumping is enabled for git push / sync.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
