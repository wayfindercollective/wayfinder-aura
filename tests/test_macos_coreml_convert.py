"""Local GGML conversion never downloads weights or edits the original model."""
import hashlib
import json
import struct

import numpy as np
import pytest

from wayfinder.utils import macos_coreml_convert as converter


def ggml_file(path, tensors):
    header = (0x67676D6C, 1, 2, 32, 1, 1, 2, 32, 1, 1, 1, 8)
    data = struct.pack("<12i", *header) + struct.pack("<2if", 1, 1, 1.0)
    data += struct.pack("<2i", 1, 1) + b"a"  # one vocabulary token
    for name, shape, kind, values in tensors:
        encoded = name.encode()
        data += struct.pack("<3i", len(shape), len(encoded), kind)
        data += struct.pack("<" + "i" * len(shape), *reversed(shape)) + encoded + values
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("kind", [0, 1])
def test_float_tensors_preserve_axis_order(tmp_path, kind):
    values = np.arange(32, dtype="<f4" if kind == 0 else "<f2").reshape(2, 16)
    model = ggml_file(tmp_path / "model.bin", [("encoder.conv1.weight", (2, 16), kind, values.tobytes())])
    reader = converter.GGMLReader(model)
    assert reader.dimensions == (2, 32, 1, 1, 1)
    np.testing.assert_array_equal(reader.tensor("encoder.conv1.weight"), values)


def test_q5_matches_native_block_layout(tmp_path):
    packed = bytes(i | (i << 4) for i in range(16))
    block = struct.pack("<eI", .5, 0xFFFF0000) + packed
    model = ggml_file(tmp_path / "q5.bin", [("encoder.weight", (32,), 6, block)])
    np.testing.assert_array_equal(converter.GGMLReader(model).tensor("encoder.weight"),
                                  (np.arange(32, dtype=np.float32) - 16) * .5)


def test_q8_signed_values(tmp_path):
    values = np.arange(-16, 16, dtype=np.int8)
    model = ggml_file(tmp_path / "q8.bin", [("encoder.weight", (32,), 8,
                                           struct.pack("<e", .25) + values.tobytes())])
    np.testing.assert_array_equal(converter.GGMLReader(model).tensor("encoder.weight"), values.astype(np.float32) * .25)


@pytest.mark.parametrize("mutation", ["truncated", "magic", "kind", "dimensions", "duplicate"])
def test_corrupt_or_unsupported_model_is_rejected(tmp_path, mutation):
    values = np.zeros(32, dtype=np.float16).tobytes()
    tensors = [("encoder.weight", (32,), 1, values)]
    if mutation == "kind":
        tensors[0] = ("encoder.weight", (32,), 99, values)
    if mutation == "dimensions":
        tensors[0] = ("encoder.weight", (-32,), 1, values)
    if mutation == "duplicate":
        tensors *= 2
    model = ggml_file(tmp_path / "bad.bin", tensors)
    if mutation == "truncated":
        model.write_bytes(model.read_bytes()[:-1])
    if mutation == "magic":
        model.write_bytes(b"bad!" + model.read_bytes()[4:])
    with pytest.raises(ValueError):
        converter.GGMLReader(model)


def package_fixture(tmp_path, monkeypatch):
    template = tmp_path / "template"
    (template / "Data/com.apple.CoreML").mkdir(parents=True)
    graph = b"weight-free graph stub"
    (template / "Data/com.apple.CoreML/model.mlmodel").write_bytes(graph)
    (template / "Manifest.json").write_text("{}")
    bindings = {"version": converter.TEMPLATE_VERSION, "dimensions": [2, 32, 1, 1, 1],
                "graph_sha256": hashlib.sha256(graph).hexdigest(), "weights_size": 320,
                "bindings": [
                    {"source": "encoder.matrix", "offset": 64, "shape": [16, 2], "transpose": True, "scale": .125},
                    {"source": "encoder.vector", "offset": 192, "shape": [32]},
                ]}
    (template / "bindings.json").write_text(json.dumps(bindings))
    monkeypatch.setattr(converter, "TURBO_DIMS", (2, 32, 1, 1, 1))
    matrix = np.arange(32, dtype="<f4").reshape(2, 16)
    vector = np.arange(32, dtype="<f2")
    model = ggml_file(tmp_path / "model.bin", [("encoder.matrix", matrix.shape, 0, matrix.tobytes()),
                                               ("encoder.vector", vector.shape, 1, vector.tobytes())])
    return template, model, matrix, vector


def test_populates_only_from_downloaded_model(tmp_path, monkeypatch):
    template, model, matrix, vector = package_fixture(tmp_path, monkeypatch)
    original = model.read_bytes()
    package = tmp_path / "encoder.mlpackage"
    manifest = converter.populate_package(model, package, template=template)
    blob = (package / "Data/com.apple.CoreML/weights/weight.bin").read_bytes()
    assert len(blob) == 320
    assert struct.unpack("<II7Q", blob[:64])[:2] == (2, 2)
    assert struct.unpack("<II7Q", blob[64:128])[:4] == (0xDEADBEEF, 1, 64, 128)
    np.testing.assert_array_equal(np.frombuffer(blob[128:192], dtype="<f2").reshape(16, 2), matrix.T * .125)
    np.testing.assert_array_equal(np.frombuffer(blob[256:], dtype="<f2"), vector)
    assert manifest == {"format": 2, "source": converter.model_identity(model), "template": converter.TEMPLATE_VERSION}
    assert model.read_bytes() == original


def test_cancellation_leaves_original_unchanged(tmp_path, monkeypatch):
    template, model, _, _ = package_fixture(tmp_path, monkeypatch)
    identity = converter.model_identity(model)
    with pytest.raises(InterruptedError):
        converter.populate_package(model, tmp_path / "cancelled.mlpackage", template=template, cancelled=lambda: True)
    assert converter.model_identity(model) == identity


def test_template_integrity_checked(tmp_path, monkeypatch):
    template, model, _, _ = package_fixture(tmp_path, monkeypatch)
    (template / "Data/com.apple.CoreML/model.mlmodel").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        converter.populate_package(model, tmp_path / "bad.mlpackage", template=template)


def test_shipped_graph_contains_no_weight_file():
    template = converter.template_path()
    bindings = converter.load_bindings(template)
    assert len(bindings["bindings"]) == 487
    assert (template / "Data/com.apple.CoreML/model.mlmodel").stat().st_size < 2 * 1024 * 1024
    assert not (template / "Data/com.apple.CoreML/weights/weight.bin").exists()
