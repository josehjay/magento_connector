#!/usr/bin/env python3
"""Keep this app's Frappe transitive git-URL pins in sync with Frappe itself.

Frappe pins a handful of its own dependencies (currently ``gunicorn`` and
``pypika``) directly to a git commit in its ``pyproject.toml``. Because bench
installs/updates apps with ``uv pip install --upgrade -e <app>``, and this app
only depends on those packages *transitively* (this app -> frappe -> gunicorn
/ pypika), ``uv``'s ``--upgrade`` resolution mode requires this app to also
declare the exact same pin directly, or install fails with:

    Package `pypika` was included as a URL dependency. URL dependencies must
    be expressed as direct requirements or constraints.

If this app's own pin then falls out of sync with Frappe's (e.g. Frappe bumps
its ``gunicorn`` commit in a new release), install instead fails with:

    Requirements contain conflicting URLs for package `gunicorn`

Both failure modes are two sides of the same coin: this app's pins for these
specific packages must always be *present* and always *identical* to
Frappe's. This script checks that (failing CI/local runs otherwise) and can
also fix it in place.

Usage:
    python scripts/check_frappe_pins.py                     # bench layout, auto-detect sibling `frappe` app
    python scripts/check_frappe_pins.py --frappe-ref version-16   # fetch from GitHub instead
    python scripts/check_frappe_pins.py --frappe-pyproject /path/to/frappe/pyproject.toml
    python scripts/check_frappe_pins.py --fix                # rewrite pyproject.toml/requirements.txt in place
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
GIT_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*@\s*(git\+\S+)$")
FRAPPE_RAW_URL = "https://raw.githubusercontent.com/frappe/frappe/{ref}/pyproject.toml"

PYPROJECT_PATH = APP_ROOT / "pyproject.toml"
REQUIREMENTS_PATH = APP_ROOT / "requirements.txt"


def extract_git_pins(dependencies: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for dep in dependencies:
        match = GIT_PIN_RE.match(dep.strip())
        if match:
            name, url = match.groups()
            pins[name.lower()] = url
    return pins


def load_local_dependencies() -> list[str]:
    deps: list[str] = []
    if PYPROJECT_PATH.exists():
        data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        deps.extend(data.get("project", {}).get("dependencies", []))
    if REQUIREMENTS_PATH.exists():
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                deps.append(line)
    return deps


def load_frappe_pyproject_text(frappe_pyproject: Path | None, frappe_ref: str) -> str:
    if frappe_pyproject is not None:
        return frappe_pyproject.read_text(encoding="utf-8")

    # Bench layout: apps/<this-app>/pyproject.toml and apps/frappe/pyproject.toml
    # are siblings, so this app's script directory is two levels below `apps/`.
    sibling_frappe = APP_ROOT.parent / "frappe" / "pyproject.toml"
    if sibling_frappe.exists():
        print(f"Using local Frappe pyproject.toml at: {sibling_frappe}")
        return sibling_frappe.read_text(encoding="utf-8")

    url = FRAPPE_RAW_URL.format(ref=frappe_ref)
    print(f"No local Frappe checkout found; fetching: {url}")
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (trusted, hardcoded host)
        return response.read().decode("utf-8")


def fix_pyproject(name: str, url: str) -> bool:
    """Insert or update the pin for `name` inside the `dependencies = [...]` array.

    Returns True if the file was changed.
    """
    if not PYPROJECT_PATH.exists():
        return False
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    block_match = re.search(r"dependencies\s*=\s*\[(.*?)\n\]", text, re.DOTALL)
    if not block_match:
        return False

    block = block_match.group(1)
    line_re = re.compile(
        rf'^(\s*)"{re.escape(name)}\s*@\s*git\+\S+"\s*,?\s*$', re.IGNORECASE | re.MULTILINE
    )
    new_line_value = f'    "{name} @ {url}",'

    if line_re.search(block):
        new_block = line_re.sub(new_line_value, block)
    else:
        new_block = block.rstrip() + f"\n{new_line_value}"

    if new_block == block:
        return False

    new_text = text[: block_match.start(1)] + new_block + text[block_match.end(1) :]
    PYPROJECT_PATH.write_text(new_text, encoding="utf-8")
    return True


def fix_requirements(name: str, url: str) -> bool:
    """Insert or update the pin for `name` as a line in requirements.txt."""
    if not REQUIREMENTS_PATH.exists():
        return False
    lines = REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
    line_re = re.compile(rf"^{re.escape(name)}\s*@\s*git\+\S+$", re.IGNORECASE)
    new_line = f"{name} @ {url}"

    for i, line in enumerate(lines):
        if line_re.match(line.strip()):
            if lines[i] == new_line:
                return False
            lines[i] = new_line
            REQUIREMENTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.append(new_line)
    REQUIREMENTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frappe-ref",
        default="develop",
        help="Git ref (branch/tag) to fetch Frappe's pyproject.toml from when no local "
        "Frappe checkout is found (default: %(default)s).",
    )
    parser.add_argument(
        "--frappe-pyproject",
        type=Path,
        default=None,
        help="Path to a local Frappe pyproject.toml to compare against, bypassing "
        "auto-detection and network access.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite pyproject.toml/requirements.txt in place to match Frappe's current pins, "
        "instead of just reporting a failure.",
    )
    args = parser.parse_args()

    frappe_text = load_frappe_pyproject_text(args.frappe_pyproject, args.frappe_ref)
    frappe_data = tomllib.loads(frappe_text)
    frappe_deps = list(frappe_data.get("project", {}).get("dependencies", []))
    frappe_pins = extract_git_pins(frappe_deps)

    if not frappe_pins:
        print("OK: Frappe currently declares no git-URL pins. Nothing to mirror.")
        return 0

    local_pins = extract_git_pins(load_local_dependencies())

    problems: list[str] = []
    fixed_any = False
    for name, frappe_url in frappe_pins.items():
        local_url = local_pins.get(name)
        if local_url == frappe_url:
            print(f"OK: '{name}' pin matches Frappe exactly.")
            continue

        if local_url is None:
            reason = f"'{name}' is required as a direct pin (currently missing here)"
        else:
            reason = (
                f"'{name}' pin is stale:\n    local:  {local_url}\n    frappe: {frappe_url}"
            )

        if args.fix:
            changed_pyproject = fix_pyproject(name, frappe_url)
            changed_requirements = fix_requirements(name, frappe_url)
            changed = changed_pyproject or changed_requirements
            if changed:
                print(f"FIXED: {reason}\n  -> now pinned to {frappe_url}")
                fixed_any = True
            else:
                problems.append(
                    f"{reason}\n  Could not auto-fix: no matching dependency list found in "
                    "pyproject.toml/requirements.txt. Add it manually:\n"
                    f'    "{name} @ {frappe_url}"'
                )
        else:
            problems.append(
                f"{reason}\n  Fix: pin '{name}' in pyproject.toml/requirements.txt to exactly:\n"
                f"    {name} @ {frappe_url}\n"
                "  Or run: python scripts/check_frappe_pins.py --fix"
            )

    if problems:
        print("\nFAILED: Frappe dependency pin(s) out of sync:\n")
        for problem in problems:
            print(problem)
        return 1

    if fixed_any:
        print("\nFixed all out-of-sync pins. Review the diff and commit it.")

    print("\nOK: all required pins match Frappe's current declarations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
