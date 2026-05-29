#!/usr/bin/env bash
# ML 权重小范围网格：每次用环境变量覆盖 .env（python-dotenv 默认不覆盖已存在的环境变量），
# 再跑 refresh-and-eval，把指标追加到 data/jc_grid_runs/summary_*.tsv。
#
# 用法（在项目根目录）：
#   chmod +x scripts/jc_ml_weight_grid.sh
#   ./scripts/jc_ml_weight_grid.sh
# 自定义权重列表（空格分隔）：
#   WEIGHTS="0.15 0.16 0.17 0.18" ./scripts/jc_ml_weight_grid.sh
# 仅打印将要执行的命令、不运行：
#   DRY_RUN=1 ./scripts/jc_ml_weight_grid.sh
#
# 单场约 30～40 分钟/次，五档约 3 小时；可夜间挂机。
#
# 跑完后：用 sort -t$'\t' -k4 -nr summary.tsv 按单场推荐命中率排序；
# 将 .env 中 PREDICT_OUTCOME_ML_WEIGHT 改为最优值，再跑一次 refresh-and-eval 固化。
#
# 按联赛微调（每次只改一项，仍用本流程验证）：
#   1) python3 main.py jc-predictions-eval --all-time   # 看「按联赛」弱项
#   2) 编辑 data/predict_league_params.json（如某联赛 last_n 或 PREDICT_DRAW_BOOST_COEF）
#   3) python3 main.py refresh-and-eval
#   4) python3 scripts/extract_jc_eval_metrics.py /tmp/your.log --ml-weight note_league_tweak
#
# 数据积累后再重训：
#   python3 main.py refresh-models          # calibrate-probs + train-outcome-ml
#   ./scripts/jc_ml_weight_grid.sh          # 或固定权重后单次 refresh-and-eval

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUTDIR="$ROOT/data/jc_grid_runs"
mkdir -p "$OUTDIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
SUMMARY="$OUTDIR/summary_ml_grid_${STAMP}.tsv"
WEIGHTS="${WEIGHTS:-0.14 0.16 0.18 0.20 0.22}"
DRY_RUN="${DRY_RUN:-0}"

echo -e "ml_weight\tsingle_hit\tsingle_n\tsingle_pct\toutcome_hit\toutcome_n\toutcome_pct" >"$SUMMARY"

for w in $WEIGHTS; do
  LOG="$OUTDIR/refresh_ml_${w}_${STAMP}.log"
  echo "=========================================="
  echo "PREDICT_OUTCOME_ML_WEIGHT=$w  ->  $LOG"
  echo "=========================================="
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY_RUN: env PREDICT_OUTCOME_ML_WEIGHT=$w python3 main.py refresh-and-eval 2>&1 | tee $LOG"
    continue
  fi
  set +e
  env PREDICT_OUTCOME_ML_WEIGHT="$w" python3 main.py refresh-and-eval 2>&1 | tee "$LOG"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    echo "refresh-and-eval 退出码 $rc（权重 $w），仍尝试解析日志" >&2
  fi
  python3 "$ROOT/scripts/extract_jc_eval_metrics.py" "$LOG" --ml-weight "$w" >>"$SUMMARY" || true
done

echo ""
echo "汇总: $SUMMARY"
echo "按单场推荐命中率降序:"
{ head -1 "$SUMMARY"; tail -n +2 "$SUMMARY" | sort -t $'\t' -k4 -gr; } | head -20
