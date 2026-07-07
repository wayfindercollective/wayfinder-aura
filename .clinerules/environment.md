# Working rules for local models (Qwen via Cline)

This repo is normally developed with **Claude Code**. When a **local model drives Cline**
here, follow these rules — they prevent the exact failure modes local models hit on this
machine (stalled shell captures, accidental paid-API spend, sandbox errors).

## Environment: Bazzite (immutable Fedora)
- **No `sudo`** and no commands that trigger a system password prompt — never propose them.
- **Unprivileged user namespaces are blocked.** Any tool relying on bubblewrap/sandboxing
  (including the `codex` CLI's default sandbox) will fail. Don't depend on them.
- **`node` / `npm` / `codex` are NOT on the default PATH.** They live under
  `~/.nvm/versions/node/v24.12.0/bin`. If you genuinely need them, prefix the command with
  `export PATH="$HOME/.nvm/versions/node/v24.12.0/bin:$PATH" &&`.
- The integrated terminal must be **bash with shell integration** (configured in VS Code
  settings). **If a command's output cannot be read, STOP and say so — never guess the
  result and continue.** Chaining on an unread value is what stalls these sessions.

## Do NOT run — these are Claude Code skills, not Cline tasks
- **`.claude/skills/codex`** shells out to the **paid OpenAI Codex CLI (~$2.40/run)** and
  needs multi-round shell-output capture. **Never invoke it from Cline.** For a plan
  sanity-check, use the single-shot flow in **`docs/qwen-plan-check.md`** instead.
- Treat everything under **`.claude/skills/`** as reference material that assumes Claude
  Code semantics — not as runnable Cline commands.

## Work shape that actually succeeds here
- Prefer **single-shot reasoning** and **scoped edits**. Avoid long chains that depend on
  capturing values (session IDs, temp file paths, PIDs) across many shell steps.
- Respect the performance rules in **`CLAUDE.md`** (overlay/waveform frame rates, timer
  intervals, inline `CTkFrame` panels not `CTkToplevel` popups, single config source in
  `src/wayfinder/config.py`). Do not regress them.
