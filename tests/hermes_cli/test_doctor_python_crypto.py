"""Regression coverage for native Python crypto diagnostics (#133935)."""

from types import SimpleNamespace
from typing import Any, cast

from hermes_cli import doctor_platform


def test_probe_python_crypto_reports_blocked_hashlib_extension():
    def import_module(name: str):
        if name == "_hashlib":
            raise ImportError("DLL load failed: blocked by Application Control policy")
        raise AssertionError(f"unexpected import: {name}")

    problem = doctor_platform._probe_python_crypto(import_module)

    assert problem is not None
    assert "_hashlib" in problem
    assert "Application Control policy" in problem


def test_probe_python_crypto_reports_missing_password_derivation_apis():
    modules = {
        "_hashlib": SimpleNamespace(),
        "hashlib": SimpleNamespace(),
    }

    problem = doctor_platform._probe_python_crypto(modules.__getitem__)

    assert problem == "hashlib is missing pbkdf2_hmac and scrypt"


def test_python_environment_check_surfaces_crypto_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_platform,
        "_probe_python_crypto",
        lambda: "_hashlib could not be imported: blocked by policy",
    )
    monkeypatch.setattr(doctor_platform, "_report_database_journal_modes", lambda: None)
    monkeypatch.setattr(doctor_platform, "_staged_venv_dir", lambda: None)
    monkeypatch.setattr(
        doctor_platform, "check_macos_tcc_anchor", lambda **_kwargs: None
    )
    monkeypatch.setattr(doctor_platform, "check_macos_full_disk_access", lambda: None)
    monkeypatch.setattr(doctor_platform, "check_macos_tcc_grants", lambda: None)

    finding = cast(Any, doctor_platform._check_python_environment)(False)

    assert any("Repair the Hermes Python runtime" in issue for issue in finding.issues)
    output = capsys.readouterr().out
    assert "Python native crypto unavailable" in output
    assert "blocked by policy" in output


def test_probe_python_crypto_accepts_complete_runtime():
    modules = {
        "_hashlib": SimpleNamespace(),
        "hashlib": SimpleNamespace(pbkdf2_hmac=object(), scrypt=object()),
    }

    assert doctor_platform._probe_python_crypto(modules.__getitem__) is None
