#!/usr/bin/env bash
# Run the offline tests: every file marked `# test-kind: offline` or `both`.
# No AWS and no proxy: each file runs one at a time with AWS credentials unset and
# PROXY_URL pointing at a dead port, so a `both` file skips its live section.
# Postgres sections run only when TEST_PG_DSN is set.
#
# Usage: bash tests/offline_tests.sh [--list] [--check] [-k SUBSTR] [--kind KIND]
#   --list       print the selected files and exit
#   --check      exit 2 if any test file lacks a valid marker; no tests run
#   -k SUBSTR    only files whose path contains SUBSTR
#   --kind KIND  select KIND (offline, live, both, gated, manual) instead of
#                offline+both; for listing. Only offline/both are safe to run.
# Env: OFFLINE_LOG_DIR (default /tmp/offline_tests), OFFLINE_TEST_TIMEOUT (default 600 s per file).
# Exit: 0 all passed, 1 a file failed, 2 bad marker or usage.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=tests/_select.sh
. "$SCRIPT_DIR/_select.sh"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then PY="$REPO_ROOT/.venv/bin/python"; else PY="$(command -v python3)"; fi
LOG_DIR="${OFFLINE_LOG_DIR:-/tmp/offline_tests}"
TIMEOUT="${OFFLINE_TEST_TIMEOUT:-600}"
MODE=run; FILTER=""; KINDS=(offline both)
while [ $# -gt 0 ]; do
    case "$1" in
        --list) MODE=list ;;
        --check) MODE=check ;;
        -k) FILTER="${2:?-k needs a value}"; shift ;;
        --kind) KINDS=("${2:?--kind needs a value}"); shift ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$MODE" = check ]; then
    bad=0
    while IFS= read -r f; do
        k=$(test_kind "$f")
        case " $TEST_KINDS " in
            *" $k "*) [ -n "$k" ] || { echo "no marker:      tests/$f"; bad=$((bad+1)); } ;;
            *) echo "invalid marker: tests/$f (${k:-none})"; bad=$((bad+1)) ;;
        esac
    done < <(all_test_files)
    [ $bad -eq 0 ] && { echo "all test files carry a valid # test-kind marker"; exit 0; }
    echo "$bad file(s) need a '# test-kind: <$TEST_KINDS>' line in their first lines" >&2
    exit 2
fi

FILES=()
while IFS= read -r f; do
    [ -n "$FILTER" ] && [[ "$f" != *"$FILTER"* ]] && continue
    FILES+=("$f")
done < <(select_tests "${KINDS[@]}")
if [ "$MODE" = list ]; then
    [ ${#FILES[@]} -gt 0 ] && printf 'tests/%s\n' "${FILES[@]}"
    exit 0
fi
[ ${#FILES[@]} -gt 0 ] || { echo "no test files selected" >&2; exit 2; }

mkdir -p "$LOG_DIR"
# macOS has no timeout(1): a tiny python wrapper kills the file's process group.
RUNNER='import os,signal,subprocess,sys
t=int(sys.argv[1]); p=subprocess.Popen(sys.argv[2:],start_new_session=True)
try: sys.exit(p.wait(timeout=t))
except subprocess.TimeoutExpired:
    os.killpg(p.pid,signal.SIGKILL); p.wait(); sys.exit(124)'
SKIP_RE='\[SKIP\]|skipping live|^SKIP[ :]'
PASS=0; FAIL=0; SKIP=0; FAILED=()
echo "Offline tests: ${#FILES[@]} file(s), interpreter $PY, logs in $LOG_DIR"
for f in "${FILES[@]}"; do
    log="$LOG_DIR/${f//\//__}.log"
    if [[ "$f" == *.sh ]]; then cmd=(bash "$SCRIPT_DIR/$f"); else cmd=("$PY" "$SCRIPT_DIR/$f"); fi
    ( cd "$REPO_ROOT" && env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
        PROXY_URL=http://127.0.0.1:9 "$PY" -c "$RUNNER" "$TIMEOUT" "${cmd[@]}" ) </dev/null >"$log" 2>&1
    rc=$?
    if [ $rc -ne 0 ]; then
        FAIL=$((FAIL+1)); FAILED+=("tests/$f (exit $rc, $log)"); printf 'FAIL  tests/%s\n' "$f"
    elif grep -qE "$SKIP_RE" "$log"; then
        SKIP=$((SKIP+1)); printf 'SKIP  tests/%s (passed; a section skipped)\n' "$f"
    else
        PASS=$((PASS+1)); printf 'PASS  tests/%s\n' "$f"
    fi
done
echo "============================================"
echo "  ${#FILES[@]} files: $PASS passed, $SKIP passed with a skipped section, $FAIL failed"
if [ $FAIL -gt 0 ]; then printf '  failed: %s\n' "${FAILED[@]}"; fi
echo "============================================"
[ $FAIL -eq 0 ]
