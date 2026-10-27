#!/usr/bin/env bash
# 本地开发启动脚本（生产环境由 Railway 用 Procfile 启动）
set -euo pipefail
cd "$(dirname "$0")"
if [[ -z "${DATA_DIR:-}" ]]; then
  mkdir -p ./data
  export DATA_DIR=./data
fi
export RUN_ON_STARTUP="${RUN_ON_STARTUP:-false}"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8099}" --reload
