"""層別抽出（docs/design.md §4.6）。

判定が終わったセッションのメッセージから、採点の候補を層ごとに単純無作為抽出して、採点キューに入れる。
層の定義は sample_draws.definition に保存し、選ばれた確率 π を後から計算できるようにする。

π（あるメッセージが採点キューに入る確率）は、そのメッセージが「母集団に含まれる」層すべてについて
π = 1 − Π(1 − n_s / N_s) とする（層ごとの抽出を独立とみなした近似）。
選ばれなかった層も含めて掛けないと、π を小さく見積もってしまう点に注意。
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..db import Database


@dataclass(frozen=True)
class Stratum:
    name: str
    definition: dict[str, Any]


def build_strata(question_types: dict[str, str], config: dict[str, Any]) -> list[Stratum]:
    """defaults.yaml の sampling から層の一覧を作る。"""
    strata = [Stratum("random", {"type": "random", "n": int(config.get("random", 0))})]
    for qid, qtype in question_types.items():
        if qtype == "noul":
            for band in config.get("noul_bands", []):
                strata.append(
                    Stratum(
                        f"{qid}:{band['name']}",
                        {
                            "type": "noul_band",
                            "question": qid,
                            "min": float(band["min"]),
                            "max": float(band["max"]),
                            "n": int(band["n"]),
                        },
                    )
                )
        else:
            for value, n in (config.get("choice", {}).get(qid) or {}).items():
                strata.append(
                    Stratum(f"{qid}:{value}", {"type": "choice", "question": qid, "value": value, "n": int(n)})
                )
    return strata


def in_population(definition: dict[str, Any], values: dict[str, str]) -> bool:
    """values: そのメッセージの {question_id: judgments.value}。"""
    kind = definition["type"]
    if kind == "random":
        return True
    value = values.get(definition["question"])
    if value is None:
        return False
    if kind == "noul_band":
        p = float(value)
        return definition["min"] <= p < definition["max"]
    if kind == "choice":
        return value == definition["value"]
    raise ValueError(f"unknown stratum type {kind!r}")


def _values_by_message(rows) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = defaultdict(dict)
    for r in rows:
        values[r["message_id"]][r["question_id"]] = r["value"]
    return values


@dataclass
class SampleResult:
    queued: int
    strata: dict[str, tuple[int, int]]  # 層の名前 -> (N, n)
    skipped: bool = False  # すでに抽出済みだった


def draw_session_samples(
    db: Database,
    session_id: int,
    variant: str,
    question_types: dict[str, str],
    config: dict[str, Any],
    *,
    rng: random.Random | None = None,
) -> SampleResult:
    """セッションの採点候補を抽出する。同じセッション・variant で二度目に呼んでも何もしない。"""
    if db.has_draws(session_id, variant):
        return SampleResult(queued=0, strata={}, skipped=True)
    rng = rng or random.SystemRandom()
    values = _values_by_message(db.session_judgments(session_id, variant))
    message_ids = sorted(values)
    draws = []
    summary: dict[str, tuple[int, int]] = {}
    queued: set[str] = set()
    for stratum in build_strata(question_types, config):
        population = [mid for mid in message_ids if in_population(stratum.definition, values[mid])]
        n = min(stratum.definition["n"], len(population))
        chosen = rng.sample(population, n) if n else []
        draws.append(
            {
                "stratum": stratum.name,
                "definition": stratum.definition,
                "population_size": len(population),
                "message_ids": chosen,
            }
        )
        summary[stratum.name] = (len(population), n)
        queued.update(chosen)
    db.save_draws(session_id, variant, draws)
    return SampleResult(queued=len(queued), strata=summary)


def inclusion_probabilities(db: Database, session_id: int, variant: str) -> dict[str, float]:
    """採点キューに入ったメッセージそれぞれの π。母集団に含まれる層すべてから計算する。"""
    draws = db.conn.execute(
        "SELECT id, population_size, draw_size, definition FROM sample_draws WHERE session_id = ? AND variant = ?",
        (session_id, variant),
    ).fetchall()
    if not draws:
        return {}
    queued = [
        r[0]
        for r in db.conn.execute(
            "SELECT DISTINCT q.message_id FROM label_queue q JOIN sample_draws d ON d.id = q.draw_id"
            " WHERE d.session_id = ? AND d.variant = ?",
            (session_id, variant),
        )
    ]
    values = _values_by_message(db.session_judgments(session_id, variant))
    result = {}
    for mid in queued:
        miss = 1.0
        for d in draws:
            if d["population_size"] and in_population(json.loads(d["definition"]), values.get(mid, {})):
                miss *= 1.0 - d["draw_size"] / d["population_size"]
        result[mid] = 1.0 - miss
    return result
