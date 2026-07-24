import json
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _compression_pair(db: SessionDB):
    base = time.time() - 100
    db.create_session("root", source="cli")
    db.create_session("tip", source="cli", parent_session_id="root")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression', message_count = 1 WHERE id = 'root'",
        (base, base + 10),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, message_count = 1 WHERE id = 'tip'",
        (base + 20,),
    )
    db._conn.commit()


def _compression_chain(db: SessionDB, length: int):
    """Build a length-N compression chain rooted at ``ids[0]``.

    Returns the list of session ids in lineage order. Each intermediate edge
    needs ``end_reason='compression'`` on the parent side — without it the
    CTE stops walking. Easiest model: every session ends as compression and
    every session after the first points its ``parent_session_id`` back at
    the previous step, so walking ``child → parent`` always crosses a
    compression-tagged edge for this lineage.
    """
    base = time.time() - 1000
    ids = [f"s{i}" for i in range(length)]
    db.create_session(ids[0], source="cli")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression', message_count = 1 WHERE id = ?",
        (base, base + 10, ids[0]),
    )
    for index, sid in enumerate(ids[1:], start=1):
        db.create_session(sid, source="cli", parent_session_id=ids[index - 1])
        # Tag the just-closed edge as compression so the CTE walks both
        # ancestors AND descendants through every step in the lineage.
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression', message_count = 1 WHERE id = ?",
            (base + 20 * index, base + 20 * index + 10, ids[index - 1]),
        )
        # And the new step itself, so a descendant walk that picks ``sid``
        # as the seed still has a compression-tagged "parent" edge to recurse on.
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, message_count = 1 WHERE id = ?",
            (base + 20 * index, sid),
        )
    db._conn.commit()
    return ids


def test_archiving_compression_tip_archives_projected_root(db):
    _compression_pair(db)

    assert db.set_session_archived("tip", True) is True

    assert db.get_session("root")["archived"] == 1
    assert db.get_session("tip")["archived"] == 1
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == []
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True, archived_only=True)] == ["tip"]


def test_unarchiving_compression_tip_unarchives_projected_root(db):
    _compression_pair(db)
    db.set_session_archived("tip", True)

    assert db.set_session_archived("tip", False) is True

    assert db.get_session("root")["archived"] == 0
    assert db.get_session("tip")["archived"] == 0
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == ["tip"]


def test_preview_archive_single_session_no_cascade(db):
    """A session with no compression ancestors/descendants reports a 1-row, 0-extra preview."""
    db.create_session("solo", source="cli")

    preview = db.preview_session_archive_lineage("solo", archived=True)

    assert preview["cascade_count"] == 1
    assert preview["cascade_extra"] == 0
    assert preview["affected_ids"] == ["solo"]
    assert preview["oldest_started_at"] is not None
    assert preview["newest_started_at"] is not None


def test_preview_archive_missing_session_reports_zero(db):
    """Calling preview with a non-existent id returns 0, not a misleading 1-row cascade."""
    preview = db.preview_session_archive_lineage("does-not-exist", archived=True)

    assert preview == {
        "cascade_count": 0,
        "cascade_extra": 0,
        "oldest_started_at": None,
        "newest_started_at": None,
        "affected_ids": [],
    }


def test_preview_archive_compression_pair_reports_two_rows(db):
    """A compression pair must surface the full lineage, not the targeted row alone."""
    _compression_pair(db)

    preview = db.preview_session_archive_lineage("tip", archived=True)

    assert preview["cascade_count"] == 2
    assert preview["cascade_extra"] == 1
    assert set(preview["affected_ids"]) == {"tip", "root"}


def test_preview_archive_compression_chain_reports_full_set(db, tmp_path):
    """A multi-step chain reports every legacy session, not just neighbours."""
    ids = _compression_chain(db, 5)

    db.close()
    db = SessionDB(tmp_path / "state.db")

    preview = db.preview_session_archive_lineage(ids[2], archived=True)

    assert preview["cascade_count"] == 5
    assert preview["cascade_extra"] == 4
    assert set(preview["affected_ids"]) == set(ids)


def test_preview_archive_already_archived_reports_zero_rows(db):
    """Already-archived rows don't appear in the preview — they're not new mutations."""
    _compression_pair(db)
    db.set_session_archived("tip", True)

    preview = db.preview_session_archive_lineage("tip", archived=True)

    assert preview["cascade_count"] == 0
    assert preview["affected_ids"] == []


def test_preview_unarchive_skips_still_archived_rows(db):
    """Unarchive preview only includes rows that aren't already unarchived."""
    _compression_pair(db)
    db.set_session_archived("tip", True)

    preview = db.preview_session_archive_lineage("tip", archived=False)

    # Both rows are currently archived (cascade forced both in tests above),
    # so the unarchive preview should see the full lineage.
    assert preview["cascade_count"] == 2
    assert preview["cascade_extra"] == 1


def test_set_session_archived_writes_audit_log(db, tmp_path):
    """Successful archive calls append a JSON line under <hermes_home>/logs/archives.jsonl."""
    _compression_pair(db)

    db.set_session_archived("tip", True)

    db.close()
    log_path = tmp_path / "logs" / "archives.jsonl"
    assert log_path.exists()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["target"] == "tip"
    assert record["archived"] is True
    assert record["rowcount"] == 2  # the cascade flips both rows


def test_set_session_archived_returns_false_for_missing_target(db, tmp_path):
    """A non-existent session id produces no audit-log entry either.

    ``set_session_archived`` returns ``False`` when zero rows were updated,
    and the audit append is gated on ``rowcount > 0`` so the log file is
    not created for an obviously failed call.
    """
    result = db.set_session_archived("does-not-exist", True)
    assert result is False

    db.close()
    log_path = tmp_path / "logs" / "archives.jsonl"
    assert not log_path.exists()
