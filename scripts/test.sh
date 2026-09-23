#!/usr/bin/env bash
# Run the test suite against a fresh throwaway OpenLDAP. Extra arguments go
# to pytest, e.g. ./scripts/test.sh --no-cov tests/test_members.py
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.test.yml)
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
trap '"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1' EXIT
"${compose[@]}" build -q
"${compose[@]}" run --rm tests pytest "$@"
