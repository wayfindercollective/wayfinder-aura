#!/usr/bin/env python3
"""Maintainer tool: extract only graph/layout, never weights, for local conversion.

The app fills this architecture with the user's downloaded GGML tensors. The
checkpoint here verifies every binding at build time; it is NOT downloaded or
used by the end-user conversion path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from wayfinder.utils.macos_coreml import TURBO_CHECKPOINT_SHA256
from wayfinder.utils.macos_coreml_convert import TEMPLATE_VERSION, TURBO_DIMS


def encoder_tensor_names() -> list[str]:
    names = ["conv1.weight", "conv1.bias", "conv2.weight", "conv2.bias", "positional_embedding"]
    for i in range(32):
        names += [f"blocks.{i}.{suffix}" for suffix in (
            "attn_ln.weight", "attn_ln.bias", "attn.query.weight", "attn.query.bias", "attn.key.weight",
            "attn.value.weight", "attn.value.bias", "attn.out.weight", "attn.out.bias", "mlp_ln.weight",
            "mlp_ln.bias", "mlp.0.weight", "mlp.0.bias", "mlp.2.weight", "mlp.2.bias",
        )]
    return ["encoder." + name for name in names + ["ln_post.weight", "ln_post.bias"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True, help="verified developer Core ML export")
    parser.add_argument("--destination", type=Path, default=ROOT / "assets/coreml/turbo")
    args = parser.parse_args()
    if args.destination.exists():
        parser.error("Refusing to overwrite an existing template")
    checksum = hashlib.sha256()
    with args.checkpoint.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            checksum.update(chunk)
    if checksum.hexdigest() != TURBO_CHECKPOINT_SHA256:
        parser.error("Expected verified official Turbo checkpoint")
    import numpy as np
    import torch
    from coremltools.models.utils import load_spec
    from coremltools.libmilstoragepython import _BlobStorageReader

    resource = args.package / "Data/com.apple.CoreML"
    graph = resource / "model.mlmodel"
    spec = load_spec(str(graph))
    ops = spec.mlProgram.functions["main"].block_specializations["CoreML6"].operations
    blobs = [op.attributes["val"] for op in ops if op.type == "const"
             and op.attributes["val"].WhichOneof("value") == "blobFileValue"]
    names = encoder_tensor_names()
    if len(blobs) != len(names):
        raise ValueError("All encoder tensors must be externalized")
    weights = torch.load(str(args.checkpoint), map_location="cpu", weights_only=True, mmap=True)["model_state_dict"]
    if set(names) != {name for name in weights if name.startswith("encoder.")}:
        raise ValueError("Incomplete encoder binding set")
    reader = _BlobStorageReader(str(resource / "weights/weight.bin"))
    bindings = []
    for name, value in zip(names, blobs):
        if value.type.tensorType.dataType != 10:
            raise ValueError("Template expects FP16 external weights")
        shape = [dim.constant.size for dim in value.type.tensorType.dimensions]
        actual = np.frombuffer(reader.read_fp16_data(value.blobFileValue.offset).tobytes(), dtype=np.float16).reshape(shape)
        source = weights[name].float().numpy()
        transpose = name.endswith("positional_embedding")
        scale = .125 if ".attn.query." in name else 1.0
        expected = ((source.T if transpose else source) * scale).reshape(shape).astype(np.float16)
        if not np.array_equal(actual, expected):
            raise ValueError(f"Unexpected graph weight transform: {name}")
        bindings.append({"source": name, "offset": value.blobFileValue.offset, "shape": shape,
                         "transpose": transpose, "scale": scale})
    # Tensor constants remaining in the graph are geometry/scalars, not weights.
    for op in ops:
        if op.type == "const" and op.attributes["val"].WhichOneof("value") == "immediateValue":
            dims = [d.constant.size for d in op.attributes["val"].type.tensorType.dimensions]
            if np.prod(dims) > 64:
                raise ValueError("Large inline tensor: cannot ship a weight-free graph")
    (args.destination / "Data/com.apple.CoreML").mkdir(parents=True)
    shutil.copy2(graph, args.destination / "Data/com.apple.CoreML/model.mlmodel")
    shutil.copy2(args.package / "Manifest.json", args.destination / "Manifest.json")
    (args.destination / "bindings.json").write_text(json.dumps({
        "version": TEMPLATE_VERSION, "dimensions": TURBO_DIMS,
        "graph_sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
        "weights_size": (resource / "weights/weight.bin").stat().st_size,
        "bindings": bindings,
    }, separators=(",", ":")))
    print(f"Weight-free Core ML graph: {len(bindings)} local tensor bindings, {graph.stat().st_size} graph bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
