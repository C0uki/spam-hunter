"""ローカルの偽 IRC サーバーを立てて、受信・再接続・PING/PONG・保存までを確かめる。"""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.db import Database, StreamContext
from app.monitor import ChannelMonitor
from app.pseudonym import Pseudonymizer
from app.twitch.irc import TwitchChatClient


def privmsg(mid, uid, text, ts=1700000000000, extra=""):
    return (
        f"@display-name=User{uid};id={mid};tmi-sent-ts={ts};user-id={uid}{extra} "
        f":user{uid}!user{uid}@user{uid}.tmi.twitch.tv PRIVMSG #testchan :{text}"
    )


class FakeTwitch:
    """接続ごとに scripts[n] を送る。各要素は、送る行のリスト（None は切断）。"""

    def __init__(self, scripts):
        self.scripts = scripts
        self.connections = 0
        self.received: list[list[str]] = []

    async def handler(self, ws):
        idx = self.connections
        self.connections += 1
        got: list[str] = []
        self.received.append(got)
        # クライアントの CAP / PASS / NICK / JOIN を受け取る
        while len(got) < 4:
            got.append(await ws.recv())
        await ws.send(":tmi.twitch.tv 001 justinfan :Welcome, GLHF!")
        script = self.scripts[idx] if idx < len(self.scripts) else []
        for item in script:
            if item is None:
                return  # 切断
            if item == "WAIT_PONG":
                got.append(await ws.recv())
                continue
            await ws.send(item)
        await ws.wait_closed()  # クライアントが切るまで開いたまま待つ


@pytest.fixture
async def fake_server():
    servers = []

    async def start(scripts):
        fake = FakeTwitch(scripts)
        server = await serve(fake.handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return fake, f"ws://127.0.0.1:{port}"

    yield start
    for s in servers:
        s.close()
        await s.wait_closed()


async def collect(client, n, timeout=5):
    out = []

    async def run():
        async for m in client.messages():
            out.append(m)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_handshake_and_receive(fake_server):
    fake, url = await fake_server([[privmsg("m1", 1, "hello")]])
    client = TwitchChatClient("TestChan", url=url, proxy=None, backoff_initial=0.01)
    msgs = await collect(client, 1)
    assert msgs[0].text == "hello"
    sent = fake.received[0]
    assert sent[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
    assert sent[2].startswith("NICK justinfan")
    assert sent[3] == "JOIN #testchan"


async def test_reconnects_after_disconnect(fake_server):
    fake, url = await fake_server(
        [
            [privmsg("m1", 1, "before"), None],
            [privmsg("m2", 1, "after")],
        ]
    )
    client = TwitchChatClient("testchan", url=url, proxy=None, backoff_initial=0.01)
    msgs = await collect(client, 2)
    assert [m.text for m in msgs] == ["before", "after"]
    assert fake.connections == 2


async def test_reconnect_command(fake_server):
    fake, url = await fake_server(
        [
            [":tmi.twitch.tv RECONNECT"],
            [privmsg("m1", 1, "after reconnect")],
        ]
    )
    client = TwitchChatClient("testchan", url=url, proxy=None, backoff_initial=0.01)
    msgs = await collect(client, 1)
    assert msgs[0].text == "after reconnect"
    assert fake.connections == 2


async def test_ping_pong_and_multiline_frame(fake_server):
    fake, url = await fake_server(
        [["PING :tmi.twitch.tv", "WAIT_PONG", privmsg("m1", 1, "a") + "\r\n" + privmsg("m2", 2, "b")]]
    )
    client = TwitchChatClient("testchan", url=url, proxy=None, backoff_initial=0.01)
    msgs = await collect(client, 2)
    assert [m.text for m in msgs] == ["a", "b"]
    assert fake.received[0][4] == "PONG :tmi.twitch.tv"


async def test_idle_timeout_triggers_reconnect(fake_server):
    fake, url = await fake_server([[], [privmsg("m1", 1, "second")]])
    client = TwitchChatClient("testchan", url=url, proxy=None, backoff_initial=0.01, idle_timeout=0.3)
    msgs = await collect(client, 1)
    assert msgs[0].text == "second"
    assert fake.connections == 2


async def test_broken_server_keeps_retrying():
    # 接続を受け付けてすぐ切るサーバー。閉じたポートへの接続は、Windows では断られるまで約2秒かかり、
    # テストが OS によって不安定になるため、どの OS でもすぐに失敗するこの形で確かめる
    async def slam(reader, writer):
        writer.close()

    server = await asyncio.start_server(slam, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    statuses = []
    client = TwitchChatClient(
        "testchan", url=f"ws://127.0.0.1:{port}", proxy=None, backoff_initial=0.01, backoff_max=0.02,
        on_status=statuses.append,
    )

    async def until_three_retries():
        async for _ in client.messages():
            pass

    task = asyncio.create_task(until_three_retries())
    try:
        for _ in range(500):
            if sum(s.startswith("reconnecting in") for s in statuses) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        server.close()
    assert sum(s.startswith("reconnecting in") for s in statuses) >= 3
    assert any(s.startswith("connection lost") for s in statuses)


async def test_monitor_records_to_db(fake_server, tmp_path):
    fake, url = await fake_server(
        [
            [
                privmsg("m1", 1, "こんにちは"),
                privmsg("m2", 2, "https://spam.example.com", extra=";reply-parent-msg-id=m1"),
                None,
            ],
            # 再接続後に同じメッセージが重複して届いても、1件として扱う
            [privmsg("m2", 2, "https://spam.example.com"), privmsg("m3", 1, "またね")],
        ]
    )
    db = Database(tmp_path / "t.db")
    shown = []
    client = TwitchChatClient("testchan", url=url, proxy=None, backoff_initial=0.01)
    monitor = ChannelMonitor(db, client, Pseudonymizer(b"s"), on_message=shown.append)
    sid = monitor.start("high_traffic", StreamContext())
    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        if monitor.recorded >= 3:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = db.conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
    assert [(r["id"], r["seq"]) for r in rows] == [("m1", 1), ("m2", 2), ("m3", 3)]
    assert rows[0]["author_pseudo_id"] == rows[2]["author_pseudo_id"] != rows[1]["author_pseudo_id"]
    assert json.loads(rows[1]["rule_flags"]) == ["url"]
    assert rows[1]["reply_parent_id"] == "m1"
    assert monitor.duplicates == 1
    # 表示名とユーザー ID はデータベースに残らない
    dump = "\n".join(db.conn.iterdump())
    assert "User1" not in dump and "user1" not in dump
    # 表示用には表示名が渡る
    assert shown[0].chat.display_name == "User1"

    session = db.get_session(sid)
    assert session["end_reason"] == "manual"
    assert session["pipeline_status"] == "ended"
    db.close()
