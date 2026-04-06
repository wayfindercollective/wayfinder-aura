# /check — Multi-Model Plan Review Skill

A Claude Code skill that sends your implementation plans to external models for independent review. Models get **tool access to your codebase** so they can read files, search code, and verify claims against real source code.

## How it works

```
You write a plan in Claude Code
  -> /check sends it to Qwen 3.5 (local, FREE)
  -> Qwen reads your actual source files via tools
  -> Qwen finds issues -> Claude evaluates each one (accept/reject)
  -> Claude revises plan -> sends to GPT 5.4 (~$0.80)
  -> GPT reads files, finds deeper issues -> Claude evaluates
  -> Claude revises -> Qwen verifies final version (FREE)
  -> APPROVED or remaining concerns listed
```

**Cost:** $0 if Qwen approves in R1. ~$0.80 typical (1 GPT round).

## Prerequisites

### 1. LM Studio (for local Qwen)

Download from [lmstudio.ai](https://lmstudio.ai). Any version 0.4.9+ supports tool calling.

**Load a model:**
- Recommended: `qwen3.5-397b-a17b` (best quality, needs ~48GB+ RAM)
- Alternative: Any model that supports tool/function calling (Qwen 2.5, Llama 3, etc.)

**Enable the API server:**
1. Open LM Studio -> Developer tab -> Start Server
2. Default port: 1234
3. Note the API key (Settings -> API Keys)

**Verify it works:**
```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer YOUR_LM_STUDIO_API_KEY"
```

### 2. OpenAI API Key (for GPT 5.4)

```bash
# Add to your shell profile (~/.zshrc, ~/.bashrc, etc.)
export OPENAI_API_KEY="sk-proj-your-key-here"
```

Get a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

### 3. Claude Code

The skill runs inside [Claude Code](https://claude.ai/code). Install it, then place the skill files in your project.

## Installation

### Option A: Copy to a specific project

```bash
# From your project root
mkdir -p .claude/skills/check
cp /path/to/SKILL.md .claude/skills/check/SKILL.md
```

### Option B: Copy to your global Claude config (all projects)

```bash
mkdir -p ~/.claude/skills/check
cp /path/to/SKILL.md ~/.claude/skills/check/SKILL.md
```

## Configuration

Edit `SKILL.md` and update these values for your setup:

### LM Studio settings (lines 27-31)

```yaml
Endpoint: http://localhost:8080/v1/chat/completions    # Change if different port
API Key: YOUR_LM_STUDIO_API_KEY                        # From LM Studio settings
Model: qwen3.5-397b-a17b                               # Change to your loaded model
```

### GPT settings (lines 34-36)

```yaml
Endpoint: https://api.openai.com/v1/responses          # Don't change
API Key: from $OPENAI_API_KEY env var                   # Set in shell profile
Model: gpt-5.4                                          # Or gpt-4o, o3, etc.
```

### Project-specific rules

The system prompts reference multi-tenancy, auth patterns, etc. Update the `=== CRITICAL CHECKS ===` blocks in the review loop (Steps 3, 6, 7) to match **your** project's safety rules.

## Usage

In Claude Code, during or after planning:

```
/check
```

That's it. Claude will:
1. Extract the current plan
2. Send it to Qwen with file-reading tools
3. Show you the review with VERDICT
4. If REVISE: evaluate findings, revise, send to GPT
5. Continue until APPROVED or max 3 rounds

## Adapting for Other Projects

The skill is designed to be project-agnostic. The only project-specific parts are:

1. **Critical checks in system prompts** — Update the `=== CRITICAL CHECKS ===` section to match your project's rules (e.g., auth patterns, DB conventions, deploy safety)

2. **The `execute_tool` function** — Uses `PROJECT_ROOT = os.getcwd()` so it automatically works from any project directory. The security check prevents path traversal outside the project.

3. **File search patterns** — The `search_code` tool searches `*.ts, *.tsx, *.js, *.md` by default. Add more extensions if needed (e.g., `*.py, *.go, *.rs`).

### Minimal adaptation example

For a Python/Django project, change the critical checks to:
```
=== CRITICAL CHECKS ===
- Auth: every view needs @login_required or permission checks
- SQL: use ORM queries, no raw SQL without parameterization
- Migrations: reversible, no data loss
- Tests: every endpoint needs test coverage
=== END ===
```

And update the grep file types in `execute_tool`:
```python
cmd = f"grep -rn --include='*.py' --include='*.html' --include='*.md' ..."
```

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│ Claude Code  │────>│ LM Studio (local)│     │ OpenAI API  │
│ (orchestrator)│<───│ Qwen 3.5 397B   │     │ GPT 5.4     │
│              │     └──────────────────┘     └─────────────┘
│  Executes    │            │                       │
│  tool calls  │<───────────┘  tool_calls           │
│  locally     │<───────────────────────────────────┘
│              │
│  read_file() │──> reads from your project filesystem
│  search_code()──> greps your codebase
│  list_dir()  │──> lists your directories
└─────────────┘
```

Claude is the orchestrator. External models request file reads via tool calls. Claude executes them locally (with path traversal protection) and returns results. Models never have direct filesystem access.

## Troubleshooting

### Qwen returns empty content
**Cause:** Thinking/reasoning mode is ON in LM Studio. Output goes to `reasoning_content` instead of `content`.
**Fix:** The skill handles this automatically (checks both fields). If still empty, increase `max_tokens` to 16384+.

### Tool calling returns 400 Bad Request
**Cause:** Older LM Studio version or model not fully loaded.
**Fix:** Update LM Studio to 0.4.9+. Wait for model to finish loading before running /check.

### GPT 5.4 returns 401
**Cause:** `$OPENAI_API_KEY` not set or expired.
**Fix:** `echo $OPENAI_API_KEY` to verify. Regenerate at platform.openai.com if expired.

### Timeout during Qwen call
**Cause:** Large model (397B) is slow. First call after loading takes ~5 min.
**Fix:** Subsequent calls are faster (~30-60s). Be patient on first call.

### Fallback to snippet mode
If tool calling consistently fails, the skill automatically falls back to Claude gathering source file snippets and pasting them into the prompt. Less accurate but always works.

## Cost Breakdown

| Round | Model | Cost | When |
|-------|-------|------|------|
| R1 | Qwen (local) | FREE | Always |
| R2 | GPT 5.4 | ~$0.80 | Only if R1 says REVISE |
| R3 | Qwen (local) | FREE | Only if R2 says REVISE |

Best case: $0 (Qwen approves in R1).
Typical: ~$0.80 (2-3 rounds).

## Files

```
.claude/skills/check/
  SKILL.md    # The skill definition (Claude reads this)
  README.md   # This file (human documentation)
```
