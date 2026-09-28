"""Twitch Helix API による配信状況の取得と、配信終了の検知（docs/design.md §4.3）。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"


@dataclass(frozen=True)
class StreamInfo:
    live: bool
    game_name: str | None = None
    title: str | None = None


class HelixClient:
    """アプリ用アクセストークン（Client Credentials）で /helix/streams を呼ぶ。"""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        token_url: str = TOKEN_URL,
        streams_url: str = STREAMS_URL,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=15)
        self._token_url = token_url
        self._streams_url = streams_url
        self._token: str | None = None
        self._token_expires = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires - 60:
            return self._token
        resp = await self._http.post(
            self._token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + float(body.get("expires_in", 3600))
        return self._token

    async def get_stream(self, user_login: str) -> StreamInfo:
        """配信中なら live=True とゲーム名・タイトルを返す。配信していなければ live=False。"""
        for attempt in range(2):
            token = await self._get_token(force=attempt > 0)
            resp = await self._http.get(
                self._streams_url,
                params={"user_login": user_login},
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # トークンの期限切れ。取り直して1回だけやり直す
            resp.raise_for_status()
            data = resp.json().get("data") or []
            if not data:
                return StreamInfo(live=False)
            s = data[0]
            return StreamInfo(live=True, game_name=s.get("game_name") or None, title=s.get("title") or None)
        raise RuntimeError("unreachable")


class StreamWatcher:
    """配信状況を定期的に取り、変わったら on_change、配信が終わったら on_offline を呼ぶ。

    配信の終了は、一度「配信中」を見たあとに、offline_checks 回続けて「配信していない」だったときとする
    （配信が始まる前から監視を始めても、すぐには終わらないようにするため）。
    取得に失敗した回は数えない。
    """

    def __init__(
        self,
        client: HelixClient,
        channel: str,
        *,
        interval: float = 300,
        offline_checks: int = 2,
        on_change: Callable[[StreamInfo], Awaitable[None] | None] | None = None,
        on_offline: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self.client = client
        self.channel = channel
        self.interval = interval
        self.offline_checks = offline_checks
        self.on_change = on_change
        self.on_offline = on_offline
        self.seen_live = False
        self.offline_streak = 0
        self.last: StreamInfo | None = None

    async def check_once(self) -> bool:
        """1回取得して処理する。配信の終了を検知したら True。"""
        try:
            info = await self.client.get_stream(self.channel)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("helix request failed: %r", exc)
            return False
        if info.live:
            self.seen_live = True
            self.offline_streak = 0
            if self.last is None or (info.game_name, info.title) != (self.last.game_name, self.last.title):
                self.last = info
                await _maybe_await(self.on_change, info)
            return False
        if not self.seen_live:
            if self.offline_streak == 0:
                log.info("#%s is not live yet; waiting for the stream to start", self.channel)
            self.offline_streak = 1
            return False
        self.offline_streak += 1
        log.info("#%s looks offline (%d/%d)", self.channel, self.offline_streak, self.offline_checks)
        if self.offline_streak >= self.offline_checks:
            await _maybe_await(self.on_offline)
            return True
        return False

    async def run(self) -> None:
        while True:
            if await self.check_once():
                return
            await asyncio.sleep(self.interval)


async def _maybe_await(fn, *args) -> None:
    if fn is None:
        return
    result = fn(*args)
    if asyncio.iscoroutine(result):
        await result
