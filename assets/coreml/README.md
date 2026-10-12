# Weight-free Core ML architecture

The `turbo` directory contains only a Core ML program, package manifest, and
verified mappings from Whisper encoder tensors to external weight offsets.
**No learned weights are included.** The installed Mac app populates every
weight from the user's existing GGML model, including dequantizing Q5_0/Q8_0,
and compiles locally with Core ML. It never downloads a second encoder.

The program uses the ANE encoder layout from whisper.cpp v1.9.1
(`f049fff95a089aa9969deb009cdd4892b3e74916`, MIT), exported with CoreMLTools
8.3.0 (BSD-3-Clause) and ane-transformers 0.1.3 (Apple Sample Code License).
Whisper's architecture/checkpoint are MIT licensed. Runtime blob storage
follows Apple's public MIL Blob v2 format. Third-party notices ship with Aura.

Regeneration is maintainer-only in a separate conversion environment. First
export the verified official Turbo checkpoint with
`packaging/macos/export_coreml_encoder.py --package-output /path/to/encoder.mlpackage`,
then pass that retained source `.mlpackage`
and the checkpoint to `packaging/macos/export_coreml_template.py`. The latter
checks all 487 transforms byte-for-byte before exporting the graph, never the
large `weights/weight.bin`. Updating the graph requires a new template version
and another native accuracy/performance check.
