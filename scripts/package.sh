#!/usr/bin/env bash
# Build YottaFlex_AI_Round3_Ethical_Collections.zip (root dir: ethical-collections-ai). Run from the repo root.
set -euo pipefail
OUT=${1:-../YottaFlex_AI_Round3_Ethical_Collections.zip}
rm -f "$OUT"
cd ..
zip -r -q "$OUT" ethical-collections-ai \
  -x '*/__pycache__/*' '*.pyc' '*/.pytest_cache/*' '*/.venv/*' '*/venv/*' '*/.git/*' '*/data/collections.db*' '*/.env' '*.egg-info/*'
unzip -tq "$OUT"
