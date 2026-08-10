#!/usr/bin/env bash
# Deploy the Lambda with Chalice, using uv as the source of truth for deps.
#
# Chalice only knows how to read requirements.txt, so we export one from uv.lock
# right before deploying and throw it away after. Never edit requirements.txt by
# hand — it is generated and gitignored.
set -euo pipefail

cd "$(dirname "$0")/.."

# .env supplies AWS_PROFILE (and everything else) — export it for the AWS SDK.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

uv sync
uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt
trap 'rm -f requirements.txt' EXIT

uv run chalice deploy "$@"
