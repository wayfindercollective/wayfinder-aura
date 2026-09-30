#!/usr/bin/env bash
# Host provisioning owns the interpreter; jobs only create a fresh virtualenv.
set -euo pipefail

python_path="${RUNNER_TOOL_CACHE:?RUNNER_TOOL_CACHE must name the provisioned tool cache}/Python/${PYTHON_VERSION:?PYTHON_VERSION must be pinned}/arm64/bin/python3"
if [[ ! -x "$python_path" ]]; then
  echo "Missing provisioned macOS Python: $python_path. Ask the runner owner to install Python 3.12.10 and register it in this cache." >&2
  exit 1
fi
"$python_path" scripts/ci/prepare-python.py --macos
