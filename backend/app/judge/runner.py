"""判定のバッチ処理（docs/design.md §4.5）。

- judge_session: 監視が終わったセッションの pending を、本番の variant で順に判定する。途中で止めても再開できる。
- judge_messages: 指定したメッセージを、任意の variant で判定する（採点済みのメッセージを別の variant で
  判定し直すときに使う。messages.judge_status は変えない）。

第2段階では、同じ関数をリアルタイム用のワーカーから呼ぶ。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..db import Database, iso, utcnow
from ..sampling import subsample_for_judging
from .backends import Backend
from .variants import Variant

log = logging.getLogger(__name__)

WELCOMES_ADVICE_JA = {"yes": "はい", "no": "いいえ", "unknown": "不明"}


def build_state(text: str, context: sqlite3.Row | None) -> dict[str, str]:
    """Laya に渡す state。キーは常に同じにそろえる（値がなければ空文字）。"""
    return {
        "comment": text,
        "game": (context["game_name"] if context else None) or "",
        "stream_title": (context["stream_title"] if context else None) or "",
        "streamer_welcomes_advice": WELCOMES_ADVICE_JA[context["welcomes_advice"] if context else "unknown"],
        "spoiler_note": (context["spoiler_note"] if context else None) or "",
    }


def answer_to_row(
    message_id: str,
    variant: str,
    question_id: str,
    qtype: str,
    answer: dict[str, Any],
    model_ver: str,
    latency_ms: float,
    judged_at: str,
) -> tuple:
    if qtype == "noul":
        value = repr(float(answer["noul"]))
        probs = None
    else:
        value = str(answer["choice"])
        probs = json.dumps(answer["probabilities"], ensure_ascii=False)
    return (
        message_id,
        variant,
        question_id,
        value,
        probs,
        answer.get("answer_confidence"),
        model_ver,
        latency_ms,
        judged_at,
    )


@dataclass
class BatchResult:
    done: int = 0
    errors: int = 0
    seconds: float = 0.0


def _judge_rows(
    db: Database,
    rows: list[sqlite3.Row],
    variant: Variant,
    backend: Backend,
    *,
    mark_status: bool,
) -> BatchResult:
    """rows をまとめて判定して保存する。まとめて失敗したら1件ずつやり直し、それでも失敗したものは error にする。"""
    result = BatchResult()
    judgeable = [r for r in rows if r["text"] is not None]
    unjudgeable = [r["id"] for r in rows if r["text"] is None]  # 匿名化済みで本文がない
    states = [build_state(r["text"], db.context_at(r["session_id"], r["sent_at"])) for r in judgeable]
    qtypes = variant.question_types()

    def predict(idx: list[int]) -> tuple[list[dict], float]:
        t0 = time.perf_counter()
        answers = backend.predict([states[i] for i in idx], variant.questions)
        return answers, time.perf_counter() - t0

    outcomes: dict[int, tuple[dict, float]] = {}
    try:
        answers, secs = predict(list(range(len(judgeable)))) if judgeable else ([], 0.0)
        for i, a in enumerate(answers):
            outcomes[i] = (a, secs * 1000 / len(judgeable))
        result.seconds += secs
    except Exception:
        log.exception("batch of %d failed; retrying one by one", len(judgeable))
        for i in range(len(judgeable)):
            try:
                (a,), secs = predict([i])
                outcomes[i] = (a, secs * 1000)
                result.seconds += secs
            except Exception:
                log.exception("message %s could not be judged", judgeable[i]["id"])

    judged_at = iso(utcnow())
    save_rows: list[tuple] = []
    done_ids: list[str] = []
    error_ids: list[str] = list(unjudgeable)
    for i, row in enumerate(judgeable):
        if i not in outcomes:
            error_ids.append(row["id"])
            continue
        answer, latency_ms = outcomes[i]
        missing = set(qtypes) - set(answer)
        if missing:
            log.error("message %s: answers missing %s", row["id"], sorted(missing))
            error_ids.append(row["id"])
            continue
        for qid, qtype in qtypes.items():
            save_rows.append(
                answer_to_row(
                    row["id"], variant.name, qid, qtype, answer[qid], backend.model_ver, latency_ms, judged_at
                )
            )
        done_ids.append(row["id"])
    db.save_judgments(save_rows, done_ids=done_ids, error_ids=error_ids, mark_status=mark_status)
    result.done = len(done_ids)
    result.errors = len(error_ids)
    return result


@dataclass
class Progress:
    session_id: int
    done: int
    errors: int
    total: int
    seconds: float

    @property
    def remaining(self) -> int:
        return self.total - self.done - self.errors

    @property
    def eta_seconds(self) -> float | None:
        handled = self.done + self.errors
        if handled == 0:
            return None
        return self.seconds / handled * self.remaining


def judge_session(
    db: Database,
    session_id: int,
    variant: Variant,
    backend: Backend,
    *,
    batch_size: int = 8,
    max_messages: int | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[Progress], None] | None = None,
) -> Progress:
    """セッションの pending を判定する。should_stop() が True になったら、いまのバッチを終えてから止める。

    max_messages: 1セッションで判定する件数の上限。超えたら、ランダムに選んだ分だけを判定する（選ぶのは初回だけ）。
    戻り値の remaining が 0 なら判定済み（pipeline_status = 'judged'）、残っていれば 'paused'。
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    session = db.get_session(session_id)
    if session is None:
        raise ValueError(f"session {session_id} not found")
    if session["ended_at"] is None:
        raise ValueError(f"session {session_id} is still recording")
    if variant.backend != backend.kind:
        raise ValueError(f"variant {variant.name!r} needs backend {variant.backend!r}, got {backend.kind!r}")
    db.claim_primary_variant(session_id, variant.name)
    population, sample = subsample_for_judging(db, session_id, max_messages)
    if sample < population:
        log.info("session %d: judging %d of %d messages (limit per session)", session_id, sample, population)
    db.set_pipeline_status(session_id, "judging")

    progress = Progress(session_id, 0, 0, db.count_pending(session_id), 0.0)
    try:
        while not should_stop():
            rows = db.pending_messages(session_id, batch_size)
            if not rows:
                break
            r = _judge_rows(db, rows, variant, backend, mark_status=True)
            progress.done += r.done
            progress.errors += r.errors
            progress.seconds += r.seconds
            if on_progress:
                on_progress(progress)
    finally:
        # 例外や Ctrl+C でも、次回は pending から再開できる状態にしておく
        status = "judged" if db.count_pending(session_id) == 0 else "paused"
        db.set_pipeline_status(session_id, status)
    return progress


def judge_messages(
    db: Database,
    message_ids: list[str],
    variant: Variant,
    backend: Backend,
    *,
    batch_size: int = 8,
    should_stop: Callable[[], bool] = lambda: False,
) -> BatchResult:
    """指定したメッセージを variant で判定し直す（judge_status は変えない）。"""
    if variant.backend != backend.kind:
        raise ValueError(f"variant {variant.name!r} needs backend {variant.backend!r}, got {backend.kind!r}")
    total = BatchResult()
    rows = db.messages_by_id(message_ids)
    for start in range(0, len(rows), batch_size):
        if should_stop():
            break
        r = _judge_rows(db, rows[start : start + batch_size], variant, backend, mark_status=False)
        total.done += r.done
        total.errors += r.errors
        total.seconds += r.seconds
    return total
