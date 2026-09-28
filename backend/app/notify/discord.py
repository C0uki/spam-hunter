"""Discord Webhook への通知（docs/design.md §4.9）。

- ルールの通知: 監視中にリアルタイムで送る。同じ投稿者の2回目以降は、一定時間ごとに件数をまとめて送る。
- まとめ通知: セッション後の抽出が終わったときに送る。コメント本文は含めない。

チャットの本文に @everyone などが含まれていても人を呼び出さないよう、allowed_mentions で無効にする。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
COLOR_RULE = 0xE67E22
COLOR_SUMMARY = 0x3498DB
FLAG_LABELS = {"repeat": "連投", "url": "URL"}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def payload_for(embeds: list[dict[str, Any]]) -> dict[str, Any]:
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.json().get("retry_after", 1.0))
    except (ValueError, AttributeError):
        return float(resp.headers.get("Retry-After", 1.0))


async def post_async(http: httpx.AsyncClient, url: str, payload: dict, *, max_retries: int = 5) -> bool:
    for _ in range(max_retries + 1):
        try:
            resp = await http.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("discord webhook request failed: %s", type(exc).__name__)  # URL（秘密の値を含む）を出さない
            return False
        if resp.status_code == 429:
            wait = _retry_after(resp)
            log.info("discord rate limited; retrying in %.2fs", wait)
            await asyncio.sleep(wait)
            continue
        if resp.is_success:
            return True
        log.warning("discord webhook returned %s: %s", resp.status_code, resp.text[:200])
        return False
    return False


def post_sync(http: httpx.Client, url: str, payload: dict, *, max_retries: int = 5) -> bool:
    for _ in range(max_retries + 1):
        try:
            resp = http.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("discord webhook request failed: %s", type(exc).__name__)  # URL（秘密の値を含む）を出さない
            return False
        if resp.status_code == 429:
            wait = _retry_after(resp)
            log.info("discord rate limited; retrying in %.2fs", wait)
            time.sleep(wait)
            continue
        if resp.is_success:
            return True
        log.warning("discord webhook returned %s: %s", resp.status_code, resp.text[:200])
        return False
    return False


# --- embeds ------------------------------------------------------------------


def rule_embed(channel: str, display_name: str, text: str, flags: list[str], sent_at: datetime) -> dict:
    labels = "・".join(FLAG_LABELS.get(f, f) for f in flags)
    return {
        "title": _truncate(f"ルールの印: {labels}", 256),
        "description": _truncate(text, 1000),
        "color": COLOR_RULE,
        "fields": [
            {"name": "チャンネル", "value": _truncate(f"#{channel}", 1024), "inline": True},
            {"name": "投稿者", "value": _truncate(display_name, 1024) or "-", "inline": True},
        ],
        "timestamp": sent_at.isoformat(),
    }


def suppressed_embed(channel: str, display_name: str, count: int, flags: set[str]) -> dict:
    labels = "・".join(FLAG_LABELS.get(f, f) for f in sorted(flags))
    return {
        "title": _truncate(f"ルールの印（まとめ）: {labels}", 256),
        "description": f"{_truncate(display_name, 200)} さんの印付きコメントが、ほかに {count} 件ありました。",
        "color": COLOR_RULE,
        "fields": [{"name": "チャンネル", "value": _truncate(f"#{channel}", 1024), "inline": True}],
    }


def summary_embed(summary: dict[str, Any]) -> dict:
    """まとめ通知。コメント本文は含めない。"""
    fields = [
        ("配信の枠", summary.get("purpose")),
        ("時間", summary.get("duration")),
        ("終わり方", summary.get("end_reason")),
        ("受信した件数", summary.get("received")),
        ("ルールの印", summary.get("rule_flagged")),
        ("判定した件数", summary.get("judged")),
        ("判定の失敗", summary.get("judge_errors")),
        ("採点キュー", summary.get("queued")),
        ("しきい値を超えた件数", summary.get("over_threshold")),
    ]
    return {
        "title": _truncate(f"セッションのまとめ: #{summary.get('channel', '')}", 256),
        "color": COLOR_SUMMARY,
        "fields": [{"name": n, "value": str(v), "inline": True} for n, v in fields if v is not None],
    }


# --- 送信キュー ------------------------------------------------------------------


class NotificationQueue:
    """embed をためて、まとめて送る（1通に最大10個）。429 は retry_after だけ待って送り直す。"""

    def __init__(self, url: str, *, http: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self._http = http or httpx.AsyncClient(timeout=15)
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self.sent_messages = 0

    def put(self, embed: dict) -> None:
        self._queue.put_nowait(embed)

    async def run(self) -> None:
        while True:
            embeds = [await self._queue.get()]
            while len(embeds) < MAX_EMBEDS_PER_MESSAGE and not self._queue.empty():
                embeds.append(self._queue.get_nowait())
            if await post_async(self._http, self.url, payload_for(embeds)):
                self.sent_messages += 1

    async def drain(self, timeout: float = 10) -> None:
        """たまっている分を送り切る（終了時用）。"""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass
class _AuthorState:
    last_sent: float
    display_name: str
    suppressed: int = 0
    flags: set[str] = field(default_factory=set)


class RuleNotifier:
    """ルールの印の通知。同じ投稿者は、最初の1回だけすぐに送り、その後は cooldown ごとに件数をまとめて送る。"""

    def __init__(
        self,
        queue: NotificationQueue,
        channel: str,
        *,
        cooldown: float = 300,
        on_notified: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.queue = queue
        self.channel = channel
        self.cooldown = cooldown
        self.on_notified = on_notified or (lambda message_id: None)
        self.clock = clock
        self._authors: dict[str, _AuthorState] = {}

    def handle(self, message_id: str, author: str, display_name: str, text: str, flags: list[str], sent_at: datetime) -> None:
        if not flags:
            return
        now = self.clock()
        state = self._authors.get(author)
        if state is None or (now - state.last_sent >= self.cooldown and state.suppressed == 0):
            self.queue.put(rule_embed(self.channel, display_name, text, flags, sent_at))
            self._authors[author] = _AuthorState(last_sent=now, display_name=display_name)
            self.on_notified(message_id)
            return
        state.suppressed += 1
        state.flags.update(flags)
        state.display_name = display_name

    def flush(self) -> None:
        """cooldown を過ぎた投稿者の、まとめ待ちの件数を送る。"""
        now = self.clock()
        for author, state in list(self._authors.items()):
            if now - state.last_sent < self.cooldown:
                continue
            if state.suppressed:
                self.queue.put(suppressed_embed(self.channel, state.display_name, state.suppressed, state.flags))
                state.last_sent = now
                state.suppressed = 0
                state.flags = set()
            elif now - state.last_sent >= self.cooldown * 2:
                del self._authors[author]  # しばらく印のない投稿者は忘れる

    def flush_all(self) -> None:
        """終了時: まとめ待ちをすべて送る。"""
        for state in self._authors.values():
            if state.suppressed:
                self.queue.put(suppressed_embed(self.channel, state.display_name, state.suppressed, state.flags))
                state.suppressed = 0
        self._authors.clear()

    async def run_flusher(self, interval: float | None = None) -> None:
        while True:
            await asyncio.sleep(interval or min(self.cooldown, 60))
            self.flush()
