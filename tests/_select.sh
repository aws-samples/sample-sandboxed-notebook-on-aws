# Shared helpers for tests/offline_tests.sh and tests/run_tests.sh (sourced, not run).
#
# Every test file carries a marker in its first lines:   # test-kind: <value>
#   offline  no AWS, no proxy
#   live     needs the running local stack (run_tests.sh)
#   both     offline part runs with PROXY_URL=http://127.0.0.1:9; a live section
#            runs against a proxy
#   gated    touches AWS only when its env gate is set, otherwise skips cleanly
#   manual   benchmark or by-hand harness; no script runs it
# Requires $SCRIPT_DIR (the tests/ directory) to be set by the caller.

TEST_KINDS="offline live both gated manual"

# All files that must carry a marker (test_*.py, bench_*.py, NetworkEgress/*.py,
# the .sh test), as paths relative to tests/, sorted.
all_test_files() {
    ( cd "$SCRIPT_DIR" && find . -type f \
        \( -name 'test_*.py' -o -name 'bench_*.py' -o -name 'test_*.sh' -o -path './NetworkEgress/*.py' \) \
        -not -path './fixtures/*' -not -path '*/__pycache__/*' | sed 's|^\./||' | LC_ALL=C sort )
}

# Marker value of $1 (path relative to tests/); empty when absent.
test_kind() {
    sed -n '1,5p' "$SCRIPT_DIR/$1" | sed -n 's/^# test-kind: *\([A-Za-z]*\) *$/\1/p' | head -1
}

# Files (relative to tests/) whose kind is one of "$@", optionally under folder
# prefix $TEST_DIRS (space-separated, e.g. "common eternal"; empty = all) and
# not under any folder in $TEST_EXCLUDE_DIRS (space-separated; empty = none).
select_tests() {
    local f k want dir ok
    while IFS= read -r f; do
        k=$(test_kind "$f")
        ok=0
        for want in "$@"; do [ "$k" = "$want" ] && ok=1; done
        [ $ok -eq 1 ] || continue
        if [ -n "${TEST_DIRS:-}" ]; then
            ok=0
            for dir in $TEST_DIRS; do case "$f" in "$dir"/*) ok=1 ;; esac; done
            [ $ok -eq 1 ] || continue
        fi
        if [ -n "${TEST_EXCLUDE_DIRS:-}" ]; then
            ok=1
            for dir in $TEST_EXCLUDE_DIRS; do case "$f" in "$dir"/*) ok=0 ;; esac; done
            [ $ok -eq 1 ] || continue
        fi
        echo "$f"
    done < <(all_test_files)
}
