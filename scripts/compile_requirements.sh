#!/usr/bin/env bash
# Compile requirements*.in with hashes inside the same Linux image the app
# runs on, so every wheel hash matches. Usage: ./scripts/compile_requirements.sh
set -euo pipefail
DOCKER="${DOCKER:-docker}"
IMAGE="python:3.14-slim-trixie"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$DOCKER" run --rm --platform linux/amd64 -v "${REPO_ROOT}:/app" -w /app "$IMAGE" bash -c "
    pip install --quiet pip-tools &&
    pip-compile --quiet --upgrade --generate-hashes --output-file=requirements.txt requirements.in &&
    pip-compile --quiet --upgrade --generate-hashes -c requirements.txt --output-file=requirements-dev.txt requirements-dev.in
"
