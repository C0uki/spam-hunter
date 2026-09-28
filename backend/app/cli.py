"""ターミナルから監視を動かす（M1）。

使い方:
    python -m app.cli monitor <チャンネル> --purpose high_traffic
    python -m app.cli monitor <チャンネル> --purpose story_firstplay --welcomes-advice no \
        --spoiler-note "ストーリー初見プレイ、第3章まで"
    python -m app.cli judge                 # 監視が終わったセッションを、本番の variant で判定する

monitor は Ctrl+C で止めると、セッションを閉じてから終了する。
judge は1回目の Ctrl+C でいまのバッチを終えてから止まり、次回はその続きから判定する。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
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

    jud = sub.add_parser("judge", help="監視が終わったセッションを Laya で判定する")
    jud.add_argument("--session", type=int, action="append", help="判定するセッション ID（省略時は判定待ちのすべて）")
    jud.add_argument("--variant", default=None, help="variant 名（省略時は questions.yaml の primary）")
    jud.add_argument("--batch-size", type=int, default=8)
    jud.add_argument("--threads", type=int, default=2, help="CPU のスレッド数（Surface は2）")
    jud.add_argument("--db", type=Path, default=None)
    jud.add_argument("-v", "--verbose", action="store_true")
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


def _judge(args: argparse.Namespace) -> int:
    from .judge.backends import load_backend
    from .judge.runner import Progress, judge_session
    from .judge.variants import load_questions

    settings = load_settings()
    config = load_questions()
    variant = config.get(args.variant)
    if args.variant and args.variant != config.primary:
        logging.warning("variant %r is not the primary (%r)", args.variant, config.primary)

    db = Database(args.db or settings.db_path)
    stop = {"requested": False}

    def on_sigint(signum, frame):
        if stop["requested"]:
            raise KeyboardInterrupt
        stop["requested"] = True
        logging.info("stopping after the current batch (Ctrl+C again to stop now)")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        session_ids = args.session or db.sessions_to_judge()
        if not session_ids:
            logging.info("no sessions waiting for judging")
            return 0
        logging.info("loading %s backend for variant %r ...", variant.backend, variant.name)
        backend = load_backend(variant.backend, threads=args.threads)

        def report(p: Progress) -> None:
            eta = f", 残り約{p.eta_seconds / 60:.0f}分" if p.eta_seconds is not None else ""
            per = p.seconds / max(1, p.done + p.errors) * 1000
            print(
                f"session {p.session_id}: {p.done + p.errors}/{p.total}"
                f" (error {p.errors}, {per:.0f} ms/件{eta})",
                flush=True,
            )

        for session_id in session_ids:
            if stop["requested"]:
                break
            p = judge_session(
                db,
                session_id,
                variant,
                backend,
                batch_size=args.batch_size,
                should_stop=lambda: stop["requested"],
                on_progress=report,
            )
            state = "done" if p.remaining == 0 else f"paused ({p.remaining} left)"
            logging.info("session %d: %s", session_id, state)
        backend.close()
    except KeyboardInterrupt:
        logging.info("stopped; the next run resumes from the pending messages")
    finally:
        signal.signal(signal.SIGINT, previous)
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
    if args.command == "judge":
        return _judge(args)
    try:
        return asyncio.run(_monitor(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
