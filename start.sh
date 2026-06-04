#!/bin/bash
# PolyBot Startup Script
# Handles: stale Tor, port conflicts, tmux, and Linux/macOS keep-awake when available.
# Usage: ./start.sh

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="polybot"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TOR_DATA_DIR="$HOME/.tor/data"

if command -v caffeinate >/dev/null 2>&1; then
    RUN_CMD="caffeinate -i $PYTHON_BIN bot.py"
else
    RUN_CMD="$PYTHON_BIN bot.py"
fi

echo "🚀 PolyBot Startup"
echo "══════════════════════════════════"
echo "Project: $PROJ_DIR"
echo "Python:  $PYTHON_BIN"

# 1. Kill stale Tor processes and clear cached data
echo "🧹 Cleaning up stale Tor processes..."
if pgrep -x tor > /dev/null 2>&1; then
    sudo pkill tor 2>/dev/null || pkill -9 tor 2>/dev/null || echo "   ⚠️  Couldn't kill tor (may need sudo)"
    sleep 2
    echo "   ✓ Tor processes killed"
else
    echo "   ✓ No stale Tor processes"
fi
rm -rf "$TOR_DATA_DIR"
echo "   ✓ Tor data cache cleared: $TOR_DATA_DIR"

# 2. Free port 9050 if occupied
if command -v lsof >/dev/null 2>&1 && lsof -i :9050 > /dev/null 2>&1; then
    echo "🔌 Port 9050 in use, killing..."
    lsof -ti :9050 | xargs kill -9 2>/dev/null || true
    sleep 2
    echo "   ✓ Port 9050 freed"
else
    echo "   ✓ Port 9050 clear"
fi

# 3. Kill existing tmux session if running
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "🔄 Killing existing tmux session '$SESSION_NAME'..."
    tmux kill-session -t "$SESSION_NAME"
    echo "   ✓ Old session killed"
else
    echo "   ✓ No existing tmux session"
fi

# 4. Launch tmux with bot
echo "🖥  Starting tmux session '$SESSION_NAME'..."
tmux new-session -d -s "$SESSION_NAME" -c "$PROJ_DIR" \
    "$RUN_CMD; echo '⚠️  Bot exited. Press enter to close.'; read"

echo ""
echo "══════════════════════════════════"
echo "✅ PolyBot running in tmux"
echo ""
echo "   Attach:  tmux attach -t $SESSION_NAME"
echo "   Detach:  Ctrl+B then D"
echo "   Stop:    tmux attach, then Ctrl+C"
echo "   Status:  tmux ls"
echo "══════════════════════════════════"
