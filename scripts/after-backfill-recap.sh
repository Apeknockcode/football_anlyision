#!/usr/bin/env bash
# backfill-log-support 完成后执行：重算历史预测 + 复盘（总览 + 按联赛）
# 用法：./scripts/after-backfill-recap.sh
# 建议先跑：python main.py backfill-log-support [--max-dates 15]

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========== 重算历史预测 =========="
python3 main.py repredict-history

echo ""
echo "========== 复盘（总览）=========="
python3 main.py prediction-log recap

echo ""
echo "========== 复盘（按联赛）=========="
python3 main.py prediction-log recap --by-competition

echo ""
echo "完成。可对比上述胜平负准确率与 MAE。"
