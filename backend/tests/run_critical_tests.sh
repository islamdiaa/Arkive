#!/usr/bin/env bash
# run_critical_tests.sh — Run critical test tiers with detailed output.
#
# Tier 0 (data integrity): Tests that prevent data loss or corruption.
#   - test_concurrent_operations.py
#   - test_api_status_fields.py
#   - test_db_dumper_failures.py
#
# Tier 1 (high priority): Tests that prevent security issues, logic bugs.
#   - test_lock_manager.py
#   - test_orchestrator_status.py
#   - test_api_auth_setup.py
#   - test_credential_redaction.py
#   - test_event_bus.py
#   - test_subprocess_runner.py
#   - test_frontend_contracts.py
#
# Usage:
#   ./run_critical_tests.sh           # Run all tiers
#   ./run_critical_tests.sh tier0     # Run tier 0 only
#   ./run_critical_tests.sh tier1     # Run tier 1 only
#
# CI mode (returns XML output):
#   CI=1 ./run_critical_tests.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BACKEND_DIR"

PYTEST_BASE_ARGS=(
    -m "not slow and not live"
    -v
    --tb=short
    --strict-markers
)

if [ "${CI:-}" = "1" ]; then
    PYTEST_BASE_ARGS+=(--junitxml=test-results/critical-tests.xml)
    mkdir -p test-results
fi

TIER="${1:-all}"
EXIT_CODE=0

run_tier() {
    local tier_name="$1"
    shift
    local files=("$@")

    echo ""
    echo "================================================================"
    echo " ${tier_name}"
    echo "================================================================"
    echo ""

    if python -m pytest "${files[@]}" "${PYTEST_BASE_ARGS[@]}"; then
        echo ""
        echo "[PASS] ${tier_name}"
    else
        echo ""
        echo "[FAIL] ${tier_name}"
        EXIT_CODE=1
    fi
}

TIER0_FILES=(
    tests/critical/test_concurrent_operations.py
    tests/critical/test_api_status_fields.py
    tests/critical/test_db_dumper_failures.py
)

TIER1_FILES=(
    tests/critical/test_lock_manager.py
    tests/critical/test_orchestrator_status.py
    tests/critical/test_api_auth_setup.py
    tests/critical/test_credential_redaction.py
    tests/critical/test_event_bus.py
    tests/critical/test_subprocess_runner.py
    tests/critical/test_frontend_contracts.py
)

echo "========================================"
echo " Arkive Critical Test Suite"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"

case "$TIER" in
    tier0)
        run_tier "TIER 0 — Data Integrity" "${TIER0_FILES[@]}"
        ;;
    tier1)
        run_tier "TIER 1 — High Priority" "${TIER1_FILES[@]}"
        ;;
    all)
        run_tier "TIER 0 — Data Integrity" "${TIER0_FILES[@]}"
        run_tier "TIER 1 — High Priority" "${TIER1_FILES[@]}"
        ;;
    *)
        echo "Usage: $0 [tier0|tier1|all]"
        exit 1
        ;;
esac

echo ""
echo "========================================"
if [ "$EXIT_CODE" -eq 0 ]; then
    echo " ALL TIERS PASSED"
else
    echo " SOME TIERS FAILED"
fi
echo "========================================"

exit "$EXIT_CODE"
