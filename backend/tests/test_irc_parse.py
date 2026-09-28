from datetime import UTC, datetime

import pytest

from app.twitch.irc import backoff_delay, parse_line, to_chat_message

PRIVMSG = (
    "@badge-info=;badges=;color=#FF0000;display-name=テスト太郎;emotes=;first-msg=0;flags=;"
    "id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;reply-parent-msg-id=parent-1;room-id=1337;"
    "subscriber=0;tmi-sent-ts=1700000000123;turbo=0;user-id=12345;user-type= "
    ":tester!tester@tester.tmi.twitch.tv PRIVMSG #somechannel :こんにちは :) 元気？"
)


def test_parse_privmsg_with_tags():
    msg = parse_line(PRIVMSG)
    assert msg.command == "PRIVMSG"
    assert msg.nick == "tester"
    assert msg.params == ["#somechannel", "こんにちは :) 元気？"]
    assert msg.tags["display-name"] == "テスト太郎"
    assert msg.tags["user-type"] == ""


def test_to_chat_message():
    chat = to_chat_message(parse_line(PRIVMSG))
    assert chat is not None
    assert chat.id == "b34ccfc7-4977-403a-8a94-33c6bac34fb8"
    assert chat.channel == "somechannel"
    assert chat.user_id == "12345"
    assert chat.login == "tester"
    assert chat.display_name == "テスト太郎"
    assert chat.text == "こんにちは :) 元気？"
    assert chat.sent_at == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)
    assert chat.reply_parent_id == "parent-1"
    assert chat.is_action is False


def test_tag_value_unescape():
    msg = parse_line(r"@system-msg=a\sb\:c\\d\ne;x= :tmi.twitch.tv USERNOTICE #c")
    assert msg.tags["system-msg"] == "a b;c\\d\ne"
    assert msg.tags["x"] == ""


def test_action_message():
    line = "@id=1;user-id=2;display-name= :u!u@u PRIVMSG #c :\x01ACTION waves\x01"
    chat = to_chat_message(parse_line(line))
    assert chat.text == "waves"
    assert chat.is_action is True
    assert chat.display_name == "u"  # 表示名が空ならログイン名


def test_privmsg_without_required_tags_is_skipped():
    assert to_chat_message(parse_line(":u!u@u PRIVMSG #c :hi")) is None


@pytest.mark.parametrize(
    "line,command,params",
    [
        ("PING :tmi.twitch.tv", "PING", ["tmi.twitch.tv"]),
        (":tmi.twitch.tv RECONNECT", "RECONNECT", []),
        (":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!", "001", ["justinfan1", "Welcome, GLHF!"]),
        (":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands", "CAP", ["*", "ACK", "twitch.tv/tags twitch.tv/commands"]),
    ],
)
def test_parse_other_commands(line, command, params):
    msg = parse_line(line)
    assert msg.command == command
    assert msg.params == params


def test_parse_empty_line_raises():
    with pytest.raises(ValueError):
        parse_line("")


def test_backoff_delay():
    delays = [backoff_delay(i, initial=1, maximum=60) for i in range(8)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
