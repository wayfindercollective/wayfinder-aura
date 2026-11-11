#!/usr/bin/env bash
# Slack notifications for Wayfinder Aura, mirroring Wayfinder OS's
# slack-push-notify.yml so both products post to the same three channels:
#   main (and stable releases)  -> prod    (SLACK_WEBHOOK_PROD)
#   dev (and pre-releases)      -> dev     (SLACK_WEBHOOK_DEV)
#   every other branch          -> feature (SLACK_WEBHOOK_FEATURE, or
#                                           SLACK_BOT_TOKEN -> SLACK_FEATURE_CHANNEL)
#
# Events (GITHUB_EVENT_NAME):
#   push          commit list + AI summary (local LiteLLM model, then GPT-4o)
#   workflow_run  the "Release" workflow finished (tag builds publish with
#                 GITHUB_TOKEN, which never fires a `release` event)
#   release       a release published by hand (e.g. the macOS preview)
#
# DRY_RUN=1 prints the destination and message instead of posting.
# NOTIFY_GIT_DIR reads commits from another clone (the local Slack poller's
# bare mirror, scripts/slack_local_poller.py); PUSH_SHAS gives the commit
# list explicitly (a new branch: only the commits no other branch has).
# A missing webhook is a warning, not a failure: until the secrets are added
# to this repo, pushes stay green.

set -uo pipefail

EVENT="${GITHUB_EVENT_NAME:-push}"
EVENT_PATH="${GITHUB_EVENT_PATH:-/dev/null}"
REPO="${GITHUB_REPOSITORY:-wayfindercollective/wayfinder-aura}"
SERVER="${GITHUB_SERVER_URL:-https://github.com}"
DRY_RUN="${DRY_RUN:-0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GIT_ROOT="${NOTIFY_GIT_DIR:-$ROOT}"
PROMPTS="$HERE/../prompts"
PRODUCT="*Wayfinder Aura*"

ev() { jq -r "$1 // empty" "$EVENT_PATH" 2>/dev/null; }

escape_mrkdwn() {
  local raw="${1:-}" max_len="${2:-300}"
  printf '%s' "$raw" | ESCAPE_MAX_LEN="$max_len" node -e '
    const fs = require("node:fs");
    const raw = fs.readFileSync(0, "utf8");
    const maxLen = Number(process.env.ESCAPE_MAX_LEN) || 300;
    const defang = (t) =>
      t
        .replace(/\b([A-Za-z][A-Za-z0-9+.-]{1,15}:\/\/|mailto:|tel:)([^\s<>]+)/gi, (_m, s, r) => s.replace(":", "[:]") + r.replace(/\./g, "[.]"))
        .replace(/\b([A-Za-z0-9._%+-]+@)([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b/g, (_m, local, domain) => local + domain.replace(/\./g, "[.]"))
        .replace(/\bwww\.[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?:[/?#][^\s<>]*)?/gi, (m) => m.replace(/\./g, "[.]"))
        .replace(/(?<![A-Za-z0-9/_.-])\b([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})([/?#][^\s<>]*)/g, (_m, h, p) => h.replace(/\./g, "[.]") + p)
        .replace(/(?<![A-Za-z0-9/_.-])\b([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|org|net|edu|gov|mil|int|io|co|ai|xyz|info|biz|example|invalid|link|click|online|site|shop|store|top))\b(?![A-Za-z0-9/_.-])/gi, (m) => m.replace(/\./g, "[.]"));
    const cleaned = defang(raw.replace(/[\r\n\t\x00-\x1f\x7f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, maxLen))
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    const clipped = cleaned.length <= maxLen ? cleaned : cleaned.slice(0, maxLen).replace(/&[a-z0-9#]*$/i, "");
    process.stdout.write(clipped);
  '
}

escape_code() {
  local s
  s="$(escape_mrkdwn "${1:-}" "${2:-200}")"
  printf '%s' "${s//\`/\'}"
}

safe_repo_url() {  # $1 = candidate URL, $2 = fallback URL
  local u="${1:-}" fallback="${2:-}" prefix="${SERVER%/}/${REPO}/" lc
  lc="$(printf '%s' "$u" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "$u" && "${#u}" -le 1024 && "$u" == "${prefix}"* ]] \
    && [[ ! "$u" =~ [[:space:]\<\>\|\"\'\`\\[:cntrl:]] ]] \
    && [[ "$lc" != *"/../"* && "$lc" != *"/./"* && "$lc" != *"/.." && "$lc" != *"/." ]] \
    && [[ "$lc" != *"/..?"* && "$lc" != *"/.?"* && "$lc" != *"/..#"* && "$lc" != *"/.#"* ]] \
    && [[ "$lc" != *"%2e"* && "$lc" != *"%2f"* && "$lc" != *"%5c"* ]]; then
    printf '%s' "$u"
  else
    printf '%s' "$fallback"
  fi
}

sanitize_summary_mrkdwn() {
  node -e '
    const fs = require("node:fs");
    const raw = fs.readFileSync(0, "utf8");
    const defang = (t) =>
      t
        .replace(/\b([A-Za-z][A-Za-z0-9+.-]{1,15}:\/\/|mailto:|tel:)([^\s<>]+)/gi, (_m, s, r) => s.replace(":", "[:]") + r.replace(/\./g, "[.]"))
        .replace(/\b([A-Za-z0-9._%+-]+@)([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b/g, (_m, local, domain) => local + domain.replace(/\./g, "[.]"))
        .replace(/\bwww\.[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?:[/?#][^\s<>]*)?/gi, (m) => m.replace(/\./g, "[.]"))
        .replace(/(?<![A-Za-z0-9/_.-])\b([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})([/?#][^\s<>]*)/g, (_m, h, p) => h.replace(/\./g, "[.]") + p)
        .replace(/(?<![A-Za-z0-9/_.-])\b([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|org|net|edu|gov|mil|int|io|co|ai|xyz|info|biz|example|invalid|link|click|online|site|shop|store|top))\b(?![A-Za-z0-9/_.-])/gi, (m) => m.replace(/\./g, "[.]"));
    const rewritten = raw
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
      .replace(/\*\*([^*\n]+)\*\*/g, "*$1*")
      .replace(/<!(channel|here|everyone)(?:\|[^>]*)?>/gi, "@$1")
      .replace(/<!subteam\^[A-Za-z0-9]+(?:\|@?([^>]+))?>/gi, (_m, l) => "@" + (l || "subteam"))
      .replace(/<@([A-Za-z0-9]+)(?:\|([^>]+))?>/g, (_m, id, l) => "@" + (l || id))
      .replace(/<#([A-Za-z0-9]+)(?:\|([^>]+))?>/g, (_m, id, l) => "#" + (l || id));
    const out = defang(rewritten)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    process.stdout.write(out.length <= 7000 ? out : out.slice(0, 7000).replace(/&[a-z0-9#]*$/i, "") + "\n_...[truncated]_");
  '
}

platform_tag() {
  case "$1" in
    main) echo " · all platforms" ;;
    macos*|mac-*|mac/*) echo " · 🍎 macOS" ;;
    *linux*|*flatpak*|*appimage*|*steamdeck*) echo " · 🐧 Linux" ;;
    *windows*|win-*|win/*) echo " · 🪟 Windows" ;;
    *) echo "" ;;
  esac
}

# --- destination -------------------------------------------------------------
destination_for() {  # $1 = prod|dev|feature
  case "$1" in
    prod)    printf '%s' "${SLACK_WEBHOOK_PROD:-}" ;;
    dev)     printf '%s' "${SLACK_WEBHOOK_DEV:-}" ;;
    feature) printf '%s' "${SLACK_WEBHOOK_FEATURE:-}" ;;
  esac
}

post() {  # $1 = prod|dev|feature, $2 = text
  local channel="$1" text="$2" webhook payload response code feature_channel ok err
  if [ "$DRY_RUN" = "1" ]; then
    printf '=== DRY RUN -> %s ===\n%s\n' "$channel" "$text"
    return 0
  fi
  webhook="$(destination_for "$channel")"
  if [ -z "$webhook" ] && [ "$channel" = "feature" ] && [ -n "${SLACK_BOT_TOKEN:-}" ]; then
    feature_channel="${SLACK_FEATURE_CHANNEL:-C0AUYDJD362}"
    payload=$(jq -cn --arg text "$text" --arg channel "$feature_channel" '{ channel: $channel, text: $text, parse: "none", unfurl_links: false, unfurl_media: false }')
    response=$(curl -sS --connect-timeout 10 --max-time 30 -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
      -H 'Content-Type: application/json' -d "$payload" 2>&1)
    ok=$(echo "$response" | jq -r '.ok // "false"' 2>/dev/null)
    if [ "$ok" != "true" ]; then
      err=$(echo "$response" | jq -r '.error // "unknown"' 2>/dev/null)
      echo "::error::Slack bot post failed: ${err}"
      return 1
    fi
    echo "Slack response: 200"
    return 0
  fi
  if [ -z "$webhook" ]; then
    echo "::warning::SLACK_WEBHOOK_$(echo "$channel" | tr '[:lower:]' '[:upper:]') is not set on this repo; skipping the Slack post."
    return 0
  fi
  case "$webhook" in
    https://hooks.slack.com/*) ;;
    *)  # never send (or print) a value that is not a Slack webhook
      echo "::error::SLACK_WEBHOOK_$(echo "$channel" | tr '[:lower:]' '[:upper:]') is not a Slack webhook URL; not posting."
      return 1 ;;
  esac
  payload=$(jq -cn --arg text "$text" '{ text: $text, parse: "none", unfurl_links: false, unfurl_media: false }')
  response=$(curl -sS --connect-timeout 10 --max-time 30 -w $'\n%{http_code}' -X POST "$webhook" \
    -H 'Content-Type: application/json' -d "$payload" 2>&1)
  code=$(echo "$response" | tail -1)
  echo "Slack response: $code"
  if [ "$code" != "200" ]; then
    echo "::error::Slack post failed (HTTP $code): $(echo "$response" | sed '$d' | head -c 300)"
    return 1
  fi
}

# --- AI summary (same tiers as Wayfinder OS) ---------------------------------
AI_INPUT_FILE=""

discover_local_model() {
  [ -z "${LITELLM_BASE_URL:-}" ] && return 1
  local args=(-sS --connect-timeout 10 --max-time 15 -w $'\n__HTTP__%{http_code}') resp code body loaded models_resp models_code models_body model
  [ -n "${LITELLM_API_KEY:-}" ] && args+=(-H "Authorization: Bearer $LITELLM_API_KEY")
  resp=$(curl "${args[@]}" "${LITELLM_BASE_URL%/}${LITELLM_STATUS_PATH:-/omlx/status}" 2>&1) || true
  code=$(echo "$resp" | awk -F'__HTTP__' '/__HTTP__/{print $2}')
  body=$(echo "$resp" | sed '/__HTTP__/d')
  [ "$code" != "200" ] && return 1
  loaded=$(echo "$body" | jq -r '[.loaded_models[]? | select(type == "string" and length > 0)][0] // empty' 2>/dev/null)
  [ -z "$loaded" ] && return 1
  models_resp=$(curl "${args[@]}" "${LITELLM_BASE_URL%/}/v1/models" 2>&1) || true
  models_code=$(echo "$models_resp" | awk -F'__HTTP__' '/__HTTP__/{print $2}')
  models_body=$(echo "$models_resp" | sed '/__HTTP__/d')
  [ "$models_code" != "200" ] && return 1
  model=$(echo "$models_body" | jq -r --arg loaded "$loaded" '
    [.data[]?.id as $id | $id | select(type == "string" and length > 0)
      | select($loaded == $id or ($loaded | startswith($id + "-")))]
    | sort_by(length) | last // empty' 2>/dev/null)
  [ -z "$model" ] && return 1
  printf '%s' "$model"
}

chat() {  # $1 url, $2 model, $3 prompt, $4 max_time, $5 auth header value (may be empty)
  local url="$1" model="$2" prompt="$3" max_time="$4" auth="$5" payload resp code body args
  payload=$(jq -n --arg prompt "$prompt" --arg model "$model" --rawfile input "$AI_INPUT_FILE" '{
      model: $model,
      messages: [
        { role: "system", content: $prompt },
        { role: "user", content: ("Here is the push to summarize:\n\n" + $input) }
      ],
      max_tokens: 4096,
      temperature: 0.3
    }')
  args=(-sS --connect-timeout 10 --max-time "$max_time" -w $'\n__HTTP__%{http_code}' -H "Content-Type: application/json")
  [ -n "$auth" ] && args+=(-H "Authorization: Bearer $auth")
  resp=$(echo "$payload" | curl "${args[@]}" "$url" -d @- 2>&1) || true
  code=$(echo "$resp" | awk -F'__HTTP__' '/__HTTP__/{print $2}')
  body=$(echo "$resp" | sed '/__HTTP__/d')
  if [ "$code" != "200" ]; then
    echo "  [$model] HTTP ${code:-?}" >&2
    return 1
  fi
  printf '%s' "$body" | node "$HERE/extract-llm-summary.cjs"
}

summarize() {  # $1 = prompt; prints "source|||summary"
  local prompt="$1" local_model default content
  default="${LITELLM_DEFAULT_MODEL:-qwen3.8-flash-next}"
  if [ -n "${LITELLM_BASE_URL:-}" ]; then
    local_model=$(discover_local_model || true)
    [ -z "$local_model" ] && local_model="$default"
    content=$(chat "${LITELLM_BASE_URL%/}/v1/chat/completions" "$local_model" "$prompt" 600 "${LITELLM_API_KEY:-}") \
      && [ -n "$content" ] && { printf 'local:%s|||%s' "$local_model" "$content"; return 0; }
    if [ "$local_model" != "$default" ]; then
      content=$(chat "${LITELLM_BASE_URL%/}/v1/chat/completions" "$default" "$prompt" 600 "${LITELLM_API_KEY:-}") \
        && [ -n "$content" ] && { printf 'local:%s|||%s' "$default" "$content"; return 0; }
    fi
  fi
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    content=$(chat "https://api.openai.com/v1/chat/completions" "gpt-4o" "$prompt" 90 "$OPENAI_API_KEY") \
      && [ -n "$content" ] && { printf 'gpt-4o|||%s' "$content"; return 0; }
  fi
  return 1
}

# --- push --------------------------------------------------------------------
notify_push() {
  local branch before after zero="0000000000000000000000000000000000000000"
  branch="${GITHUB_REF_NAME:-$(ev '.ref' | sed 's#^refs/heads/##')}"
  before="${PUSH_BEFORE:-$(ev '.before')}"
  after="${PUSH_AFTER:-$(ev '.after')}"
  [ -z "$after" ] && after="$(git -C "$GIT_ROOT" rev-parse HEAD)"
  if [ "$after" = "$zero" ]; then
    echo "Branch ${branch} was deleted; nothing to announce."
    return 0
  fi

  local shas="${PUSH_SHAS:-}"
  if [ -z "$shas" ] && [ -n "$before" ] && [ "$before" != "$zero" ] && git -C "$GIT_ROOT" cat-file -e "${before}^{commit}" 2>/dev/null; then
    shas=$(git -C "$GIT_ROOT" rev-list --reverse "${before}..${after}" | tail -250)
  fi
  [ -z "$shas" ] && shas="$after"

  local commits="" authors="" count=0 sha short subject author safe_subject
  while IFS= read -r sha; do
    [ -z "$sha" ] && continue
    short="${sha:0:8}"
    subject=$(git -C "$GIT_ROOT" log -1 --format=%s "$sha")
    author=$(git -C "$GIT_ROOT" log -1 --format=%an "$sha" | tr '\r\n\t' '   ' | sed 's/  */ /g; s/^ //; s/ $//')
    safe_subject=$(escape_mrkdwn "$subject" 240)
    count=$((count + 1))
    commits="${commits}${count}. <${SERVER}/${REPO}/commit/${sha}|\`${short}\`> ${safe_subject}"$'\n'
    [ -n "$author" ] && authors="${authors}${author}"$'\n'
  done <<< "$shas"
  [ -z "$commits" ] && { echo "::error::No commits resolved for ${after}."; return 1; }
  local unique_authors="" author_total raw_author esc_author
  author_total=$(printf '%s' "$authors" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')
  while IFS= read -r raw_author; do
    [ -z "$raw_author" ] && continue
    esc_author=$(escape_mrkdwn "$raw_author" 60)
    [ -z "$esc_author" ] && continue
    if [ -z "$unique_authors" ]; then
      unique_authors="$esc_author"
    else
      unique_authors="${unique_authors}, ${esc_author}"
    fi
  done < <(printf '%s' "$authors" | sed '/^$/d' | sort -u | head -5)
  [ -z "$unique_authors" ] && unique_authors="unknown"
  if [ "${author_total:-0}" -gt 5 ]; then
    unique_authors="${unique_authors} +$((author_total - 5)) others"
  fi

  AI_INPUT_FILE=$(mktemp)
  {
    echo "Number of commits in this push: ${count}"
    echo ""
    if [ "$count" -le 2 ]; then
      echo "Commit messages (full body):"
      while IFS= read -r sha; do
        [ -z "$sha" ] && continue
        echo "=== commit ==="
        git -C "$GIT_ROOT" log -1 --format=%B "$sha"
      done <<< "$shas"
    else
      echo "Commit subjects (one per line):"
      while IFS= read -r sha; do
        [ -z "$sha" ] && continue
        echo "- $(git -C "$GIT_ROOT" log -1 --format=%s "$sha")"
      done <<< "$shas"
    fi
    echo ""
    echo "Largest product-source changes (tests, docs, generated files excluded):"
    if [ -n "$before" ] && [ "$before" != "$zero" ]; then
      git -C "$GIT_ROOT" diff "${before}..${after}" --numstat 2>/dev/null \
        | awk -F '\t' '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
            p=$3
            if (p !~ /(^|\/)(tests?|docs?|scripts|\.github|\.claude|dist|build)(\/|$)/ && p !~ /(\.lock$|\.json$)/) print ($1 + $2) "\t" p
          }' | sort -nr | head -60 || true
    fi
  } > "$AI_INPUT_FILE"

  local channel="feature" prompt_file="slack-engineering-summary.txt"
  if [ "$branch" = "main" ]; then
    channel="prod"
    prompt_file="slack-release-summary.txt"
  elif [ "$branch" = "dev" ]; then
    channel="dev"
    prompt_file="slack-engineering-summary.txt"
  fi

  local summary="" source="" result=""
  if [ "$DRY_RUN" != "1" ] || [ -n "${LITELLM_BASE_URL:-}${OPENAI_API_KEY:-}" ]; then
    result=$(summarize "$(<"$PROMPTS/$prompt_file")" || true)
    if [ -n "$result" ]; then
      source="${result%%|||*}"
      # Convert Markdown **bold** and escape Slack mentions/links in model output.
      summary=$(printf '%s' "${result#*|||}" | sanitize_summary_mrkdwn)
    else
      echo "No summary model answered; posting the commit list only."
    fi
  fi
  rm -f "$AI_INPUT_FILE"

  if [ "$count" -gt 15 ]; then
    commits="$(printf '%s' "$commits" | head -15)"$'\n'"_...and $((count - 15)) more commits_"$'\n'
  fi

  local header text model_tag="" safe_branch safe_model
  safe_branch=$(escape_code "$branch")
  header="${PRODUCT} · *${unique_authors}* pushed to \`${safe_branch}\`$(platform_tag "$branch")"
  case "$source" in
    local:*)
      safe_model=$(escape_mrkdwn "${source#local:}")
      model_tag="🏠 _${safe_model} (Mac Studio)_"
      ;;
    gpt-4o)  model_tag="☁️ _GPT-4o (cloud fallback)_" ;;
  esac
  text="$header"
  [ -n "$summary" ] && text="${text}"$'\n\n'"${summary}"
  [ -n "$model_tag" ] && text="${text}"$'\n'"${model_tag}"
  text="${text}"$'\n\n'"${commits}"
  if [ -n "$before" ] && [ "$before" != "$zero" ]; then
    text="${text}<${SERVER}/${REPO}/compare/${before:0:12}...${after:0:12}|View changes>"
  fi
  post "$channel" "$text"
}

# --- releases ----------------------------------------------------------------
release_assets_text() {  # $1 = release JSON
  local prefix="${SERVER%/}/${REPO}/"
  printf '%s\n' "$1" | jq -r --arg prefix "$prefix" '
    def platform($n):
      if ($n | test("\\.dmg$"; "i")) then "🍎 macOS"
      elif ($n | test("\\.(appimage|flatpak(ref)?|deb|rpm)$|linux"; "i")) then "🐧 Linux"
      elif ($n | test("\\.(exe|msi|msix)$|windows"; "i")) then "🪟 Windows"
      else empty end;
    def has_traversal($s):
      ($s | ascii_downcase) as $lc
      | ($lc | contains("\\"))
        or ($lc | contains("/../"))
        or ($lc | contains("/./"))
        or ($lc | endswith("/.."))
        or ($lc | endswith("/."))
        or ($lc | contains("/..?"))
        or ($lc | contains("/.?"))
        or ($lc | contains("/..#"))
        or ($lc | contains("/.#"))
        or ($lc | contains("%2e"))
        or ($lc | contains("%2f"))
        or ($lc | contains("%5c"));
    def safe_url($u):
      ($u | type == "string")
      and (($u | length) > 0 and ($u | length) <= 1024)
      and ($u | startswith($prefix))
      and (has_traversal($u) | not)
      and (($u | explode | any(. <= 32 or . == 34 or . == 39 or . == 60 or . == 62 or . == 92 or . == 96 or . == 124 or . == 127)) | not);
    def esc($s):
      ($s | tostring | gsub("[\\r\\n]+"; " ") | gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;") | gsub("\\|"; "/"));
    [.assets[]? | select(.name != null and safe_url(.browser_download_url))
      | {p: platform(.name), n: esc(.name), u: .browser_download_url} | select(.p != null)]
    | group_by(.p)
    | map(.[0].p + ": " + (map("<" + .u + "|" + .n + ">") | join(", ")))
    | .[]'
}

announce_release() {  # $1 = release JSON, $2 = who
  local rel="$1" who="$2" tag name url pre channel="prod" kind="released" assets text safe_name safe_who safe_url
  tag=$(printf '%s\n' "$rel" | jq -r '.tag_name')
  name=$(printf '%s\n' "$rel" | jq -r '.name // .tag_name')
  url=$(printf '%s\n' "$rel" | jq -r '.html_url')
  pre=$(printf '%s\n' "$rel" | jq -r '.prerelease')
  safe_name=$(escape_mrkdwn "$name")
  safe_who=$(escape_mrkdwn "$who")
  safe_url=$(safe_repo_url "$url" "${SERVER%/}/${REPO}/releases")
  if [ "$pre" = "true" ]; then
    channel="dev"
    kind="pre-release published"
  fi
  assets=$(release_assets_text "$rel")
  if [[ "$name" == *"Wayfinder Aura"* ]]; then
    text="🚀 *${safe_name}* ${kind}${safe_who:+ by *$safe_who*}"
  else
    text="🚀 ${PRODUCT} *${safe_name}* ${kind}${safe_who:+ by *$safe_who*}"
  fi
  [ -n "$assets" ] && text="${text}"$'\n\n'"${assets}"
  text="${text}"$'\n\n'"<${safe_url}|Release notes and downloads>"
  post "$channel" "$text"
}

fetch_release() {  # $1 = tag
  if [ -n "${RELEASE_JSON_FILE:-}" ]; then
    cat "$RELEASE_JSON_FILE"
    return 0
  fi
  gh api "repos/${REPO}/releases/tags/$1" 2>/dev/null
}

notify_release_event() {
  local action
  action=$(ev '.action')
  [ "$action" != "published" ] && { echo "Release action ${action}; nothing to announce."; return 0; }
  announce_release "$(jq '.release' "$EVENT_PATH")" "$(ev '.sender.login')"
}

notify_release_workflow() {
  local conclusion head url tag rel who safe_head safe_conclusion safe_url
  conclusion=$(ev '.workflow_run.conclusion')
  head=$(ev '.workflow_run.head_branch')
  url=$(ev '.workflow_run.html_url')
  who=$(ev '.workflow_run.actor.login')
  safe_head=$(escape_code "$head")
  safe_conclusion=$(escape_mrkdwn "$conclusion")
  safe_url=$(safe_repo_url "$url" "${SERVER%/}/${REPO}/actions")
  case "$conclusion" in
    success) ;;
    cancelled|skipped) echo "Release run ${conclusion}; nothing to announce."; return 0 ;;
    *)
      local channel="dev"
      case "$head" in v[0-9]*) [[ "$head" != *-* ]] && channel="prod" ;; esac
      post "$channel" "⚠️ ${PRODUCT} release build for \`${safe_head}\` finished with *${safe_conclusion}*. <${safe_url}|See the run>"
      return $?
      ;;
  esac
  case "$head" in
    v[0-9]*)
      tag="$head"
      rel=$(fetch_release "$tag")
      if [ -z "$rel" ]; then
        post "dev" "⚠️ ${PRODUCT} release build for \`${safe_head}\` passed, but no GitHub release was found. <${safe_url}|See the run>"
        return $?
      fi
      announce_release "$rel" "$who"
      ;;
    *)
      post "dev" "📦 ${PRODUCT} manual build on \`${safe_head}\` finished. <${safe_url}|Artifacts>"
      ;;
  esac
}

case "$EVENT" in
  push) notify_push ;;
  release) notify_release_event ;;
  workflow_run) notify_release_workflow ;;
  *) echo "Unhandled event ${EVENT}; nothing to do." ;;
esac
