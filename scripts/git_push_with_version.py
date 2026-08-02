#!/usr/bin/env python3
"""git push wrapper: bump patch version, then push with a clean exit code.

Used via `alias.push` so Source Control / CLI pushes do not hit the
pre-push nested-repush + exit-1 pattern (which looks like a failed push
even when the remote was updated successfully).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTO_VERSION = ROOT / "scripts" / "auto_version_on_push.py"


def main(argv: list[str]) -> int:
	if os.environ.get("SKIP_VERSION_BUMP") != "1":
		bump = subprocess.run(
			[sys.executable, str(AUTO_VERSION)],
			cwd=ROOT,
			check=False,
		)
		if bump.returncode != 0:
			return bump.returncode

	env = os.environ.copy()
	env["CUSTOM_VERSION_BUMPED"] = "1"
	# Bypass alias.push to avoid recursion; skip pre-push bump logic.
	cmd = ["git", "-c", "alias.push=", "push", *argv]
	return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))
