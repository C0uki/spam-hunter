"""ターミナルから監視を動かす（M1）。

使い方:
    python -m app.cli monitor <チャンネル> --purpose high_traffic
    python -m app.cli monitor <チャンネル> --purpose story_firstplay --welcomes-advice no \
        --spoiler-note "ストーリー初見プレイ、第3章まで"

Ctrl+C で止めると、セッションを閉じてから終了する。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_settings
from .db import PURPOSES, WELCOMES_ADVICE, Database, StreamContext, validate_session_params
from .monitor import ChannelMonitor, RecordedMessage
from .pseudonym import Pseudonymizer, load_or_create_salt
from .rules import RuleConfig, RuleEngine
from .twitch.irc import TwitchChatClient


def _print_message(m: RecordedMessage) -> None:
    local = m.chat.sent_at.astimezone().strftime("%H:%M:%S")
    flags = f"  [{','.join(m.rule_flags)}]" if m.rule_flags else ""
    print(f"{local} #{m.seq:<5} {m.chat.display_name}: {m.chat.text}{flags}", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    mon = sub.add_parser("monitor", help="チャンネルを監視して記録する")
    mon.add_argument("channel", help="チャンネルのログイン名")
    mon.add_argument("--purpose", required=True, choices=PURPOSES, help="配信の枠")
    mon.add_argument("--welcomes-advice", default="unknown", choices=WELCOMES_ADVICE)
    mon.add_argument("--spoiler-note", default=None)
    mon.add_argument("--stream-title", default=None)
    mon.add_argument("--game", default=None)
    mon.add_argument("--db", type=Path, default=None, help="SQLite のパス（既定: data/moderation.db）")
    mon.add_argument("--repeat-window", type=float, default=RuleConfig.repeat_window_sec)
    mon.add_argument("--repeat-count", type=int, default=RuleConfig.repeat_min_count)
    mon.add_argument("--quiet", action="store_true", help="コメントを表示しない")
    mon.add_argument("-v", "--verbose", action="store_true")
    return parser


async def _monitor(args: argparse.Namespace) -> int:
    settings = load_settings()
    context = StreamContext(
        welcomes_advice=args.welcomes_advice,
        game_name=args.game,
        stream_title=args.stream_title,
        spoiler_note=args.spoiler_note,
    )
    try:
        validate_session_params(args.purpose, context)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = Database(args.db or settings.db_path)
    try:
        dangling = db.close_dangling_sessions()
        if dangling:
            logging.warning("closed sessions left open by a previous run: %s", dangling)
        salt = load_or_create_salt(settings.data_dir, settings.pseudonym_salt)
        client = TwitchChatClient(args.channel, on_status=lambda s: logging.info("[irc] %s", s))
        monitor = ChannelMonitor(
            db,
            client,
            Pseudonymizer(salt),
            RuleEngine(RuleConfig(args.repeat_window, args.repeat_count)),
            on_message=None if args.quiet else _print_message,
        )
        session_id = monitor.start(args.purpose, context)
        logging.info("session %d started for #%s (Ctrl+C to stop)", session_id, client.channel)
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass
        logging.info("session %d: %d messages recorded", session_id, monitor.recorded)
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows のコンソールで表示できない文字があっても止まらない
    try:
        return asyncio.run(_monitor(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
