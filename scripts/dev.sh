#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -x .venv/bin/python ]]; then
  echo "缺少 .venv，请先运行 scripts/setup.sh"
  exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
  echo "缺少前端依赖，请先运行 scripts/setup.sh"
  exit 1
fi

cleanup() {
  jobs -pr | xargs -r kill
}
trap cleanup EXIT INT TERM

.venv/bin/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 --reload &
.venv/bin/python -m app.worker &
npm --prefix frontend run dev &
wait

