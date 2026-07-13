#!/usr/bin/env python3
"""Guard against reintroducing stale Frappe transitive git-URL pins.

Frappe pins a handful of its own dependencies (e.g. ``gunicorn``, ``pypika``)
directly to a git commit in its ``pyproject.toml``. ``uv`` requires that any
package sourced from a git URL resolve to *exactly one* URL across the whole
dependency graph. If this app also declares a pin for one of those packages,
and Frappe later bumps its own pin (which it does routinely), the two pins
diverge and ``bench update`` / ``uv pip install`` fails with:

    Requirements contain conflicting URLs for package `gunicorn`

The fix is almost always: don't pin it here at all. This script fails CI (or
a local run) if it ever finds a git-URL pin in this app's dependency files
that no longer matches what Frappe itself declares, so drift is caught before
it reaches a production bench.

Usage:
    python scripts/check_frappe_pins.py                     # bench layout, auto-detect sibling `frappe` app
    python scripts/check_frappe_pins.py --frappe-ref version-16   # fetch from GitHub instead
    python scripts/check_frappe_pins.py --frappe-pyproject /path/to/frappe/pyproject.toml
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


def extract_git_pins(dependencies: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for dep in dependencies:
        match = GIT_PIN_RE.match(dep.strip())
        if match:
            name, url = match.groups()
            pins[name.lower()] = url
    return pins


def load_local_dependencies() -> list[str]:
    pyproject_path = APP_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps: list[str] = list(data.get("project", {}).get("dependencies", []))

    requirements_path = APP_ROOT / "requirements.txt"
    if requirements_path.exists():
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
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
    args = parser.parse_args()

    local_deps = load_local_dependencies()
    local_pins = extract_git_pins(local_deps)

    if not local_pins:
        print("OK: no git-URL pins found in this app's dependencies. Nothing to verify.")
        return 0

    frappe_text = load_frappe_pyproject_text(args.frappe_pyproject, args.frappe_ref)
    frappe_data = tomllib.loads(frappe_text)
    frappe_deps = list(frappe_data.get("project", {}).get("dependencies", []))
    frappe_pins = extract_git_pins(frappe_deps)

    problems: list[str] = []
    for name, local_url in local_pins.items():
        frappe_url = frappe_pins.get(name)
        if frappe_url is None:
            print(
                f"NOTE: '{name}' is pinned locally to {local_url!r} but Frappe does not "
                "pin it via a git URL. Verify this app genuinely needs its own pin."
            )
            continue
        if frappe_url != local_url:
            problems.append(
                f"'{name}' is pinned to a stale/mismatched URL:\n"
                f"    local:  {local_url}\n"
                f"    frappe: {frappe_url}\n"
                f"  Fix: remove the pin for '{name}' from pyproject.toml/requirements.txt "
                "entirely (preferred, since Frappe already pins it), or update it to match "
                "Frappe's current pin exactly."
            )
        else:
            print(
                f"NOTE: '{name}' is pinned locally and matches Frappe's current pin. "
                "Consider removing the duplicate pin entirely so it can never go stale."
            )

    if problems:
        print("\nFAILED: stale Frappe dependency pin(s) detected:\n")
        for problem in problems:
            print(problem)
        return 1

    print("\nOK: all local git-URL pins match Frappe's current pins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
