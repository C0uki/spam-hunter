"""セッションが終わったあとの流れ（docs/design.md §3）: 判定 → 層別抽出 → まとめ通知。

どこで止まっても、もう一度呼べば続きから進む（pipeline_status を見て判断する）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .db import Database
from .judge.backends import Backend
from .judge.runner import Progress, judge_session
from .judge.variants import Variant
from .sampling import draw_session_samples

log = logging.getLogger(__name__)

PENDING_STATUSES = ("ended", "judging", "paused", "judged", "sampling")
JUDGE_STATUSES = ("ended", "judging", "paused")


def max_messages_per_session(defaults: dict[str, Any]) -> int | None:
    """defaults.yaml の judge.max_messages_per_session。空・0 以下なら上限なし。"""
    value = defaults.get("judge", {}).get("max_messages_per_session")
    if value in (None, "", "none"):
        return None
    value = int(value)
    return value if value > 0 else None


def resolve_threads(value: Any) -> int:
    """judge.threads。'auto' や空なら、CPU の論理スレッド数（Surface Go 2 なら4）を使う。"""
    if value in (None, "", "auto"):
        return os.cpu_count() or 2
    return max(1, int(value))


def sessions_for_pipeline(db: Database) -> list[int]:
    marks = ",".join("?" * len(PENDING_STATUSES))
    rows = db.conn.execute(
        f"SELECT id FROM sessions WHERE pipeline_status IN ({marks}) ORDER BY id", PENDING_STATUSES
    ).fetchall()
    return [r["id"] for r in rows]


def count_over_threshold(db: Database, session_id: int, variant: str, policy: dict[str, Any]) -> int:
    """§4.8 のしきい値のポリシーに当てはまるメッセージの件数（「もし通知していたら」の件数）。"""
    over: set[str] = set()
    items = set(policy.get("noul_items", []))
    threshold = float(policy.get("noul_threshold", 1.1))
    choice_notify = {q: set(v) for q, v in (policy.get("choice_notify") or {}).items()}
    for r in db.session_judgments(session_id, variant):
        q, v = r["question_id"], r["value"]
        if q in items and float(v) >= threshold:
            over.add(r["message_id"])
        elif q in choice_notify and v in choice_notify[q]:
            over.add(r["message_id"])
    return len(over)


def _format_duration(started: str, ended: str | None) -> str | None:
    if not ended:
        return None
    seconds = int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
    return f"{seconds // 3600}時間{seconds % 3600 // 60}分"


def build_summary(db: Database, session_id: int, variant: str, policy: dict[str, Any]) -> dict[str, Any]:
    s = db.get_session(session_id)
    summary = {
        "channel": s["channel_login"],
        "purpose": s["purpose"],
        "duration": _format_duration(s["started_at"], s["ended_at"]),
        "end_reason": s["end_reason"],
        **db.session_summary_counts(session_id),
        "over_threshold": count_over_threshold(db, session_id, variant, policy),
    }
    return summary


def run_pipeline(
    db: Database,
    session_id: int,
    variant: Variant,
    backend_factory: Callable[[], Backend],
    defaults: dict[str, Any],
    *,
    send_summary: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[Progress], None] | None = None,
) -> str:
    """セッションを先に進められるところまで進め、最後の pipeline_status を返す。

    backend_factory は、判定が必要なときにだけ呼ぶ（モデルの読み込みは重いため）。
    """
    session = db.get_session(session_id)
    if session is None:
        raise ValueError(f"session {session_id} not found")
    status = session["pipeline_status"]
    if session["primary_variant"] and session["primary_variant"] != variant.name:
        raise ValueError(
            f"session {session_id} was judged with variant {session['primary_variant']!r}, not {variant.name!r}"
        )

    if status in JUDGE_STATUSES:
        if db.count_pending(session_id) > 0:
            backend = backend_factory()
            judge_session(
                db,
                session_id,
                variant,
                backend,
                batch_size=int(defaults.get("judge", {}).get("batch_size", 1)),
                max_messages=max_messages_per_session(defaults),
                should_stop=should_stop,
                on_progress=on_progress,
            )
        else:
            db.claim_primary_variant(session_id, variant.name)
            db.set_pipeline_status(session_id, "judged")
        status = db.get_session(session_id)["pipeline_status"]
        if status != "judged":
            return status  # 一時停止など

    variant_name = variant.name
    if status in ("judged", "sampling"):
        db.set_pipeline_status(session_id, "sampling")
        result = draw_session_samples(
            db, session_id, variant_name, variant.question_types(), defaults.get("sampling", {})
        )
        log.info("session %d: %d messages queued for labeling", session_id, result.queued)
        db.set_pipeline_status(session_id, "done")
        status = "done"
        if send_summary and defaults.get("notify", {}).get("session_summary", True):
            try:
                send_summary(build_summary(db, session_id, variant_name, defaults.get("policy", {})))
            except Exception:
                log.exception("failed to send the session summary")
    return status
