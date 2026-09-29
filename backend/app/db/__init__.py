"""SQLite へのアクセス。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schema import migrate

PURPOSES = ("story_firstplay", "high_traffic", "other")
WELCOMES_ADVICE = ("yes", "no", "unknown")


def iso(dt: datetime) -> str:
    """UTC の ISO 8601（ミリ秒まで）。文字列の比較で時刻の前後が決まるよう、形式をそろえる。"""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StreamContext:
    welcomes_advice: str = "unknown"
    game_name: str | None = None
    stream_title: str | None = None
    spoiler_note: str | None = None


def validate_session_params(purpose: str, context: StreamContext) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}: {purpose!r}")
    if context.welcomes_advice not in WELCOMES_ADVICE:
        raise ValueError(f"welcomes_advice must be one of {WELCOMES_ADVICE}")
    if purpose == "story_firstplay" and context.welcomes_advice == "unknown":
        raise ValueError("story_firstplay では、指示を歓迎しているか（yes/no）の入力が必須です")


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # テーブルを作り直すマイグレーションのため、外部キーの強制は移行が終わってから有効にする
        self.conn.execute("PRAGMA foreign_keys = OFF")
        migrate(self.conn)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    # --- sessions -----------------------------------------------------------

    def start_session(
        self,
        channel_login: str,
        purpose: str,
        context: StreamContext,
        *,
        started_at: datetime | None = None,
    ) -> int:
        validate_session_params(purpose, context)
        started = iso(started_at or utcnow())
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sessions (channel_login, purpose, started_at, pipeline_status)"
                " VALUES (?, ?, ?, 'recording')",
                (channel_login, purpose, started),
            )
            session_id = cur.lastrowid
            self._insert_context(session_id, context, started, "manual")
        return session_id

    def add_context(
        self,
        session_id: int,
        context: StreamContext,
        *,
        source: str,
        valid_from: datetime | None = None,
    ) -> None:
        with self.conn:
            self._insert_context(session_id, context, iso(valid_from or utcnow()), source)

    def _insert_context(self, session_id: int, c: StreamContext, valid_from: str, source: str) -> None:
        self.conn.execute(
            "INSERT INTO session_context"
            " (session_id, valid_from, game_name, stream_title, welcomes_advice, spoiler_note, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, valid_from, c.game_name, c.stream_title, c.welcomes_advice, c.spoiler_note, source),
        )

    def end_session(self, session_id: int, reason: str, *, ended_at: datetime | None = None) -> None:
        """セッションを閉じる。判定のバッチ処理（M2 以降）はこの 'ended' のセッションを拾う。"""
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ?, pipeline_status = 'ended'"
                " WHERE id = ? AND ended_at IS NULL",
                (iso(ended_at or utcnow()), reason, session_id),
            )

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def close_dangling_sessions(self) -> list[int]:
        """前回の異常終了で開いたままのセッションを 'error' で閉じる。"""
        rows = self.conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL").fetchall()
        ids = [r["id"] for r in rows]
        for session_id in ids:
            last = self.conn.execute(
                "SELECT MAX(sent_at) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            ended = datetime.fromisoformat(last) if last else None
            self.end_session(session_id, "error", ended_at=ended)
        return ids

    # --- messages -----------------------------------------------------------

    def next_seq(self, session_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] + 1

    def insert_message(
        self,
        *,
        message_id: str,
        session_id: int,
        seq: int,
        author_pseudo_id: str,
        text: str,
        sent_at: datetime,
        reply_parent_id: str | None,
        rule_flags: list[str],
    ) -> bool:
        """保存できたら True。同じメッセージ ID がすでにあれば（再接続時の重複など）False。"""
        with self.conn:
            cur = self.conn.execute(
                # 無視するのはメッセージ ID の重複だけ（通し番号の衝突はエラーにする）
                "INSERT INTO messages"
                " (id, session_id, seq, author_pseudo_id, text, sent_at, reply_parent_id,"
                "  rule_flags, judge_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')"
                " ON CONFLICT(id) DO NOTHING",
                (
                    message_id,
                    session_id,
                    seq,
                    author_pseudo_id,
                    text,
                    iso(sent_at),
                    reply_parent_id,
                    json.dumps(rule_flags) if rule_flags else None,
                ),
            )
        return cur.rowcount == 1

    # --- judging ------------------------------------------------------------

    def set_pipeline_status(self, session_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET pipeline_status = ? WHERE id = ?", (status, session_id)
            )

    def claim_primary_variant(self, session_id: int, variant: str) -> None:
        """セッションの本番 variant を決める。すでに別の variant で判定を始めていればエラー。"""
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET primary_variant = ? WHERE id = ? AND primary_variant IS NULL",
                (variant, session_id),
            )
        current = self.get_session(session_id)["primary_variant"]
        if current != variant:
            raise ValueError(
                f"session {session_id} is already being judged with variant {current!r}, not {variant!r}"
            )

    def sessions_to_judge(self) -> list[int]:
        """監視が終わって判定が済んでいない（途中で止まったものを含む）セッション。"""
        rows = self.conn.execute(
            "SELECT id FROM sessions WHERE pipeline_status IN ('ended', 'judging', 'paused')"
            " ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]

    def count_pending(self, session_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND judge_status = 'pending'",
            (session_id,),
        ).fetchone()[0]

    def pending_messages(self, session_id: int, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, session_id, text, sent_at FROM messages"
            " WHERE session_id = ? AND judge_status = 'pending'"
            " ORDER BY sent_at, seq LIMIT ?",
            (session_id, limit),
        ).fetchall()

    def messages_by_id(self, message_ids: list[str]) -> list[sqlite3.Row]:
        if not message_ids:
            return []
        marks = ",".join("?" * len(message_ids))
        rows = self.conn.execute(
            f"SELECT id, session_id, text, sent_at FROM messages WHERE id IN ({marks})",
            message_ids,
        ).fetchall()
        order = {mid: i for i, mid in enumerate(message_ids)}
        return sorted(rows, key=lambda r: order[r["id"]])

    def context_at(self, session_id: int, at: str) -> sqlite3.Row | None:
        """そのセッションで、時刻 at の時点に有効だった配信状況（なければ最初のもの）。"""
        row = self.conn.execute(
            "SELECT * FROM session_context WHERE session_id = ? AND valid_from <= ?"
            " ORDER BY valid_from DESC, id DESC LIMIT 1",
            (session_id, at),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM session_context WHERE session_id = ? ORDER BY valid_from, id LIMIT 1",
                (session_id,),
            ).fetchone()
        return row

    def save_judgments(
        self,
        rows: list[tuple],
        *,
        done_ids: list[str],
        error_ids: list[str],
        mark_status: bool,
    ) -> None:
        """判定結果を1つのトランザクションで保存する。

        rows: (message_id, variant, question_id, value, probs_json, answer_confidence,
               model_ver, latency_ms, judged_at)
        mark_status: 本番の判定なら True（messages.judge_status を更新する）。
        """
        with self.conn:
            self.conn.executemany(
                "INSERT INTO judgments"
                " (message_id, variant, question_id, value, probs, answer_confidence,"
                "  model_ver, latency_ms, judged_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(message_id, variant, question_id) DO UPDATE SET"
                "  value = excluded.value, probs = excluded.probs,"
                "  answer_confidence = excluded.answer_confidence, model_ver = excluded.model_ver,"
                "  latency_ms = excluded.latency_ms, judged_at = excluded.judged_at",
                rows,
            )
            if mark_status:
                self.conn.executemany(
                    "UPDATE messages SET judge_status = 'done' WHERE id = ?", [(i,) for i in done_ids]
                )
                self.conn.executemany(
                    "UPDATE messages SET judge_status = 'error' WHERE id = ?", [(i,) for i in error_ids]
                )

    # --- context / notifications ----------------------------------------------

    def latest_context(self, session_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM session_context WHERE session_id = ? ORDER BY valid_from DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def mark_rule_notified(self, message_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET rule_notified = 1 WHERE id = ?", (message_id,))

    # --- sampling ---------------------------------------------------------------

    def has_draws(self, session_id: int, variant: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sample_draws WHERE session_id = ? AND variant = ? LIMIT 1",
                (session_id, variant),
            ).fetchone()
            is not None
        )

    def session_judgments(self, session_id: int, variant: str) -> list[sqlite3.Row]:
        """判定済み（done）のメッセージの、その variant の判定結果。"""
        return self.conn.execute(
            "SELECT j.message_id, j.question_id, j.value FROM judgments j"
            " JOIN messages m ON m.id = j.message_id"
            " WHERE m.session_id = ? AND m.judge_status = 'done' AND j.variant = ?",
            (session_id, variant),
        ).fetchall()

    def save_draws(self, session_id: int, variant: str, draws: list[dict]) -> None:
        """draws: {stratum, definition(dict), population_size, message_ids} のリスト。1つのトランザクションで保存する。"""
        drawn_at = iso(utcnow())
        with self.conn:
            for d in draws:
                cur = self.conn.execute(
                    "INSERT INTO sample_draws"
                    " (session_id, variant, stratum, population_size, draw_size, drawn_at, definition)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        variant,
                        d["stratum"],
                        d["population_size"],
                        len(d["message_ids"]),
                        drawn_at,
                        json.dumps(d["definition"], ensure_ascii=False),
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO label_queue (message_id, draw_id) VALUES (?, ?)",
                    [(mid, cur.lastrowid) for mid in d["message_ids"]],
                )

    def session_summary_counts(self, session_id: int) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS received,"
            " SUM(rule_flags IS NOT NULL) AS rule_flagged,"
            " SUM(judge_status = 'done') AS judged,"
            " SUM(judge_status = 'error') AS judge_errors,"
            " SUM(judge_status = 'skipped') AS judge_skipped"
            " FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        queued = self.conn.execute(
            "SELECT COUNT(DISTINCT q.message_id) FROM label_queue q"
            " JOIN sample_draws d ON d.id = q.draw_id WHERE d.session_id = ?",
            (session_id,),
        ).fetchone()[0]
        return {
            "received": row["received"] or 0,
            "rule_flagged": row["rule_flagged"] or 0,
            "judged": row["judged"] or 0,
            "judge_errors": row["judge_errors"] or 0,
            "judge_skipped": row["judge_skipped"] or 0,
            "queued": queued,
        }

    # --- 判定対象の抽出（1セッションで判定する件数の上限） ------------------------------

    def get_subsample(self, session_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM judge_subsamples WHERE session_id = ?", (session_id,)
        ).fetchone()

    def session_message_statuses(self, session_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, judge_status FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()

    def save_subsample(self, session_id: int, population_size: int, sample_size: int, skipped_ids: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO judge_subsamples (session_id, population_size, sample_size, drawn_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, population_size, sample_size, iso(utcnow())),
            )
            self.conn.executemany(
                "UPDATE messages SET judge_status = 'skipped' WHERE id = ? AND judge_status = 'pending'",
                [(mid,) for mid in skipped_ids],
            )
