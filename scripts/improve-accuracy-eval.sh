#!/usr/bin/env bash
# 调参或补数据后，重算历史预测并输出准确率（全量 + 2024 + 按联赛），便于对比是否提升。
# 用法：./scripts/improve-accuracy-eval.sh
# 可选：PREDICT_DRAW_BOOST_COEF=0.22 ./scripts/improve-accuracy-eval.sh  （先试 0.22 再对比）

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========== 1. 用当前参数重算历史预测 =========="
python3 main.py repredict-history

echo ""
echo "========== 2. 全量评估 =========="
python3 main.py predict-eval

echo ""
echo "========== 3. 2024 年评估 =========="
python3 main.py predict-eval --year 2024

echo ""
echo "========== 4. 2024 年按联赛 =========="
python3 main.py predict-eval --year 2024 --by-competition

echo ""
echo "完成。若刚补全数据或希望更新校准/ML，可再跑："
echo "  python3 main.py refresh-models && python3 main.py refresh-and-eval"
echo "调参建议：python3 main.py suggest-improvements --year 2024"
