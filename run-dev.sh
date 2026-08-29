#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null || true
. .venv/bin/activate 2>/dev/null || true
pip install -q -r requirements.txt
export APP_ENV=dev
echo "http://127.0.0.1:8000"
python3 wsgi.py
