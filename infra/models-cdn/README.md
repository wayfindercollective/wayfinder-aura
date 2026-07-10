# Wayfinder Aura Models CDN (Cloudflare R2 + Worker)

Serves Whisper / GGUF weights from R2. **Ultra** objects require a valid
license Bearer token (same Ed25519 token as desktop activation).

Full step-by-step: [`../../docs/MODELS-CDN-SETUP.md`](../../docs/MODELS-CDN-SETUP.md)

## Quick deploy

```bash
cd infra/models-cdn
npm install
npx wrangler login
npx wrangler r2 bucket create wayfinder-aura-models
# Upload pilot objects (see SETUP doc), then:
npx wrangler secret put LICENSE_PUBLIC_KEY_HEX
# paste: e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a
npx wrangler deploy
```

Set the Worker URL on desktops:

```bash
export WAYFINDER_MODELS_CDN_BASE=https://wayfinder-models-cdn.<your-subdomain>.workers.dev
```

Or `models_cdn_base` in `~/.config/wayfinder-aura/config.json`.
