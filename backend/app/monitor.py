"""1配信分の監視（受信 → ルール判定 → 保存 → 表示）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from .db import Database, StreamContext
from .pseudonym import Pseudonymizer
from .rules import RuleEngine
from .twitch.irc import ChatMessage

log = logging.getLogger(__name__)


class ChatSource(Protocol):
    channel: str

    def messages(self) -> AsyncIterator[ChatMessage]: ...


@dataclass(frozen=True)
class RecordedMessage:
    """保存した1件。display_name はここ（ライブ表示）でだけ使い、保存はしない。"""

    session_id: int
    seq: int
    chat: ChatMessage
    author_pseudo_id: str
    rule_flags: list[str]


class ChannelMonitor:
    def __init__(
        self,
        db: Database,
        source: ChatSource,
        pseudonymizer: Pseudonymizer,
        rules: RuleEngine | None = None,
        on_message: Callable[[RecordedMessage], None] | None = None,
    ) -> None:
        self.db = db
        self.source = source
        self.pseudonymize = pseudonymizer
        self.rules = rules or RuleEngine()
        self.on_message = on_message or (lambda m: None)
        self.session_id: int | None = None
        self.recorded = 0
        self.duplicates = 0

    def start(self, purpose: str, context: StreamContext) -> int:
        self.session_id = self.db.start_session(self.source.channel, purpose, context)
        return self.session_id

    async def run(self) -> None:
        """受信を続ける。キャンセルされたら 'manual'、例外なら 'error' でセッションを閉じる。"""
        if self.session_id is None:
            raise RuntimeError("start() must be called before run()")
        reason = "error"
        seq = self.db.next_seq(self.session_id)
        try:
            async for chat in self.source.messages():
                author = self.pseudonymize(chat.user_id)
                flags = self.rules.evaluate(author, chat.text, chat.sent_at)
                inserted = self.db.insert_message(
                    message_id=chat.id,
                    session_id=self.session_id,
                    seq=seq,
                    author_pseudo_id=author,
                    text=chat.text,
                    sent_at=chat.sent_at,
                    reply_parent_id=chat.reply_parent_id,
                    rule_flags=flags,
                )
                if not inserted:
                    self.duplicates += 1
                    continue
                self.recorded += 1
                self.on_message(RecordedMessage(self.session_id, seq, chat, author, flags))
                seq += 1
            reason = "offline"  # 受信元が終わった（テストの偽の受信元など）
        except (asyncio.CancelledError, KeyboardInterrupt):
            reason = "manual"
            raise
        finally:
            self.db.end_session(self.session_id, reason)
            log.info(
                "session %s ended (%s): %d recorded, %d duplicates",
                self.session_id,
                reason,
                self.recorded,
                self.duplicates,
            )
