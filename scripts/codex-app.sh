#!/bin/bash
# Launch Codex Desktop App with CodexPlusPlus injection.
# Requires the proxy to already be running (./scripts/start.sh) and the
# CodexPlusPlus venv to be set up (./scripts/codex-app-setup.sh).
#
# Extra args are forwarded to `python -m codex_session_delete launch`,
# so e.g. `./scripts/codex-app.sh --debug-port 9230` works.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$REPO_ROOT/vendor/.venv/bin/python"
PROXY_PORT="${PROXY_PORT:-18765}"

# 1. Verify the proxy is running.
if ! curl -sf "http://localhost:${PROXY_PORT}/health" >/dev/null 2>&1; then
    echo "Error: proxy not responding on http://localhost:${PROXY_PORT}/health" >&2
    echo "Start it first:  ./scripts/start.sh -p glm   (or -p kimi)" >&2
    exit 1
fi

# 2. Verify the CodexPlusPlus venv is ready.
if [ ! -x "$VENV_PY" ]; then
    echo "Error: $VENV_PY not found" >&2
    echo "Run setup first:  ./scripts/codex-app-setup.sh" >&2
    exit 1
fi

# 3. Hint about the base-URL env var; do not override if the user already set it.
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -z "${OPENAI_API_BASE:-}" ]; then
    echo "Note: neither OPENAI_BASE_URL nor OPENAI_API_BASE is set."
    echo "      If Codex Desktop honors them, you may want:"
    echo "        export OPENAI_BASE_URL=http://localhost:${PROXY_PORT}/v1"
    echo "      Otherwise configure the base URL inside Codex App settings."
fi

exec "$VENV_PY" -m codex_session_delete launch "$@"
