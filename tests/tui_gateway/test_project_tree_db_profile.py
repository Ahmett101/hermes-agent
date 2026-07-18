"""Tests for tui_gateway.server._project_tree_db profile scoping (#64987)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tui_gateway import server as srv


def test_no_params_falls_back_to_launch_db():
    with mock.patch.object(srv, "_get_db") as get_db, mock.patch.object(
        srv, "_profile_home", return_value=None
    ):
        srv._project_tree_db(None)
        get_db.assert_called_once()


def test_empty_params_falls_back_to_launch_db():
    with mock.patch.object(srv, "_get_db") as get_db, mock.patch.object(
        srv, "_profile_home", return_value=None
    ):
        srv._project_tree_db({})
        get_db.assert_called_once()


def test_explicit_profile_opens_profile_db():
    fake_home = Path("/tmp/fake-profile-home")
    with mock.patch.object(
        srv, "_profile_home", return_value=fake_home
    ), mock.patch.object(srv, "_get_db") as get_db, mock.patch(
        "hermes_state.SessionDB", autospec=False
    ) as SessionDB:
        srv._project_tree_db({"profile": "assistant"})
        get_db.assert_not_called()
        SessionDB.assert_called_once_with(db_path=fake_home / "state.db")


def test_unknown_profile_falls_back_to_launch_db():
    with mock.patch.object(srv, "_profile_home", return_value=None), mock.patch.object(
        srv, "_get_db"
    ) as get_db:
        srv._project_tree_db({"profile": "does-not-exist"})
        get_db.assert_called_once()


def test_profile_db_open_failure_falls_back_to_launch_db():
    fake_home = Path("/tmp/fake-profile-home")
    with mock.patch.object(
        srv, "_profile_home", return_value=fake_home
    ), mock.patch.object(srv, "_get_db") as get_db, mock.patch(
        "hermes_state.SessionDB", side_effect=RuntimeError("boom")
    ):
        srv._project_tree_db({"profile": "assistant"})
        get_db.assert_called_once()


@pytest.mark.parametrize("params", [None, {}, {"profile": ""}, {"profile": "default"}])
def test_launch_profile_uses_launch_db(params):
    with mock.patch.object(srv, "_profile_home", return_value=None), mock.patch.object(
        srv, "_get_db"
    ) as get_db:
        srv._project_tree_db(params)
        get_db.assert_called_once()
