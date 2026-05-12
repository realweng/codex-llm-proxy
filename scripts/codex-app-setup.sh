#!/bin/bash
# One-time setup for Codex Desktop App enhancement (CodexPlusPlus).
# Creates a dedicated venv under vendor/.venv and installs the CodexPlusPlus
# submodule in editable mode. The main proxy (proxy.py) stays stdlib-only and
# is unaffected by this venv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SUBMODULE_DIR="$REPO_ROOT/vendor/CodexPlusPlus"
VENV_DIR="$REPO_ROOT/vendor/.venv"

# 1. Python 3.11+ check (upstream pyproject.toml hard-requires it)
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not found in PATH" >&2
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "Error: CodexPlusPlus requires Python 3.11+; got $PY_VERSION" >&2
    echo "Install a newer Python via brew (macOS) or pyenv, then retry." >&2
    exit 1
fi

# 2. Submodule check
if [ ! -f "$SUBMODULE_DIR/pyproject.toml" ]; then
    echo "Error: $SUBMODULE_DIR/pyproject.toml not found" >&2
    echo "Run: git submodule update --init --recursive" >&2
    exit 1
fi

# 3. Create venv (idempotent)
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    echo "Reusing existing venv at $VENV_DIR"
fi

# 4. Upgrade pip + install CodexPlusPlus in editable mode
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -e "$SUBMODULE_DIR"

# 5. Smoke test
"$VENV_DIR/bin/python" -c "import codex_session_delete; print('codex_session_delete import OK')"

cat <<EOF

Setup complete.

Next steps:
  1. Start the proxy in another terminal:
       ./scripts/start.sh -p glm    # or kimi
  2. Launch Codex Desktop with injection:
       ./scripts/codex-app.sh

EOF
