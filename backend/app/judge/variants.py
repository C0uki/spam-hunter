"""config/questions.yaml の読み込み（variant の継承と上書きを解決する）。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "questions.yaml"
BACKENDS = ("torch", "onnx", "onnx-int8")
QUESTION_TYPES = ("noul", "choice")


@dataclass(frozen=True)
class Variant:
    name: str
    backend: str
    questions: dict[str, dict[str, Any]]

    def question_types(self) -> dict[str, str]:
        return {qid: q["type"] for qid, q in self.questions.items()}


@dataclass(frozen=True)
class QuestionConfig:
    primary: str
    variants: dict[str, Variant]

    @property
    def primary_variant(self) -> Variant:
        return self.variants[self.primary]

    def get(self, name: str | None) -> Variant:
        name = name or self.primary
        if name not in self.variants:
            raise KeyError(f"unknown variant {name!r}; known: {sorted(self.variants)}")
        return self.variants[name]


def _resolve(name: str, raw: dict[str, dict], seen: tuple[str, ...] = ()) -> dict[str, Any]:
    if name in seen:
        raise ValueError(f"variant inheritance loop: {' -> '.join(seen + (name,))}")
    if name not in raw:
        raise ValueError(f"unknown variant {name!r}")
    spec = raw[name] or {}
    if "extends" in spec:
        base = _resolve(spec["extends"], raw, seen + (name,))
    else:
        base = {"backend": "torch", "questions": {}}
    result = copy.deepcopy(base)
    if "backend" in spec:
        result["backend"] = spec["backend"]
    for qid, q in (spec.get("questions") or {}).items():
        result["questions"][qid] = copy.deepcopy(q)
    for qid, patch in (spec.get("overrides") or {}).items():
        if qid not in result["questions"]:
            raise ValueError(f"variant {name!r} overrides unknown question {qid!r}")
        result["questions"][qid].update(copy.deepcopy(patch))
    if "noul_labels" in spec:
        for q in result["questions"].values():
            if q.get("type") == "noul":
                q["labels"] = dict(spec["noul_labels"])
    return result


def _validate(name: str, resolved: dict[str, Any]) -> Variant:
    backend = resolved["backend"]
    if backend not in BACKENDS:
        raise ValueError(f"variant {name!r}: backend must be one of {BACKENDS}")
    questions = resolved["questions"]
    if not questions:
        raise ValueError(f"variant {name!r} has no questions")
    for qid, q in questions.items():
        qtype = q.get("type")
        if qtype not in QUESTION_TYPES:
            raise ValueError(f"variant {name!r}, question {qid!r}: type must be one of {QUESTION_TYPES}")
        if not q.get("instructions"):
            raise ValueError(f"variant {name!r}, question {qid!r}: instructions is required")
        criteria = q.get("criteria") or {}
        if qtype == "noul" and criteria and set(criteria) != {"false", "true"}:
            raise ValueError(f"variant {name!r}, question {qid!r}: noul criteria keys must be false/true")
        if qtype == "choice":
            if len(criteria) < 2:
                raise ValueError(f"variant {name!r}, question {qid!r}: choice needs 2+ options")
            bad = {k for k in criteria if str(k).lower() in {"true", "false", "yes", "no"}}
            if bad:
                # README の Honest limits: 真偽を表す語の選択肢名にモデルが引きずられる
                raise ValueError(f"variant {name!r}, question {qid!r}: avoid boolean-word choice keys {bad}")
    return Variant(name=name, backend=backend, questions=questions)


def load_questions(path: Path | str = DEFAULT_PATH) -> QuestionConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = data.get("variants") or {}
    variants = {name: _validate(name, _resolve(name, raw)) for name in raw}
    primary = data.get("primary")
    if primary not in variants:
        raise ValueError(f"primary variant {primary!r} is not defined")
    return QuestionConfig(primary=primary, variants=variants)
