#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

run_step() {
  echo ""
  echo "==> $1"
  eval "$1"
}

echo "Starting project verification..."
run_step "./.venv/bin/python -m compileall -q backend/app"
run_step "./.venv/bin/python -c \"from backend.app.main import app; print(app.title)\""
echo ""
echo "Verification script finished."
echo "To preview locally, run:"
echo "./.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8810"
