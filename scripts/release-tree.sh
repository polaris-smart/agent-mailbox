#!/usr/bin/env bash
# 造一棵"干净发布树"：从 HEAD 导出 + **剥离内部资料** ✓（老板「发布一定是很干净的系统」✓）
# 用法：bash scripts/release-tree.sh <outdir>   ⇒ 之后用 scripts/check-hygiene.py --all --root <outdir> 验证 ✓
set -euo pipefail
OUT="${1:?用法: bash scripts/release-tree.sh <outdir>}"
[ -e "${OUT}" ] && { echo "✗ 目标已存在（不覆盖 ✓）：${OUT}" >&2; exit 1; }
mkdir -p "${OUT}"
git -C "$(dirname "$0")/.." archive HEAD | tar -x -C "${OUT}"
# 剥离清单（**内部资料**：过程产物/评审/证据/设计稿/任务书/本机配置样例）
REMOVE=(
  "docs/reviews"        # 第 6 轮对抗评审报告（内部证据 ✓）
  "docs/archive"        # 历史归档
  "docs/evidence"       # 各版证据记录（含开发机路径 ✓）
  "docs/designs"        # 设计稿
  "docs/diagrams"       # 过程图
)
for path in "${REMOVE[@]}"; do
  if [ -e "${OUT}/$path" ]; then rm -rf "${OUT}/${path:?}"; echo "  剥离 $path ✓"; fi
done
while IFS= read -r internal; do
  [ -z "$internal" ] && continue
  [ -e "${OUT}/$internal" ] && { rm -f "${OUT}/$internal"; echo "  剥离 $internal ✓"; }
done < <(cd "${OUT}" && ls docs/*任务书* docs/*账号配置* 2>/dev/null || true)
echo "  ✓ 干净树就绪：${OUT}（文件数 $(find "${OUT}" -type f | wc -l | tr -d ' ')）"
