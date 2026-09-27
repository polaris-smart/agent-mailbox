#!/usr/bin/env bash
# verify_release.sh — 发布物抽查（分发包面），0.7.4 起随 §6 每次发版必跑。
# 判据由 HS 0.7.4 派单写死（t-35③），判据本身改动须 HS 复核：
#   sdist 内容层: grep -rEn '/U[s]ers/|inte[r]ia|/h[o]me/' 命中=0，且 wake-zc.sh 类本机薄壳=0
#   wheel  清单级: scripts/ 不出现；内容层同 sdist 判据=0
#   回归自测: 0.7.2 产物应 FAIL、0.7.3 产物应 PASS（判据有效性自证）
# 用法: scripts/verify_release.sh <wheel> <sdist> [旧wheel] [旧sdist]
#   两参模式 = 只验当版；四参模式 = 先跑 0.7.2/0.7.3 回归自测再验当版。
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }
[ $# -ge 2 ] || fail "usage: verify_release.sh <wheel> <sdist> [old_wheel] [old_sdist]"
WHEEL=$1; SDIST=$2

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

scan_pkg() { # $1=解包目录 $2=标签
    local dir=$1 tag=$2 hits
    # 零命中时 grep 退出码 1 × pipefail 会杀脚本 —— 显式 || true 兜住
    hits=$((grep -rEn '/U[s]ers/|inte[r]ia|/h[o]me/' "$dir" 2>/dev/null || true) | wc -l | tr -d ' ')
    [ "$hits" = "0" ] || fail "$tag: 本机路径/用户名命中 $hits 处（判据=0）"
    if find "$dir" -name 'wake-zc.sh' | grep -q .; then
        fail "$tag: 本机薄壳 wake-zc.sh 在包内（判据=不存在）"
    fi
    echo "  $tag: 内容层干净 ✅"
}

# —— 可选回归自测：旧产物按判据必须 FAIL ——
if [ $# -ge 4 ]; then
    OLD_WHEEL=$3; OLD_SDIST=$4
    echo "== 回归自测（判据有效性自证）=="
    T=$(mktemp -d); trap 'rm -rf "$WORK" "$T"' EXIT
    mkdir "$T/w" "$T/s"
    tar xzf "$OLD_SDIST" -C "$T/s" 2>/dev/null && \
        scan_pkg "$T/s"/* "$OLD_SDIST 旧 sdist" \
        && fail "回归自测失败：0.7.2 旧 sdist 竟通过判据（判据失效）"
    unzip -q "$OLD_WHEEL" -d "$T/w" 2>/dev/null
    if find "$T/w" -name 'scripts' -o -name 'wake-zc.sh' | grep -q .; then
        echo "  $OLD_WHEEL 旧 wheel 按判据识别为含 scripts ✅（预期 FAIL 面）"
    else
        echo "  $OLD_WHEEL 旧 wheel 清单级无 scripts（与当版同净，回归锚仅 sdist 面）"
    fi
    rm -rf "$T"
fi

# —— 当版判据 ——
echo "== 当版抽查（分发包面）=="
mkdir "$WORK/s" "$WORK/w"
tar xzf "$SDIST" -C "$WORK/s"
scan_pkg "$WORK/s"/* "$SDIST sdist"

unzip -q "$WHEEL" -d "$WORK/w"
if find "$WORK/w" -path '*scripts*' | grep -q .; then
    fail "$WHEEL wheel: 清单级出现 scripts/（判据=0）"
fi
scan_pkg "$WORK/w" "$WHEEL wheel"

echo "== PASS（分发包面）== 注意：本结论限分发包面，仓面是否清理是另一口径。"
