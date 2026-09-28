"""ターミナルから監視・判定を動かす。

使い方:
    python -m app.cli monitor <チャンネル> --purpose high_traffic
    python -m app.cli monitor <チャンネル> --purpose story_firstplay --welcomes-advice no \
        --spoiler-note "ストーリー初見プレイ、第3章まで"
    python -m app.cli pipeline              # 監視が終わったセッションを「判定 → 層別抽出 → まとめ通知」まで進める
    python -m app.cli judge                 # 判定だけを行う

monitor は Ctrl+C で止めると、セッションを閉じてから pipeline に進む（--no-pipeline で進まない）。
.env に TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET があれば、配信状況を自動で取得し、配信が終わったら自動で止まる。
.env に DISCORD_WEBHOOK_URL があれば、ルールの印とセッション後のまとめを通知する。
pipeline と judge は、1回目の Ctrl+C でいまのバッチを終えてから止まり、次回はその続きから進む。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from .config import Settings, load_defaults, load_settings
from .db import PURPOSES, WELCOMES_ADVICE, Database, StreamContext, validate_session_params
from .monitor import ChannelMonitor, RecordedMessage, apply_stream_info
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
    mon.add_argument("--repeat-window", type=float, default=None, help="連投とみなす秒数（既定は defaults.yaml）")
    mon.add_argument("--repeat-count", type=int, default=None, help="連投とみなす回数（既定は defaults.yaml）")
    mon.add_argument("--no-pipeline", action="store_true", help="監視を止めたあと、判定に進まない")
    mon.add_argument("--no-notify", action="store_true", help="Discord に通知しない")
    mon.add_argument("--quiet", action="store_true", help="コメントを表示しない")
    mon.add_argument("-v", "--verbose", action="store_true")

    for name, help_text in (
        ("pipeline", "監視が終わったセッションを、判定 → 層別抽出 → まとめ通知まで進める"),
        ("judge", "監視が終わったセッションを Laya で判定する（抽出と通知はしない）"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--session", type=int, action="append", help="セッション ID（省略時は待っているすべて）")
        p.add_argument("--variant", default=None, help="variant 名（省略時は questions.yaml の primary）")
        p.add_argument("--batch-size", type=int, default=None, help="既定は defaults.yaml")
        p.add_argument("--threads", type=int, default=None, help="CPU のスレッド数（既定は defaults.yaml）")
        p.add_argument("--db", type=Path, default=None)
        p.add_argument("--no-notify", action="store_true", help="Discord に通知しない")
        p.add_argument("-v", "--verbose", action="store_true")
    return parser


# --- monitor --------------------------------------------------------------------


async def _monitor_async(args: argparse.Namespace, settings: Settings, defaults: dict, db: Database, holder: dict) -> None:
    from .notify.discord import NotificationQueue, RuleNotifier
    from .twitch.helix import HelixClient, StreamWatcher

    rules_cfg = defaults.get("rules", {})
    notify_cfg = defaults.get("notify", {})
    helix_cfg = defaults.get("helix", {})
    context = StreamContext(
        welcomes_advice=args.welcomes_advice,
        game_name=args.game,
        stream_title=args.stream_title,
        spoiler_note=args.spoiler_note,
    )

    salt = load_or_create_salt(settings.data_dir, settings.pseudonym_salt)
    client = TwitchChatClient(args.channel, on_status=lambda s: logging.info("[irc] %s", s))

    queue = None
    rule_notifier = None
    if settings.discord_webhook_url and not args.no_notify:
        queue = NotificationQueue(settings.discord_webhook_url)
        if notify_cfg.get("rule_realtime", True):
            rule_notifier = RuleNotifier(
                queue,
                client.channel,
                cooldown=float(notify_cfg.get("rule_author_cooldown_sec", 300)),
                on_notified=db.mark_rule_notified,
            )

    def on_message(m: RecordedMessage) -> None:
        if not args.quiet:
            _print_message(m)
        if rule_notifier and m.rule_flags:
            rule_notifier.handle(m.chat.id, m.author_pseudo_id, m.chat.display_name, m.chat.text, m.rule_flags, m.chat.sent_at)

    monitor = ChannelMonitor(
        db,
        client,
        Pseudonymizer(salt),
        RuleEngine(
            RuleConfig(
                repeat_window_sec=args.repeat_window or float(rules_cfg.get("repeat_window_sec", 60)),
                repeat_min_count=args.repeat_count or int(rules_cfg.get("repeat_min_count", 3)),
                url_enabled=bool(rules_cfg.get("url_enabled", True)),
            )
        ),
        on_message=on_message,
    )
    session_id = monitor.start(args.purpose, context)
    holder["session_id"] = session_id
    logging.info("session %d started for #%s (Ctrl+C to stop)", session_id, client.channel)

    monitor_task = asyncio.create_task(monitor.run())
    aux: list[asyncio.Task] = []
    helix = None
    if queue:
        aux.append(asyncio.create_task(queue.run()))
    if rule_notifier:
        aux.append(asyncio.create_task(rule_notifier.run_flusher()))
    if settings.twitch_client_id and settings.twitch_client_secret:
        helix = HelixClient(settings.twitch_client_id, settings.twitch_client_secret)

        def on_change(info) -> None:
            logging.info("[helix] game=%r title=%r", info.game_name, info.title)
            apply_stream_info(db, session_id, info.game_name, info.title)

        def on_offline() -> None:
            logging.info("[helix] the stream has ended; stopping the session")
            monitor.stop_reason = "offline"
            monitor_task.cancel()

        watcher = StreamWatcher(
            helix,
            client.channel,
            interval=float(helix_cfg.get("poll_interval_sec", 300)),
            offline_checks=int(helix_cfg.get("offline_checks_to_end", 2)),
            on_change=on_change,
            on_offline=on_offline,
        )
        aux.append(asyncio.create_task(watcher.run()))
    else:
        logging.info("TWITCH_CLIENT_ID is not set; stream info is manual and the session ends only with Ctrl+C")

    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    finally:
        logging.info("session %d: %d messages recorded", session_id, monitor.recorded)
        if rule_notifier:
            rule_notifier.flush_all()
        if queue:
            await queue.drain()
        for t in aux:
            t.cancel()
        await asyncio.gather(*aux, return_exceptions=True)
        if queue:
            await queue.aclose()
        if helix:
            await helix.aclose()


def _monitor(args: argparse.Namespace) -> int:
    settings = load_settings()
    defaults = load_defaults()
    try:
        validate_session_params(
            args.purpose,
            StreamContext(welcomes_advice=args.welcomes_advice),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = Database(args.db or settings.db_path)
    holder: dict[str, Any] = {}
    try:
        dangling = db.close_dangling_sessions()
        if dangling:
            logging.warning("closed sessions left open by a previous run: %s", dangling)
        try:
            asyncio.run(_monitor_async(args, settings, defaults, db, holder))
        except KeyboardInterrupt:
            pass
        session_id = holder.get("session_id")
        if session_id is not None and not args.no_pipeline:
            logging.info("moving on to judging (Ctrl+C to pause; `python -m app.cli pipeline` resumes)")
            _run_pipelines(db, [session_id], settings, defaults, args, judge_only=False)
    finally:
        db.close()
    return 0


# --- pipeline / judge --------------------------------------------------------------


def _summary_sender(settings: Settings, args: argparse.Namespace):
    if not settings.discord_webhook_url or getattr(args, "no_notify", False):
        return None
    import httpx

    from .notify.discord import payload_for, post_sync, summary_embed

    def send(summary: dict) -> None:
        with httpx.Client(timeout=15) as http:
            post_sync(http, settings.discord_webhook_url, payload_for([summary_embed(summary)]))

    return send


def _run_pipelines(
    db: Database,
    session_ids: list[int],
    settings: Settings,
    defaults: dict,
    args: argparse.Namespace,
    *,
    judge_only: bool,
) -> None:
    from .judge.backends import load_backend
    from .judge.runner import Progress, judge_session
    from .judge.variants import load_questions
    from .pipeline import run_pipeline

    config = load_questions()
    variant = config.get(getattr(args, "variant", None))
    if variant.name != config.primary:
        logging.warning("variant %r is not the primary (%r)", variant.name, config.primary)
    judge_cfg = dict(defaults.get("judge", {}))
    if getattr(args, "batch_size", None):
        judge_cfg["batch_size"] = args.batch_size
    threads = getattr(args, "threads", None) or int(judge_cfg.get("threads", 2))
    run_defaults = {**defaults, "judge": judge_cfg}

    backend_holder: dict[str, Any] = {}

    def backend_factory():
        if "backend" not in backend_holder:
            logging.info("loading %s backend for variant %r ...", variant.backend, variant.name)
            backend_holder["backend"] = load_backend(variant.backend, threads=threads)
        return backend_holder["backend"]

    def report(p: Progress) -> None:
        eta = f", 残り約{p.eta_seconds / 60:.0f}分" if p.eta_seconds is not None else ""
        per = p.seconds / max(1, p.done + p.errors) * 1000
        print(
            f"session {p.session_id}: {p.done + p.errors}/{p.total} (error {p.errors}, {per:.0f} ms/件{eta})",
            flush=True,
        )

    stop = {"requested": False}

    def on_sigint(signum, frame):
        if stop["requested"]:
            raise KeyboardInterrupt
        stop["requested"] = True
        logging.info("stopping after the current batch (Ctrl+C again to stop now)")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        for session_id in session_ids:
            if stop["requested"]:
                break
            if judge_only:
                p = judge_session(
                    db,
                    session_id,
                    variant,
                    backend_factory(),
                    batch_size=int(judge_cfg.get("batch_size", 1)),
                    should_stop=lambda: stop["requested"],
                    on_progress=report,
                )
                status = "judged" if p.remaining == 0 else f"paused ({p.remaining} left)"
            else:
                status = run_pipeline(
                    db,
                    session_id,
                    variant,
                    backend_factory,
                    run_defaults,
                    send_summary=_summary_sender(settings, args),
                    should_stop=lambda: stop["requested"],
                    on_progress=report,
                )
            logging.info("session %d: %s", session_id, status)
    except KeyboardInterrupt:
        logging.info("stopped; the next run resumes where it left off")
    finally:
        signal.signal(signal.SIGINT, previous)
        if "backend" in backend_holder:
            backend_holder["backend"].close()


def _pipeline(args: argparse.Namespace, *, judge_only: bool) -> int:
    from .pipeline import sessions_for_pipeline

    settings = load_settings()
    defaults = load_defaults()
    db = Database(args.db or settings.db_path)
    try:
        session_ids = args.session or (db.sessions_to_judge() if judge_only else sessions_for_pipeline(db))
        if not session_ids:
            logging.info("no sessions waiting")
            return 0
        _run_pipelines(db, session_ids, settings, defaults, args, judge_only=judge_only)
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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # リクエストごとのログ（URL を含む）を出さない
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows のコンソールで表示できない文字があっても止まらない
    if args.command == "monitor":
        return _monitor(args)
    return _pipeline(args, judge_only=args.command == "judge")


if __name__ == "__main__":
    sys.exit(main())
