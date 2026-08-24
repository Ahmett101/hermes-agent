"""Regression tests for Desktop-owned ``hermes serve`` lifecycle tracking."""

from hermes_cli.web_server import (
    _is_serve_orphaned,
    _parent_start_marker_mismatch_is_conclusive,
    _valid_parent_start_marker,
)


def test_parent_watchdog_tracks_recorded_desktop_pid_not_immediate_ppid():
    """Windows venv launch shims must not make a live Desktop look orphaned."""

    assert _is_serve_orphaned(4242, pid_exists=lambda pid: pid == 4242) is False
    assert _is_serve_orphaned(4242, pid_exists=lambda _pid: False) is True


def test_parent_watchdog_fails_safe_when_liveness_probe_errors():
    def broken_probe(_pid: int) -> bool:
        raise OSError("process table temporarily unavailable")

    assert _is_serve_orphaned(4242, pid_exists=broken_probe) is False


def test_parent_watchdog_accepts_electron_windows_creation_time_marker():
    unix_ms = 1_723_456_789_123
    dotnet_ticks = 621_355_968_000_000_000 + unix_ms * 10_000 + 9_999

    assert _valid_parent_start_marker(f"winms:{unix_ms}") is True
    assert (
        _is_serve_orphaned(
            4242,
            f"winms:{unix_ms}",
            process_start_marker=lambda _pid: f"win:{dotnet_ticks}",
        )
        is False
    )


def test_parent_watchdog_rejects_reused_pid_with_different_windows_creation_time():
    unix_ms = 1_723_456_789_123
    next_process_ticks = 621_355_968_000_000_000 + (unix_ms + 1) * 10_000

    assert (
        _is_serve_orphaned(
            4242,
            f"winms:{unix_ms}",
            process_start_marker=lambda _pid: f"win:{next_process_ticks}",
        )
        is True
    )


def test_parent_watchdog_preserves_legacy_exact_windows_marker():
    marker = "win:638908765432109876"

    assert (
        _is_serve_orphaned(
            4242,
            marker,
            process_start_marker=lambda _pid: marker,
        )
        is False
    )


def test_parent_watchdog_treats_ps_marker_mismatch_as_inconclusive():
    """macOS ps lstart formatting drift must not kill a healthy backend."""

    assert _parent_start_marker_mismatch_is_conclusive(
        "ps:Mon Aug 24 14:30:30 2026",
        "ps:Mon  Aug 24 14:30:30 2026",
    ) is False
    assert (
        _is_serve_orphaned(
            4242,
            "ps:Mon Aug 24 14:30:30 2026",
            process_start_marker=lambda _pid: "ps:Mon  Aug 24 14:30:30 2026",
        )
        is False
    )


def test_parent_watchdog_keeps_machine_marker_mismatches_conclusive():
    """Linux/Windows machine markers still protect against PID reuse."""

    assert _parent_start_marker_mismatch_is_conclusive("linux:123", "linux:456") is True
    assert (
        _is_serve_orphaned(
            4242,
            "linux:123",
            process_start_marker=lambda _pid: "linux:456",
        )
        is True
    )
