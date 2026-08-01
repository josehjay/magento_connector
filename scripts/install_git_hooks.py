#!/usr/bin/env python3
"""Point this repo at .githooks so pre-push auto-versioning is active."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = ".githooks"


def main() -> int:
	hooks_dir = ROOT / HOOKS_PATH
	pre_push = hooks_dir / "pre-push"
	if not pre_push.exists():
		print(f"Missing {pre_push}", file=sys.stderr)
		return 1

	result = subprocess.run(
		["git", "config", "core.hooksPath", HOOKS_PATH],
		cwd=ROOT,
		text=True,
		capture_output=True,
		check=False,
	)
	if result.returncode != 0:
		print(result.stderr or "git config failed", file=sys.stderr)
		return result.returncode

	# Best-effort executable bit for non-Windows clones.
	try:
		pre_push.chmod(pre_push.stat().st_mode | 0o111)
	except OSError:
		pass

	print(f"Configured core.hooksPath={HOOKS_PATH}")
	print("Automatic version bumping is enabled for git push / sync.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
