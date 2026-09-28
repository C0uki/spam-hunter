import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database, StreamContext
from app.judge.runner import build_state, judge_messages, judge_session
from app.judge.variants import load_questions

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeBackend:
    """本文に「消えろ」があれば abuse が高く出る、偽の判定器。"""

    def __init__(self, kind="torch", fail_on=(), fail_batches=False):
        self.kind = kind
        self.model_ver = f"fake/{kind}"
        self.calls: list[list[dict]] = []
        self.fail_on = set(fail_on)
        self.fail_batches = fail_batches

    def predict(self, states, questions):
        self.calls.append(states)
        if self.fail_batches and len(states) > 1:
            raise RuntimeError("batch failed")
        out = []
        for s in states:
            if s["comment"] in self.fail_on:
                raise RuntimeError("cannot judge")
            bad = "消えろ" in s["comment"]
            answers = {}
            for qid, q in questions.items():
                if q["type"] == "noul":
                    p = 0.95 if bad and qid == "abuse" else 0.05
                    answers[qid] = {"type": "noul", "noul": p, "answer_confidence": max(p, 1 - p)}
                else:
                    keys = list(q["criteria"])
                    choice = keys[-1] if bad else keys[0]
                    probs = {k: (0.8 if k == choice else 0.2 / (len(keys) - 1)) for k in keys}
                    answers[qid] = {"type": "choice", "choice": choice, "probabilities": probs, "answer_confidence": 0.8}
            out.append(answers)
        return out

    def close(self):
        pass


@pytest.fixture
def config():
    return load_questions()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def make_session(db, texts, context=None, end=True):
    sid = db.start_session("chan", "story_firstplay", context or StreamContext(welcomes_advice="no", game_name="RPG"), started_at=T0)
    for i, text in enumerate(texts, start=1):
        db.insert_message(
            message_id=f"s{sid}m{i}", session_id=sid, seq=i, author_pseudo_id="p", text=text,
            sent_at=T0 + timedelta(seconds=i), reply_parent_id=None, rule_flags=[],
        )
    if end:
        db.end_session(sid, "manual")
    return sid


# --- variants -----------------------------------------------------------------


def test_questions_yaml_loads(config):
    v1 = config.primary_variant
    assert config.primary == "v1"
    assert v1.backend == "torch"
    assert list(v1.questions) == ["abuse", "personal_attack", "spam_promo", "sexual", "spoiler", "backseat", "severity"]
    assert v1.question_types()["severity"] == "choice"
    assert list(v1.questions["severity"]["criteria"]) == ["none", "caution", "severe"]


def test_variant_inheritance(config):
    ab = config.get("v1-labelsAB")
    assert ab.backend == "torch"
    assert ab.questions["abuse"]["labels"] == {"true": "A", "false": "B"}
    assert "labels" not in ab.questions["severity"]
    assert "labels" not in config.get("v1").questions["abuse"]  # 元の variant は変わらない
    assert config.get("v1-onnx-int8").backend == "onnx-int8"
    assert config.get("v1-onnx-int8").questions == config.get("v1").questions


def test_unknown_variant(config):
    with pytest.raises(KeyError):
        config.get("nope")


def _write(tmp_path, text):
    p = tmp_path / "q.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_overrides_and_validation(tmp_path):
    p = _write(tmp_path, """
primary: a
variants:
  a:
    questions:
      x: {type: noul, instructions: "x?"}
  b:
    extends: a
    overrides:
      x: {instructions: "changed?"}
""")
    cfg = load_questions(p)
    assert cfg.get("b").questions["x"]["instructions"] == "changed?"
    assert cfg.get("a").questions["x"]["instructions"] == "x?"


@pytest.mark.parametrize(
    "body",
    [
        "primary: a\nvariants:\n  a:\n    questions:\n      s: {type: choice, instructions: '?', criteria: {'yes': a, 'no': b}}",
        "primary: a\nvariants:\n  a:\n    backend: gpu\n    questions:\n      x: {type: noul, instructions: '?'}",
        "primary: a\nvariants:\n  a:\n    extends: b\n  b:\n    extends: a",
        "primary: z\nvariants:\n  a:\n    questions:\n      x: {type: noul, instructions: '?'}",
        "primary: a\nvariants:\n  a:\n    questions:\n      x: {type: score, instructions: '?'}",
    ],
)
def test_invalid_configs(tmp_path, body):
    with pytest.raises(ValueError):
        load_questions(_write(tmp_path, body))


# --- state ----------------------------------------------------------------------


def test_build_state_uses_context_at_send_time(db):
    sid = make_session(db, [], end=False)
    db.add_context(sid, StreamContext(welcomes_advice="yes", game_name="Next Game"), source="helix",
                   valid_from=T0 + timedelta(seconds=10))
    before = build_state("a", db.context_at(sid, (T0 + timedelta(seconds=5)).isoformat(timespec="milliseconds")))
    after = build_state("b", db.context_at(sid, (T0 + timedelta(seconds=15)).isoformat(timespec="milliseconds")))
    assert before == {"comment": "a", "game": "RPG", "stream_title": "", "streamer_welcomes_advice": "いいえ", "spoiler_note": ""}
    assert after["game"] == "Next Game"
    assert after["streamer_welcomes_advice"] == "はい"


def test_build_state_without_context():
    assert build_state("x", None)["streamer_welcomes_advice"] == "不明"


# --- judge_session -----------------------------------------------------------------


def test_judge_session_all(db, config):
    sid = make_session(db, ["こんにちは", "消えろ", "wwww"])
    backend = FakeBackend()
    progress = judge_session(db, sid, config.primary_variant, backend, batch_size=2)
    assert (progress.done, progress.errors, progress.remaining) == (3, 0, 0)
    assert [len(c) for c in backend.calls] == [2, 1]
    session = db.get_session(sid)
    assert session["pipeline_status"] == "judged"
    assert session["primary_variant"] == "v1"
    assert db.count_pending(sid) == 0

    rows = db.conn.execute("SELECT * FROM judgments WHERE message_id = ? ORDER BY question_id", (f"s{sid}m2",)).fetchall()
    assert len(rows) == 7
    by_q = {r["question_id"]: r for r in rows}
    assert float(by_q["abuse"]["value"]) == pytest.approx(0.95)
    assert by_q["abuse"]["probs"] is None
    assert by_q["severity"]["value"] == "severe"
    assert json.loads(by_q["severity"]["probs"])["severe"] == pytest.approx(0.8)
    assert by_q["abuse"]["variant"] == "v1"
    assert by_q["abuse"]["model_ver"] == "fake/torch"
    assert by_q["abuse"]["latency_ms"] >= 0


def test_judge_session_resumes_after_stop(db, config):
    sid = make_session(db, [f"c{i}" for i in range(5)])
    backend = FakeBackend()
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    p1 = judge_session(db, sid, config.primary_variant, backend, batch_size=2, should_stop=stop_after_first)
    assert (p1.done, p1.remaining) == (2, 3)
    assert db.get_session(sid)["pipeline_status"] == "paused"
    assert sid in db.sessions_to_judge()

    p2 = judge_session(db, sid, config.primary_variant, backend, batch_size=2)
    assert (p2.done, p2.total) == (3, 3)
    assert db.get_session(sid)["pipeline_status"] == "judged"
    assert sid not in db.sessions_to_judge()
    assert db.conn.execute("SELECT COUNT(DISTINCT message_id) FROM judgments").fetchone()[0] == 5


def test_judge_session_exception_leaves_resumable_state(db, config):
    sid = make_session(db, ["a", "b", "c"])

    class Boom(FakeBackend):
        def predict(self, states, questions):
            if len(self.calls) >= 1:
                raise KeyboardInterrupt
            return super().predict(states, questions)

    with pytest.raises(KeyboardInterrupt):
        judge_session(db, sid, config.primary_variant, Boom(), batch_size=1)
    assert db.get_session(sid)["pipeline_status"] == "paused"
    assert db.count_pending(sid) == 2


def test_failed_batch_retries_one_by_one(db, config):
    sid = make_session(db, ["ok1", "bad", "ok2"])
    backend = FakeBackend(fail_on={"bad"})
    progress = judge_session(db, sid, config.primary_variant, backend, batch_size=3)
    assert (progress.done, progress.errors) == (2, 1)
    statuses = dict(db.conn.execute("SELECT id, judge_status FROM messages").fetchall())
    assert statuses == {f"s{sid}m1": "done", f"s{sid}m2": "error", f"s{sid}m3": "done"}
    assert db.get_session(sid)["pipeline_status"] == "judged"


def test_primary_variant_is_fixed_per_session(db, config):
    sid = make_session(db, ["a", "b"])
    judge_session(db, sid, config.primary_variant, FakeBackend(), batch_size=1, should_stop=iter([False, True]).__next__)
    with pytest.raises(ValueError):
        judge_session(db, sid, config.get("v1-labelsAB"), FakeBackend())


def test_backend_must_match_variant(db, config):
    sid = make_session(db, ["a"])
    with pytest.raises(ValueError):
        judge_session(db, sid, config.primary_variant, FakeBackend(kind="onnx-int8"))


def test_cannot_judge_while_recording(db, config):
    sid = make_session(db, ["a"], end=False)
    with pytest.raises(ValueError):
        judge_session(db, sid, config.primary_variant, FakeBackend())


# --- judge_messages (他の variant での判定し直し) ------------------------------------


def test_rejudge_with_other_variant_keeps_status(db, config):
    sid = make_session(db, ["こんにちは", "消えろ"])
    judge_session(db, sid, config.primary_variant, FakeBackend())
    backend = FakeBackend()
    r = judge_messages(db, [f"s{sid}m2"], config.get("v1-labelsAB"), backend)
    assert (r.done, r.errors) == (1, 0)
    assert backend.calls[0][0]["comment"] == "消えろ"
    variants = db.conn.execute(
        "SELECT variant, COUNT(*) FROM judgments GROUP BY variant ORDER BY variant"
    ).fetchall()
    assert [tuple(v) for v in variants] == [("v1", 14), ("v1-labelsAB", 7)]
    assert db.get_session(sid)["pipeline_status"] == "judged"


def test_rejudge_skips_scrubbed_messages(db, config):
    sid = make_session(db, ["a"])
    db.conn.execute("UPDATE messages SET text = NULL")
    db.conn.commit()
    r = judge_messages(db, [f"s{sid}m1"], config.primary_variant, FakeBackend())
    assert (r.done, r.errors) == (0, 1)
    assert db.conn.execute("SELECT judge_status FROM messages").fetchone()[0] == "pending"


def test_rejudge_overwrites_same_variant(db, config):
    sid = make_session(db, ["a"])
    judge_session(db, sid, config.primary_variant, FakeBackend())
    judge_messages(db, [f"s{sid}m1"], config.primary_variant, FakeBackend())
    assert db.conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 7
