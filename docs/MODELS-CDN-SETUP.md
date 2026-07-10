# Models CDN setup (Cloudflare R2 + Worker)

Host Whisper / GGUF weights on **your** R2 bucket. The desktop app downloads
Ultra models with `Authorization: Bearer <license token>` (same Ed25519 token
from Convex `/activate`). Free models can later be public on the same bucket.

## Architecture

```
App ──Bearer token──► Worker ──R2──► model file
         ▲
         │ verify Ed25519 (public key only)
Convex /activate signs token (private key only on Convex)
```

## Pilot objects (do these first)

| R2 object key | Source (download once, then upload) | License feature |
|---------------|-------------------------------------|-----------------|
| `whisper/ggml-large-v3-turbo-q5_0.bin` | [HF ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin) | `large_models` |
| `llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf` | [HF bartowski/…](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf) | `large_cleanup_models` |

App catalog entries already set `cdn_object` + `requires_feature` for these two.

## 1. Create the R2 bucket

Cloudflare dashboard → **R2** → Create bucket:

- Name: `wayfinder-aura-models` (must match `infra/models-cdn/wrangler.toml`)

Or CLI:

```bash
cd /path/to/wayfinder-aura/infra/models-cdn
npm install
npx wrangler login
npx wrangler r2 bucket create wayfinder-aura-models
```

## 2. Upload pilot files

From a machine with disk + bandwidth:

```bash
# Fetch from HF once
mkdir -p /tmp/wfa-models/whisper /tmp/wfa-models/llm
curl -L -o /tmp/wfa-models/whisper/ggml-large-v3-turbo-q5_0.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
curl -L -o /tmp/wfa-models/llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"

# Prefer Worker multipart (Cloudflare skill: R2 createMultipartUpload / uploadPart).
# Requires ADMIN_UPLOAD_SECRET on the Worker (set via `wrangler secret put`).
export ADMIN_UPLOAD_SECRET=...   # from infra/models-cdn/.secrets/admin_upload_secret on deploy host
export MODELS_CDN_BASE=https://wayfinder-models-cdn.peter-7b5.workers.dev

python3 scripts/r2_worker_multipart_upload.py \
  --key whisper/ggml-large-v3-turbo-q5_0.bin \
  --file ~/.cache/wayfinder-aura-model-uploads/whisper/ggml-large-v3-turbo-q5_0.bin
python3 scripts/r2_worker_multipart_upload.py \
  --key llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  --file ~/.cache/wayfinder-aura-model-uploads/llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf
```

Alternative (S3 API + R2 access keys): `scripts/r2_multipart_upload.py`.  
Helper: `scripts/upload-models-r2.sh` fetches HF sources then uploads.

## 3. Deploy the Worker

```bash
cd infra/models-cdn
npm install
# Same public key as desktop LICENSE_PUBLIC_KEY_HEX in license.py
npx wrangler secret put LICENSE_PUBLIC_KEY_HEX
# paste: e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a

npx wrangler deploy
```

Note the workers.dev URL, e.g.  
`https://wayfinder-models-cdn.<account>.workers.dev`

Optional: attach custom domain `models.wayfindercollective.io` in the Worker routes.

## 4. Point the desktop app at the CDN

**Env (dev):**

```bash
export WAYFINDER_MODELS_CDN_BASE=https://wayfinder-models-cdn.<account>.workers.dev
```

**Config (user install):**

```json
"models_cdn_base": "https://wayfinder-models-cdn.<account>.workers.dev"
```

If unset, the app falls back to Hugging Face URLs (still requires Ultra feature for pilot models before download starts).

## 5. Deploy Convex feature tokens (A1)

In `Dev/wayfinder-licensing`:

```bash
cd /path/to/wayfinder-licensing
npx convex deploy   # or convex dev for the dev deployment
```

`/activate` now returns tokens with `features: [...]` and `v: 2`.  
Redeploy so existing Ultra users get a feature list on next refresh (mid-grace).

## 6. Smoke test

```bash
# Health
curl -sS "$CDN/health"

# Ultra object without token → 401
curl -sS -o /dev/null -w "%{http_code}\n" "$CDN/v1/objects/whisper/ggml-large-v3-turbo-q5_0.bin"

# With token (copy token from ~/.config/wayfinder-aura/license.json after activate)
curl -sS -H "Authorization: Bearer $TOKEN" \
  -o /tmp/turbo-q5.bin \
  "$CDN/v1/objects/whisper/ggml-large-v3-turbo-q5_0.bin"
```

In the app: activate Ultra → download **Large v3 Turbo Q5** or **Qwen3 4B Instruct**.

## Later: host *all* models

1. Upload free models under the same key scheme, e.g.  
   `whisper/ggml-base.en.bin`, `llm/google_gemma-3-1b-it-Q4_K_M.gguf`
2. Add keys to Worker `PUBLIC_OBJECTS` (wrangler.toml `[vars]` or dashboard).
3. Set `cdn_object` on every catalog entry; keep HF `url` as fallback.
4. Optionally remove HF URLs from shipping builds once CDN is proven.

### Suggested key layout

```
whisper/ggml-tiny.en.bin
whisper/ggml-base.en.bin
whisper/ggml-small.en.bin
whisper/ggml-medium.en.bin
whisper/ggml-large-v3-turbo-q5_0.bin
whisper/ggml-large-v3-turbo.bin
whisper/ggml-large-v3.bin
llm/google_gemma-3-1b-it-Q4_K_M.gguf
llm/Qwen3.5-2B-Q4_K_M.gguf
llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf
llm/…
```

## Security notes

- Private signing key stays **only** on Convex (`LICENSE_SIGNING_PRIVATE_KEY`).
- Worker holds **public** key only.
- Client-side Python can still be patched; without a valid token the Worker will not serve Ultra objects. Open weights may still exist on HF — CDN protects *your* delivery path.
- Never commit R2 API tokens or wrangler state with secrets.

## Checklist

- [ ] R2 bucket `wayfinder-aura-models`
- [ ] Pilot objects uploaded
- [ ] Worker deployed + `LICENSE_PUBLIC_KEY_HEX` secret
- [ ] Convex licensing deployed (features in token)
- [ ] `models_cdn_base` / `WAYFINDER_MODELS_CDN_BASE` set
- [ ] Smoke: 401 without token, 200 with Ultra token
- [ ] App download of Turbo Q5 + Qwen3 4B works when activated
