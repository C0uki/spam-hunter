"""多言語用の Laya を ONNX に書き出す（INT8 量子化版も作る）。

Laya 本体の scripts/export_onnx.py（v0.3.21）と同じ手順だが、多言語用モデルの subfolder を指定できるようにしたもの。

使い方（backend/ で）:
    python scripts/export_onnx.py                     # models/laya-multilingual.onnx と .int8.onnx を作る
    python scripts/export_onnx.py --output path/to/laya.onnx --no-quantize
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "models" / "laya-multilingual.onnx"


def int8_output_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".int8" + output_path.suffix)


def export(model_id: str, subfolder: str | None, output_path: Path) -> None:
    import torch
    from laya.agent import Agent

    agent = Agent(model_id, subfolder=subfolder, compile=False, device="cpu")
    inputs = (
        torch.randint(0, 100, (1, 16), dtype=torch.long),  # input_ids
        torch.ones((1, 16), dtype=torch.long),  # attention_mask
        torch.tensor([[1, 5]], dtype=torch.long),  # marker_pos
        torch.tensor([[True, True]], dtype=torch.bool),  # marker_mask
        torch.tensor([0], dtype=torch.long),  # qtype
    )
    dynamic_axes = {
        "input_ids": {0: "batch_size", 1: "seq_len"},
        "attention_mask": {0: "batch_size", 1: "seq_len"},
        "marker_pos": {0: "batch_size", 1: "num_markers"},
        "marker_mask": {0: "batch_size", 1: "num_markers"},
        "qtype": {0: "batch_size"},
        "logits": {0: "batch_size", 1: "num_markers"},
        "act_logits": {0: "batch_size"},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        agent.model,
        inputs,
        str(output_path),
        export_params=True,
        opset_version=18,
        do_constant_folding=True,
        input_names=["input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"],
        output_names=["logits", "act_logits"],
        dynamic_axes=dynamic_axes,
        dynamo=False,
    )


def quantize(model_path: Path, output_path: Path) -> None:
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    model = onnx.load(str(model_path))
    # torch の書き出しが残す中間の形状情報は、量子化時の形状推論と食い違うことがある（Laya 本体と同じ対処）
    del model.graph.value_info[:]
    quantize_dynamic(
        model_input=model,
        model_output=str(output_path),
        op_types_to_quantize=["MatMul"],
        per_channel=True,
        weight_type=QuantType.QInt8,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="convaiinnovations/laya")
    parser.add_argument("--subfolder", default="multilingual")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    export(args.model, args.subfolder or None, args.output)
    print(f"wrote {args.output} ({os.path.getsize(args.output) / 1e6:.0f} MB)")
    if not args.no_quantize:
        out = int8_output_path(args.output)
        quantize(args.output, out)
        print(f"wrote {out} ({os.path.getsize(out) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
