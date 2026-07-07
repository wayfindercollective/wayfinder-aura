# Qwen-native plan check (shell-free)

A "check my plan integrity" flow for the **local model in Cline** — no terminal, no session
IDs, no paid CLI, no chance of a shell-integration stall. It's a single message in, a single
review out.

> This replaces `.claude/skills/codex` when you're driving with Qwen. That skill is for
> Claude Code only — it spends real OpenAI money and needs multi-round shell capture.

## How to use
Paste the block below into Cline, filling in your plan. Qwen reviews it in one shot.

```
Review this implementation plan against the checklist. For EACH item output
PASS / FAIL / N/A and one line of reasoning. Then give an overall VERDICT:
APPROVED or REVISE, with the top 3 things to fix if REVISE.

=== CHECKS (source of truth: .claude/skills/codex/SKILL.md — keep in sync) ===
1. Timers: no polling under 100ms, no QTimer(0). Idle polling >=500ms, active >=100ms.
2. No popup dialogs: inline CTkFrame panels only, never CTkToplevel.
3. Single config source: all config lives in src/wayfinder/config.py only.
4. Code placement: new code goes in src/wayfinder/, not root-level legacy files.
5. Canvas safety: all Tk canvas ops wrapped in try/except.
6. Frame rates preserved: overlay 15fps, waveform 30fps idle / 15fps active — never lowered.
7. Standard widgets: use CustomTkinter as-is, no custom CTk wrapper classes.
8. Whisper cleanup: extend clean_whisper_artifacts(), don't add new cleanup layers.
9. Overlay states: state changes force a repaint + honor minimum display time.
10. Process isolation: the overlay runs as a separate PyQt6 subprocess — respect that boundary.
11. Tests: the plan includes pytest coverage in tests/.
=== END CHECKS ===

PLAN:
<paste your plan here>
```

## Notes
- This is a *self-review* by the same local model — a sanity gate, not a second independent
  model. For a true second opinion from a different model, run `/codex` from **Claude Code**
  (not Cline), where the paid Codex CLI and shell capture work as designed.
- If checks 1–11 ever change, update `.claude/skills/codex/SKILL.md` and this file together.
