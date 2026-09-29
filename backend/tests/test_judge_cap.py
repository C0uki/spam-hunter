"""1セッションで判定する件数の上限（Q20）と、スキーマ v3 への移行。"""

import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database, StreamContext
from app.db.schema import MIGRATIONS, migrate
from app.judge.runner import judge_session
from app.judge.variants import load_questions
from app.pipeline import max_messages_per_session, resolve_threads, run_pipeline
from app.sampling import draw_session_samples, first_phase_probability, inclusion_probabilities, subsample_for_judging
from tests.test_judge import FakeBackend

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def variant():
    return load_questions().primary_variant


def make_session(db, n):
    sid = db.start_session("chan", "high_traffic", StreamContext(), started_at=T0)
    for i in range(1, n + 1):
        db.insert_message(message_id=f"m{i}", session_id=sid, seq=i, author_pseudo_id="p", text=f"t{i}",
                          sent_at=T0 + timedelta(seconds=i), reply_parent_id=None, rule_flags=[])
    db.end_session(sid, "manual")
    return sid


def statuses(db):
    return dict(db.conn.execute("SELECT judge_status, COUNT(*) FROM messages GROUP BY judge_status").fetchall())


def test_subsample_marks_the_rest_skipped_and_is_stable(db):
    sid = make_session(db, 10)
    assert subsample_for_judging(db, sid, 4, rng=random.Random(1)) == (10, 4)
    assert statuses(db) == {"pending": 4, "skipped": 6}
    chosen = {r[0] for r in db.conn.execute("SELECT id FROM messages WHERE judge_status = 'pending'")}
    # 2回目は選び直さない（一時停止から再開しても、判定する分は変わらない）
    assert subsample_for_judging(db, sid, 2, rng=random.Random(2)) == (10, 4)
    assert {r[0] for r in db.conn.execute("SELECT id FROM messages WHERE judge_status = 'pending'")} == chosen
    assert first_phase_probability(db, sid) == pytest.approx(0.4)


@pytest.mark.parametrize("cap", [None, 10, 50])
def test_no_subsample_when_under_limit(db, cap):
    sid = make_session(db, 10)
    assert subsample_for_judging(db, sid, cap) == (10, 10)
    assert statuses(db) == {"pending": 10}
    assert first_phase_probability(db, sid) == 1.0


def test_session_judged_before_the_limit_existed_is_not_subsampled(db):
    sid = make_session(db, 10)
    db.conn.execute("UPDATE messages SET judge_status = 'done' WHERE id = 'm1'")
    db.conn.commit()
    assert subsample_for_judging(db, sid, 3) == (10, 10)
    assert statuses(db) == {"done": 1, "pending": 9}


def test_judge_session_respects_limit(db, variant):
    sid = make_session(db, 12)
    backend = FakeBackend()
    progress = judge_session(db, sid, variant, backend, batch_size=2, max_messages=5)
    assert (progress.total, progress.done, progress.remaining) == (5, 5, 0)
    assert sum(len(c) for c in backend.calls) == 5
    assert statuses(db) == {"done": 5, "skipped": 7}
    assert db.get_session(sid)["pipeline_status"] == "judged"


def test_judge_session_resumes_without_reselecting(db, variant):
    sid = make_session(db, 12)
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    judge_session(db, sid, variant, FakeBackend(), batch_size=2, max_messages=6, should_stop=stop_after_first)
    assert statuses(db) == {"done": 2, "pending": 4, "skipped": 6}
    judge_session(db, sid, variant, FakeBackend(), batch_size=2, max_messages=6)
    assert statuses(db) == {"done": 6, "skipped": 6}


def test_inclusion_probability_includes_first_phase(db, variant):
    sid = make_session(db, 10)
    judge_session(db, sid, variant, FakeBackend(), batch_size=5, max_messages=5)
    cfg = {"random": 2, "noul_bands": [], "choice": {}}
    draw_session_samples(db, sid, variant.name, variant.question_types(), cfg, rng=random.Random(3))
    pis = inclusion_probabilities(db, sid, variant.name)
    assert len(pis) == 2
    # 1段目 5/10 × 2段目（判定した5件から2件）2/5
    assert all(p == pytest.approx(0.5 * 2 / 5) for p in pis.values())


def test_pipeline_uses_limit_and_reports_skipped(db, variant):
    sid = make_session(db, 8)
    summaries = []
    defaults = {"judge": {"batch_size": 2, "max_messages_per_session": 3}, "sampling": {"random": 2}}
    assert run_pipeline(db, sid, variant, FakeBackend, defaults, send_summary=summaries.append) == "done"
    assert summaries[0]["judged"] == 3
    assert summaries[0]["judge_skipped"] == 5
    # 抽出は判定したメッセージからだけ行う
    queued = {r[0] for r in db.conn.execute("SELECT message_id FROM label_queue")}
    judged = {r[0] for r in db.conn.execute("SELECT id FROM messages WHERE judge_status = 'done'")}
    assert queued <= judged


@pytest.mark.parametrize(
    "value,expected",
    [(5000, 5000), ("3000", 3000), (None, None), ("", None), (0, None), (-1, None)],
)
def test_max_messages_setting(value, expected):
    assert max_messages_per_session({"judge": {"max_messages_per_session": value}}) == expected
    assert max_messages_per_session({}) is None


def test_resolve_threads(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    assert resolve_threads("auto") == 4
    assert resolve_threads(None) == 4
    assert resolve_threads(2) == 2
    assert resolve_threads("3") == 3
    monkeypatch.setattr("os.cpu_count", lambda: None)
    assert resolve_threads("auto") == 2


def test_migrates_v2_database_with_data_to_v3(tmp_path):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    for version, script in enumerate(MIGRATIONS[:2], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO sessions (id, channel_login, purpose, started_at, pipeline_status) VALUES (1, 'c', 'other', 't', 'judged')")
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, author_pseudo_id, text, sent_at, rule_flags, judge_status, rule_notified)"
        " VALUES ('m1', 1, 1, 'p', 'hello', 't', '[\"url\"]', 'done', 1)"
    )
    conn.execute(
        "INSERT INTO judgments (message_id, variant, question_id, value, model_ver, judged_at)"
        " VALUES ('m1', 'v1', 'abuse', '0.1', 'x', 't')"
    )
    conn.commit()
    conn.close()

    db = Database(path)  # v3 まで移行する
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    row = db.conn.execute("SELECT * FROM messages WHERE id = 'm1'").fetchone()
    assert (row["text"], row["judge_status"], row["rule_flags"], row["rule_notified"]) == ("hello", "done", '["url"]', 1)
    assert db.conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # 新しい状態 'skipped' を入れられる。存在しない状態は入れられない
    db.conn.execute("UPDATE messages SET judge_status = 'skipped' WHERE id = 'm1'")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE messages SET judge_status = 'bogus' WHERE id = 'm1'")
    # 外部キーは移行後も効いている
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO judgments (message_id, variant, question_id, value, model_ver, judged_at)"
            " VALUES ('nope', 'v1', 'abuse', '0.1', 'x', 't')"
        )
    db.close()
