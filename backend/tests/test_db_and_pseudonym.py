import sqlite3
from datetime import UTC, datetime

import pytest

from app.db import Database, StreamContext
from app.db.schema import MIGRATIONS
from app.pseudonym import SALT_FILENAME, Pseudonymizer, load_or_create_salt

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_migration_creates_all_tables(db):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "sessions", "session_context", "messages", "judgments", "sample_draws",
        "label_queue", "labels", "label_meta", "settings",
    } <= names
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_reopen_does_not_remigrate(tmp_path):
    Database(tmp_path / "t.db").close()
    Database(tmp_path / "t.db").close()


def test_session_lifecycle(db):
    sid = db.start_session("chan", "high_traffic", StreamContext(stream_title="タイトル"), started_at=T0)
    row = db.get_session(sid)
    assert row["pipeline_status"] == "recording"
    assert row["primary_variant"] is None
    ctx = db.conn.execute("SELECT * FROM session_context WHERE session_id = ?", (sid,)).fetchone()
    assert ctx["stream_title"] == "タイトル"
    assert ctx["welcomes_advice"] == "unknown"
    assert ctx["source"] == "manual"
    assert ctx["valid_from"] == "2026-01-01T12:00:00.000+00:00"

    db.end_session(sid, "manual")
    row = db.get_session(sid)
    assert row["pipeline_status"] == "ended"
    assert row["end_reason"] == "manual"
    ended_at = row["ended_at"]
    db.end_session(sid, "error")  # 二度目は何もしない
    assert db.get_session(sid)["ended_at"] == ended_at
    assert db.get_session(sid)["end_reason"] == "manual"


def test_story_firstplay_requires_welcomes_advice(db):
    with pytest.raises(ValueError):
        db.start_session("chan", "story_firstplay", StreamContext())
    db.start_session("chan", "story_firstplay", StreamContext(welcomes_advice="no"))


def test_invalid_purpose(db):
    with pytest.raises(ValueError):
        db.start_session("chan", "nope", StreamContext())


def _insert(db, sid, mid, seq, flags=()):
    return db.insert_message(
        message_id=mid, session_id=sid, seq=seq, author_pseudo_id="p", text="t",
        sent_at=T0, reply_parent_id=None, rule_flags=list(flags),
    )


def test_insert_message_and_duplicate(db):
    sid = db.start_session("chan", "other", StreamContext())
    assert _insert(db, sid, "m1", 1, ["url"])
    assert not _insert(db, sid, "m1", 2)
    row = db.conn.execute("SELECT * FROM messages WHERE id = 'm1'").fetchone()
    assert row["judge_status"] == "pending"
    assert row["rule_flags"] == '["url"]'
    assert row["sent_at"] == "2026-01-01T12:00:00.000+00:00"
    assert db.next_seq(sid) == 2


def test_seq_unique_per_session(db):
    sid = db.start_session("chan", "other", StreamContext())
    _insert(db, sid, "m1", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, sid, "m2", 1)


def test_close_dangling_sessions(db):
    sid = db.start_session("chan", "other", StreamContext())
    _insert(db, sid, "m1", 1)
    assert db.close_dangling_sessions() == [sid]
    row = db.get_session(sid)
    assert row["end_reason"] == "error"
    assert row["ended_at"] == "2026-01-01T12:00:00.000+00:00"  # 最後のメッセージの時刻
    assert db.close_dangling_sessions() == []


def test_pseudonymizer_is_deterministic_and_salted():
    a = Pseudonymizer(b"salt-a")
    b = Pseudonymizer(b"salt-b")
    assert a("12345") == a("12345")
    assert a("12345") != a("12346")
    assert a("12345") != b("12345")
    assert len(a("12345")) == 32
    assert "12345" not in a("12345")


def test_salt_file_created_once(tmp_path):
    s1 = load_or_create_salt(tmp_path)
    assert (tmp_path / SALT_FILENAME).exists()
    assert load_or_create_salt(tmp_path) == s1
    assert load_or_create_salt(tmp_path, "from-env") == b"from-env"
