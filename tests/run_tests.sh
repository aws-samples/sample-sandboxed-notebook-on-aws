#!/usr/bin/env bash
# Run the live suites against a running proxy: every file marked `# test-kind: live`
# (or `both`) in any tests/ folder EXCEPT the mode-specific ones, plus those under
# the folder for the proxy's current persistence mode (tests/eternal/ or
# tests/checkpoint/). Selection is by marker, so a live file in a new folder is
# picked up without editing this script.
# Offline tests are run by tests/offline_tests.sh; gated and manual files are run
# by hand (see README › Testing).
# Requires: proxy running (local_install.sh). Port: $PROXY_PORT (default 8081).
#
# LIVE_TEST_X86=1 also re-runs the common + mode-specific tests with
# TEST_ARCH=x86_64 (real x86_64 VMs, extra cost). Default: arm64 only.
set -euo pipefail

PROXY_URL="http://localhost:${PROXY_PORT:-8081}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=tests/_select.sh
. "$SCRIPT_DIR/_select.sh"
# One interpreter for everything: the repo venv when it exists, else python3.
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    PY="$(command -v python3)"
fi
PASSED=0
FAILED=0
SKIPPED=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo "============================================"
echo "  MicroVM Test Suite"
echo "============================================"
echo ""

# Test-only dependencies (dill, plotly, ...) have one source: tests/requirements-dev.txt.
# Installed into $PY quietly, and only when an import check fails.
ensure_test_deps() {
    "$PY" -c "import dill, plotly" >/dev/null 2>&1 && return 0
    echo ">> Installing test deps into $PY from tests/requirements-dev.txt"
    if ! "$PY" -m pip install --quiet --disable-pip-version-check -r "$SCRIPT_DIR/requirements-dev.txt"; then
        echo "  ⚠ pip install failed — tests that need dill/plotly will fail"
    fi
}
echo ">> Interpreter: $PY"
ensure_test_deps
echo ""
# Check proxy
echo ">> Checking proxy..."
HEALTH=$(curl -s "$PROXY_URL/health" 2>/dev/null || echo "")
if [ -z "$HEALTH" ]; then
    echo "  ❌ Proxy not reachable at $PROXY_URL"
    echo "  Run: ./local_install.sh"
    exit 1
fi

MODE=$(echo "$HEALTH" | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('persistence_mode','unknown'))")
MAX_LIFE=$(echo "$HEALTH" | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('max_lifetime_seconds',0))")
echo "  Mode: $MODE"
echo "  Max Lifetime: ${MAX_LIFE}s"
echo ""

# $2 = optional arch suffix: logs go to /tmp/test_<name>_<suffix>.log so an
# x86_64 re-run never overwrites the default run's log.
run_test() {
    local test_file="$1"
    local suffix="${2:-}"
    local test_name=$(basename "$test_file" .py)
    local log="/tmp/test_${test_name}${suffix:+_$suffix}.log"
    echo -n "  Running $test_name${suffix:+ ($suffix)}... "
    if "$PY" "$test_file" > "$log" 2>&1; then
        echo -e "${GREEN}PASSED${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAILED${NC} (see $log)"
        FAILED=$((FAILED + 1))
    fi
}

# Common + mode-specific loops. $1 = arch suffix for labels/logs ("" = default run).
run_suite() {
    local suffix="${1:-}"
    local label="${suffix:+ ($suffix)}"
    echo ">> Common tests (mode-agnostic: every live/both file outside eternal/ and checkpoint/)$label"
    for test_file in $(TEST_EXCLUDE_DIRS="eternal checkpoint" select_tests live both); do
        run_test "$SCRIPT_DIR/$test_file" "$suffix"
    done
    echo ""

    if [ "$MODE" = "eternal" ]; then
        echo ">> Eternal mode tests$label"
        for test_file in $(TEST_DIRS=eternal select_tests live both); do
            run_test "$SCRIPT_DIR/$test_file" "$suffix"
        done
    elif [ "$MODE" = "checkpoint" ]; then
        echo ">> Checkpoint mode tests$label"
        for test_file in $(TEST_DIRS=checkpoint select_tests live both); do
            run_test "$SCRIPT_DIR/$test_file" "$suffix"
        done
    else
        echo "  ⚠ Unknown mode: $MODE — skipping mode-specific tests$label"
        SKIPPED=$((SKIPPED + 1))
    fi
}

# Default pass: no TEST_ARCH, so live tests send no "arch" and the proxy
# launches its default (arm64). Unchanged from before x86_64 support.
run_suite ""

# Opt-in x86_64 pass (LIVE_TEST_X86=1): the same loops again with
# TEST_ARCH=x86_64, so every live launch asks for (and asserts) x86_64. Off by
# default because it launches real x86_64 VMs (extra cost, roughly doubles the
# run). Results are counted separately below.
DEFAULT_PASSED=$PASSED; DEFAULT_FAILED=$FAILED; DEFAULT_SKIPPED=$SKIPPED
X86_PASSED=0; X86_FAILED=0; X86_SKIPPED=0
if [ "${LIVE_TEST_X86:-0}" = "1" ]; then
    echo ""
    PASSED=0; FAILED=0; SKIPPED=0
    export TEST_ARCH=x86_64
    run_suite "x86_64"
    unset TEST_ARCH
    X86_PASSED=$PASSED; X86_FAILED=$FAILED; X86_SKIPPED=$SKIPPED
fi
PASSED=$((DEFAULT_PASSED + X86_PASSED))
FAILED=$((DEFAULT_FAILED + X86_FAILED))
SKIPPED=$((DEFAULT_SKIPPED + X86_SKIPPED))

echo ""
echo "============================================"
if [ "${LIVE_TEST_X86:-0}" = "1" ]; then
    echo -e "  Default: ${GREEN}$DEFAULT_PASSED passed${NC}, ${RED}$DEFAULT_FAILED failed${NC}, $DEFAULT_SKIPPED skipped"
    echo -e "  x86_64:  ${GREEN}$X86_PASSED passed${NC}, ${RED}$X86_FAILED failed${NC}, $X86_SKIPPED skipped"
fi
echo -e "  Results: ${GREEN}$PASSED passed${NC}, ${RED}$FAILED failed${NC}, $SKIPPED skipped"
echo "============================================"
[ $FAILED -eq 0 ] || exit 1
