"""Populate a weight-free Core ML graph from the user's downloaded GGML model.

No network, Torch, coremltools, or Xcode is required at runtime. NumPy is
already an audio dependency. The small graph/bindings are build-time assets;
every learned encoder tensor comes from the local model, including Q5 weights.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

TEMPLATE_VERSION = "turbo-ggml-v1"
TURBO_DIMS = (1500, 1280, 20, 32, 128)
_TYPES = {0: (1, 4), 1: (1, 2), 6: (32, 22), 8: (32, 34)}


def model_identity(path: Path) -> dict:
    info = path.stat()
    return {"name": path.name, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


class GGMLReader:
    """Bounded parser for whisper.cpp's shipped F32/F16/Q5_0/Q8_0 format."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.identity = model_identity(self.path)
        self.tensors = {}
        with self.path.open("rb") as handle:
            limit = self.identity["size"]

            def read(count):
                if count < 0 or handle.tell() + count > limit:
                    raise ValueError("Truncated GGML model")
                data = handle.read(count)
                if len(data) != count:
                    raise ValueError("Truncated GGML model")
                return data

            def integers(count):
                return struct.unpack("<" + "i" * count, read(count * 4))

            header = integers(12)
            if header[0] != 0x67676D6C:
                raise ValueError("Not a Whisper GGML model")
            self.dimensions = (header[2], header[3], header[4], header[5], header[10])
            rows, columns = integers(2)
            if not (0 < rows <= 256 and 0 < columns <= 4096):
                raise ValueError("Invalid mel filter dimensions")
            read(rows * columns * 4)
            vocabulary, = integers(1)
            if not 0 < vocabulary <= 100000:
                raise ValueError("Invalid GGML vocabulary")
            for _ in range(vocabulary):
                length, = integers(1)
                if not 0 <= length <= 65536:
                    raise ValueError("Invalid GGML token")
                read(length)
            while handle.tell() < limit:
                rank, length, kind = integers(3)
                if not (1 <= rank <= 4 and 0 < length <= 1024) or kind not in _TYPES:
                    raise ValueError("Unsupported GGML tensor format")
                shape = tuple(reversed(integers(rank)))
                if any(d <= 0 for d in shape) or math.prod(shape) > 200000000:
                    raise ValueError("Invalid GGML tensor dimensions")
                name = read(length).decode("utf-8")
                if name in self.tensors:
                    raise ValueError("Duplicate GGML tensor")
                block, size = _TYPES[kind]
                elements = math.prod(shape)
                if elements % block:
                    raise ValueError("Incomplete GGML quantization block")
                byte_count = elements // block * size
                offset = handle.tell()
                if offset + byte_count > limit:
                    raise ValueError("Truncated GGML tensor")
                self.tensors[name] = (shape, kind, offset, byte_count)
                handle.seek(byte_count, 1)
        if model_identity(self.path) != self.identity:
            raise ValueError("Model changed while reading")

    def tensor(self, name: str) -> np.ndarray:
        shape, kind, offset, size = self.tensors[name]
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size)
        if len(data) != size:
            raise ValueError("Model changed during conversion")
        if kind in (0, 1):
            result = np.frombuffer(data, dtype="<f4" if kind == 0 else "<f2").astype(np.float32)
        elif kind == 8:
            blocks = np.frombuffer(data, dtype=np.dtype([("d", "<f2"), ("qs", "i1", 32)]))
            result = (blocks["qs"].astype(np.float32) * blocks["d"].astype(np.float32)[:, None]).reshape(-1)
        else:
            # ggml-quants.c: dequantize_row_q5_0 (32 values per 22-byte block).
            blocks = np.frombuffer(data, dtype=np.dtype([("d", "<f2"), ("qh", "<u4"), ("qs", "u1", 16)]))
            bits = blocks["qh"][:, None]
            positions = np.arange(16, dtype=np.uint32)
            low = (blocks["qs"] & 15) | (((bits >> positions) & 1) << 4)
            high = (blocks["qs"] >> 4) | (((bits >> (positions + 16)) & 1) << 4)
            values = np.concatenate((low, high), axis=1).astype(np.float32) - 16
            result = (values * blocks["d"].astype(np.float32)[:, None]).reshape(-1)
        if not np.isfinite(result).all():
            raise ValueError("Non-finite GGML tensor")
        return result.reshape(shape)


def template_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return root / "assets/coreml/turbo"


def load_bindings(template: Path) -> dict:
    manifest = json.loads((template / "bindings.json").read_text())
    if manifest.get("version") != TEMPLATE_VERSION or tuple(manifest.get("dimensions", ())) != TURBO_DIMS:
        raise ValueError("Unsupported Core ML graph")
    graph = template / "Data/com.apple.CoreML/model.mlmodel"
    if hashlib.sha256(graph.read_bytes()).hexdigest() != manifest["graph_sha256"]:
        raise ValueError("Core ML graph checksum mismatch")
    return manifest


def populate_package(model_path: Path, destination: Path, *, template: Path | None = None,
                     cancelled=lambda: False) -> dict:
    """Generate local weights one tensor at a time (bounded working memory).

    The caller supplies a private staging directory and compiles/publishes the
    package only after completion. Never modifies the original GGML download.
    """
    template = template or template_path()
    manifest = load_bindings(template)
    reader = GGMLReader(model_path)
    if reader.dimensions != TURBO_DIMS:
        raise ValueError("No Core ML graph for this model architecture")
    destination.mkdir()
    shutil.copy2(template / "Manifest.json", destination / "Manifest.json")
    resource = destination / "Data/com.apple.CoreML"
    (resource / "weights").mkdir(parents=True)
    shutil.copy2(template / "Data/com.apple.CoreML/model.mlmodel", resource / "model.mlmodel")
    bindings = manifest["bindings"]
    if {name for name in reader.tensors if name.startswith("encoder.")} != {
            binding["source"] for binding in bindings}:
        raise ValueError("Encoder tensor set does not match graph")
    with (resource / "weights/weight.bin").open("xb") as output:
        # Apple MIL Blob v2: a 64-byte header, followed by aligned 64-byte
        # metadata records and FP16 payloads. No learned weights are bundled.
        output.write(struct.pack("<II7Q", len(bindings), 2, *([0] * 7)))
        for binding in bindings:
            if cancelled():
                raise InterruptedError("Background encoder conversion superseded")
            values = reader.tensor(binding["source"])
            if binding.get("transpose"):
                values = values.T
            values = values * binding.get("scale", 1.0)
            values = values.reshape(binding["shape"])
            payload = np.ascontiguousarray(values, dtype="<f2").tobytes()
            if not np.isfinite(np.frombuffer(payload, dtype="<f2")).all():
                raise ValueError("Converted tensor exceeds FP16 range")
            offset = binding["offset"]
            if offset < output.tell() or offset % 64:
                raise ValueError("Invalid Core ML blob layout")
            output.write(bytes(offset - output.tell()))
            output.write(struct.pack("<II7Q", 0xDEADBEEF, 1, len(payload), offset + 64, *([0] * 5)))
            output.write(payload)
        if output.tell() != manifest["weights_size"]:
            raise ValueError("Incomplete Core ML weight file")
    if cancelled() or model_identity(model_path) != reader.identity:
        raise InterruptedError("Model changed during conversion")
    return {"format": 2, "source": reader.identity, "template": TEMPLATE_VERSION}
