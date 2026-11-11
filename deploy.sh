#!/usr/bin/env bash
# 把 qq-aura 部署到 ~/.hermes/mcp-servers/qq-aura（独立 venv + 启动壳）。
# 源码留在外挂盘，部署目录只放一份跑得起来的副本，外挂盘拔掉也不影响 Hermes 启动。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${1:-$HOME/.hermes/mcp-servers/qq-aura}"
UV="${UV:-$(command -v uv || echo "$HOME/.hermes/bin/uv")}"

mkdir -p "$DST"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude 'tests' \
  --exclude '.git' --exclude '*.pyc' \
  "$SRC"/ "$DST"/

cd "$DST"
[ -d .venv ] || "$UV" venv .venv --python 3.12
"$UV" pip install -r requirements.txt --python .venv/bin/python -q

cat > run_mcp.sh <<'EOS'
#!/bin/sh
# MCP stdio 启动壳：cwd 必须是包目录上一级，-m 才能找到 aura
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m aura.mcp_server
EOS
chmod +x run_mcp.sh

echo "已部署到 $DST"
"$DST/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, ".")
from aura import __version__
from aura.config import load_config
try:
    from mcp.server.fastmcp import FastMCP  # noqa
    print("依赖 OK，版本", __version__, "配置", load_config()["_path"])
except Exception as e:
    print("依赖有问题：", e)
    sys.exit(1)
PY