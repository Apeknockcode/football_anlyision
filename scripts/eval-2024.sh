#!/usr/bin/env bash
# 2024 年：模型预测 → 复盘/回填 → 准确率统计
# 用法：./scripts/eval-2024.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========== 1. 2024 年模型预测（对已完赛无预测的场次落库）=========="
python3 main.py backfill-predictions --years 2024

echo ""
echo "========== 2. 复盘（预测 vs 实际，比分已在 matches 中无需单独回填）=========="
python3 main.py predict-eval --year 2024
python3 main.py prediction-log recap --from-db --year 2024 --recap-limit 10

echo ""
echo "========== 3. 2024 年预测准确率统计（按联赛）=========="
python3 main.py predict-eval --year 2024 --by-competition

echo ""
echo "完成。胜平负准确率、MAE、精确比分命中率见上方输出。"
