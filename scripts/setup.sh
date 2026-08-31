#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm --prefix frontend install
.venv/bin/alembic upgrade head

echo "安装完成。运行 scripts/dev.sh 启动系统。"

