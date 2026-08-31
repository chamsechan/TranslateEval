#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -x .venv/bin/python || ! -d frontend/node_modules ]]; then
  echo "依赖未安装，请先运行 scripts/setup.sh"
  exit 1
fi

npm --prefix frontend run build
.venv/bin/alembic upgrade head

cleanup() {
  jobs -pr | xargs -r kill
}
trap cleanup EXIT INT TERM

.venv/bin/python -m app.worker &
.venv/bin/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port "${TRANSLATION_EVAL_PORT:-8000}" &
wait

