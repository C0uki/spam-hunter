"""Twitch チャット（IRC over WebSocket）の受信。

読み取り専用の匿名接続（justinfan）で1チャンネルに参加し、PRIVMSG を ChatMessage として返す。
切断されたら指数バックオフで再接続する。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://irc-ws.chat.twitch.tv:443"

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass(frozen=True)
class IrcMessage:
    command: str
    params: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str | None = None

    @property
    def nick(self) -> str | None:
        if self.prefix is None:
            return None
        return self.prefix.split("!", 1)[0]


@dataclass(frozen=True)
class ChatMessage:
    id: str
    channel: str
    user_id: str
    login: str
    display_name: str
    text: str
    sent_at: datetime
    reply_parent_id: str | None = None
    is_action: bool = False


def _unescape_tag_value(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            if i + 1 < len(value):
                out.append(_TAG_ESCAPES.get(value[i + 1], value[i + 1]))
                i += 2
                continue
            i += 1  # 末尾の単独のバックスラッシュは捨てる
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_line(line: str) -> IrcMessage:
    """IRC の1行（IRCv3 タグ付き）を分解する。"""
    line = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    prefix = None
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag_value(value)
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if line.startswith(":"):
        trailing = line[1:]
        line = ""
    else:
        line, sep, rest = line.partition(" :")
        if sep:
            trailing = rest
    parts = line.split()
    if not parts:
        raise ValueError("IRC line has no command")
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return IrcMessage(command=parts[0].upper(), params=params, tags=tags, prefix=prefix)


def to_chat_message(msg: IrcMessage, *, now: datetime | None = None) -> ChatMessage | None:
    """PRIVMSG を ChatMessage にする。必要なタグがなければ None。"""
    if msg.command != "PRIVMSG" or len(msg.params) < 2:
        return None
    msg_id = msg.tags.get("id")
    user_id = msg.tags.get("user-id")
    if not msg_id or not user_id:
        return None
    text = msg.params[-1]
    is_action = False
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[len("\x01ACTION ") : -1]
        is_action = True
    sent_at = now or datetime.now(UTC)
    ts = msg.tags.get("tmi-sent-ts")
    if ts and ts.isdigit():
        sent_at = datetime.fromtimestamp(int(ts) / 1000, UTC)
    login = msg.nick or ""
    return ChatMessage(
        id=msg_id,
        channel=msg.params[0].lstrip("#"),
        user_id=user_id,
        login=login,
        display_name=msg.tags.get("display-name") or login,
        text=text,
        sent_at=sent_at,
        reply_parent_id=msg.tags.get("reply-parent-msg-id") or None,
        is_action=is_action,
    )


class _ReconnectRequested(Exception):
    pass


def backoff_delay(attempt: int, *, initial: float, maximum: float) -> float:
    """attempt 回目（0始まり）の待ち時間: initial, 2*initial, 4*initial, ... 上限 maximum。"""
    return min(maximum, initial * (2**attempt))


class TwitchChatClient:
    """1チャンネル分の受信。messages() を async for で回すと、切断されても自動で再接続し続ける。"""

    def __init__(
        self,
        channel: str,
        *,
        url: str = DEFAULT_URL,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        idle_timeout: float = 360.0,
        proxy: str | bool | None = True,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.channel = channel.lower().lstrip("#")
        self.url = url
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.idle_timeout = idle_timeout
        self.proxy = proxy
        self._on_status = on_status or (lambda s: log.info("%s", s))
        self.connect_count = 0

    async def messages(self) -> AsyncIterator[ChatMessage]:
        attempt = 0
        while True:
            try:
                async with connect(self.url, proxy=self.proxy, open_timeout=15) as ws:
                    self.connect_count += 1
                    await self._handshake(ws)
                    self._on_status(f"connected to #{self.channel}")
                    while True:
                        frame = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                        if isinstance(frame, bytes):
                            frame = frame.decode("utf-8", errors="replace")
                        for line in frame.split("\r\n"):
                            if not line:
                                continue
                            try:
                                msg = parse_line(line)
                            except ValueError:
                                log.warning("unparsable IRC line: %r", line[:200])
                                continue
                            chat = await self._handle(ws, msg)
                            if msg.command in ("001", "JOIN", "PRIVMSG", "ROOMSTATE"):
                                attempt = 0
                            if chat is not None:
                                yield chat
            except _ReconnectRequested:
                self._on_status("server requested reconnect")
                attempt = 0
            except TimeoutError:
                self._on_status(f"no data for {self.idle_timeout:.0f}s; reconnecting")
            except (OSError, WebSocketException) as exc:
                self._on_status(f"connection lost: {exc!r}")
            delay = backoff_delay(attempt, initial=self.backoff_initial, maximum=self.backoff_max)
            attempt += 1
            self._on_status(f"reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)

    async def _handshake(self, ws) -> None:
        nick = f"justinfan{random.randint(10000, 99999)}"
        await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        await ws.send("PASS SCHMOOPIIE")
        await ws.send(f"NICK {nick}")
        await ws.send(f"JOIN #{self.channel}")

    async def _handle(self, ws, msg: IrcMessage) -> ChatMessage | None:
        if msg.command == "PING":
            await ws.send("PONG :" + (msg.params[-1] if msg.params else "tmi.twitch.tv"))
            return None
        if msg.command == "RECONNECT":
            raise _ReconnectRequested
        if msg.command == "NOTICE":
            self._on_status(f"NOTICE {msg.tags.get('msg-id', '')}: {msg.params[-1] if msg.params else ''}")
            return None
        if msg.command == "PRIVMSG":
            chat = to_chat_message(msg)
            if chat is None:
                log.warning("PRIVMSG without id/user-id tags was skipped")
            return chat
        return None
