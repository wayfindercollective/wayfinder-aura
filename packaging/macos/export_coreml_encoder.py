#!/usr/bin/env python3
"""Prepare an experimental Turbo Core ML sidecar, preserving the full audio window.

Run in a separate conversion environment, not Aura's UI/runtime environment.
Uses the encoder implementation from the pinned whisper.cpp checkout.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from wayfinder.utils.macos_coreml import COREML_MANIFEST, TURBO_CHECKPOINT_SHA256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True, help="official large-v3-turbo.pt")
    parser.add_argument("--destination", type=Path, required=True, help="directory containing the Turbo ggml model")
    parser.add_argument("--package-output", type=Path, help="retain source .mlpackage for weight-free graph regeneration")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Core ML conversion requires macOS")
    destination = args.destination.expanduser().resolve()
    target = destination / "ggml-large-v3-turbo-encoder.mlmodelc"
    if target.exists():
        parser.error(f"Refusing to overwrite {target}")
    package_output = args.package_output.expanduser().resolve() if args.package_output else None
    if package_output and (package_output.suffix != ".mlpackage" or package_output.exists()):
        parser.error("--package-output must name a new .mlpackage directory")
    digest = hashlib.sha256()
    with args.checkpoint.expanduser().open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != TURBO_CHECKPOINT_SHA256:
        parser.error("Checkpoint is not the verified official Turbo weights")
    from build import WHISPER_COMMIT, WHISPER_SOURCE_DIR
    commit = subprocess.check_output(["git", "-C", str(WHISPER_SOURCE_DIR), "rev-parse", "HEAD"], text=True).strip()
    if commit != WHISPER_COMMIT:
        parser.error("The encoder implementation must be the pinned whisper.cpp checkout")

    import coremltools as ct
    import numpy as np
    import torch
    from whisper.model import ModelDimensions

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    spec = importlib.util.spec_from_file_location("aura_upstream_coreml", WHISPER_SOURCE_DIR / "models/convert-whisper-to-coreml.py")
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    checkpoint = torch.load(str(args.checkpoint.expanduser()), map_location="cpu", weights_only=True, mmap=True)
    dims = ModelDimensions(**checkpoint["dims"])
    encoder = converter.AudioEncoderANE(dims.n_mels, dims.n_audio_ctx, dims.n_audio_state,
                                        dims.n_audio_head, dims.n_audio_layer).eval()
    encoder._register_load_state_dict_pre_hook(converter.linear_to_conv2d_map)
    # The ANE normalization hook divides bias by weight. One Turbo coefficient
    # is 77,824 (> FP16's 65,504 ceiling): doing this on raw half checkpoint
    # tensors creates Inf BEFORE Core ML's overflow guard can protect it.
    # Match the official converter's float32 parameter-loading sequence.
    weights = {name.removeprefix("encoder."): (tensor.float() if "_ln." in name or "ln_post." in name else tensor)
               for name, tensor in checkpoint["model_state_dict"].items() if name.startswith("encoder.")}
    encoder.load_state_dict(weights, strict=True)
    del weights, checkpoint
    gc.collect()
    with torch.no_grad():
        traced = torch.jit.trace(encoder, torch.zeros((1, dims.n_mels, 3000)), check_trace=False)
    model = ct.convert(
        traced, convert_to="mlprogram",
        inputs=[ct.TensorType(name="logmel_data", shape=(1, dims.n_mels, 3000), dtype=np.float32)],
        outputs=[ct.TensorType(name="output", dtype=np.float32)],
        minimum_deployment_target=ct.target.macOS13,
        compute_units=ct.ComputeUnit.ALL, compute_precision=ct.precision.FLOAT16,
        skip_model_load=True,
    )
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aura-coreml-export-", dir=destination) as staging:
        staging = Path(staging)
        package = package_output or staging / "encoder.mlpackage"
        package.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(package))
        subprocess.run(["xcrun", "coremlc", "compile", str(package), str(staging)], check=True)
        compiled = staging / (package.stem + ".mlmodelc")
        (compiled / COREML_MANIFEST).write_text(json.dumps({
            "format": 1, "checkpoint_sha256": TURBO_CHECKPOINT_SHA256,
            "conversion": "fp32-normalization-load-fp16-compute",
            "whisper_commit": WHISPER_COMMIT, "coremltools": ct.__version__,
        }, indent=2), encoding="utf-8")
        compiled.rename(target)
    print(f"Experimental Core ML encoder ready: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
