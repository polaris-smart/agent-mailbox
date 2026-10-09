#!/usr/bin/env bash
# 本地"六道门"一键跑（CI 跑其中 #1–#5；#6 陈旧门只在本地有意义）
# 用法：bash scripts/gates.sh [--strict-staleness]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
strict_flag=0
for a in "$@"; do [ "$a" = "--strict-staleness" ] && strict_flag=1; done
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
fail=0
run() {  # run <标签> <命令…>
  local label="$1"; shift
  printf '── %s\n' "$label"
  if "$@"; then printf '   ✓ %s\n' "$label"; else printf '   ✗ %s\n' "$label"; fail=1; fi
}
run "门1 ruff check"      "$PY" -m ruff check src scripts tests
run "门2 ruff format"     "$PY" -m ruff format --check src tests
run "门3 pytest"          "$PY" -m pytest -q
run "门4 selfcheck"       "$PY" scripts/../.venv/bin/agent-mailbox selfcheck 2>/dev/null || agent-mailbox selfcheck
run "门5 卫生门"          "$PY" scripts/check-hygiene.py

# 门6 陈旧门（本地专用：CI 是全新克隆，mtime=克隆时间 ⇒ 无意义）
printf '── 门6 陈旧门（索引 vs HEAD）\n'
export STRICT_STALENESS="$strict_flag"   # 必须 export ✗ 否则子进程看不到（评审 + 我实测双证）
"$PY" - <<'PY'
import pathlib, sys
sys.path.insert(0, "src")
from agent_mailbox import workbench_cli_query as cq

root = pathlib.Path(".")
checks = [("aoci", root / ".aoci" / "baseline.json"), ("codegraph", root / ".codegraph" / "codegraph.db"),
          ("graphify", root / "graphify-out" / "graph.json")]
worst = 0.0
for name, index in checks:
    facts = cq.index_staleness(root, index)
    days = facts.get("stale_days")
    if days is None:
        print(f"   · {name}: 无索引或无法判定（不可当证据）")
        continue
    worst = max(worst, float(days))
    flag = "✗ 超阈值" if float(days) > 3 else "✓"
    print(f"   · {name}: 落后 {days} 天 {flag}")
import os
strict = os.environ.get("STRICT_STALENESS") == "1"
if worst > 3:
    print("   ⇒ 索引过旧：**不得当证据**，请刷新（aoci/codegraph）")
    sys.exit(1 if strict else 0)
print("   ✓ 陈旧门通过")
PY
[ $? -ne 0 ] && { echo "   ✗ 门6 陈旧门（strict 模式）"; fail=1; }
printf '\n== 汇总：%s ==\n' "$([ $fail -eq 0 ] && echo '全绿 ✓' || echo '有红灯 ✗')"
exit $fail
