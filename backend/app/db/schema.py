"""SQLite のスキーマとマイグレーション（PRAGMA user_version で版を管理する）。

テーブルの定義は docs/design.md §5 に合わせる。
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[str] = [
    # v1: 設計書 第2版 §5
    """
    CREATE TABLE sessions (
      id              INTEGER PRIMARY KEY,
      channel_login   TEXT NOT NULL,
      purpose         TEXT NOT NULL CHECK (purpose IN ('story_firstplay', 'high_traffic', 'other')),
      started_at      TEXT NOT NULL,
      ended_at        TEXT,
      end_reason      TEXT CHECK (end_reason IN ('manual', 'offline', 'error')),
      pipeline_status TEXT NOT NULL,
      primary_variant TEXT
    );

    CREATE TABLE session_context (
      id              INTEGER PRIMARY KEY,
      session_id      INTEGER NOT NULL REFERENCES sessions(id),
      valid_from      TEXT NOT NULL,
      game_name       TEXT,
      stream_title    TEXT,
      welcomes_advice TEXT NOT NULL CHECK (welcomes_advice IN ('yes', 'no', 'unknown')),
      spoiler_note    TEXT,
      source          TEXT NOT NULL CHECK (source IN ('helix', 'manual'))
    );
    CREATE INDEX idx_session_context_session ON session_context(session_id, valid_from);

    CREATE TABLE messages (
      id                TEXT PRIMARY KEY,
      session_id        INTEGER NOT NULL REFERENCES sessions(id),
      seq               INTEGER NOT NULL,
      author_pseudo_id  TEXT,
      text              TEXT,
      sent_at           TEXT NOT NULL,
      reply_parent_id   TEXT,
      rule_flags        TEXT,
      judge_status      TEXT NOT NULL CHECK (judge_status IN ('pending', 'done', 'error')),
      rule_notified     INTEGER NOT NULL DEFAULT 0,
      kept_as_context   INTEGER NOT NULL DEFAULT 0,
      scrubbed_at       TEXT,
      UNIQUE (session_id, seq)
    );
    CREATE INDEX idx_messages_session_status ON messages(session_id, judge_status);
    CREATE INDEX idx_messages_sent_at ON messages(sent_at);

    CREATE TABLE judgments (
      message_id        TEXT NOT NULL REFERENCES messages(id),
      variant           TEXT NOT NULL,
      question_id       TEXT NOT NULL,
      value             TEXT NOT NULL,
      probs             TEXT,
      answer_confidence REAL,
      model_ver         TEXT NOT NULL,
      latency_ms        REAL,
      judged_at         TEXT NOT NULL,
      PRIMARY KEY (message_id, variant, question_id)
    );

    CREATE TABLE sample_draws (
      id              INTEGER PRIMARY KEY,
      session_id      INTEGER NOT NULL REFERENCES sessions(id),
      variant         TEXT NOT NULL,
      stratum         TEXT NOT NULL,
      population_size INTEGER NOT NULL,
      draw_size       INTEGER NOT NULL,
      drawn_at        TEXT NOT NULL,
      UNIQUE (session_id, variant, stratum)
    );

    CREATE TABLE label_queue (
      message_id      TEXT NOT NULL REFERENCES messages(id),
      draw_id         INTEGER NOT NULL REFERENCES sample_draws(id),
      PRIMARY KEY (message_id, draw_id)
    );

    CREATE TABLE labels (
      message_id   TEXT NOT NULL REFERENCES messages(id),
      question_id  TEXT NOT NULL,
      truth        TEXT NOT NULL,
      labeled_at   TEXT NOT NULL,
      PRIMARY KEY (message_id, question_id)
    );

    CREATE TABLE label_meta (
      message_id     TEXT PRIMARY KEY REFERENCES messages(id),
      needs_context  INTEGER NOT NULL,
      labeled_at     TEXT NOT NULL
    );

    CREATE TABLE settings (
      key    TEXT PRIMARY KEY,
      value  TEXT NOT NULL
    );
    """,
    # v2: 層の定義を保存する（選ばれた確率 π を、母集団に含まれる層すべてから計算するため。§4.6）
    """
    ALTER TABLE sample_draws ADD COLUMN definition TEXT NOT NULL DEFAULT '{}';
    """,
]


def migrate(conn: sqlite3.Connection) -> int:
    """未適用のマイグレーションを順に適用し、適用後の版を返す。"""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema v{current} is newer than this code (v{len(MIGRATIONS)})"
        )
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    return len(MIGRATIONS)
