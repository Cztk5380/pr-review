#!/usr/bin/env bash
# review.sh — 跨平台启动脚本（macOS / Linux）
# 用法：bash review.sh <PR链接或PR编号> [--owner X --repo Y] [--backend agent|api]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 优先使用虚拟环境
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "[ERROR] 未找到 Python，请先安装 Python 3 或创建虚拟环境 .venv" >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/review_draft.py" "$@"
