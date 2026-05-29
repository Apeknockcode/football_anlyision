#!/usr/bin/env bash
# 2025 年预测复盘 + 联赛调参建议：先看 2025 表现，再按建议改 data/predict_league_params.json，重算后对比
# 用法：./scripts/recap-and-tune-2025.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========== 2025 年复盘（DB 全量，按联赛）=========="
python3 main.py prediction-log recap --from-db --by-competition --year 2025 --recap-limit 50

echo ""
echo "========== 2025 年预测评估（按联赛）=========="
python3 main.py predict-eval --by-competition --year 2025

echo ""
echo "========== 联赛级调参建议（基于 2025 年）=========="
python3 main.py suggest-improvements --year 2025

echo ""
echo "========== 后续操作 =========="
echo "1. 根据上方「联赛级调参建议」编辑 data/predict_league_params.json（可调 last_n、PREDICT_DRAW_BOOST_COEF、PREDICT_HOME_ADVANTAGE 等）"
echo "2. 重算历史预测并查看新指标："
echo "   python3 main.py repredict-history"
echo "   python3 main.py predict-eval --by-competition --year 2025"
echo "   python3 main.py prediction-log recap --from-db --by-competition --year 2025"
echo "3. 若样本足够可更新校准与 ML：python3 main.py refresh-models && python3 main.py refresh-and-eval"
