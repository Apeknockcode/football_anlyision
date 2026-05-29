#!/usr/bin/env bash
# 一键用复盘结果自动调高命中率：导出细分命中率 → 用细分覆盖再评估 → 提示启用后今日推荐自动应用
# 用法：./scripts/jc-segment-auto.sh
# 启用后：在 .env 中设置 JC_REC_USE_SEGMENT_STATS=true，今日推荐将自动使用细分覆盖

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

python3 main.py jc-segment-auto
