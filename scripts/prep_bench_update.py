#!/usr/bin/env python3
"""Sync Frappe git-URL pins from the local bench, commit, and push to GitHub.

Run this on the ERPNext server before retrying `bench update` after a pin
conflict, or as a cron/scheduled job. Uses the sibling `apps/frappe`
pyproject.toml when present.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("check_frappe_pins.py")


def main(argv: list[str]) -> int:
	return subprocess.run(
		[sys.executable, str(SCRIPT), "--fix", "--commit", "--push", *argv],
		check=False,
	).returncode


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))
