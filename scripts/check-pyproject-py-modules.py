#!/usr/bin/env python3
"""
Guard against the #5502 / #74620 regression class: root-level Python
modules that exist in the repo tree but aren't declared in
``[tool.setuptools] py-modules``. Pip/curl installs run from the repo
tree and never notice the omission; sealed Nix builds (uv2nix) silently
omit the module and the user sees ``ModuleNotFoundError`` at runtime —
the worst kind of bug because the failure mode only surfaces on one
install path.

This script is intentionally cheap (one ``ast`` parse + one directory
scan + one set diff) so it can run on every PR without slowing CI.

Usage:
    python scripts/check-pyproject-py-modules.py             # check repo root
    python scripts/check-pyproject-py-modules.py --root /p   # check a path
    python scripts/check-pyproject-py-modules.py --quiet     # only print errors

Exit status:
    0 — every root-level *.py file is declared in py-modules
    1 — at least one missing module (CI must fail)

Suppress a deliberate choice (e.g. ``setup.py`` is for legacy builds, or
a test-only root module) by editing ``EXCLUDED_FROM_PY_MODULES`` below —
there is no inline marker, because this check is about declarative
configuration, not source patterns, and the suppression belongs with
the declaration that needs to know about it.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files at the repo root that legitimately live there but should NOT be
# declared as a single-file setuptools module:
#   - ``setup.py``     legacy shim, not installed as a module
#   - files starting with a leading underscore are skipped by setuptools
#     automatically and ignored here as well (kept here as documentation)
EXCLUDED_FROM_PY_MODULES = frozenset({"setup.py"})

# Modules that are intentionally NOT declared despite living at the
# repo root. Empty by default; populated only when a maintainer explicitly
# decides a root-level module should ship as something other than a
# setuptools single-file module (e.g. a namespace package, a stub).
# Document the rationale in the commit message that adds an entry here so
# the next person debugging this list has the context.
INTENTIONALLY_UNDECLARED: dict[str, str] = {}


def _declared_py_modules(pyproject: Path) -> list[str]:
    """Read ``[tool.setuptools].py-modules`` out of pyproject.toml.

    Uses ``tomllib`` (stdlib in 3.11+, which pyproject.toml already
    pins via ``requires-python``). No third-party dependency so the
    check stays runnable in minimal CI runners and inside installers
    that don't have ``tomli`` / ``tomlkit`` available yet.
    """
    with pyproject.open("rb") as fp:
        data = tomllib.load(fp)
    try:
        return list(data["tool"]["setuptools"]["py-modules"])
    except (KeyError, TypeError) as e:
        raise SystemExit(
            f"error: could not find [tool.setuptools].py-modules list in "
            f"{pyproject}: {e}. If you renamed the declaration, update "
            f"the parser in {Path(__file__).name}."
        ) from None


def _root_py_modules(root: Path) -> list[str]:
    """Return every ``*.py`` file at the repo root, as a module name
    (without the ``.py`` suffix). Excludes files that setuptools
    wouldn't ship anyway (leading-underscore names) and our own
    ``EXCLUDED_FROM_PY_MODULES`` allow-list."""
    modules: list[str] = []
    for path in sorted(root.glob("*.py")):
        name = path.stem
        if name.startswith("_"):
            continue
        if path.name in EXCLUDED_FROM_PY_MODULES:
            continue
        modules.append(name)
    return modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Path to the hermes-agent repo root (default: %(default)s).",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="Path to pyproject.toml (default: <root>/pyproject.toml).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only failures, not the OK summary.",
    )
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    pyproject: Path = (args.pyproject or (root / "pyproject.toml")).resolve()
    if not pyproject.exists():
        print(f"error: pyproject.toml not found at {pyproject}", file=sys.stderr)
        return 1

    declared = set(_declared_py_modules(pyproject))
    actual = set(_root_py_modules(root))

    missing = sorted(actual - declared)            # files NOT declared -> MUST fix
    stale = sorted(declared - actual)              # declared but missing files -> WARN

    failures: list[str] = []
    warnings: list[str] = []

    if missing:
        named = [m for m in missing if m not in INTENTIONALLY_UNDECLARED]
        silently_allowed = [m for m in missing if m in INTENTIONALLY_UNDECLARED]
        if named:
            failures.append(
                "the following root-level *.py files are NOT declared in "
                "[tool.setuptools].py-modules and will be missing from a sealed "
                "uv2nix wheel build (#5502 / #74620 regression class):\n"
                + "\n".join(f"  - {m}.py" for m in named)
                + "\n\nFix: add the module name to py-modules in pyproject.toml. "
                "If the omission is intentional, document the rationale by adding "
                "an entry to INTENTIONALLY_UNDECLARED in scripts/check-pyproject-py-modules.py."
            )
        if silently_allowed and not args.quiet:
            for m in silently_allowed:
                print(
                    f"note: {m}.py is intentionally undeclared — "
                    f"{INTENTIONALLY_UNDECLARED[m]}"
                )

    # Stale entries (declared but no matching root-level *.py) are a
    # forward-compatibility hedge, not a hard error: a maintainer may
    # legitimately add a module name *before* the corresponding .py
    # lands in a follow-up PR (issue #74620 itself followed this path —
    # the v0.19.0 fix pre-declared hermes_state_common before the file
    # shipped). Flag as warnings so the list never silently rots but
    # doesn't block forward progress.
    if stale:
        warnings.append(
            "the following py-modules entries do NOT correspond to a root-level "
            "*.py file (likely a forward-declared module whose .py file is in "
            "flight on another branch — non-blocking, but please clean up when "
            "the corresponding file lands):\n"
            + "\n".join(f"  - {m}.py" for m in stale)
        )

    for w in warnings:
        print(f"warning: {w}\n", file=sys.stderr)

    if failures:
        for f in failures:
            print(f"error: {f}\n", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f"OK: all {len(actual)} root-level *.py modules are declared in "
            f"[tool.setuptools].py-modules."
            + (f" ({len(stale)} forward-declared entry/entries pending.)" if stale else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
