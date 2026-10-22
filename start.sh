#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$task_root"
if [[ ! -x .venv/bin/guomi-gateway || ! -f .tools/gmssl/manifest.json ]]; then
    bash scripts/bootstrap.sh --skip-checks --prepare-native
fi
export LD_LIBRARY_PATH="$task_root/.tools/gmssl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec .venv/bin/guomi-gateway webui-all "$@"
