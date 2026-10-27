#!/usr/bin/env bash
# Instala o que falta, compila o painel e sobe o monitor em http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env

if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi

if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm install && npm run build)
fi

if [ "${1:-}" = "--demo" ]; then
  export SIMULATE=1 DB_PATH=data/demo.db
  rm -f data/demo.db
  mkdir -p data
  (cd backend && .venv/bin/python -m app.demo_seed)
fi

cd backend
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
