---
name: check
description: Multi-model plan review with codebase tool access. Local model R1 (free) finds issues, GPT R2 (~$0.80) validates, local model R3 (free) verifies. 3 rounds max.
---

## Scope

Reviews PLANS only. Cost: ~$0.80 per full 3-round review.

## When to use

- When the user runs `/check` during or after plan mode
- Only when explicitly invoked -- never trigger automatically

## Model routing

| Round | Model | Method | Purpose |
|-------|-------|--------|---------|
| R1 | LOCAL_MODEL (local) | LM Studio Chat Completions + tools | Find obvious issues (FREE) |
| R2 | GPT_MODEL (API) | OpenAI Responses API + tools | Serious review (~$0.80) |
| R3 | LOCAL_MODEL (local) | LM Studio Chat Completions + tools | Verify final fixes (FREE, only if R2 says REVISE) |

## API configs — EDIT THESE

### Local model (LM Studio)
- **Endpoint:** http://localhost:8080/v1/chat/completions
- **API Key:** YOUR_LM_STUDIO_API_KEY
- **Model:** YOUR_MODEL_NAME (e.g. qwen3.5-397b-a17b, llama-3.1-70b, etc.)

### GPT (OpenAI)
- **Endpoint:** https://api.openai.com/v1/responses
- **API Key:** from $OPENAI_API_KEY env var
- **Model:** gpt-5.4 (or gpt-4o, o3, etc.)

## Important: thinking mode (Qwen/DeepSeek models)

Some models have thinking/reasoning enabled. When this happens, output appears in `reasoning_content` instead of `content`. Always:
1. Set `max_tokens: 16384`
2. Check both response fields:
```python
content = msg.get('content', '') or ''
reasoning = msg.get('reasoning_content', '') or ''
output = content if content.strip() else reasoning
```

## Tool definitions

Both models get tools to explore the codebase. Claude executes tool calls locally.

### Tools (Chat Completions format — for local model)

```python
tools_chat_completions = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file from the project. Returns contents with line numbers. Max 500 lines.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path relative to project root"}, "start_line": {"type": "integer", "description": "First line (1-indexed)"}, "end_line": {"type": "integer", "description": "Last line (1-indexed)"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_directory", "description": "List files and subdirectories.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory relative to project root"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_code", "description": "Search for a regex pattern. Max 50 matches.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Regex pattern"}, "path": {"type": "string", "description": "Subdirectory to search. Defaults to root."}}, "required": ["pattern"]}}}
]
```

### Tools (Responses API format — for GPT)

```python
tools_responses_api = [
    {"type": "function", "name": "read_file", "description": "Read a file from the project. Returns contents with line numbers. Max 500 lines.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path relative to project root"}, "start_line": {"type": "integer", "description": "First line (1-indexed)"}, "end_line": {"type": "integer", "description": "Last line (1-indexed)"}}, "required": ["path"]}},
    {"type": "function", "name": "list_directory", "description": "List files and subdirectories.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory relative to project root"}}, "required": ["path"]}},
    {"type": "function", "name": "search_code", "description": "Search for a regex pattern. Max 50 matches.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Regex pattern"}, "path": {"type": "string", "description": "Subdirectory to search. Defaults to root."}}, "required": ["pattern"]}}
]
```

## Tool execution

```python
import subprocess, os, json

PROJECT_ROOT = os.getcwd()

# EDIT THIS: Add your project's file extensions
FILE_EXTENSIONS = "*.ts' --include='*.tsx' --include='*.js' --include='*.md"
# Python project: "*.py' --include='*.html' --include='*.md"
# Go project: "*.go' --include='*.mod' --include='*.md"
# Rust project: "*.rs' --include='*.toml' --include='*.md"

def execute_tool(name, arguments):
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
        safe_pattern = pattern.replace("'", "'\\''")
        cmd = f"grep -rn --include='{FILE_EXTENSIONS}' '{safe_pattern}' '{real}' | head -50"
    else:
        return f'Error: unknown tool {name}'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout or result.stderr or '(no output)'
        return output[:8000]
    except subprocess.TimeoutExpired:
        return 'Error: command timed out after 30s'
    except Exception as e:
        return f'Error: {e}'
```

## Review loop

### 1. Generate session ID

```bash
REVIEW_ID=$(uuidgen | tr '[:upper:]' '[:lower:]' | head -c 8)
```

### 2. Write the current plan to `/tmp/check-plan-${REVIEW_ID}.md`

### 3. Round 1 -- Local model (FREE) with tool calling

```python
import json, urllib.request, time

plan = open(f'/tmp/check-plan-{REVIEW_ID}.md').read()

messages = [
    {'role': 'system', 'content': '''You are a senior code reviewer.

You have tools to explore the actual codebase. USE THEM. Do not guess — read the real files.

Strategy:
1. Read the project's main config/rules file (CLAUDE.md, README.md, etc.)
2. Read the critical files mentioned in the plan
3. Verify every claim against real code
4. Be strategic — max 5 tool rounds.'''},
    {'role': 'user', 'content': f'Review this implementation plan:\n\n{plan}\n\n=== CRITICAL CHECKS ===\nEDIT: Add your project-specific safety rules here.\nExamples:\n- Auth: every endpoint needs authentication\n- Data isolation: tenant data must not leak\n- Input validation: all user input sanitized\n- Test coverage: changes must include tests\n=== END ===\n\nUse your tools to read the source files referenced in the plan. Verify everything.\n\nEnd with VERDICT: APPROVED or VERDICT: REVISE'}
]

MAX_TOOL_ROUNDS = 5
round_start = time.time()
output = ''
got_final = False

for tool_round in range(MAX_TOOL_ROUNDS):
    if time.time() - round_start > 600:
        output = '(Timed out)\n' + output
        break
    payload = json.dumps({
        'model': 'YOUR_MODEL_NAME',  # EDIT THIS
        'messages': messages,
        'tools': tools_chat_completions,
        'temperature': 0.3,
        'max_tokens': 16384
    }).encode()
    req = urllib.request.Request(
        'http://localhost:8080/v1/chat/completions',  # EDIT if different port
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer YOUR_LM_STUDIO_API_KEY'}  # EDIT
    )
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read())
    choice = result['choices'][0]
    msg = choice['message']
    messages.append(msg)

    if choice.get('finish_reason') == 'tool_calls' or msg.get('tool_calls'):
        for i, tc in enumerate(msg.get('tool_calls', [])):
            tool_id = tc.get('id', f'call_{tool_round}_{i}')
            tool_result = execute_tool(tc['function']['name'], tc['function']['arguments'])
            messages.append({'role': 'tool', 'tool_call_id': tool_id, 'content': tool_result})
        continue
    else:
        content = msg.get('content', '') or ''
        reasoning = msg.get('reasoning_content', '') or ''
        output = content if content.strip() else reasoning
        got_final = True
        break

# If all tool rounds used up, make one final call WITHOUT tools to get text response
if not got_final and not output:
    payload = json.dumps({
        'model': 'YOUR_MODEL_NAME',  # EDIT THIS
        'messages': messages + [{'role': 'user', 'content': 'You have gathered enough context. Now provide your final review with VERDICT: APPROVED or VERDICT: REVISE. Do not make any more tool calls.'}],
        'temperature': 0.3,
        'max_tokens': 16384
        # Note: no 'tools' key — forces text response
    }).encode()
    req = urllib.request.Request(
        'http://localhost:8080/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer YOUR_LM_STUDIO_API_KEY'}
    )
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read())
    msg = result['choices'][0]['message']
    content = msg.get('content', '') or ''
    reasoning = msg.get('reasoning_content', '') or ''
    output = content if content.strip() else reasoning

with open(f'/tmp/check-review-{REVIEW_ID}-r1.md', 'w') as f:
    f.write(output)
```

### 4. Check verdict and evaluate findings

Present: `## /check -- Round 1 (Local Model)`

If `VERDICT: APPROVED` -> stop.
If `VERDICT: REVISE` -> critically evaluate EVERY finding:
- **Accept** -- real issue, fixing it
- **Reject** -- incorrect/irrelevant (explain why)
- **Partially accept** -- valid concern but wrong fix

### 5. Revise the plan, address only accepted issues

### 6. Round 2 -- GPT (OpenAI Responses API, ~$0.80) with tool calling

```python
import json, urllib.request, os, time

plan = open(f'/tmp/check-plan-{REVIEW_ID}.md').read()

input_messages = [
    {'role': 'system', 'content': 'You are a senior staff engineer reviewing a revised plan. You have tools to explore the codebase. USE THEM. This plan has been through one round already. Catch what the first reviewer missed.\n\nStrategy: Read project rules first, then critical files. Max 5 tool rounds.'},
    {'role': 'user', 'content': f'Review this revised plan:\n\n{plan}\n\n=== CRITICAL CHECKS ===\nEDIT: Same checks as R1\n=== END ===\n\nUse your tools to read source files. Verify everything.\n\nEnd with VERDICT: APPROVED or VERDICT: REVISE'}
]

MAX_TOOL_ROUNDS = 5
previous_response_id = None
tool_results_input = None
output = ''

for tool_round in range(MAX_TOOL_ROUNDS):
    body = {
        'model': 'gpt-5.4',  # EDIT: or gpt-4o, o3, etc.
        'tools': tools_responses_api,
        'temperature': 0.3,
        'max_output_tokens': 8192
    }
    if previous_response_id and tool_results_input:
        body['previous_response_id'] = previous_response_id
        body['input'] = tool_results_input
    else:
        body['input'] = input_messages

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {os.environ["OPENAI_API_KEY"]}'}
    )
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    previous_response_id = result.get('id')
    tool_calls = [item for item in result.get('output', []) if item.get('type') == 'function_call']

    if tool_calls:
        tool_results_input = []
        for tc in tool_calls:
            tool_result = execute_tool(tc['name'], tc['arguments'])
            tool_results_input.append({'type': 'function_call_output', 'call_id': tc['call_id'], 'output': tool_result})
        continue
    else:
        for item in result.get('output', []):
            if item.get('type') == 'message':
                for c in item.get('content', []):
                    output += c.get('text', '')
        break

with open(f'/tmp/check-review-{REVIEW_ID}-r2.md', 'w') as f:
    f.write(output)
```

### 7. Round 3 -- Local model (FREE, final verification)

Same loop as R1 but with R2 context. Only runs if R2 says REVISE.

### 8. Final result

```
## /check Review Complete
**Status:** Approved after [N] rounds
**Models used:** Local (R1) -> GPT (R2) [-> Local (R3)]
**Tool calls:** [N] total
**Estimated cost:** ~$0.80
```

## Summary

```
R1: Claude writes plan -> Local model reviews with tool access (FREE) -> REVISE?
R2: Claude revises -> GPT reviews with tool access (~$0.80) -> REVISE?
R3: Claude revises -> Local model verifies with tool access (FREE) -> APPROVED?
```

Claude critically evaluates all feedback between rounds. This is a two-way argument, not compliance.

## Fallback: snippet mode

If tool calling fails, Claude reads critical files and includes them in the prompt directly. Less accurate but always works.

## Post-approval: test coverage gate

After approval, verify the plan includes tests. If not, add them.
