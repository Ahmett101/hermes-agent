"""Tests for scripts/check-pyproject-py-modules.py.

Guards the #5502 / #74620 regression class: a root-level ``*.py`` file
that isn't declared in ``[tool.setuptools].py-modules`` ships fine via
``pip install -e .`` (it just runs from the repo tree) but is silently
omitted from a sealed uv2nix wheel build, surfacing as a runtime
``ModuleNotFoundError`` for Nix users only — the worst kind of bug
because the failure mode is platform-specific.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check-pyproject-py-modules.py"
_spec = importlib.util.spec_from_file_location("check_pyproject_py_modules", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load check-pyproject-py-modules.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sys.modules["check_pyproject_py_modules"] = _mod


# ---------------------------------------------------------------------------
# _declared_py_modules
# ---------------------------------------------------------------------------


def test_declared_py_modules_reads_current_pyproject():
    """The real pyproject.toml must parse cleanly — if this fails the
    script's TOML read drifted from the actual schema."""
    declared = _mod._declared_py_modules(Path(__file__).resolve().parents[2] / "pyproject.toml")
    assert "run_agent" in declared
    assert "hermes_constants" in declared
    # 74620 fix: the new entries must already be in the list.
    assert "hermes_state_common" in declared
    assert "mini_swe_runner" in declared


def test_declared_py_modules_missing_table_errors(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'foo'\n", encoding="utf8")
    with pytest.raises(SystemExit) as exc_info:
        _mod._declared_py_modules(pyproject)
    assert exc_info.value.code != 0
    assert "could not find" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _root_py_modules
# ---------------------------------------------------------------------------


def test_root_py_modules_includes_top_level_only(tmp_path):
    (tmp_path / "alpha.py").write_text("", encoding="utf8")
    (tmp_path / "beta.py").write_text("", encoding="utf8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "gamma.py").write_text("", encoding="utf8")
    found = _mod._root_py_modules(tmp_path)
    assert found == ["alpha", "beta"]


def test_root_py_modules_skips_underscore_prefix(tmp_path):
    (tmp_path / "_internal.py").write_text("", encoding="utf8")
    (tmp_path / "public.py").write_text("", encoding="utf8")
    found = _mod._root_py_modules(tmp_path)
    assert found == ["public"]


def test_root_py_modules_excludes_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("", encoding="utf8")
    (tmp_path / "real.py").write_text("", encoding="utf8")
    found = _mod._root_py_modules(tmp_path)
    assert found == ["real"]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _write_pyproject(tmp_path: Path, declared: list[str]) -> Path:
    """Write a minimal pyproject.toml that parses cleanly and exposes
    the supplied py-modules list under [tool.setuptools]."""
    pyproject = tmp_path / "pyproject.toml"
    lines = [
        "[project]",
        "name = 'fixture'",
        "version = '0.0.0'",
        "",
        "[tool.setuptools]",
        f"py-modules = {declared!r}",
    ]
    pyproject.write_text("\n".join(lines), encoding="utf8")

    # Also satisfy REPO_ROOT conventions if a test opts into the global
    # INTENTIONALLY_UNDECLARED list — we don't modify the module here
    # because that's a process-wide side effect other tests rely on.
    return pyproject


def test_main_passes_when_every_file_declared(tmp_path, monkeypatch, capsys):
    pyproject = _write_pyproject(tmp_path, ["alpha", "beta"])
    (tmp_path / "alpha.py").write_text("", encoding="utf8")
    (tmp_path / "beta.py").write_text("", encoding="utf8")

    rc = _mod.main(
        [
            "--root",
            str(tmp_path),
            "--pyproject",
            str(pyproject),
            "--quiet",
        ]
    )
    assert rc == 0


def test_main_fails_when_module_undeclared(tmp_path, monkeypatch, capsys):
    pyproject = _write_pyproject(tmp_path, ["alpha"])  # beta missing
    (tmp_path / "alpha.py").write_text("", encoding="utf8")
    (tmp_path / "beta.py").write_text("", encoding="utf8")

    rc = _mod.main(
        [
            "--root",
            str(tmp_path),
            "--pyproject",
            str(pyproject),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "beta.py" in err
    assert "NOT declared" in err


def test_main_warns_when_declared_but_missing(tmp_path, monkeypatch, capsys):
    """Forward-declared modules (declared in py-modules, file not yet
    on disk) must NOT fail the build — they're a legitimate hedge
    against the 74620 regression (issue: pre-declared then file
    arrives in a later PR)."""
    pyproject = _write_pyproject(tmp_path, ["alpha", "forward_declared"])
    (tmp_path / "alpha.py").write_text("", encoding="utf8")
    # forward_declared.py is intentionally absent.

    rc = _mod.main(
        [
            "--root",
            str(tmp_path),
            "--pyproject",
            str(pyproject),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 0, f"forward-declared entry must not block CI; got: {err}"
    assert "forward_declared" in err
    assert "warning:" in err


def test_main_honors_intentionally_undeclared(tmp_path, monkeypatch, capsys):
    """A module listed in INTENTIONALLY_UNDECLARED is skipped without
    error — same affordance the upstream #5502 fix used to mark test
    fixtures that intentionally live at the repo root."""
    pyproject = _write_pyproject(tmp_path, ["alpha"])
    (tmp_path / "alpha.py").write_text("", encoding="utf8")
    (tmp_path / "beta.py").write_text("", encoding="utf8")

    monkeypatch.setitem(
        _mod.INTENTIONALLY_UNDECLARED,
        "beta",
        "test fixture; ships only with dev extras",
    )

    rc = _mod.main(
        [
            "--root",
            str(tmp_path),
            "--pyproject",
            str(pyproject),
            "--quiet",
        ]
    )
    assert rc == 0
