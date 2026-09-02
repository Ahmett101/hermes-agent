"""Gateway restart cleanup for stale dashboard backends."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway_cli


def test_gateway_restart_dashboard_cleanup_uses_managed_reap(monkeypatch):
    from hermes_cli import dashboard_procs

    calls = []

    def fake_kill(**kwargs):
        calls.append(kwargs)
        return {"matched": [], "killed": [], "failed": []}

    monkeypatch.setattr(dashboard_procs, "_kill_stale_dashboard_processes", fake_kill)

    gateway_cli._cleanup_stale_dashboard_backends_for_gateway_restart()

    assert calls == [
        {
            "reason": "the gateway is restarting to load current code",
            "restart_managed": True,
        }
    ]


def test_launchd_restart_cleans_stale_dashboard_backends_first(monkeypatch):
    calls = []

    monkeypatch.setattr(gateway_cli.sys, "platform", "darwin")
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(
        gateway_cli,
        "_cleanup_stale_dashboard_backends_for_gateway_restart",
        lambda: calls.append("dashboard-cleanup"),
    )
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: None)
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *a, **k: calls.append("launchd-restart")
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)

    gateway_cli.launchd_restart()

    assert calls[:2] == ["dashboard-cleanup", "launchd-restart"]
