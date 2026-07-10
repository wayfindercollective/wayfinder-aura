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
  "whisper/ggml-large-v3-turbo-q5_0.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
  "llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf|https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
)

# Free-tier + remaining Ultra catalog hosts (pilot listed above).
ALL_EXTRA=(
  "whisper/ggml-tiny.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
  "whisper/ggml-base.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
  "whisper/ggml-small.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
  "whisper/ggml-medium.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin"
  "whisper/ggml-medium.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin"
  "whisper/ggml-large-v3-turbo.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
  "whisper/ggml-large-v3.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin"
  "whisper/ggml-tiny.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin"
  "whisper/ggml-base.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
  "whisper/ggml-small.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
  "llm/google_gemma-3-1b-it-Q4_K_M.gguf|https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/main/google_gemma-3-1b-it-Q4_K_M.gguf"
  "llm/qwen2.5-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
  "llm/Qwen3.5-2B-Q4_K_M.gguf|https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf"
  "llm/LFM2.5-1.2B-Instruct-Q4_K_M.gguf|https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/resolve/main/LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
  "llm/smollm2-360m-instruct-q8_0.gguf|https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/main/smollm2-360m-instruct-q8_0.gguf"
  "llm/Llama-3.2-1B-Instruct-Q4_K_M.gguf|https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
  "llm/Phi-3-mini-4k-instruct-q4.gguf|https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf"
)

MODE="${1:-}"
if [[ "$MODE" == "--free-only" ]]; then
  ENTRIES=(
    "whisper/ggml-tiny.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
    "whisper/ggml-base.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
    "whisper/ggml-small.en.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
    "whisper/ggml-tiny.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin"
    "whisper/ggml-base.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
    "whisper/ggml-small.bin|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
    "llm/google_gemma-3-1b-it-Q4_K_M.gguf|https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/main/google_gemma-3-1b-it-Q4_K_M.gguf"
    "llm/qwen2.5-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    "llm/Qwen3.5-2B-Q4_K_M.gguf|https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf"
    "llm/LFM2.5-1.2B-Instruct-Q4_K_M.gguf|https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/resolve/main/LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
    "llm/smollm2-360m-instruct-q8_0.gguf|https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/main/smollm2-360m-instruct-q8_0.gguf"
    "llm/Llama-3.2-1B-Instruct-Q4_K_M.gguf|https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
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
