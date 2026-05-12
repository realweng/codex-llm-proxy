#!/bin/bash
# Start Codex LLM Proxy (supports GLM and Kimi backends)
# Usage: ./start.sh [-p <glm|kimi>]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/tmp/codex-llm-proxy.log"
PID_FILE="/tmp/codex-llm-proxy.pid"

usage() {
    echo "Usage: $0 [-p <glm|kimi>]"
    echo "  -p <provider>  Specify backend provider (default: glm)"
    exit 1
}

# Parse options
while getopts "p:" opt; do
    case $opt in
        p) BACKEND="$OPTARG" ;;
        *) usage ;;
    esac
done

# Validate backend
BACKEND="${BACKEND:-glm}"
if [ "$BACKEND" != "glm" ] && [ "$BACKEND" != "kimi" ]; then
    echo "Error: Unsupported provider '$BACKEND'. Use 'glm' or 'kimi'."
    exit 1
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "Proxy already running (PID: $PID, backend: $BACKEND)"
        exit 0
    fi
fi

# Check for API key based on backend
if [ "$BACKEND" = "kimi" ]; then
    if [ -z "$KIMI_API_KEY" ]; then
        echo "Error: KIMI_API_KEY environment variable is not set"
        echo "Please run: export KIMI_API_KEY='your_api_key'"
        exit 1
    fi
else
    if [ -z "$GLM_API_KEY" ]; then
        echo "Error: GLM_API_KEY environment variable is not set"
        echo "Please run: export GLM_API_KEY='your_api_key'"
        exit 1
    fi
fi

# Start proxy
export BACKEND

# Snapshot ~/.codex/config.toml and rewrite it to point Codex at this proxy.
# Mirrors codex-app-transfer's autoApplyOnStart behavior. Restored by stop.sh.
python3 "$SCRIPT_DIR/codex_config.py" apply --port "${PROXY_PORT:-18765}" --backend "$BACKEND" || \
    echo "Warning: codex config apply failed (proxy will still start)" >&2

echo "Starting Codex LLM Proxy (backend: $BACKEND)..."
nohup python3 "$SCRIPT_DIR/../proxy.py" > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
sleep 1

# Verify
if curl -s http://localhost:18765/health > /dev/null 2>&1; then
    echo "✓ Proxy started successfully (PID: $PID, backend: $BACKEND)"
    echo "  Health check: http://localhost:18765/health"
    echo "  Log file: $LOG_FILE"
else
    echo "✗ Proxy failed to start. Check log: $LOG_FILE"
    exit 1
fi
