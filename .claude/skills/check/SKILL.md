---
name: check
description: Hybrid plan review using local Qwen 3.5 397B and GPT (model from ~/.codex/config.toml, currently 5.6). Models get file-reading tools to explore the codebase themselves. Qwen R1 (free) finds issues, GPT R2 (~$0.80) validates, Qwen R3 (free) verifies. 3 rounds max.
---

## Scope

Reviews PLANS only. For post-implementation code review, use `/ship`.
Cost: ~$0.80 per full 3-round review (1 GPT round + 2 local Qwen rounds).

## When to use

- When the user runs `/check` during or after plan mode
- When the user wants a second opinion on a plan from a different model
- Only when explicitly invoked -- never trigger automatically

## Model routing

| Round | Model | Method | Purpose |
|-------|-------|--------|---------|
| R1 | Qwen 3.5 397B (local) | LM Studio Chat Completions + tools | Find obvious issues (FREE) |
| R2 | GPT 5.4 (API) | OpenAI Responses API + tools | Serious review (~$0.80) |
| R3 | Qwen 3.5 397B (local) | LM Studio Chat Completions + tools | Verify final fixes (FREE, only if R2 says REVISE) |

## API configs

### Qwen 3.5 (local LM Studio via LiteLLM proxy)
- **Endpoint:** http://localhost:8080/v1/chat/completions
- **API Key:** REDACTED_LOCAL_PROXY_KEY
- **Model:** qwen3.5-397b-a17b

### GPT 5.4 (OpenAI)
- **Endpoint:** https://api.openai.com/v1/responses
- **API Key:** from $OPENAI_API_KEY env var (set in ~/.zshrc)
- **Model:** gpt-5.6

## Important: Qwen thinking mode

Qwen may have thinking/reasoning enabled in LM Studio. When this happens, reasoning tokens count against `max_tokens` and the actual review appears in `reasoning_content` instead of `content`. Always:
1. Set `max_tokens: 16384` (room for ~4K thinking + ~8K review)
2. Check both response fields:
```python
content = msg.get('content', '') or ''
reasoning = msg.get('reasoning_content', '') or ''
output = content if content.strip() else reasoning
```

## Tool definitions

Both models get tools to explore the codebase. Claude executes tool calls locally and returns results.

### Tools (Chat Completions format — for Qwen)

```python
tools_chat_completions = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project. Returns file contents with line numbers. Max 500 lines without line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to project root (e.g. 'src/wayfinder/core/transcriber.py')"},
                    "start_line": {"type": "integer", "description": "First line to read (1-indexed). Omit for start of file."},
                    "end_line": {"type": "integer", "description": "Last line to read (1-indexed). Omit for end of file."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to project root (e.g. 'src/wayfinder/core')"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern in the codebase. Returns matching lines with file paths and line numbers. Max 50 matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Subdirectory to search in (relative to project root). Defaults to project root."}
                },
                "required": ["pattern"]
            }
        }
    }
]
```

### Tools (Responses API format — for GPT 5.4)

```python
tools_responses_api = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file from the project. Returns file contents with line numbers. Max 500 lines without line range.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to project root (e.g. 'src/wayfinder/core/transcriber.py')"},
                "start_line": {"type": "integer", "description": "First line to read (1-indexed). Omit for start of file."},
                "end_line": {"type": "integer", "description": "Last line to read (1-indexed). Omit for end of file."}
            },
            "required": ["path"]
        }
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": "List files and subdirectories in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to project root (e.g. 'src/wayfinder/core')"}
            },
            "required": ["path"]
        }
    },
    {
        "type": "function",
        "name": "search_code",
        "description": "Search for a regex pattern in the codebase. Returns matching lines with file paths and line numbers. Max 50 matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Subdirectory to search in (relative to project root). Defaults to project root."}
            },
            "required": ["pattern"]
        }
    }
]
```

## Tool execution

Claude executes tool calls locally. Run this Python helper for both Qwen and GPT loops:

```python
import subprocess, os, json

PROJECT_ROOT = os.getcwd()  # Should be the project root when /check is invoked

def execute_tool(name, arguments):
    """Execute a tool call locally. Returns the result string."""
    args = json.loads(arguments) if isinstance(arguments, str) else arguments

    if name == 'read_file':
        path = args['path']
        if not path.startswith('/'):
            path = os.path.join(PROJECT_ROOT, path)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(PROJECT_ROOT)):
            return 'Error: path outside project root'
        if not os.path.exists(real):
            return f'Error: file not found: {path}'
        start = args.get('start_line')
        end = args.get('end_line')
        if start and end:
            cmd = f"sed -n '{start},{end}p' '{real}' | cat -n"
        elif start:
            cmd = f"tail -n +{start} '{real}' | head -500 | cat -n"
        else:
            cmd = f"head -500 '{real}' | cat -n"

    elif name == 'list_directory':
        path = args.get('path', '')
        if not path.startswith('/'):
            path = os.path.join(PROJECT_ROOT, path)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(PROJECT_ROOT)):
            return 'Error: path outside project root'
        cmd = f"ls -la '{real}'"

    elif name == 'search_code':
        pattern = args['pattern']
        search_path = args.get('path', '')
        if not search_path.startswith('/'):
            search_path = os.path.join(PROJECT_ROOT, search_path)
        real = os.path.realpath(search_path)
        if not real.startswith(os.path.realpath(PROJECT_ROOT)):
            return 'Error: path outside project root'
        # Escape single quotes in pattern
        safe_pattern = pattern.replace("'", "'\\''")
        cmd = f"grep -rn --include='*.py' --include='*.md' --include='*.toml' --include='*.cfg' --include='*.yaml' --include='*.yml' '{safe_pattern}' '{real}' | head -50"
    else:
        return f'Error: unknown tool {name}'

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout or result.stderr or '(no output)'
        return output[:8000]  # Cap at 8K chars per tool result
    except subprocess.TimeoutExpired:
        return 'Error: command timed out after 30s'
    except Exception as e:
        return f'Error: {e}'
```

## Review loop

### 1. Generate a unique session ID

```bash
REVIEW_ID=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 8)
```

### 2. Write the current plan

Write the current implementation plan to `/tmp/check-plan-${REVIEW_ID}.md`.

If there is no plan in the current context, ask the user what they want reviewed.

### 3. Round 1 -- Qwen 3.5 (local, FREE) with tool calling

Run the Qwen tool-calling loop. The model can read files, search code, and list directories to verify the plan against real code.

```python
import json, urllib.request, time

plan = open(f'/tmp/check-plan-{REVIEW_ID}.md').read()

messages = [
    {'role': 'system', 'content': '''You are a senior code reviewer for a Linux voice dictation app (Python, CustomTkinter, PyQt6, whisper.cpp, llama.cpp).

You have tools to explore the actual codebase. USE THEM. Do not guess about code structure — read the real files.

Strategy:
1. Read CLAUDE.md from the project root to understand project rules
2. Read the critical files mentioned in the plan (the actual source code being modified)
3. Verify every claim in the plan against the real code
4. Check for timer/polling issues, config duplication, UI pattern violations

Be strategic — you have max 5 tool rounds. Read targeted sections (use start_line/end_line for large files).'''},
    {'role': 'user', 'content': f'Review this implementation plan:\n\n{plan}\n\n=== CRITICAL CHECKS ===\n- Timers: no polling/timers under 100ms (QTimer(0) caused 100% CPU)\n- Config: single source of truth in src/wayfinder/config.py only\n- UI: no popup dialogs (CTkToplevel) — use inline CTkFrame panels\n- Widgets: standard CustomTkinter only, no custom wrappers\n- Code placement: new code in src/wayfinder/, not root-level legacy files\n- Canvas safety: all Tk canvas ops wrapped in try/except\n- Overlay: state changes need forced repaint + minimum display time\n- Cleanup: whisper artifact cleanup goes through clean_whisper_artifacts() only\n- Performance: optimizations must not degrade visual quality (30fps waveform, 15fps overlay)\n- Tones: LLM prompt changes must be tested across all 5 tones at both intensities\n=== END ===\n\nUse your tools to read the actual source files referenced in the plan. Verify everything against the real code.\n\nEnd with VERDICT: APPROVED or VERDICT: REVISE'}
]

MAX_TOOL_ROUNDS = 5
ROUND_TIMEOUT = 600  # 10 minutes max (397B model is slow)
round_start = time.time()
output = ''
got_final = False

for tool_round in range(MAX_TOOL_ROUNDS):
    if time.time() - round_start > ROUND_TIMEOUT:
        output = '(Round timed out)\n' + output
        break

    payload = json.dumps({
        'model': 'qwen3.5-397b-a17b',
        'messages': messages,
        'tools': tools_chat_completions,
        'temperature': 0.3,
        'max_tokens': 16384
    }).encode()

    req = urllib.request.Request(
        'http://localhost:8080/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer REDACTED_LOCAL_PROXY_KEY'}
    )
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read())
    choice = result['choices'][0]
    msg = choice['message']

    # Append assistant message to conversation
    messages.append(msg)

    # Check if model wants to call tools
    if choice.get('finish_reason') == 'tool_calls' or msg.get('tool_calls'):
        for tc in msg.get('tool_calls', []):
            tool_id = tc.get('id', f'call_{tool_round}_{msg["tool_calls"].index(tc)}')
            tool_result = execute_tool(tc['function']['name'], tc['function']['arguments'])
            messages.append({
                'role': 'tool',
                'tool_call_id': tool_id,
                'content': tool_result
            })
        continue  # Next round
    else:
        # Model returned final text — extract output
        content = msg.get('content', '') or ''
        reasoning = msg.get('reasoning_content', '') or ''
        output = content if content.strip() else reasoning
        got_final = True
        break

# If loop exhausted all tool rounds without a final text response,
# make one more call WITHOUT tools to force a text completion
if not got_final and not output:
    payload = json.dumps({
        'model': 'qwen3.5-397b-a17b',
        'messages': messages + [{'role': 'user', 'content': 'You have gathered enough context. Now provide your final review with VERDICT: APPROVED or VERDICT: REVISE. Do not make any more tool calls.'}],
        'temperature': 0.3,
        'max_tokens': 16384
        # Note: no 'tools' key — forces text response
    }).encode()
    req = urllib.request.Request(
        'http://localhost:8080/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer REDACTED_LOCAL_PROXY_KEY'}
    )
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read())
    msg = result['choices'][0]['message']
    content = msg.get('content', '') or ''
    reasoning = msg.get('reasoning_content', '') or ''
    output = content if content.strip() else reasoning

# Save review
with open(f'/tmp/check-review-{REVIEW_ID}-r1.md', 'w') as f:
    f.write(output)
```

### 4. Read R1 review and check verdict

Present with header: `## /check -- Round 1 (Qwen 3.5 397B, local)`

If `VERDICT: APPROVED` -> stop. No need for GPT.
If `VERDICT: REVISE` -> continue to step 4b.

### 4b. Critically evaluate each finding

Before revising, evaluate EVERY finding:

1. **Is this actually correct?** Claude has filesystem access -- verify claims against the actual codebase.
2. **Is this relevant?** Valid but irrelevant findings get rejected.
3. **Is the plan already correct?** Defend the plan if it already handles the concern.

For each finding, state assessment:
- **Accept** -- "This is a real issue, fixing it."
- **Reject** -- "This is incorrect/irrelevant because..."
- **Partially accept** -- "The concern is valid but the fix is wrong."

### 5. Revise the plan

Address only accepted issues. Rewrite `/tmp/check-plan-${REVIEW_ID}.md`. Tell the user what changed and what was rejected.

### 6. Round 2 -- GPT 5.4 (OpenAI Responses API, ~$0.80) with tool calling

Send the improved plan to GPT 5.4 with tools. GPT uses the Responses API format (different from Chat Completions).

```python
import json, urllib.request, os, time

plan = open(f'/tmp/check-plan-{REVIEW_ID}.md').read()

input_messages = [
    {'role': 'system', 'content': '''You are a senior staff engineer reviewing a revised implementation plan for a Linux voice dictation app (Python, CustomTkinter, PyQt6, whisper.cpp, llama.cpp).

You have tools to explore the actual codebase. USE THEM. Do not guess about code structure — read the real files.

This plan has been through one round of review already. Catch anything the first reviewer missed.

Strategy:
1. Read CLAUDE.md to understand project rules
2. Read the critical files from the plan — verify claims against real code
3. Focus on: timer safety, config duplication, UI patterns, performance regressions
4. Be strategic — max 5 tool rounds.'''},
    {'role': 'user', 'content': f'Review this revised plan:\n\n{plan}\n\n=== CRITICAL CHECKS ===\n- Timers: no polling/timers under 100ms (QTimer(0) caused 100% CPU)\n- Config: single source of truth in src/wayfinder/config.py only\n- UI: no popup dialogs (CTkToplevel) — use inline CTkFrame panels\n- Widgets: standard CustomTkinter only, no custom wrappers\n- Code placement: new code in src/wayfinder/, not root-level legacy files\n- Canvas safety: all Tk canvas ops wrapped in try/except\n- Overlay: state changes need forced repaint + minimum display time\n- Cleanup: whisper artifact cleanup goes through clean_whisper_artifacts() only\n- Performance: optimizations must not degrade visual quality (30fps waveform, 15fps overlay)\n- Tones: LLM prompt changes must be tested across all 5 tones at both intensities\n=== END ===\n\nUse your tools to read the actual source files. Verify everything.\n\nEnd with VERDICT: APPROVED or VERDICT: REVISE'}
]

MAX_TOOL_ROUNDS = 5
ROUND_TIMEOUT = 300  # 5 minutes for GPT (generous for tool rounds)
round_start = time.time()
previous_response_id = None
tool_results_input = None
output = ''
got_final = False

for tool_round in range(MAX_TOOL_ROUNDS):
    if time.time() - round_start > ROUND_TIMEOUT:
        output = '(Round timed out)\n' + output
        break

    body = {
        "model": __import__('subprocess').check_output(['python3', __import__('os').path.expanduser('~/.codex/resolve_review_model.py')], text=True).strip(),
        'tools': tools_responses_api,
        'temperature': 0.3,
        'max_output_tokens': 8192
    }

    if previous_response_id and tool_results_input:
        # Continue from previous response — send only new tool results
        body['previous_response_id'] = previous_response_id
        body['input'] = tool_results_input
    else:
        body['input'] = input_messages

    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.environ["OPENAI_API_KEY"]}'
        }
    )
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())

    previous_response_id = result.get('id')

    # Check for tool calls in output
    tool_calls = [item for item in result.get('output', []) if item.get('type') == 'function_call']

    if tool_calls:
        tool_results_input = []
        for tc in tool_calls:
            tool_result = execute_tool(tc['name'], tc['arguments'])
            tool_results_input.append({
                'type': 'function_call_output',
                'call_id': tc['call_id'],
                'output': tool_result
            })
        continue
    else:
        # Extract final text
        for item in result.get('output', []):
            if item.get('type') == 'message':
                for c in item.get('content', []):
                    output += c.get('text', '')
        got_final = True
        break

# If loop exhausted all tool rounds, make one more call WITHOUT tools
if not got_final and not output and previous_response_id:
    body = {
        "model": __import__('subprocess').check_output(['python3', __import__('os').path.expanduser('~/.codex/resolve_review_model.py')], text=True).strip(),
        'previous_response_id': previous_response_id,
        'input': [{'role': 'user', 'content': 'You have gathered enough context. Now provide your final review with VERDICT: APPROVED or VERDICT: REVISE. Do not make any more tool calls.'}],
        'temperature': 0.3,
        'max_output_tokens': 8192
        # Note: no 'tools' key — forces text response
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {os.environ["OPENAI_API_KEY"]}'}
    )
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    for item in result.get('output', []):
        if item.get('type') == 'message':
            for c in item.get('content', []):
                output += c.get('text', '')

# Save review
with open(f'/tmp/check-review-{REVIEW_ID}-r2.md', 'w') as f:
    f.write(output)
```

Present with header: `## /check -- Round 2 (GPT 5.4, OpenAI API)`

If `VERDICT: APPROVED` -> stop.
If `VERDICT: REVISE` -> evaluate findings (step 4b), revise, continue to R3.

### 7. Round 3 -- Qwen 3.5 (local, FREE -- final verification) with tool calling

Same Qwen tool-calling loop pattern as R1, but with R2 context in the prompt:

```python
messages = [
    {'role': 'system', 'content': '''You are a senior code reviewer doing a final verification pass. You have tools to explore the actual codebase. A plan has been through two review rounds (Qwen R1 + GPT 5.4 R2). Verify fixes are correct against the real code.'''},
    {'role': 'user', 'content': f'Round 2 (GPT 5.4) raised these concerns:\n\n{r2_review}\n\n---\n\nRevised plan:\n\n{plan}\n\nVerify against the actual source code (use your tools to read files):\n1. Were R2 concerns properly addressed?\n2. Did revisions introduce new issues?\n3. Does the plan match the actual code structure?\n\nEnd with VERDICT: APPROVED or VERDICT: REVISE'}
]

# Same tool-calling loop as R1 (use Qwen endpoint, tools_chat_completions, etc.)
```

Present with: `## /check -- Round 3 (Qwen 3.5 397B, local -- final verification)`

### 8. Final result

If approved:
```
## /check Review Complete
**Status:** Approved after [N] rounds
**Models used:** Qwen 3.5 (R1) -> GPT 5.4 (R2) [-> Qwen 3.5 (R3)]
**Estimated cost:** ~$0.80
**Tool calls made:** [N] total across all rounds
```

If max rounds reached:
```
## /check Review -- Final
**Status:** Max rounds (3) reached -- not fully approved
**Remaining concerns:** [List unresolved issues]
---
**Review complete but concerns remain. Decide whether to proceed.**
```

## Summary

```
Round 1: Claude writes plan -> Qwen 3.5 reviews WITH tool access to codebase (FREE) -> REVISE?
Round 2: Claude revises -> GPT 5.4 reviews WITH tool access (~$0.80) -> REVISE?
Round 3: Claude revises -> Qwen 3.5 verifies WITH tool access (FREE) -> APPROVED?
```

Best case: Qwen approves in R1, $0.
Typical: 2-3 rounds, ~$0.80 total.
Claude critically evaluates all feedback between rounds -- this is a two-way argument, not a compliance exercise.

Key improvement: Models can now explore the codebase themselves via read_file, list_directory, and search_code tools. They no longer rely on pre-gathered snippets, so they can verify claims, check related files, and discover context that Claude might have missed.

## Post-approval: test coverage gate

After approval, Claude MUST verify the plan includes tests. If not, add them. Tests in `tests/` using pytest. Run: `python3 -m pytest tests/ -v --tb=short`.

## Fallback: snippet mode

If tool calling fails (LM Studio not running, model not loaded, 400 errors), fall back to the old approach:
1. Claude reads CLAUDE.md + critical files referenced in the plan
2. Write all gathered context to `/tmp/check-context-${REVIEW_ID}.md`
3. Include context in the prompt directly (no tools)
4. Keep total context under ~30K tokens for Qwen, ~60K for GPT

This ensures /check always works even when tool calling is unavailable.
