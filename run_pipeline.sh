#!/usr/bin/env bash
# Strict mode is confined to THIS child script, never the caller's SSH shell.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Optional persistent, user-owned RPC environment.  Kept outside the repo and
# loaded before Python starts so D0 never silently falls back to a public RPC.
RPC_ENV="${MDLMM_RPC_ENV:-$HOME/.config/meteora_dlmm/rpc.env}"
if [[ -f "$RPC_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RPC_ENV"
  set +a
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON="${PYTHON:-python3}"; fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m mdlmm.cli "$@" --root "$ROOT"
