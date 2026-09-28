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
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        migrate(self.conn)

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
