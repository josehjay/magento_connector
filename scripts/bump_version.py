#!/usr/bin/env python3
"""Bump or read the app semver in connector/__init__.py and setup.py."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_FILE = ROOT / "connector" / "__init__.py"
SETUP_FILE = ROOT / "setup.py"

INIT_PATTERN = re.compile(r'^(__version__\s*=\s*)(["\'])(\d+)\.(\d+)\.(\d+)\2\s*$', re.M)
SETUP_PATTERN = re.compile(r'^(\s*version\s*=\s*)(["\'])(\d+)\.(\d+)\.(\d+)\2(\s*,\s*)$', re.M)


def read_version() -> tuple[int, int, int]:
	text = INIT_FILE.read_text(encoding="utf-8")
	match = INIT_PATTERN.search(text)
	if not match:
		raise SystemExit(f"Could not find __version__ in {INIT_FILE}")
	return int(match.group(3)), int(match.group(4)), int(match.group(5))


def format_version(version: tuple[int, int, int]) -> str:
	return f"{version[0]}.{version[1]}.{version[2]}"


def bump_patch(version: tuple[int, int, int]) -> tuple[int, int, int]:
	major, minor, patch = version
	return major, minor, patch + 1


def replace_version(path: Path, pattern: re.Pattern[str], new_version: str) -> None:
	text = path.read_text(encoding="utf-8")

	def _repl(match: re.Match[str]) -> str:
		suffix = match.group(6) if match.lastindex and match.lastindex >= 6 else ""
		return f"{match.group(1)}{match.group(2)}{new_version}{match.group(2)}{suffix}"

	new_text, count = pattern.subn(_repl, text, count=1)
	if count != 1:
		raise SystemExit(f"Failed to update version in {path}")
	path.write_text(new_text, encoding="utf-8")


def write_version(new_version: str) -> None:
	replace_version(INIT_FILE, INIT_PATTERN, new_version)
	if SETUP_FILE.exists():
		replace_version(SETUP_FILE, SETUP_PATTERN, new_version)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--print-current",
		action="store_true",
		help="Print the current version and exit",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Print current -> next version without writing files",
	)
	args = parser.parse_args(argv)

	current = read_version()
	current_s = format_version(current)
	if args.print_current:
		print(current_s)
		return 0

	nxt = bump_patch(current)
	next_s = format_version(nxt)
	if args.dry_run:
		print(f"{current_s} -> {next_s}")
		return 0

	write_version(next_s)
	print(next_s)
	return 0


if __name__ == "__main__":
	sys.exit(main())
