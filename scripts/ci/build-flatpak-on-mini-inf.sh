#!/usr/bin/env bash
# Build a trusted main/tag commit on mini-inf and copy the bundle back.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
REMOTE_HOST="mini-inf"
SOURCE_REF="origin/main"
TAG=""
OUTPUT=""
FORCE=0

usage() {
  cat >&2 <<'EOF'
Usage: build-flatpak-on-mini-inf.sh [options]

Options:
  --ref REF       Trusted commit/ref to build (default: origin/main)
  --tag vX.Y.Z    Build a release manifest from this tag
  --host HOST     SSH host alias (default: mini-inf)
  --output PATH   Local bundle destination
  --force         Replace an existing local destination
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      SOURCE_REF=$2
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      TAG=$2
      SOURCE_REF=$2
      shift 2
      ;;
    --host)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REMOTE_HOST=$2
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OUTPUT=$2
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ ! "$REMOTE_HOST" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid SSH host alias: $REMOTE_HOST" >&2
  exit 2
fi
if [[ ! "$SOURCE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
  echo "Invalid source ref: $SOURCE_REF" >&2
  exit 2
fi
if [[ -n "$TAG" && ! "$TAG" =~ ^v[0-9][0-9A-Za-z._-]*$ ]]; then
  echo "Invalid release tag: $TAG" >&2
  exit 2
fi

git -C "$REPO_ROOT" fetch --quiet origin main --tags
source_sha=$(git -C "$REPO_ROOT" rev-parse --verify "${SOURCE_REF}^{commit}")
if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$source_sha" origin/main; then
  echo "Refusing to build $SOURCE_REF: it is not contained in origin/main." >&2
  exit 1
fi

short_sha=${source_sha:0:12}
if [[ -z "$OUTPUT" ]]; then
  if [[ -n "$TAG" ]]; then
    OUTPUT="$REPO_ROOT/wayfinder-aura-${TAG#v}.flatpak"
  else
    OUTPUT="$REPO_ROOT/wayfinder-aura-$short_sha.flatpak"
  fi
fi
if [[ -e "$OUTPUT" && "$FORCE" -ne 1 ]]; then
  echo "Destination exists; pass --force to replace it: $OUTPUT" >&2
  exit 1
fi

remote_name="wayfinder-aura-$source_sha.flatpak"
remote_tag=${TAG:--}
ssh -o BatchMode=yes "$REMOTE_HOST" bash -s -- "$source_sha" "$remote_tag" "$remote_name" <<'REMOTE'
set -Eeuo pipefail

source_sha=$1
tag=$2
remote_name=$3
if [[ "$tag" == "-" ]]; then
  tag=""
fi
cache_root="$HOME/.cache/wayfinder-aura-flatpak"
mirror="$cache_root/repository.git"
worktree="$cache_root/worktrees/$source_sha"
artifact="$cache_root/artifacts/$remote_name"

mkdir -p -- "$cache_root/worktrees" "$cache_root/artifacts" "$cache_root/state"
if [[ ! -d "$mirror" ]]; then
  git clone --mirror https://github.com/wayfindercollective/wayfinder-aura.git "$mirror"
fi
git --git-dir="$mirror" fetch --quiet --prune origin \
  '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
git --git-dir="$mirror" cat-file -e "${source_sha}^{commit}"
git --git-dir="$mirror" merge-base --is-ancestor "$source_sha" refs/heads/main
git --git-dir="$mirror" worktree prune
if [[ -d "$worktree" ]]; then
  git --git-dir="$mirror" worktree remove --force "$worktree"
fi
git --git-dir="$mirror" worktree add --quiet --detach "$worktree" "$source_sha"

cleanup_worktree() {
  git --git-dir="$mirror" worktree remove --force "$worktree" >/dev/null 2>&1 || true
}
trap cleanup_worktree EXIT

build_args=(
  nice -n 10 ionice -c 3
  bash "$worktree/scripts/ci/build-flatpak-candidate.sh"
  --state-dir "$cache_root/state"
  --output "$artifact"
)
if [[ -n "$tag" ]]; then
  build_args+=(--tag "$tag")
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemd-run --user --scope --quiet \
  --property=CPUQuota=200% \
  --property=MemoryHigh=16G \
  --property=MemoryMax=24G \
  --property=TasksMax=2048 \
  "${build_args[@]}"
REMOTE

mkdir -p -- "$(dirname -- "$OUTPUT")"
if [[ "$FORCE" -eq 1 ]]; then
  rm -f -- "$OUTPUT"
fi
scp -- "$REMOTE_HOST:.cache/wayfinder-aura-flatpak/artifacts/$remote_name" "$OUTPUT"
sha256sum "$OUTPUT"
echo "Flatpak copied to $OUTPUT"
