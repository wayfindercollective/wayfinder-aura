# Remote model catalog + scout

Ship **new downloadable models without an app rebuild** by publishing a small
JSON catalog next to the Models CDN, then optionally running a scout that
notifies you when Hugging Face has new candidates.

## Architecture

```
Built-in catalogs (wayfinder_main.py)  ──offline fallback──┐
                                                           ├─► effective picker lists
Remote GET /v1/catalog  (R2: catalog/v1.json)  ──merge──┘
```

- **R2 weights** still need upload (multipart script).
- **Catalog JSON** lists what the app should show (`cdn_object`, free vs Ultra).
- Clients fetch catalog on startup (6h disk cache). Failures fall back to built-in.

## Publish catalog (after editing built-in or catalog/v1.json)

```bash
export PATH="$HOME/.nvm/versions/node/v24.12.0/bin:$PATH"
export MODELS_CDN_BASE=https://wayfinder-models-cdn.peter-7b5.workers.dev
# ADMIN_UPLOAD_SECRET auto-loaded from infra/models-cdn/.secrets/ if present

# From current app catalogs:
python3 scripts/publish_model_catalog.py

# Or edit catalog/v1.json by hand, then:
python3 scripts/publish_model_catalog.py --skip-export --file catalog/v1.json
```

Clients hit: `{MODELS_CDN_BASE}/v1/catalog`

Redeploy Worker once if you just added the route:

```bash
cd infra/models-cdn && npx wrangler deploy
```

## Add a new model without shipping the app

1. Upload file:  
   `python3 scripts/r2_worker_multipart_upload.py --key whisper/….bin --file …`
2. If free: add key to Worker `PUBLIC_OBJECTS` in `wrangler.toml` + redeploy.
3. Add entry under `whisper` or `llm` in `catalog/v1.json` (or app built-ins + re-export).
4. `python3 scripts/publish_model_catalog.py --skip-export`
5. Users get it on next app launch / cache expiry.

## Retire a model (never delete the key)

To **retire** a model remotely, set `"disabled": true` on that id and re-publish:

```json
"llm": { "phi-3-mini": { "disabled": true } }
```

`merge_section()` pops a `disabled` id out of the merged catalog, so the model stops
being offered **on already-installed clients too**. Deleting the id from
`catalog/v1.json` does the opposite of what you want: with nothing to overlay, an old
client keeps offering its own built-in entry forever. A retired row therefore stays in
the published catalog as `{"disabled": true}` indefinitely.

Retiring is catalog-only. Do **not** delete the R2 object and do **not** trim
`FREE_CLEANUP_MODEL_FILENAMES` or the `config._LLM_PREFERENCE` legacy tail — users who
already downloaded the file must keep being able to load it from disk.

The 2026-09 refresh retired `lfm2.5-1.2b`, `llama3.2-1b`, `phi-3-mini`,
`qwen2.5-1.5b` and `smollm2-360m` this way. The shipped lineup is Gemma 3 1B (Free,
recommended) / Qwen 3.5 2B (Free) / Qwen3 4B Instruct 2507 (Ultra).

## Scout (don’t monitor HF yourself)

```bash
python3 scripts/model_scout.py
# Digest: ~/.local/share/wayfinder-aura/model-scout-latest.md

# Optional: set a notify command (Personal OS, notify-send, etc.)
export WAYFINDER_SCOUT_NOTIFY_CMD='notify-send "Wayfinder models" "Scout digest ready"'
python3 scripts/model_scout.py --notify
```

### Daily cron (user systemd example)

```ini
# ~/.config/systemd/user/wayfinder-model-scout.service
[Unit]
Description=Wayfinder model scout

[Service]
Type=oneshot
Environment=PATH=%h/.nvm/versions/node/v24.12.0/bin:/usr/bin
WorkingDirectory=%h/Dev/wayfinder-aura
ExecStart=/usr/bin/python3 scripts/model_scout.py --notify
```

```ini
# ~/.config/systemd/user/wayfinder-model-scout.timer
[Unit]
Description=Daily Wayfinder model scout

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now wayfinder-model-scout.timer
```

Scout **never** auto-publishes. You approve → upload → catalog publish.

## Schema (v1)

```json
{
  "version": 1,
  "updated_at": "2026-07-10T00:00:00Z",
  "whisper": {
    "tiny.en": {
      "name": "Tiny (English)",
      "filename": "ggml-tiny.en.bin",
      "cdn_object": "whisper/ggml-tiny.en.bin",
      "url": "https://huggingface.co/…",
      "size": "75 MB",
      "size_bytes": 75000000,
      "speed_rating": 5,
      "accuracy_rating": 1
    }
  },
  "llm": {
    "gemma3-1b": {
      "name": "Gemma 3 1B ⭐",
      "filename": "google_gemma-3-1b-it-Q4_K_M.gguf",
      "cdn_object": "llm/google_gemma-3-1b-it-Q4_K_M.gguf",
      "sha256": "12bf0fff…",
      "size_bytes": 806058496,
      "recommended": true,
      "url": "https://huggingface.co/…/resolve/<commit-sha>/…"
    },
    "qwen3.5-2b": {
      "name": "Qwen 3.5 2B",
      "filename": "Qwen3.5-2B-Q4_K_M.gguf",
      "cdn_object": "llm/Qwen3.5-2B-Q4_K_M.gguf",
      "sha256": "aaf42c8b…",
      "size_bytes": 1280835840,
      "url": "https://huggingface.co/…/resolve/<commit-sha>/…"
    },
    "qwen3-4b-2507": {
      "name": "Qwen3 4B Instruct 2507",
      "filename": "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
      "cdn_object": "llm/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
      "requires_feature": "large_cleanup_models",
      "sha256": "2fde00ce…",
      "size_bytes": 2497280736,
      "url": "https://huggingface.co/…/resolve/<commit-sha>/…"
    },
    "phi-3-mini": { "disabled": true }
  }
}
```

`sha256` and an exact `size_bytes` are **not optional** for an LLM row. `size_bytes`
is the pre-hash download bound, and `merge_section()` rejects any row that reuses a
shipped filename while weakening its digest, size, or `requires_feature` — a rejected
row is silently ignored by clients, so a wrong number is a *silent* publish failure.
Always regenerate the live rows with `scripts/export_model_catalog.py` rather than
hand-editing them, and never write a `/resolve/main/` URL: `main` moves, the pinned
digest does not.

Allowed entry keys are allowlisted in `src/wayfinder/model_catalog.py`.

## Config

| Key / env | Meaning |
|-----------|---------|
| `models_cdn_base` | Worker base (also default catalog host) |
| `models_catalog_url` / `WAYFINDER_MODELS_CATALOG_URL` | Override full catalog URL |
