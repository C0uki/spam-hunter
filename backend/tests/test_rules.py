from datetime import UTC, datetime, timedelta

import pytest

from app.rules import FLAG_REPEAT, FLAG_URL, RuleConfig, RuleEngine, contains_url, normalize

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("wwwww", "ww"),
        ("ww", "ww"),
        ("ｗｗｗ", "ww"),  # 全角 → 半角
        ("草 草 草", "草草"),  # 空白の除去のあと、3文字以上の並びを2文字に
        ("ＡＢＣ　ｄｅｆ", "abcdef"),
        ("すごーーーーい", "すごーーい"),
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/abc",
        "見て http://x.y",
        "www.example.org",
        "bit.ly/abc123",
        "ここexample.comへ",
        "ｈｔｔｐｓ：／／ｅｘａｍｐｌｅ．ｃｏｍ",  # 全角
        "discord.gg/xxxx",
    ],
)
def test_contains_url(text):
    assert contains_url(text)


@pytest.mark.parametrize("text", ["こんにちは", "3.14 だよ", "v1.2 きた", "e.g. これ", "おつ。。。"])
def test_does_not_contain_url(text):
    assert not contains_url(text)


def test_repeat_flagged_on_third_within_window():
    rules = RuleEngine()
    assert rules.evaluate("a", "おはよう", T0) == []
    assert rules.evaluate("a", "おはよう  ", T0 + timedelta(seconds=10)) == []
    assert rules.evaluate("a", "ｵﾊﾖｳ", T0 + timedelta(seconds=15)) == []  # 半角カナは NFKC で全角カナ（別の文字列）
    assert rules.evaluate("a", "おはよう", T0 + timedelta(seconds=20)) == [FLAG_REPEAT]
    assert rules.evaluate("a", "おはよう", T0 + timedelta(seconds=30)) == [FLAG_REPEAT]


def test_repeat_normalized_variants_count_together():
    rules = RuleEngine()
    rules.evaluate("a", "wwwww", T0)
    rules.evaluate("a", "ｗｗｗ", T0 + timedelta(seconds=1))
    assert rules.evaluate("a", "w w w w", T0 + timedelta(seconds=2)) == [FLAG_REPEAT]


def test_repeat_outside_window_not_flagged():
    rules = RuleEngine()
    rules.evaluate("a", "spam", T0)
    rules.evaluate("a", "spam", T0 + timedelta(seconds=30))
    assert rules.evaluate("a", "spam", T0 + timedelta(seconds=61)) == []


def test_repeat_is_per_author():
    rules = RuleEngine()
    rules.evaluate("a", "88888", T0)
    rules.evaluate("b", "88888", T0)
    assert rules.evaluate("c", "88888", T0) == []


def test_repeat_config():
    rules = RuleEngine(RuleConfig(repeat_window_sec=5, repeat_min_count=2))
    rules.evaluate("a", "x", T0)
    assert rules.evaluate("a", "x", T0 + timedelta(seconds=4)) == [FLAG_REPEAT]


def test_url_and_repeat_together():
    rules = RuleEngine()
    for i in range(2):
        rules.evaluate("a", "https://spam.example.com", T0 + timedelta(seconds=i))
    assert rules.evaluate("a", "https://spam.example.com", T0 + timedelta(seconds=2)) == [FLAG_REPEAT, FLAG_URL]


def test_url_disabled():
    rules = RuleEngine(RuleConfig(url_enabled=False))
    assert rules.evaluate("a", "https://example.com", T0) == []


def test_old_authors_are_swept():
    rules = RuleEngine()
    for i in range(1000):
        rules.evaluate(f"user{i}", "hi", T0)
    rules.evaluate("late", "hi", T0 + timedelta(seconds=120))
    for i in range(999):
        rules.evaluate("late", f"x{i}", T0 + timedelta(seconds=120))
    assert len(rules._repeat) == 1
