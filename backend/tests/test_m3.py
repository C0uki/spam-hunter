"""M3: Helix、Discord 通知、層別抽出、セッション後の流れ。外部サービスは httpx.MockTransport で置き換える。"""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import load_defaults
from app.db import Database, StreamContext
from app.db.schema import MIGRATIONS, migrate
from app.judge.variants import load_questions
from app.monitor import ChannelMonitor, apply_stream_info
from app.notify.discord import (
    NotificationQueue,
    RuleNotifier,
    payload_for,
    post_async,
    rule_embed,
    summary_embed,
)
from app.pipeline import build_summary, count_over_threshold, run_pipeline, sessions_for_pipeline
from app.pseudonym import Pseudonymizer
from app.sampling import build_strata, draw_session_samples, in_population, inclusion_probabilities
from app.twitch.helix import HelixClient, StreamInfo, StreamWatcher
from app.twitch.irc import ChatMessage
from tests.test_judge import FakeBackend

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# --- Helix ------------------------------------------------------------------------


def helix_transport(streams: list, token_calls: list, fail_first_with_401=False):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": f"tok{len(token_calls)}", "expires_in": 3600})
        assert request.headers["Client-Id"] == "cid"
        if fail_first_with_401 and state["n"] == 0:
            state["n"] += 1
            return httpx.Response(401, json={"message": "invalid token"})
        item = streams.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json={"data": item, "pagination": {}})

    return httpx.MockTransport(handler)


def make_helix(streams, token_calls, **kw):
    http = httpx.AsyncClient(transport=helix_transport(streams, token_calls, **kw))
    return HelixClient("cid", "secret", http=http, token_url="https://id.twitch.tv/oauth2/token")


async def test_helix_live_and_offline():
    calls = []
    helix = make_helix([[{"game_name": "RPG", "title": "初見", "type": "live"}], []], calls)
    assert await helix.get_stream("chan") == StreamInfo(True, "RPG", "初見")
    assert await helix.get_stream("chan") == StreamInfo(False)
    assert len(calls) == 1  # トークンは使い回す
    assert calls[0]["grant_type"] == "client_credentials"
    await helix.aclose()


async def test_helix_refreshes_token_on_401():
    calls = []
    helix = make_helix([[]], calls, fail_first_with_401=True)
    assert (await helix.get_stream("chan")).live is False
    assert len(calls) == 2
    await helix.aclose()


async def test_stream_watcher_changes_and_offline():
    calls, changes, ended = [], [], []
    live = [{"game_name": "RPG", "title": "t1"}]
    streams = [
        [],  # 配信が始まる前（数えない）
        [],
        live,
        live,  # 変化なし
        [{"game_name": "Other", "title": "t1"}],  # ゲームを切り替えた
        httpx.ConnectError("boom"),  # 取得の失敗は数えない
        [],
        [],  # 2回続けて配信していない → 終了
    ]
    helix = make_helix(streams, calls)
    watcher = StreamWatcher(helix, "chan", interval=0, offline_checks=2, on_change=changes.append, on_offline=lambda: ended.append(1))
    await asyncio.wait_for(watcher.run(), 5)
    assert [c.game_name for c in changes] == ["RPG", "Other"]
    assert ended == [1]
    assert streams == []
    await helix.aclose()


async def test_stream_watcher_offline_streak_resets_when_live_again():
    calls, ended = [], []
    live = [{"game_name": "RPG", "title": "t"}]
    helix = make_helix([live, [], live, [], []], calls)
    watcher = StreamWatcher(helix, "chan", interval=0, offline_checks=2, on_offline=lambda: ended.append(1))
    results = [await watcher.check_once() for _ in range(5)]
    assert results == [False, False, False, False, True]
    await helix.aclose()


def test_apply_stream_info_keeps_manual_fields(db):
    sid = db.start_session("chan", "story_firstplay", StreamContext(welcomes_advice="no", spoiler_note="3章まで"), started_at=T0)
    apply_stream_info(db, sid, "RPG", "初見プレイ")
    ctx = db.latest_context(sid)
    assert (ctx["game_name"], ctx["stream_title"], ctx["welcomes_advice"], ctx["spoiler_note"], ctx["source"]) == (
        "RPG", "初見プレイ", "no", "3章まで", "helix"
    )


# --- Discord ----------------------------------------------------------------------


def discord_transport(responses: list, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        status, body = responses.pop(0) if responses else (204, None)
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    return httpx.MockTransport(handler)


async def test_post_retries_after_429():
    sent = []
    http = httpx.AsyncClient(transport=discord_transport([(429, {"retry_after": 0.01, "global": False}), (204, None)], sent))
    assert await post_async(http, "https://discord.test/hook", payload_for([{"title": "x"}]))
    assert len(sent) == 2
    assert sent[0]["allowed_mentions"] == {"parse": []}  # @everyone などで人を呼び出さない
    await http.aclose()


async def test_post_gives_up_on_other_errors():
    sent = []
    http = httpx.AsyncClient(transport=discord_transport([(400, {"message": "bad"})], sent))
    assert not await post_async(http, "https://discord.test/hook", payload_for([]))
    await http.aclose()


async def test_queue_coalesces_up_to_ten_embeds():
    sent = []
    http = httpx.AsyncClient(transport=discord_transport([], sent))
    queue = NotificationQueue("https://discord.test/hook", http=http)
    for i in range(13):
        queue.put({"title": str(i)})
    task = asyncio.create_task(queue.run())
    await queue.drain()
    await asyncio.sleep(0.05)
    task.cancel()
    assert [len(m["embeds"]) for m in sent] == [10, 3]
    await queue.aclose()


def test_rule_embed_truncates_and_labels():
    e = rule_embed("chan", "表示名", "あ" * 2000, ["repeat", "url"], T0)
    assert e["title"] == "ルールの印: 連投・URL"
    assert len(e["description"]) == 1000


def test_summary_embed_has_no_comment_text():
    e = summary_embed({"channel": "chan", "received": 10, "queued": 3, "purpose": "other"})
    assert e["title"].endswith("#chan")
    assert {f["name"] for f in e["fields"]} == {"受信した件数", "採点キュー", "配信の枠"}


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, embed):
        self.items.append(embed)


def test_rule_notifier_throttles_per_author():
    now = {"t": 0.0}
    q = FakeQueue()
    notified = []
    n = RuleNotifier(q, "chan", cooldown=300, on_notified=notified.append, clock=lambda: now["t"])
    n.handle("m1", "a", "A", "spam", ["repeat"], T0)
    n.handle("m2", "a", "A", "spam", ["repeat"], T0)
    n.handle("m3", "a", "A", "http://x.com", ["url"], T0)
    n.handle("m4", "b", "B", "spam", ["repeat"], T0)
    n.handle("m5", "c", "C", "hello", [], T0)  # 印なしは通知しない
    assert notified == ["m1", "m4"]
    assert len(q.items) == 2

    now["t"] = 100
    n.flush()
    assert len(q.items) == 2  # まだ cooldown の途中
    now["t"] = 301
    n.flush()
    assert len(q.items) == 3
    assert "ほかに 2 件" in q.items[-1]["description"]
    assert "連投・URL" in q.items[-1]["title"]

    # まとめを送ったあと、さらに cooldown を過ぎて印がなければ忘れる。次の印はまたすぐ送る
    now["t"] = 1000
    n.flush()
    n.handle("m6", "a", "A", "spam", ["repeat"], T0)
    assert notified[-1] == "m6"


def test_rule_notifier_flush_all():
    q = FakeQueue()
    n = RuleNotifier(q, "chan", cooldown=300, clock=lambda: 0.0)
    n.handle("m1", "a", "A", "x", ["url"], T0)
    n.handle("m2", "a", "A", "x", ["url"], T0)
    n.flush_all()
    assert len(q.items) == 2


# --- 層別抽出 --------------------------------------------------------------------------


def seed_judged_session(db, values: list[tuple[float, str]], variant="v1"):
    """values: 各メッセージの (abuse の P(true), severity)。ほかの noul は 0.1。"""
    sid = db.start_session("chan", "high_traffic", StreamContext(), started_at=T0)
    rows = []
    for i, (p, sev) in enumerate(values, start=1):
        mid = f"m{i}"
        db.insert_message(message_id=mid, session_id=sid, seq=i, author_pseudo_id="p", text=f"t{i}",
                          sent_at=T0 + timedelta(seconds=i), reply_parent_id=None, rule_flags=[])
        for qid in ("abuse", "personal_attack", "spam_promo", "sexual", "spoiler", "backseat"):
            rows.append((mid, variant, qid, repr(p if qid == "abuse" else 0.1), None, 0.9, "fake", 1.0, "t"))
        rows.append((mid, variant, "severity", sev, "{}", 0.9, "fake", 1.0, "t"))
    db.end_session(sid, "manual")
    db.save_judgments(rows, done_ids=[f"m{i}" for i in range(1, len(values) + 1)], error_ids=[], mark_status=True)
    db.claim_primary_variant(sid, variant)
    db.set_pipeline_status(sid, "judged")
    return sid


@pytest.fixture
def qtypes():
    return load_questions().primary_variant.question_types()


def test_build_strata_from_defaults(qtypes):
    names = [s.name for s in build_strata(qtypes, load_defaults()["sampling"])]
    assert names[0] == "random"
    assert "abuse:high" in names and "backseat:low" in names
    assert "severity:severe" in names and "severity:caution" in names
    assert len(names) == 1 + 6 * 3 + 2


def test_in_population_bands():
    d = {"type": "noul_band", "question": "abuse", "min": 0.7, "max": 0.9, "n": 3}
    assert in_population(d, {"abuse": "0.7"})
    assert not in_population(d, {"abuse": "0.9"})
    assert not in_population(d, {})


def test_draw_samples_and_idempotency(db, qtypes):
    values = [(0.95, "severe")] * 5 + [(0.8, "caution")] * 5 + [(0.1, "none")] * 90
    sid = seed_judged_session(db, values)
    cfg = {"random": 10, "noul_bands": [{"name": "high", "min": 0.9, "max": 1.01, "n": 4},
                                        {"name": "mid", "min": 0.7, "max": 0.9, "n": 3}],
           "choice": {"severity": {"severe": 2, "caution": 2}}}
    r = draw_session_samples(db, sid, "v1", qtypes, cfg, rng=random.Random(1))
    assert r.strata["random"] == (100, 10)
    assert r.strata["abuse:high"] == (5, 4)
    assert r.strata["abuse:mid"] == (5, 3)
    assert r.strata["personal_attack:high"] == (0, 0)  # 母集団が空でも記録する
    assert r.strata["severity:severe"] == (5, 2)
    stored = db.conn.execute("SELECT COUNT(*) FROM sample_draws WHERE session_id = ?", (sid,)).fetchone()[0]
    assert stored == len(r.strata)
    definition = json.loads(db.conn.execute("SELECT definition FROM sample_draws WHERE stratum = 'abuse:high'").fetchone()[0])
    assert definition == {"type": "noul_band", "question": "abuse", "min": 0.9, "max": 1.01, "n": 4}

    again = draw_session_samples(db, sid, "v1", qtypes, cfg)
    assert again.skipped
    assert db.conn.execute("SELECT COUNT(*) FROM sample_draws").fetchone()[0] == stored


def test_inclusion_probability_counts_every_population_stratum(db, qtypes):
    # 高い帯の5件は、random（N=100, n=10）と abuse:high（N=5, n=4）と severity:severe（N=5, n=2）の母集団に入る
    values = [(0.95, "severe")] * 5 + [(0.1, "none")] * 95
    sid = seed_judged_session(db, values)
    cfg = {"random": 10, "noul_bands": [{"name": "high", "min": 0.9, "max": 1.01, "n": 4}],
           "choice": {"severity": {"severe": 2}}}
    draw_session_samples(db, sid, "v1", qtypes, cfg, rng=random.Random(2))
    pis = inclusion_probabilities(db, sid, "v1")
    high = [pis[m] for m in pis if int(m[1:]) <= 5]
    low = [pis[m] for m in pis if int(m[1:]) > 5]
    expected_high = 1 - (1 - 10 / 100) * (1 - 4 / 5) * (1 - 2 / 5)
    assert high and all(p == pytest.approx(expected_high) for p in high)
    assert low and all(p == pytest.approx(10 / 100) for p in low)


# --- セッション後の流れ ------------------------------------------------------------------


def make_ended_session(db, texts):
    sid = db.start_session("chan", "high_traffic", StreamContext(), started_at=T0)
    for i, text in enumerate(texts, start=1):
        db.insert_message(message_id=f"m{i}", session_id=sid, seq=i, author_pseudo_id="p", text=text,
                          sent_at=T0 + timedelta(seconds=i), reply_parent_id=None,
                          rule_flags=["url"] if "http" in text else [])
    db.end_session(sid, "offline", ended_at=T0 + timedelta(hours=1, minutes=5))
    return sid


def test_run_pipeline_end_to_end(db):
    variant = load_questions().primary_variant
    sid = make_ended_session(db, ["こんにちは", "消えろ", "http://spam.example.com", "草"])
    summaries = []
    loads = []

    def factory():
        loads.append(1)
        return FakeBackend()

    status = run_pipeline(db, sid, variant, factory, load_defaults(), send_summary=summaries.append)
    assert status == "done"
    assert loads == [1]
    assert db.get_session(sid)["pipeline_status"] == "done"
    assert sid not in sessions_for_pipeline(db)
    s = summaries[0]
    assert s["received"] == 4 and s["judged"] == 4 and s["rule_flagged"] == 1
    assert s["queued"] == 4  # 4件しかないので、random 層で全件が入る
    assert s["over_threshold"] == 1  # 「消えろ」だけ（FakeBackend は abuse=0.95・severity=severe）
    assert s["duration"] == "1時間5分"
    assert s["end_reason"] == "offline"
    assert "text" not in json.dumps(s, ensure_ascii=False)

    # 二度目は何もしない
    assert run_pipeline(db, sid, variant, factory, load_defaults(), send_summary=summaries.append) == "done"
    assert len(summaries) == 1


def test_run_pipeline_pause_and_resume(db):
    variant = load_questions().primary_variant
    sid = make_ended_session(db, ["a", "b", "c"])
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    status = run_pipeline(db, sid, variant, FakeBackend, {"judge": {"batch_size": 1}}, should_stop=stop_after_first)
    assert status == "paused"
    assert sid in sessions_for_pipeline(db)
    assert run_pipeline(db, sid, variant, FakeBackend, {"judge": {"batch_size": 1}}) == "done"


def test_run_pipeline_without_messages_does_not_load_backend(db):
    variant = load_questions().primary_variant
    sid = make_ended_session(db, [])

    def factory():
        raise AssertionError("backend should not be loaded")

    assert run_pipeline(db, sid, variant, factory, {}) == "done"


def test_run_pipeline_rejects_other_variant(db):
    cfg = load_questions()
    sid = make_ended_session(db, ["a"])
    run_pipeline(db, sid, cfg.primary_variant, FakeBackend, {})
    with pytest.raises(ValueError):
        run_pipeline(db, sid, cfg.get("v1-labelsAB"), FakeBackend, {})


def test_summary_failure_does_not_break_pipeline(db):
    variant = load_questions().primary_variant
    sid = make_ended_session(db, ["a"])

    def broken(summary):
        raise RuntimeError("discord down")

    assert run_pipeline(db, sid, variant, FakeBackend, {}, send_summary=broken) == "done"


def test_count_over_threshold_policy(db):
    sid = seed_judged_session(db, [(0.95, "none"), (0.5, "severe"), (0.1, "caution"), (0.1, "none")])
    policy = {"noul_items": ["abuse"], "noul_threshold": 0.9, "choice_notify": {"severity": ["severe"]}}
    assert count_over_threshold(db, sid, "v1", policy) == 2
    assert build_summary(db, sid, "v1", policy)["over_threshold"] == 2


# --- 監視の停止理由 ------------------------------------------------------------------------


class EndlessSource:
    channel = "chan"

    async def messages(self):
        i = 0
        while True:
            i += 1
            yield ChatMessage(id=f"m{i}", channel="chan", user_id="1", login="u", display_name="U",
                              text="hi", sent_at=T0 + timedelta(seconds=i))
            await asyncio.sleep(0.01)


async def test_monitor_stop_reason_offline(db):
    monitor = ChannelMonitor(db, EndlessSource(), Pseudonymizer(b"s"))
    sid = monitor.start("other", StreamContext())
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.05)
    monitor.stop_reason = "offline"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.get_session(sid)["end_reason"] == "offline"


# --- マイグレーション ---------------------------------------------------------------------


def test_migrates_v1_database_to_latest(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute("INSERT INTO sessions (channel_login, purpose, started_at, pipeline_status) VALUES ('c', 'other', 't', 'ended')")
    conn.execute("INSERT INTO sample_draws (session_id, variant, stratum, population_size, draw_size, drawn_at) VALUES (1, 'v1', 'random', 1, 1, 't')")
    conn.commit()
    assert migrate(conn) == len(MIGRATIONS)
    assert conn.execute("SELECT definition FROM sample_draws").fetchone()[0] == "{}"
    conn.close()
