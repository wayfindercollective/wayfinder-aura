#!/usr/bin/env bash
# Slack notifications for Wayfinder Aura, mirroring Wayfinder OS's
# slack-push-notify.yml so both products post to the same channels:
#   main (and stable releases)  -> SLACK_WEBHOOK_PROD
#   every other branch / pre-release -> SLACK_WEBHOOK_DEV
#
# Events (GITHUB_EVENT_NAME):
#   push          commit list + AI summary (local LiteLLM model, then GPT-4o)
#   workflow_run  the "Release" workflow finished (tag builds publish with
#                 GITHUB_TOKEN, which never fires a `release` event)
#   release       a release published by hand (e.g. the macOS preview)
#
# DRY_RUN=1 prints the destination and message instead of posting.
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
PRODUCT="*Wayfinder Aura*"

ev() { jq -r "$1 // empty" "$EVENT_PATH" 2>/dev/null; }

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
destination_for() {  # $1 = prod|dev
  if [ "$1" = "prod" ]; then
    printf '%s' "${SLACK_WEBHOOK_PROD:-}"
  else
    printf '%s' "${SLACK_WEBHOOK_DEV:-}"
  fi
}

post() {  # $1 = prod|dev, $2 = text
  local channel="$1" text="$2" webhook payload response code
  if [ "$DRY_RUN" = "1" ]; then
    printf '=== DRY RUN -> %s ===\n%s\n' "$channel" "$text"
    return 0
  fi
  webhook="$(destination_for "$channel")"
  if [ -z "$webhook" ]; then
    echo "::warning::SLACK_WEBHOOK_$(echo "$channel" | tr '[:lower:]' '[:upper:]') is not set on this repo; skipping the Slack post."
    return 0
  fi
  payload=$(jq -n --arg text "$text" '{ text: $text }')
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
  [ -z "$after" ] && after="$(git -C "$ROOT" rev-parse HEAD)"
  if [ "$after" = "$zero" ]; then
    echo "Branch ${branch} was deleted; nothing to announce."
    return 0
  fi

  local shas=""
  if [ -n "$before" ] && [ "$before" != "$zero" ] && git -C "$ROOT" cat-file -e "${before}^{commit}" 2>/dev/null; then
    shas=$(git -C "$ROOT" rev-list --reverse "${before}..${after}" | tail -250)
  fi
  [ -z "$shas" ] && shas="$after"

  local commits="" authors="" count=0 sha short subject author
  while IFS= read -r sha; do
    [ -z "$sha" ] && continue
    short="${sha:0:8}"
    subject=$(git -C "$ROOT" log -1 --format=%s "$sha")
    author=$(git -C "$ROOT" log -1 --format=%an "$sha")
    count=$((count + 1))
    commits="${commits}${count}. <${SERVER}/${REPO}/commit/${sha}|\`${short}\`> ${subject}"$'\n'
    authors="${authors}${author}"$'\n'
  done <<< "$shas"
  [ -z "$commits" ] && { echo "::error::No commits resolved for ${after}."; return 1; }
  local unique_authors
  unique_authors=$(echo "$authors" | sed '/^$/d' | sort -u | paste -sd ',' - | sed 's/,/, /g')

  AI_INPUT_FILE=$(mktemp)
  {
    echo "Number of commits in this push: ${count}"
    echo ""
    if [ "$count" -le 2 ]; then
      echo "Commit messages (full body):"
      while IFS= read -r sha; do
        [ -z "$sha" ] && continue
        echo "=== commit ==="
        git -C "$ROOT" log -1 --format=%B "$sha"
      done <<< "$shas"
    else
      echo "Commit subjects (one per line):"
      while IFS= read -r sha; do
        [ -z "$sha" ] && continue
        echo "- $(git -C "$ROOT" log -1 --format=%s "$sha")"
      done <<< "$shas"
    fi
    echo ""
    echo "Largest product-source changes (tests, docs, generated files excluded):"
    if [ -n "$before" ] && [ "$before" != "$zero" ]; then
      git -C "$ROOT" diff "${before}..${after}" --numstat 2>/dev/null \
        | awk -F '\t' '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
            p=$3
            if (p !~ /(^|\/)(tests?|docs?|scripts|\.github|\.claude|dist|build)(\/|$)/ && p !~ /(\.lock$|\.json$)/) print ($1 + $2) "\t" p
          }' | sort -nr | head -60 || true
    fi
  } > "$AI_INPUT_FILE"

  local channel="dev" prompt_file="slack-engineering-summary.txt"
  if [ "$branch" = "main" ]; then
    channel="prod"
    prompt_file="slack-release-summary.txt"
  fi

  local summary="" source="" result=""
  if [ "$DRY_RUN" != "1" ] || [ -n "${LITELLM_BASE_URL:-}${OPENAI_API_KEY:-}" ]; then
    result=$(summarize "$(<"$ROOT/.github/prompts/$prompt_file")" || true)
    if [ -n "$result" ]; then
      source="${result%%|||*}"
      summary="${result#*|||}"
    else
      echo "No summary model answered; posting the commit list only."
    fi
  fi
  rm -f "$AI_INPUT_FILE"

  if [ "$count" -gt 15 ]; then
    commits="$(echo "$commits" | head -15)"$'\n'"_...and $((count - 15)) more commits_"$'\n'
  fi

  local header text model_tag=""
  header="${PRODUCT} · *${unique_authors}* pushed to \`${branch}\`$(platform_tag "$branch")"
  case "$source" in
    local:*) model_tag="🏠 _${source#local:} (Mac Studio)_" ;;
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
  echo "$1" | jq -r '
    def platform($n):
      if ($n | test("\\.dmg$"; "i")) then "🍎 macOS"
      elif ($n | test("\\.(appimage|flatpak(ref)?|deb|rpm)$|linux"; "i")) then "🐧 Linux"
      elif ($n | test("\\.(exe|msi|msix)$|windows"; "i")) then "🪟 Windows"
      else empty end;
    [.assets[]? | {p: platform(.name), n: .name, u: .browser_download_url} | select(.p != null)]
    | group_by(.p)
    | map(.[0].p + ": " + (map("<" + .u + "|" + .n + ">") | join(", ")))
    | .[]'
}

announce_release() {  # $1 = release JSON, $2 = who
  local rel="$1" who="$2" tag name url pre channel="prod" kind="released" assets text
  tag=$(echo "$rel" | jq -r '.tag_name')
  name=$(echo "$rel" | jq -r '.name // .tag_name')
  url=$(echo "$rel" | jq -r '.html_url')
  pre=$(echo "$rel" | jq -r '.prerelease')
  if [ "$pre" = "true" ]; then
    channel="dev"
    kind="pre-release published"
  fi
  assets=$(release_assets_text "$rel")
  if [[ "$name" == *"Wayfinder Aura"* ]]; then
    text="🚀 *${name}* ${kind}${who:+ by *$who*}"
  else
    text="🚀 ${PRODUCT} *${name}* ${kind}${who:+ by *$who*}"
  fi
  [ -n "$assets" ] && text="${text}"$'\n\n'"${assets}"
  text="${text}"$'\n\n'"<${url}|Release notes and downloads>"
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
  local conclusion head url tag rel who
  conclusion=$(ev '.workflow_run.conclusion')
  head=$(ev '.workflow_run.head_branch')
  url=$(ev '.workflow_run.html_url')
  who=$(ev '.workflow_run.actor.login')
  case "$conclusion" in
    success) ;;
    cancelled|skipped) echo "Release run ${conclusion}; nothing to announce."; return 0 ;;
    *)
      local channel="dev"
      case "$head" in v[0-9]*) [[ "$head" != *-* ]] && channel="prod" ;; esac
      post "$channel" "⚠️ ${PRODUCT} release build for \`${head}\` finished with *${conclusion}*. <${url}|See the run>"
      return $?
      ;;
  esac
  case "$head" in
    v[0-9]*)
      tag="$head"
      rel=$(fetch_release "$tag")
      if [ -z "$rel" ]; then
        post "dev" "⚠️ ${PRODUCT} release build for \`${tag}\` passed, but no GitHub release was found. <${url}|See the run>"
        return $?
      fi
      announce_release "$rel" "$who"
      ;;
    *)
      post "dev" "📦 ${PRODUCT} manual build on \`${head}\` finished. <${url}|Artifacts>"
      ;;
  esac
}

case "$EVENT" in
  push) notify_push ;;
  release) notify_release_event ;;
  workflow_run) notify_release_workflow ;;
  *) echo "Unhandled event ${EVENT}; nothing to do." ;;
esac
