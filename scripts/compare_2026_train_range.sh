#!/usr/bin/env bash
# 对比「全量训练」vs「仅用 2021–2025 训练」对 2026 年命中率的影响。
# 步骤：1) 导出当前 2026 基线  2) 备份校准与 ML  3) 用 2021–2025 重训  4) 重跑 2026 预测  5) 导出新基线  6) 对比
set -e
cd "$(dirname "$0")/.."
DATA=data
BACKUP_SUFFIX=$(date +%Y%m%d_%H%M%S)

echo "========== 1) 导出当前 2026 评估基线（全量训练下的预测）=========="
python3 main.py jc-predictions-eval --year 2026 --export-baseline "$DATA/jc_eval_2026_baseline.json" || true
if [[ ! -f "$DATA/jc_eval_2026_baseline.json" ]]; then
  echo "未生成基线文件，请确保已有 2026 年 jc_predictions 与 jingcai_match_analysis 已完赛数据。"
  exit 1
fi

echo ""
echo "========== 2) 备份当前校准与 ML 模型 =========="
mkdir -p "$DATA/backup"
for f in calibration_params.json outcome_ml_model.joblib outcome_ml_model.joblib.meta.json; do
  if [[ -f "$DATA/$f" ]]; then
    cp "$DATA/$f" "$DATA/backup/${f}.${BACKUP_SUFFIX}"
    echo "已备份 $DATA/$f -> $DATA/backup/${f}.${BACKUP_SUFFIX}"
  fi
done

echo ""
echo "========== 3) 仅用 2021–2025 样本重新训练校准 =========="
python3 main.py calibrate-probs --train-date-from 2021-01-01 --train-date-to 2025-12-31
echo ""
echo "========== 4) 仅用 2021–2025 样本重新训练 ML 修正层 =========="
python3 main.py train-outcome-ml --train-date-from 2021-01-01 --train-date-to 2025-12-31

echo ""
echo "========== 5) 重跑 2026 年竞彩预测（使用新校准与 ML）=========="
python3 main.py predict-jc-from-self-improvement --year 2026 --include-finished

echo ""
echo "========== 6) 导出「仅用往年训练」后的 2026 评估基线 =========="
python3 main.py jc-predictions-eval --year 2026 --export-baseline "$DATA/jc_eval_2026_after.json"

echo ""
echo "========== 7) 对比：全量训练 vs 仅 2021–2025 训练（2026 年命中率）=========="
python3 main.py jc-eval-compare --before "$DATA/jc_eval_2026_baseline.json" --after "$DATA/jc_eval_2026_after.json"

echo ""
echo "完成。若要恢复原来的校准与 ML，可从 data/backup/*.${BACKUP_SUFFIX} 复制回 data/ 并重新跑 2026 预测。"
