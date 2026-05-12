#!/bin/bash
# Stop Codex LLM Proxy

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/codex-llm-proxy.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "Stopping proxy (PID: $PID)..."
        kill "$PID"
        rm -f "$PID_FILE"
        echo "✓ Proxy stopped"
    else
        echo "Proxy process not running (stale PID file)"
        rm -f "$PID_FILE"
    fi
else
    # Try to find and kill by process name
    PID=$(pgrep -f "proxy.py" | head -1)
    if [ -n "$PID" ]; then
        echo "Stopping proxy (PID: $PID)..."
        kill "$PID"
        echo "✓ Proxy stopped"
    else
        echo "Proxy is not running"
    fi
fi

# Always attempt to restore ~/.codex/config.toml (idempotent — no-op if apply
# never ran or already restored).
if [ -f "$SCRIPT_DIR/codex_config.py" ]; then
    python3 "$SCRIPT_DIR/codex_config.py" restore || \
        echo "Warning: codex config restore failed; check ~/.codex-llm-proxy/codex-config.snapshot.toml" >&2
fi
