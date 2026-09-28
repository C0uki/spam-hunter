"""Laya の判定器（PyTorch 版と ONNX 版）。

どちらも predict(states, questions) -> 各 state の answers（Laya の出力の "answers"）を返す。
torch や onnxruntime は重いので、使うときに初めて import する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

MODEL_ID = "convaiinnovations/laya"
SUBFOLDER = "multilingual"
MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
ONNX_FILES = {
    "onnx": "laya-multilingual.onnx",
    "onnx-int8": "laya-multilingual.int8.onnx",
}


class Backend(Protocol):
    kind: str
    model_ver: str

    def predict(
        self, states: list[dict[str, Any]], questions: dict[str, dict[str, Any]]
    ) -> list[dict[str, dict[str, Any]]]: ...

    def close(self) -> None: ...


def _laya_version() -> str:
    import laya

    return laya.__version__


class LayaTorchBackend:
    kind = "torch"

    def __init__(self, threads: int = 2) -> None:
        import laya
        import torch

        torch.set_num_threads(threads)
        self._agent = laya.load(MODEL_ID, subfolder=SUBFOLDER, device="cpu")
        self.model_ver = f"laya-{_laya_version()}/{SUBFOLDER}/torch"

    def predict(self, states, questions):
        # バッチの区切りは呼び出し側（runner）が決めるので、渡された分を1回で判定する
        results = self._agent.predict_batch(states, questions)
        return [r["answers"] for r in results]

    def close(self) -> None:
        self._agent = None


class LayaOnnxBackend:
    def __init__(
        self,
        kind: str = "onnx-int8",
        threads: int = 2,
        models_dir: Path = MODELS_DIR,
    ) -> None:
        import onnxruntime as ort
        from laya.onnx_agent import ONNXAgent

        path = models_dir / ONNX_FILES[kind]
        if not path.exists():
            raise FileNotFoundError(
                f"{path} がありません。先に `python scripts/export_onnx.py` で書き出してください"
            )
        self.kind = kind
        self._agent = ONNXAgent(MODEL_ID, onnx_path=str(path), subfolder=SUBFOLDER)
        # ONNXAgent はスレッド数を指定できないので、セッションを作り直す（Surface の2コアに合わせるため）
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self._agent.session = ort.InferenceSession(
            str(path), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.model_ver = f"laya-{_laya_version()}/{SUBFOLDER}/{kind}"

    def predict(self, states, questions):
        # バッチの区切りは呼び出し側（runner）が決めるので、渡された分を1回で判定する
        results = self._agent.predict_batch(states, questions)
        return [r["answers"] for r in results]

    def close(self) -> None:
        self._agent = None


def load_backend(kind: str, threads: int = 2, **kwargs: Any) -> Backend:
    if kind == "torch":
        return LayaTorchBackend(threads=threads, **kwargs)
    if kind in ONNX_FILES:
        return LayaOnnxBackend(kind=kind, threads=threads, **kwargs)
    raise ValueError(f"unknown backend {kind!r}")
