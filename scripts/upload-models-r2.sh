#!/usr/bin/env bash
# Upload pilot (or all) model files to R2 for the models CDN Worker.
# Prerequisites: wrangler logged in, bucket wayfinder-aura-models exists.
# Usage:
#   ./scripts/upload-models-r2.sh              # pilot only
#   ./scripts/upload-models-r2.sh --all-listed # every key in the map below that exists under CACHE_DIR
set -euo pipefail

BUCKET="${R2_BUCKET:-wayfinder-aura-models}"
CACHE_DIR="${MODEL_CACHE_DIR:-$HOME/.cache/wayfinder-aura-model-uploads}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra/models-cdn"

# object_key|source_url
PILOT=(
  "whisper/ggml-large-v3-turbo-q5_0.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q5_0.bin"
  "llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf|https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/ae44f08e1392f39c0e474af10c3ff8355c8b6688/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
)

# Free-tier + remaining Ultra catalog hosts (pilot listed above).
ALL_EXTRA=(
  "whisper/ggml-tiny.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.en.bin"
  "whisper/ggml-base.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.en.bin"
  "whisper/ggml-small.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.en.bin"
  "whisper/ggml-medium.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-medium.en.bin"
  "whisper/ggml-medium.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-medium.bin"
  "whisper/ggml-large-v3-turbo.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo.bin"
  "whisper/ggml-large-v3.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3.bin"
  "whisper/ggml-tiny.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.bin"
  "whisper/ggml-base.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.bin"
  "whisper/ggml-small.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.bin"
  "llm/google_gemma-3-1b-it-Q4_K_M.gguf|https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/116f76234503685a98f572982177b11d44ec8ff1/google_gemma-3-1b-it-Q4_K_M.gguf"
  "llm/qwen2.5-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf"
  "llm/Qwen3.5-2B-Q4_K_M.gguf|https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf"
  "llm/LFM2.5-1.2B-Instruct-Q4_K_M.gguf|https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/resolve/76022b8bfa64af5862d6bce90a676c3cc9b17b52/LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
  "llm/smollm2-360m-instruct-q8_0.gguf|https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/593b5a2e04c8f3e4ee880263f93e0bd2901ad47f/smollm2-360m-instruct-q8_0.gguf"
  "llm/Llama-3.2-1B-Instruct-Q4_K_M.gguf|https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/067b946cf014b7c697f3654f621d577a3e3afd1c/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
  "llm/Phi-3-mini-4k-instruct-q4.gguf|https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/a64113399c2f6b8ad3e11c394733a2ddadaa7f33/Phi-3-mini-4k-instruct-q4.gguf"
)

# Since the 2026-08-17 audit the app pins a sha256 per model and refuses a
# download that does not match. Overwriting a live object therefore breaks that
# model for every installed client, so this script:
#   1. fetches from revision-pinned URLs (never mutable `main`),
#   2. verifies each file against the pin in the app catalog BEFORE uploading,
#   3. refuses to upload a key the catalog does not pin, unless --allow-unpinned.
# Together those mean an upload can only ever write the exact bytes installed
# clients already expect, so re-running it is safe and cannot break anyone.
# To ship DIFFERENT weights, upload under a NEW key and publish it as a new
# catalog entry — see infra/models-cdn/README.md.
ALLOW_UNPINNED=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --allow-unpinned) ALLOW_UNPINNED=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

# Expected sha256 for an R2 object key, read from the shipped app catalog.
expected_sha_for_key() {
  python3 - "$1" <<'PYEOF'
import ast, sys
key = sys.argv[1]
src = open("wayfinder_main.py", encoding="utf-8").read()
for node in ast.parse(src).body:
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id in (
            "WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"
        ):
            for info in ast.literal_eval(node.value).values():
                if info.get("cdn_object") == key:
                    print(info.get("sha256") or "")
                    sys.exit(0)
print("")
PYEOF
}

MODE="${1:-}"
if [[ "$MODE" == "--free-only" ]]; then
  ENTRIES=(
    "whisper/ggml-tiny.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.en.bin"
    "whisper/ggml-base.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.en.bin"
    "whisper/ggml-small.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.en.bin"
    "whisper/ggml-tiny.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.bin"
    "whisper/ggml-base.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.bin"
    "whisper/ggml-small.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.bin"
    "llm/google_gemma-3-1b-it-Q4_K_M.gguf|https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/116f76234503685a98f572982177b11d44ec8ff1/google_gemma-3-1b-it-Q4_K_M.gguf"
    "llm/qwen2.5-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    "llm/Qwen3.5-2B-Q4_K_M.gguf|https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf"
    "llm/LFM2.5-1.2B-Instruct-Q4_K_M.gguf|https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/resolve/76022b8bfa64af5862d6bce90a676c3cc9b17b52/LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
    "llm/smollm2-360m-instruct-q8_0.gguf|https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/593b5a2e04c8f3e4ee880263f93e0bd2901ad47f/smollm2-360m-instruct-q8_0.gguf"
    "llm/Llama-3.2-1B-Instruct-Q4_K_M.gguf|https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/067b946cf014b7c697f3654f621d577a3e3afd1c/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
  )
elif [[ "$MODE" == "--all-listed" ]]; then
  ENTRIES=("${PILOT[@]}" "${ALL_EXTRA[@]}")
else
  ENTRIES=("${PILOT[@]}")
fi

mkdir -p "$CACHE_DIR"
command -v npx >/dev/null || { echo "npx/wrangler required"; exit 1; }

# Prefer Worker multipart (needs ADMIN_UPLOAD_SECRET). Fall back to wrangler for small files.
MODELS_CDN_BASE="${MODELS_CDN_BASE:-https://wayfinder-models-cdn.peter-7b5.workers.dev}"
if [[ -z "${ADMIN_UPLOAD_SECRET:-}" && -f "$ROOT/infra/models-cdn/.secrets/admin_upload_secret" ]]; then
  ADMIN_UPLOAD_SECRET="$(cat "$ROOT/infra/models-cdn/.secrets/admin_upload_secret")"
  export ADMIN_UPLOAD_SECRET
fi

for entry in "${ENTRIES[@]}"; do
  key="${entry%%|*}"
  url="${entry#*|}"
  local_path="$CACHE_DIR/$key"
  mkdir -p "$(dirname "$local_path")"
  if [[ ! -f "$local_path" ]]; then
    echo "↓ Fetch $key"
    curl -fL --retry 3 -o "$local_path" "$url"
  else
    echo "· Cached $key"
  fi
  # Verify against the app's pin before it can reach the bucket. This is the
  # guard that makes an accidental overwrite impossible rather than merely
  # discouraged: bytes the shipped clients would reject never get uploaded.
  # The digest gate — not an existence probe — is what makes an accidental
  # overwrite impossible. Probing the Worker cannot tell "missing" from
  # "exists": gated keys authenticate before the existence check, so a missing
  # Ultra object also answers 401. Instead: bytes that disagree with the
  # shipped pin never reach the bucket, so re-uploading a pinned key can only
  # ever write the identical bytes clients already expect.
  expected_sha="$(cd "$ROOT" && expected_sha_for_key "$key")"
  if [[ -n "$expected_sha" ]]; then
    actual_sha="$(sha256sum "$local_path" | awk '{print $1}')"
    if [[ "$actual_sha" != "$expected_sha" ]]; then
      echo "✗ $key does NOT match the pin in wayfinder_main.py" >&2
      echo "    expected: $expected_sha" >&2
      echo "    actual:   $actual_sha" >&2
      echo "  Uploading this would break the model for every installed client." >&2
      echo "  Ship new weights under a NEW key + catalog entry instead." >&2
      exit 1
    fi
    echo "✓ $key matches the shipped pin"
  elif [[ "$ALLOW_UNPINNED" -eq 1 ]]; then
    echo "⚠ $key has no pin in the app catalog — uploading unverified (--allow-unpinned)"
  else
    echo "✗ $key is not in the app catalog, so there is no pin to verify it against." >&2
    echo "  Add the catalog entry first (filename, sha256, exact size_bytes)," >&2
    echo "  or pass --allow-unpinned if you are deliberately staging a new object." >&2
    exit 1
  fi

  size=$(stat -c%s "$local_path" 2>/dev/null || stat -f%z "$local_path")
  if [[ -n "${ADMIN_UPLOAD_SECRET:-}" ]]; then
    echo "↑ R2 $BUCKET/$key (worker multipart)"
    python3 "$ROOT/scripts/r2_worker_multipart_upload.py" --key "$key" --file "$local_path"
  elif [[ "$size" -gt $((300 * 1024 * 1024)) ]]; then
    echo "↑ R2 $BUCKET/$key (boto3 S3 multipart — needs R2_* keys)"
    python3 "$ROOT/scripts/r2_multipart_upload.py" --key "$key" --file "$local_path" --bucket "$BUCKET"
  else
    echo "↑ R2 $BUCKET/$key (wrangler --remote)"
    npx wrangler r2 object put "${BUCKET}/${key}" --file "$local_path" --remote
  fi
done

echo "Done. Deploy Worker if needed: cd infra/models-cdn && npx wrangler deploy"
