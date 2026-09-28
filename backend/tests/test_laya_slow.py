"""本物の Laya を使うテスト。python -m pytest -m slow で実行する（モデルのダウンロードが必要）。"""

import pytest

from app.db import Database, StreamContext
from app.judge.backends import MODELS_DIR, ONNX_FILES, load_backend
from app.judge.runner import judge_session
from app.judge.variants import load_questions

pytestmark = pytest.mark.slow
pytest.importorskip("laya")


@pytest.mark.parametrize("variant_name", ["v1", "v1-labelsAB", "v1-onnx-int8"])
def test_real_laya_end_to_end(tmp_path, variant_name):
    variant = load_questions().get(variant_name)
    if variant.backend in ONNX_FILES and not (MODELS_DIR / ONNX_FILES[variant.backend]).exists():
        pytest.skip("ONNX model not exported (python scripts/export_onnx.py)")
    db = Database(tmp_path / "t.db")
    sid = db.start_session("chan", "high_traffic", StreamContext())
    texts = ["今日の配信たのしい！", "お前ほんとに下手くそだな、消えろ"]
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, text in enumerate(texts, start=1):
        db.insert_message(message_id=f"m{i}", session_id=sid, seq=i, author_pseudo_id="p", text=text,
                          sent_at=t0 + timedelta(seconds=i), reply_parent_id=None, rule_flags=[])
    db.end_session(sid, "manual")

    backend = load_backend(variant.backend, threads=2)
    progress = judge_session(db, sid, variant, backend, batch_size=2)
    assert (progress.done, progress.errors) == (2, 0)

    rows = db.conn.execute("SELECT message_id, question_id, value FROM judgments").fetchall()
    assert len(rows) == 14
    got = {(r["message_id"], r["question_id"]): r["value"] for r in rows}
    for mid in ("m1", "m2"):
        for qid, qtype in variant.question_types().items():
            if qtype == "noul":
                assert 0.0 <= float(got[(mid, qid)]) <= 1.0
            else:
                assert got[(mid, qid)] in variant.questions[qid]["criteria"]
    db.close()
