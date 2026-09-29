"""M2 の性能測定: バッチサイズ × バックエンドごとに、7項目の判定にかかる時間を測る。

測定用のコメントは自作のサンプル（実際の配信のコメントは使わない。docs/design.md §8）。
PyTorch 版以外のバックエンドについては、PyTorch 版との判定の差（P(true) の差、判定が変わった件数）も出す。

使い方（backend/ で）:
    python scripts/bench.py                                   # torch と onnx-int8、バッチ 1/4/8、2スレッド
    python scripts/bench.py --backends torch onnx onnx-int8 --batch-sizes 1 4 8 16 --rounds 3
    python scripts/bench.py --json bench-result.json
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.judge.backends import load_backend  # noqa: E402
from app.judge.runner import build_state  # noqa: E402
from app.judge.variants import load_questions  # noqa: E402

# 自作のサンプル。普通のコメントが多め、荒らし系が少し、という配信チャットに近い比率にしている
SAMPLE_COMMENTS = [
    "こんばんは！", "初見です", "88888888", "wwwwww", "今日も配信ありがとう",
    "そのボス強いよね", "かわいい", "え、今のすごくない？", "おつかれさまです", "草",
    "BGMいいね", "何時までやる予定ですか？", "声きれい", "ナイス！", "うますぎる",
    "そこ右に隠し部屋あるよ", "早く次行けよ", "左の宝箱取って", "ラスボスの正体は主人公の父親",
    "この後あのキャラ死ぬよ", "下手くそすぎて見てられない", "お前の配信つまらん", "消えろ",
    "@someone お前に言ってない黙れ", "フォロワー1000人を500円で！ bit.ly/xxxxx",
    "私のチャンネルも見てね", "無料でギフト配布中 example.com", "エロい", "パンツ見せて",
    "今日は雨だった", "晩ごはん何食べた？", "次のゲームなにやるの", "ここのステージ好き",
    "がんばれー", "それな", "うわああああ", "神回", "音量ちょっと小さいかも",
    "こいつ頭おかしい", "配信者の顔ひどいな",
]


def make_states(n: int, context: dict | None = None) -> list[dict]:
    ctx = context or {
        "game_name": "サンプルRPG",
        "stream_title": "【初見】サンプルRPG #3",
        "welcomes_advice": "no",
        "spoiler_note": "ストーリー初見プレイ、第3章まで",
    }
    comments = (SAMPLE_COMMENTS * (n // len(SAMPLE_COMMENTS) + 1))[:n]
    return [build_state(c, ctx) for c in comments]


def rss_mb() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / 1e6


def run_backend(kind, questions, states, batch_sizes, rounds, threads, *, need_answers=True):
    gc.collect()
    t0 = time.perf_counter()
    backend = load_backend(kind, threads=threads)
    load_s = time.perf_counter() - t0
    print(f"  loaded in {load_s:.1f}s; warming up ...", flush=True)
    backend.predict(states[:2], questions)  # ウォームアップ

    timings = {}
    for bs in batch_sizes:
        per_round = []
        for r in range(rounds):
            t = time.perf_counter()
            done = 0
            for i in range(0, len(states), bs):
                backend.predict(states[i : i + bs], questions)
                done += len(states[i : i + bs])
                elapsed = time.perf_counter() - t
                # 長い測定でも動いているのが分かるよう、途中経過を同じ行に書き直して表示する
                print(
                    f"\r  batch {bs}, round {r + 1}/{rounds}: {done}/{len(states)} 件"
                    f"（{elapsed / done * 1000:.0f} ms/件）",
                    end="",
                    flush=True,
                )
            per_round.append((time.perf_counter() - t) / len(states))
            print(flush=True)
        timings[bs] = statistics.median(per_round)
    # PyTorch 版との判定の差を比べるときだけ、比較用の判定をする
    answers = backend.predict(states[: len(SAMPLE_COMMENTS)], questions) if need_answers else None
    mem = rss_mb()
    backend.close()
    del backend
    gc.collect()
    return {"load_s": load_s, "sec_per_msg": timings, "answers": answers, "rss_mb": mem}


def compare(base_answers, answers, qtypes):
    diffs = {}
    for qid, qtype in qtypes.items():
        if qtype == "noul":
            deltas = [abs(a[qid]["noul"] - b[qid]["noul"]) for a, b in zip(base_answers, answers)]
            flips = sum((a[qid]["noul"] >= 0.5) != (b[qid]["noul"] >= 0.5) for a, b in zip(base_answers, answers))
            diffs[qid] = {"max_abs_diff": max(deltas), "mean_abs_diff": statistics.mean(deltas), "flips": flips}
        else:
            flips = sum(a[qid]["choice"] != b[qid]["choice"] for a, b in zip(base_answers, answers))
            diffs[qid] = {"flips": flips}
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backends", nargs="+", default=["torch", "onnx-int8"])
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--messages", type=int, default=40, help="1回の測定で判定する件数")
    parser.add_argument("--rounds", type=int, default=3, help="測定の回数（中央値を使う）")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--variant", default=None, help="質問を取る variant（省略時は primary）")
    parser.add_argument("--json", type=Path, default=None, help="結果を JSON で保存する")
    args = parser.parse_args()

    variant = load_questions().get(args.variant)
    questions = variant.questions
    qtypes = variant.question_types()
    states = make_states(args.messages)

    print(f"# {platform.platform()} / {platform.processor() or platform.machine()} / threads={args.threads}")
    print(f"# questions={len(questions)} ({variant.name}), messages={args.messages}, rounds={args.rounds}\n")

    results = {}
    do_compare = "torch" in args.backends and len(args.backends) > 1
    for kind in args.backends:
        print(f"loading {kind} ...", flush=True)
        results[kind] = run_backend(
            kind, questions, states, args.batch_sizes, args.rounds, args.threads, need_answers=do_compare
        )

    print("\n## 1件あたりの判定時間（7項目）")
    header = "| backend | 読み込み(秒) | メモリ(MB)※ | " + " | ".join(f"batch {b}" for b in args.batch_sizes) + " |"
    print(header)
    print("|" + "---|" * (3 + len(args.batch_sizes)))
    for kind, r in results.items():
        cells = []
        for b in args.batch_sizes:
            s = r["sec_per_msg"][b]
            cells.append(f"{s * 1000:.0f} ms（{1 / s:.1f} 件/秒）")
        mem = f"{r['rss_mb']:.0f}" if r["rss_mb"] else "-"
        print(f"| {kind} | {r['load_s']:.1f} | {mem} | " + " | ".join(cells) + " |")

    print("\n※ メモリはプロセス全体の使用量。複数のバックエンドを続けて測ると、前のものの分が残ることがある")

    print("\n## 1万件の判定にかかる時間の見込み（いちばん速いバッチサイズ）")
    for kind, r in results.items():
        best_b = min(r["sec_per_msg"], key=r["sec_per_msg"].get)
        print(f"- {kind}: 約{r['sec_per_msg'][best_b] * 10000 / 3600:.1f} 時間（batch {best_b}）")

    report = {"results": {}, "agreement_vs_torch": {}}
    if do_compare:
        n = len(results["torch"]["answers"])
        print(f"\n## PyTorch 版との判定の差（サンプル{n}件）")
        for kind, r in results.items():
            if kind == "torch":
                continue
            diff = compare(results["torch"]["answers"], r["answers"], qtypes)
            report["agreement_vs_torch"][kind] = diff
            print(f"### {kind}")
            for qid, d in diff.items():
                if "max_abs_diff" in d:
                    print(f"- {qid}: P(true) の差 最大 {d['max_abs_diff']:.3f} / 平均 {d['mean_abs_diff']:.3f}、0.5 を境に判定が変わった件数 {d['flips']}")
                else:
                    print(f"- {qid}: 選ばれた段階が変わった件数 {d['flips']}")

    if args.json:
        for kind, r in results.items():
            report["results"][kind] = {
                "load_s": r["load_s"],
                "rss_mb": r["rss_mb"],
                "ms_per_msg": {str(b): s * 1000 for b, s in r["sec_per_msg"].items()},
            }
        report["platform"] = platform.platform()
        report["threads"] = args.threads
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
