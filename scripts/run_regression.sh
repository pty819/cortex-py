#!/usr/bin/env bash
# 全量回归。需 DB 代理(python3 scripts/db_proxy.py)在跑。
set -uo pipefail
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
run() { local name="$1"; shift; echo "### $name ###"; if "$@"; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi; }
run "stage0-smoke"   python3 scripts/stage0/run_all.py
run "stage6-verify"  uv run python scripts/verify_stage6.py
run "stage7-verify"  uv run python scripts/verify_stage7.py
run "mcp-stdio"      uv run python scripts/verify_mcp.py
run "mcp-http"       uv run python scripts/verify_mcp_http.py
echo ""
echo "=== 回归汇总: PASS=$PASS FAIL=$FAIL ==="
exit $FAIL
