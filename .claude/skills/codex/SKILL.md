---
name: codex
description: Send the current implementation plan to OpenAI Codex for independent review (default Terra). Use /codex, /codex terra, /codex sol, or /codex luna to pick GPT-5.6 tier. Claude revises until APPROVED. Max 3 rounds.
---

## Scope

This reviews PLANS only. For post-implementation code review, use `/ship`.
Cost: ~$2.40 per multi-round review. Use after extensive planning only.

## When to use

- When the user runs `/codex` during or after plan mode
- When the user wants a second opinion on a plan from a different model
- Only when explicitly invoked — never trigger automatically


## Model selection (GPT-5.6 tiers)

Default is **Terra** (`gpt-5.6-terra`) from `~/.codex/config.toml`.

Parse optional user args for a tier or full model id. Accepted tokens (case-insensitive):

| Arg | Model | Use |
|-----|--------|-----|
| *(none)* | `gpt-5.6-terra` | Default plan / light code review |
| `terra` | `gpt-5.6-terra` | Balanced |
| `sol` / `soul` | `gpt-5.6-sol` | Flagship / heavy review |
| `luna` | `gpt-5.6-luna` | Fast / cheap pass |
| `gpt-5.6-sol` etc. | as given | Full API id |
| other `gpt-*` / `o*` | as given | Explicit override |

```
/codex              # Terra (default)
/codex terra
/codex sol
/codex luna
/codex sol plan     # tier + other args if the skill supports them
```

**Before every `codex exec`**, resolve:

```bash
# Extract first token that is a tier or model id from the user invocation, else empty.
USER_MODEL_ARG="<sol|terra|luna|... or empty>"
CODEX_MODEL=$(python3 "$HOME/.codex/resolve_review_model.py" ${USER_MODEL_ARG:+"$USER_MODEL_ARG"})
echo "Using Codex model: $CODEX_MODEL"
```

Always announce the resolved model to the user at the start of the review (e.g. "Codex review with **gpt-5.6-sol**").


## Review loop

When invoked, perform the following iterative review loop:

### 1. Generate a unique session ID

```bash
REVIEW_ID=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 8)
```

Use this for all temp file paths: `/tmp/claude-plan-${REVIEW_ID}.md` and `/tmp/codex-review-${REVIEW_ID}.md`.

### 2. Write the current plan

Write the current implementation plan to `/tmp/claude-plan-${REVIEW_ID}.md`.

The plan is whatever implementation plan exists in the current conversation context (from plan mode, or a plan discussed in chat). Write the full plan content to the file.

If there is no plan in the current context, ask the user what they want reviewed.

### 3. Run Codex review

**Sandbox note:** This machine (Bazzite/immutable OS) blocks unprivileged user namespaces, preventing Codex's bubblewrap sandbox. We use `--dangerously-bypass-approvals-and-sandbox` instead of `-s read-only`. This is safe because Codex only does read-only plan reviews and the OS is immutable.

```bash
export PATH="$HOME/.nvm/versions/node/v24.12.0/bin:$PATH" && \
CODEX_MODEL=$(python3 "$HOME/.codex/resolve_review_model.py" ${USER_MODEL_ARG:+"$USER_MODEL_ARG"})
env -u OPENAI_API_KEY codex exec \
  -m "$CODEX_MODEL" \
  -c model_reasoning_effort="xhigh" \
  --dangerously-bypass-approvals-and-sandbox \
  -o /tmp/codex-review-${REVIEW_ID}.md \
  "BEFORE reviewing anything, read the CLAUDE.md file in the project root. This is the master instruction file for this codebase.

Then review the implementation plan in /tmp/claude-plan-${REVIEW_ID}.md.

=== CRITICAL CHECKS ===
- Timers: no polling under 100ms, no QTimer(0). Idle polling >=500ms, active >=100ms
- No popup dialogs: must use inline CTkFrame panels, never CTkToplevel
- Single config source: all config in src/wayfinder/config.py only
- Code placement: new code goes in src/wayfinder/, not root-level legacy files
- Canvas safety: all Tk canvas ops wrapped in try/except
- Performance: don't reduce render rates (overlay 15fps, waveform 30fps idle/15fps active)
- Standard widgets: no custom CTk wrappers, use framework as-is
- Whisper cleanup: extend clean_whisper_artifacts(), don't add new layers
- Overlay states: forced repaint + minimum display time on state changes
- Tone matrix: if touching postprocessor.py, verify all 5 tones × 2 intensities
=== END CRITICAL CHECKS ===

As a reviewer, focus specifically on:
1. Performance regression — timer intervals, CPU usage, frame rates that could degrade
2. X11/Wayland compatibility — will this work on both display servers?
3. Correctness — will this plan actually achieve the stated goals?
4. Risks — edge cases, data loss, race conditions, subprocess lifecycle issues
5. Missing steps — anything forgotten or assumed?
6. Alternatives — is there a simpler approach?
7. Security — command injection via ydotool, unsafe subprocess calls, key exposure
8. CLAUDE.md compliance — does the plan follow the conventions and rules defined in the project?
9. Test coverage — does the plan include tests?
10. Process isolation — overlay runs as a separate PyQt6 subprocess; changes must respect that boundary

Be specific and actionable. If the plan is solid, end with:
VERDICT: APPROVED

If changes are needed, end with:
VERDICT: REVISE" </dev/null 2>&1 | tee /tmp/codex-full-${REVIEW_ID}.txt | tail -80
```

After running, capture the Codex session ID for resume:

```bash
CODEX_SESSION_ID=$(grep -oP 'session id: \K[0-9a-f-]+' /tmp/codex-full-${REVIEW_ID}.txt)
```

You MUST use this exact ID to resume in subsequent rounds.

### 4. Read review and check verdict

Read the Codex review from `/tmp/codex-review-${REVIEW_ID}.md`.

Present the full review to the user so they can see the feedback.

If `VERDICT: APPROVED` → display the final approved status and stop.

If `VERDICT: REVISE` → continue to step 4b.

### 4b. Critically evaluate each finding

Before revising anything, evaluate EVERY finding Codex raised. For each one, determine:

- **Is this actually correct?** Codex can misread code or misunderstand the plan. Verify claims against the actual codebase before accepting them.
- **Is this relevant to our context?** A finding can be technically valid but irrelevant.
- **Is the plan already correct?** If Codex flags something the plan already handles, defend the plan.

For each finding, state your assessment to the user:

- **Accept** — "This is a real issue, fixing it." (explain why)
- **Reject** — "This is incorrect/irrelevant because..." (explain why, do NOT change the plan)
- **Partially accept** — "The concern is valid but the suggested fix is wrong. Here's what I'll do instead..."

When resubmitting to Codex, include your rejections:

> "I did NOT address finding #X because [reason]. If you still disagree, explain why."

This is a two-way argument, not a compliance exercise. The goal is a better plan, not an approved plan.

### 5. Revise the plan

Revise the plan — address only the issues you accepted in step 4b. Do NOT change the plan for rejected findings. Update the plan content and rewrite `/tmp/claude-plan-${REVIEW_ID}.md`.

Tell the user what you changed, what you rejected, and why for each.

### 6. Re-submit to Codex

```bash
export PATH="$HOME/.nvm/versions/node/v24.12.0/bin:$PATH" && \
env -u OPENAI_API_KEY codex exec resume ${CODEX_SESSION_ID} \
  "I've revised the plan based on your feedback. The updated plan is in /tmp/claude-plan-${REVIEW_ID}.md. Here's what I changed:

[List the specific changes made]

Please re-review. End with VERDICT: APPROVED or VERDICT: REVISE" </dev/null 2>&1 | tee /tmp/codex-full-${REVIEW_ID}-r2.txt | tail -80
```

**Note:** `codex exec resume` does NOT support the `-o` flag. Output is captured via `tee` instead.

If `resume ${CODEX_SESSION_ID}` fails (e.g., session expired), fall back to a fresh `codex exec` call with context about the prior rounds.

Then go back to Step 4.

### 7. Max rounds

Maximum 3 rounds. If not approved after 3 rounds, display:

```
## Codex Review — Final (model: $CODEX_MODEL)
**Status:** ⚠️ Max rounds (3) reached — not fully approved
**Remaining concerns:** [List unresolved issues from last review]
---
**Codex still has concerns. Review the remaining items and decide whether to proceed or continue refining.**
```

## Summary of the loop

- **Round 1:** Claude sends plan → Codex reviews → REVISE?
- **Round 2:** Claude revises → Codex re-reviews (resume session) → REVISE?
- **Round 3:** Claude revises → Codex re-reviews (resume session) → APPROVED ✅

Max 3 rounds. Each round preserves Codex's conversation context via session resume. Claude critically evaluates Codex feedback between rounds — accepting valid concerns, rejecting incorrect ones, and defending correct decisions.

## Configuration

**Model (Terra default + sol/terra/luna args):**

```bash
# /codex          → gpt-5.6-terra
# /codex sol      → gpt-5.6-sol
# /codex terra    → gpt-5.6-terra
# /codex luna     → gpt-5.6-luna
USER_MODEL_ARG=""   # set from user slash-command args if present
CODEX_MODEL=$(python3 "$HOME/.codex/resolve_review_model.py" ${USER_MODEL_ARG:+"$USER_MODEL_ARG"})
```

Single source of default: `model = "gpt-5.6-terra"` in `~/.codex/config.toml`.
Tier aliases live in `~/.codex/resolve_review_model.py` (also accepts `soul` → sol).
When OpenAI ships a new generation, update those two files — do **not** hardcode `-m gpt-5.x` in skills.


- **OAuth only:** always run codex with `env -u OPENAI_API_KEY` so it uses the ChatGPT OAuth tokens in `~/.codex/auth.json`, never a stray `OPENAI_API_KEY` from the environment.
- Use `--dangerously-bypass-approvals-and-sandbox` (bwrap blocked on this OS). Codex only does read-only reviews — never write operations.
- **Always close stdin with `</dev/null`** before `2>&1` in every `codex exec` — without it codex stalls waiting on stdin and the Bash tool backgrounds the call.
- Always capture full stdout via `tee` to extract session IDs for resume
- Always present Codex's full feedback to the user between rounds
- Always prefix codex commands with `export PATH="$HOME/.nvm/versions/node/v24.12.0/bin:$PATH" &&`

## Post-approval: test coverage gate

After the plan is approved and before implementation begins, Claude MUST verify the plan includes a test section. If it doesn't, add one before proceeding. Tests go in `tests/` using pytest. Run with: `python3 -m pytest tests/ -v --tb=short`.
