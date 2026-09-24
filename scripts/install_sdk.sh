#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v node >/dev/null
command -v npm >/dev/null
"$ROOT/run_pipeline.sh" source-sync
cd "$ROOT/scripts/sdk"
# First install creates package-lock; subsequent runs use exact transitive lock.
if [[ -f package-lock.json ]]; then npm ci --ignore-scripts; else npm install --ignore-scripts; fi
npm ls --json > installed_dependencies.json
