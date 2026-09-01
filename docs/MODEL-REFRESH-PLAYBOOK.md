# Model refresh playbook

How a post-processing model gets replaced. **Every step here is manual and
reviewed.** The biweekly scout (`.github/workflows/model-scout.yml`) only
*reports*; it has no credential that could publish, and it must stay that way.

Current lineup (2026-09):

| Tier | Model | Tier gate | Why |
|------|-------|-----------|-----|
| Light | Gemma 3 1B (`google_gemma-3-1b-it-Q4_K_M.gguf`) | Free, `recommended` | Most consistent gentle-guide cleanup across tones |
| Medium | Qwen 3.5 2B (`Qwen3.5-2B-Q4_K_M.gguf`) | Free | Capable, roomier; less consistent than Gemma for light cleanup |
| Heavy | Qwen3 4B Instruct 2507 (`Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`) | Ultra (`large_cleanup_models`) | Sharpest instruction follower at 4B, **no reasoning latency** |

---

## Open item — candidate #1 for the next cycle

**Evaluate `bartowski/Qwen_Qwen3.5-4B-GGUF` against the 2507 incumbent.**

| Field | Value |
|-------|-------|
| File | `Qwen_Qwen3.5-4B-Q4_K_M.gguf` |
| Revision | `4168f45a16a1290d65a4ec0fa312ae917a4c15d6` |
| sha256 | `13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983` |
| size_bytes | `3013027808` |
| License | Apache-2.0 (redistributable via our CDN) |

Pin verified against the HuggingFace API in 2026-09. **Fitness is unmeasured.**
It was deliberately *not* adopted in the 2026-09 refresh because it is a
*thinking* vision-language model with `enable_thinking` on by default — not a
non-thinking Instruct-2507 successor — and swapping a model with 198 rows of
measured eval evidence for an unmeasured newer name is the regression this
playbook exists to prevent. Adopt it only if step (b) below says it wins.

---

## (a) Read the scout report

The scout runs on the 1st and 15th at 08:00 UTC and comments on the open
`model-refresh` issue when `candidates > 0`. Locally:

```bash
python3 scripts/model_scout.py            # human digest
python3 scripts/model_scout.py --json     # JSON only on stdout
```

Two groups, and the difference matters:

- **`PIN_WATCH`** — the three repos we ship. A hit means upstream re-released a
  file we pin, so our revision pin may be stale. This is a *re-pin*, not a model
  change: re-verify the digest before touching anything.
- **`DISCOVERY`** — curated candidate repos we do not ship. A hit is a
  suggestion to evaluate. It is *never* a reason to change anything.

The discovery list is curated on purpose. An open-ended HuggingFace search
returns unranked noise and needs relevance logic nobody would maintain; extend
`DISCOVERY` in `scripts/model_scout.py` when a new family is worth watching.

## (b) Evaluate — the 11-cell matrix, not a 15-cell grid

```bash
# On the Flatpak HOST (the GPU and llama-simple only exist there):
PYTHONPATH=src python3 scripts/eval_tones.py \
    --model ~/.local/share/wayfinder-aura/llm-models/<candidate>.gguf
```

`scripts/eval_tones.py`'s `DEFAULT_CELLS` is **enumerated, not a cross-product**:
5 tones x standard, 5 tones x strong, plus `casual` x `caricature` = **11 cells**.
Do not "improve" it into a 5x3 = 15-cell grid. A tone x intensity product would
produce caricature for every tone, which is not what project rule 5 asks for.

**Win condition: quality *and* latency at least as good as the incumbent on
Strong and Caricature.** Novelty is not a win condition. A newer model that is
slower per dictation is a regression, because the user waits for it on every
utterance.

Watch specifically for a weak model drifting under caricature prompts: echoing
the input unchanged (how LFM2.5 failed), self-annotation like
`[Here's the cleaned version]`, or restructuring in *standard* mode. The harness
scores these — check `FAILURE_MARKERS` and the preservation gates, do not eyeball
the report.

## (c) License check — can we redistribute it?

We rehost weights on our own R2 CDN, so the license must permit redistribution
by us **and** must not attach conditions that follow our downstream users.

- **Apache-2.0 / MIT** — fine, no fresh review needed.
- **LFM Open License** — *rejected*. Its $10M revenue cap follows every
  downstream user, which is incompatible with shipping the weights from our CDN.
  This is why LFM2.5 1.2B was retired in 2026-09.
- **Gemma Terms of Use** — the existing Gemma 3 1B rehost is pre-existing and
  accepted. **Any *new* Gemma pick needs a fresh redistribution review** — do not
  assume the current one generalises.
- **Llama Community License** — acceptable-use terms follow redistribution;
  needs review, not an automatic yes.

## (d) Prefer a non-thinking Instruct SKU for the Heavy tier

A thinking model pays reasoning latency on *every dictation*, not just hard
ones. That is why `qwen3.5:4b` is described in `get_recommended_models()` as
"thinks before answering (slower)" and why the Heavy tier is still 2507.

## (e) On a win: update every sync point

There are **five** data sites plus two watchers. Pins come from the HuggingFace
API, never typed by hand:

```bash
curl -s https://huggingface.co/api/models/<repo>/tree/main \
  | python3 -c 'import json,sys; [print(i["path"], i["lfs"]["oid"], i["size"]) for i in json.load(sys.stdin) if i.get("lfs")]'
# revision for the URL:
curl -s https://huggingface.co/api/models/<repo> | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
```

**Five data sites:**

| # | Site | What changes | Ratcheted by |
|---|------|--------------|--------------|
| 1 | `wayfinder_main.LLM_GGUF_MODELS` | full entry: revision-pinned `url`, `sha256`, exact `size_bytes`, derived `size` label, `cdn_object`, `requires_feature` | `TestShippedPins` |
| 2 | `src/wayfinder/core/setup.py::LLM_MODELS` | same digest and byte count, byte-identical; the LLM filename sets must stay equal **both ways**, so removing a model here fails too | `TestCatalogCopiesAgree` |
| 3 | `src/wayfinder/config.py::_LLM_PREFERENCE` | prepend the new filename — **never trim the legacy tail** | `TestRecommendationsMatchTheLineup` |
| 4 | `catalog/v1.json` | regenerate with `scripts/export_model_catalog.py`, then re-add retirement rows. The **live** (non-`disabled`) LLM ids must equal the shipped ids **exactly, both ways** — this prevents drift between the built-in lineup and its published remote overlays | `TestMonitoringMirrorsTheCatalog` |
| 5 | all four recommendation surfaces: `postprocessor.get_recommended_models()`, `ui/components.MODEL_RECOMMENDATIONS`, `postprocessor.get_upgrade_suggestion_for_intensity()`, `postprocessor.get_model_recommendation_for_style()` | move the ⭐ to the new default; never leave a ⭐ on a retired model; the intensity API must name only shipped `.gguf` filenames, and neither `recommended` nor `also_works` may name a retired model | `TestRecommendationsMatchTheLineup` |

**Two watchers:**

| # | Site | What changes | Ratcheted by |
|---|------|--------------|--------------|
| 6 | `src/wayfinder/core/model_updates.py::MONITORED_MODELS` | swap the `current_filename` **and** the `repo_id`. `current_filename` is an exact set match against the shipped LLMs; `repo_id` is checked against the repository encoded in that model's pinned download URL, so a typo'd or stale repo fails | `TestMonitoringMirrorsTheCatalog` |
| 7 | `scripts/model_scout.py::PIN_WATCH` | swap the watched repo + `name_re` | `TestMonitoringMirrorsTheCatalog` |

Site 5 is easy to forget and has no runtime error to warn you: the app simply
keeps recommending a model you retired.

This table has been wrong three times, always the same way: a one-directional
check reads as full coverage. Sites 3 and 5 were added in 2026-09 after review
found them unratcheted. A second pass found site 5 only *half* covered
(`get_recommended_models()` and the UI string were checked;
`get_upgrade_suggestion_for_intensity()` and `get_model_recommendation_for_style()`
were not) and site 2 checked one way only. A third pass found site 4 checked
only published -> app (so **deleting** a live published row passed) and site 6
never validating `repo_id` at all, despite this table claiming it did. All are
closed above, each verified by deleting the row and watching CI go red.

**What is actually enforced:** removing a shipped model from any of the seven
sites fails CI, with one deliberate exception — per-style coverage in
`get_model_recommendation_for_style()`, named in the non-guarantees below. Sites 2, 4, 6 and 7 are exact set equalities, so inventing an
entry the app does not ship fails too. Site 5 was the last gap: every check
there rejected a *retired* name but none noticed a *shipped* one going missing,
so deleting Qwen3 4B from `get_recommended_models()`, from both heavy branches
of `get_upgrade_suggestion_for_intensity()`, or from `MODEL_RECOMMENDATIONS`
left the whole suite green. Three coverage guards now close it — every shipped
model must be listed by the API, named in the UI string, and suggested by at
least one intensity.

**Five deliberate non-guarantees**, so this table is not read as more than it is:

- The `avoid` list returned by `get_model_recommendation_for_style()` is **not**
  ratcheted, and must not be. Naming `phi3:mini` there is a warning *against* a
  retired model; a blanket "no retired tag anywhere" rule would delete it.
- Free-text prose is not checked — `message` strings, and `MONITORED_MODELS`
  `description` fields. If prose names a model, a human has to catch it.
- The `_LLM_PREFERENCE` **legacy tail** is deliberately unconstrained *in
  ordering and extra contents*: it exists to keep already-downloaded retired
  models selectable, so an unknown filename appended there is allowed. Three
  things about it *are* ratcheted, so "only the lead entry" would understate
  it: the lead entry must be the catalog's `recommended` model, every shipped
  filename must appear somewhere in the list, and the legacy sentinel
  `qwen2.5-1.5b-instruct-q4_k_m.gguf` must survive **by that exact filename** —
  only the byte-exact on-disk name is loadable, so a near-miss that merely
  contains `qwen2.5-1.5b` still orphans the downloaded file. What you may
  freely change is the order of the tail and anything additional you put there.
- Per-style **union coverage** in `get_model_recommendation_for_style()` is not
  enforced, and must not be: it names one model per tone, and `gemma3-1b` is
  intentionally recommended by no style, so requiring coverage would force a
  fake recommendation. State the rest per field, because the two are enforced
  differently:
  - `recommended` is strict — it may not be empty, and every tag in it must map
    to a live shipped model. Emptying a branch fails CI, and so does naming an
    unshipped tag there.
  - `also_works` is checked against retired tags **only**. An unshipped
    Ollama-only tag passes deliberately — `qwen3.5:4b`, `qwen2.5:3b` and
    `llama3.2:3b` are live entries today and ship no GGUF of ours.
  - What nothing enforces is the union: no test requires every shipped model to
    be named by *some* style, so **replacing** a live tag with another live tag
    passes. Deleting the last tag from a branch does not — that is the empty
    check above, not coverage.
- Whisper `setup.py` <-> app catalog is a **subset** check, not equality (Setup
  offers 6 of 11 on purpose). Only the LLM catalogs are equal both ways.

Never write a `/resolve/main/` URL: `main` moves, the pinned digest does not.
Never invent or copy-forward a digest.

## (f) Retire the loser — `disabled: true`, never deletion

```json
"llm": { "phi-3-mini": { "disabled": true } }
```

`merge_section()` pops a `disabled` id out of a client's merged catalog, so the
model stops being offered on already-installed clients too. *Deleting* the key
does the opposite: with nothing to overlay, an old client keeps offering its own
built-in entry forever.

Retirement is catalog-only. Do **not**:

- delete the R2 object (old clients still re-download it — verified 2026-09:
  the four retired free objects are still served with their original byte
  counts, and `Phi-3-mini` still correctly answers `401` without an Ultra bearer);
- edit `PUBLIC_OBJECTS` in `infra/models-cdn/wrangler.toml`;
- trim `FREE_CLEANUP_MODEL_FILENAMES` in `postprocessor.py` (it is a retention
  list for users who already downloaded a retired *Free* model — trimming it
  would silently gate a file they already have);
- trim the `config._LLM_PREFERENCE` legacy tail.

## (g) Ship — object first, catalog second

```bash
python3 -m pytest tests/test_catalog_ratchet.py -q     # all five mirrors agree
python3 scripts/verify-model-digests.py --hf-metadata  # pins match upstream

# 1. OBJECT FIRST: add the new key to PILOT in scripts/upload-models-r2.sh, then
./scripts/upload-models-r2.sh
#    needs ADMIN_UPLOAD_SECRET (read from infra/models-cdn/.secrets/admin_upload_secret)
#    or R2_* keys / `npx wrangler login`.

# 2. Verify the object landed intact BEFORE anything points at it:
python3 scripts/verify-model-digests.py --source cdn --only <new-id>

# 3. CATALOG SECOND:
python3 scripts/publish_model_catalog.py
```

**The order is load-bearing.** `merge_section()` adds an id it has never seen as
*CDN-only* — it strips the HuggingFace `url`, because a model we never shipped
cannot vouch for an arbitrary origin. A catalog row published ahead of its object
therefore gives every client a download that can only 404.

Two more failure modes worth knowing before you upload:

- **Hash mismatch / interrupted download.** Downloads fail closed on a sha256
  mismatch (2026-08-17 audit, F-C), and `size_bytes` is the pre-hash bound that
  stops a truncated or endless stream. A wrong `size_bytes` is therefore not
  cosmetic — `merge_section()` rejects the whole row as "weakens its size" and
  the publish becomes a *silent* no-op. This actually happened: `catalog/v1.json`
  shipped `qwen3.5-2b` at `1390000000` against a real 1280835840-byte object from
  2026-07 until 2026-09, and no client ever applied that row.
- **Never overwrite a mismatched object.** If an R2 key already exists with
  different bytes, publish under a *new* filename instead. Overwriting breaks
  every installed client pinned to the old digest.

## (h) Chat templates need measurement first

`_CHAT_TEMPLATE_MODELS` / `_CHAT_TEMPLATES` in `postprocessor.py` are an
allowlist. Adding a model to it changes the prompt path, so it requires a
**measured tone-eval pass first** (project rule 5: all 5 tones at standard *and*
strong). Do not add a template because the model card mentions one.

---

## What must never be automated

The scout must never replace a model, edit a catalog, upload weights, publish
`catalog/v1.json`, or open a PR. An automated PR that moves a model pin is a
supply-chain hazard; an automated publish is worse. `model-scout.yml` holds only
`GITHUB_TOKEN` with `issues: write`, so it *cannot* reach R2 or HuggingFace with
write access — keep it that way.
