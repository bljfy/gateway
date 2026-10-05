#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_root"
task_skip_checks=false
if [[ "${1:-}" == "--skip-checks" && $# == 1 ]]; then
    task_skip_checks=true
elif [[ $# != 0 ]]; then
    printf 'Usage: bash scripts/bootstrap.sh [--skip-checks]\n' >&2
    exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    printf 'This bootstrap supports Linux x86_64 only.\n' >&2
    exit 2
fi
task_uv_version="$(tr -d '\r\n' < .uv-version)"
task_python_version="$(tr -d '\r\n' < .python-version)"
task_uv="$task_root/.tools/uv/uv"
mkdir -p .tools/uv
if [[ ! -x "$task_uv" ]]; then
    curl --fail --location --retry 3 \
        "https://github.com/astral-sh/uv/releases/download/$task_uv_version/uv-x86_64-unknown-linux-gnu.tar.gz" \
        --output .tools/uv.tar.gz
    printf '%s  %s\n' '741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf' '.tools/uv.tar.gz' | sha256sum --check -
    tar -xzf .tools/uv.tar.gz -C .tools/uv --strip-components=1
fi
task_actual_version="$("$task_uv" --version)"
if [[ "$task_actual_version" != "uv $task_uv_version "* ]]; then
    printf 'Local uv version does not match .uv-version; remove .tools/uv and rerun.\n' >&2
    exit 1
fi
export UV_CACHE_DIR="$task_root/.tools/cache"
export UV_PYTHON_INSTALL_DIR="$task_root/.tools/python"
"$task_uv" python install --no-bin "$task_python_version"
"$task_uv" sync --locked --dev
if [[ "$task_skip_checks" == false ]]; then
    if [[ ! -f .tools/gmssl/manifest.json ]]; then
        "$task_uv" run --locked python -m gateway.crypto.build_native
    fi
    export GMSSL_LIBRARY="$task_root/.tools/gmssl/lib/libgmssl.so.3.1"
    export GMSSL_SHA256
    GMSSL_SHA256="$("$task_uv" run --locked python -c 'import json; print(json.load(open(".tools/gmssl/manifest.json"))["sha256"])')"
    export LD_LIBRARY_PATH="$task_root/.tools/gmssl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    "$task_uv" run --locked ruff check src test scripts
    "$task_uv" run --locked ruff format --check src test scripts
    "$task_uv" run --locked mypy src scripts
    "$task_uv" run --locked pytest test/ -m 'not e2e'
fi
printf 'Environment ready. Use .tools/uv/uv run --locked <command> in this checkout.\n'
