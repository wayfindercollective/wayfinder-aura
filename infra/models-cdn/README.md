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

## ⚠ Never overwrite an existing object's bytes

Since the 2026-08-17 security audit the app **pins a sha256 for every shipped
model and refuses a download that does not match it**. The pin is keyed by
*filename*, and a remote catalog cannot override a digest we shipped — that is
what makes a catalog compromise unable to swap weights.

The operational consequence:

> Re-uploading different bytes to an existing object key breaks that model's
> download for **everyone running the current build**, with a "this model file
> doesn't match" error. A new catalog id does not help — the pin follows the
> filename.

To ship new weights:

- **Preferred, no app update needed:** upload under a **new filename**
  (`ggml-<model>-v2.bin`), publish it as a new catalog id, and `disabled: true`
  the old one. A brand-new catalog entry is rejected unless it carries **all
  three** of `cdn_object`, `sha256`, and an exact `size_bytes` (the byte count,
  not a rounded estimate — it bounds the transfer before hashing). Any `url` on
  a new entry is dropped: models we did not ship are CDN-only. Free objects must
  also be listed in `PUBLIC_OBJECTS`.
- **Otherwise:** ship an app release that carries the new pin (update both
  `wayfinder_main.py` and `src/wayfinder/core/setup.py` — `tests/test_catalog_ratchet.py`
  fails if the two copies drift).

Before any release, and after touching R2, verify the pins still hold:

```bash
# seconds, no downloads — compares pins against Hugging Face LFS oids
python3 scripts/verify-model-digests.py --hf-metadata

# authoritative: hashes every object, needs an Ultra activation for gated ones
python3 scripts/verify-model-digests.py --full --source both
```

Set the Worker URL on desktops:

```bash
export WAYFINDER_MODELS_CDN_BASE=https://wayfinder-models-cdn.<your-subdomain>.workers.dev
```

Or `models_cdn_base` in `~/.config/wayfinder-aura/config.json`.
