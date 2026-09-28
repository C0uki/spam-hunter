"""Laya を使わないルール判定（連投・URL）。"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime

_WHITESPACE = re.compile(r"\s+")
_RUNS = re.compile(r"(.)\1{2,}", re.DOTALL)

_TLDS = (
    "com|net|org|info|biz|io|gg|tv|jp|co|ly|me|xyz|link|site|shop|app|dev|live|"
    "online|store|top|club|fun|click|ru|cn|kr|us|uk|to|cc|be|la|so|ai"
)
_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    # 日本語に続けて書かれたドメイン（「ここexample.comへ」）も拾うため、\b ではなく ASCII の境界で区切る
    rf"|(?<![a-z0-9.-])[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.(?:{_TLDS})(?![a-z0-9-])(?:/\S*)?",
    re.IGNORECASE,
)

FLAG_REPEAT = "repeat"
FLAG_URL = "url"


def normalize(text: str) -> str:
    """連投の比較用。NFKC（全角・半角の統一）、小文字化、空白の除去、3文字以上の同じ文字の並びを2文字に縮める。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _WHITESPACE.sub("", text)
    return _RUNS.sub(r"\1\1", text)


def contains_url(text: str) -> bool:
    return _URL.search(unicodedata.normalize("NFKC", text)) is not None


@dataclass(frozen=True)
class RuleConfig:
    repeat_window_sec: float = 60.0
    repeat_min_count: int = 3
    url_enabled: bool = True


class RepeatDetector:
    """同じ投稿者が、同じ内容（正規化後）を window 秒以内に min_count 回以上書いたら True。

    投稿者の区別はメモリ上だけで行う。
    """

    def __init__(self, window_sec: float, min_count: int) -> None:
        self.window_sec = window_sec
        self.min_count = min_count
        self._history: dict[str, deque[tuple[float, str]]] = {}
        self._calls = 0

    def add(self, author: str, normalized: str, at: datetime) -> bool:
        ts = at.timestamp()
        history = self._history.setdefault(author, deque())
        history.append((ts, normalized))
        while history and history[0][0] < ts - self.window_sec:
            history.popleft()
        self._calls += 1
        if self._calls % 1000 == 0:
            self._sweep(ts)
        if not normalized:
            return False
        return sum(1 for _, n in history if n == normalized) >= self.min_count

    def _sweep(self, now: float) -> None:
        stale = [a for a, h in self._history.items() if not h or h[-1][0] < now - self.window_sec]
        for author in stale:
            del self._history[author]

    def __len__(self) -> int:
        return len(self._history)


class RuleEngine:
    def __init__(self, config: RuleConfig | None = None) -> None:
        self.config = config or RuleConfig()
        self._repeat = RepeatDetector(self.config.repeat_window_sec, self.config.repeat_min_count)

    def evaluate(self, author: str, text: str, at: datetime) -> list[str]:
        flags: list[str] = []
        if self._repeat.add(author, normalize(text), at):
            flags.append(FLAG_REPEAT)
        if self.config.url_enabled and contains_url(text):
            flags.append(FLAG_URL)
        return flags
