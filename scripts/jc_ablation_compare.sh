#!/usr/bin/env bash
# 竞彩对照实验：备份 artifacts → 按实验开关重算指定年份 → 评估
# 详见 docs/jc_ablation_experiments.md
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

AB_YEAR="${AB_YEAR:-2025}"
# 逗号分隔；默认只跑代理年。全量时: export JC_REPREDICT_YEARS=2021,2022,2023,2024,2025,2026
JC_REPREDICT_YEARS="${JC_REPREDICT_YEARS:-$AB_YEAR}"
CALIB_BACKUP_SRC="${CALIB_BACKUP_SRC:-data/backup/calibration_params.json.20260314_141607}"

usage() {
  cat <<EOF
用法:
  bash scripts/jc_ablation_compare.sh backup
  bash scripts/jc_ablation_compare.sh eval-full
  bash scripts/jc_ablation_compare.sh eval-proxy
  bash scripts/jc_ablation_compare.sh run <exp_name>

实验名 exp_name:
  exp_current          不重算，只 eval-proxy（当前库 + 当前 AB_YEAR）
  exp_no_calib         PREDICT_USE_CALIBRATION=false + 重算 JC_REPREDICT_YEARS + eval
  exp_no_ml            PREDICT_USE_OUTCOME_ML=false + …
  exp_no_calib_no_ml   两者 false + …
  exp_old_weights      融合权重 0.53/0.21 + 校准/ML 按当前 .env（默认 true）+ …
  exp_restore_calib_20260314  从 CALIB_BACKUP_SRC 覆盖 data/calibration_params.json 后重算 + eval

环境变量:
  AB_YEAR=2025                  eval-proxy / exp_current 的评估年
  JC_REPREDICT_YEARS=2025       或 2021,2022,...,2026
  JC_TIMESLICE_PROFILE=off      可与 .env 一致
  CALIB_BACKUP_SRC=...          恢复校准时用的源文件

示例（快速对比）:
  export AB_YEAR=2025
  bash scripts/jc_ablation_compare.sh backup
  bash scripts/jc_ablation_compare.sh run exp_no_calib
  bash scripts/jc_ablation_compare.sh run exp_old_weights
EOF
}

backup_artifacts() {
  local d="data/backup/ablation_$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$d"
  [[ -f data/calibration_params.json ]] && cp -a data/calibration_params.json "$d/"
  [[ -f data/outcome_ml_model.joblib ]] && cp -a data/outcome_ml_model.joblib "$d/"
  echo "已备份到: $d"
}

repredict_jc_years() {
  local ys="$1"
  IFS=',' read -ra _years <<< "$ys"
  for y in "${_years[@]}"; do
    y=$(echo "$y" | tr -d ' ')
    [[ -z "$y" ]] && continue
    echo "--- predict-jc-from-self-improvement --year $y ---"
    python3 main.py predict-jc-from-self-improvement --year "$y" --include-finished
  done
}

eval_proxy() {
  echo "=== jc-predictions-eval --single-rec-only --year $AB_YEAR ==="
  python3 main.py jc-predictions-eval --single-rec-only --year "$AB_YEAR"
}

eval_full() {
  echo "=== jc-predictions-eval --single-rec-only --all-time ==="
  python3 main.py jc-predictions-eval --single-rec-only --all-time
}

run_exp() {
  local name="${1:-}"
  case "$name" in
    exp_current)
      eval_proxy
      ;;
    exp_no_calib)
      export PREDICT_USE_CALIBRATION=false
      repredict_jc_years "$JC_REPREDICT_YEARS"
      eval_proxy
      ;;
    exp_no_ml)
      export PREDICT_USE_OUTCOME_ML=false
      repredict_jc_years "$JC_REPREDICT_YEARS"
      eval_proxy
      ;;
    exp_no_calib_no_ml)
      export PREDICT_USE_CALIBRATION=false
      export PREDICT_USE_OUTCOME_ML=false
      repredict_jc_years "$JC_REPREDICT_YEARS"
      eval_proxy
      ;;
    exp_old_weights)
      export PREDICT_ODDS_FUSION_WEIGHT_MODEL=0.53
      export PREDICT_API_PREDICTIONS_WEIGHT=0.21
      export PREDICT_OUTCOME_ML_WEIGHT=0.16
      repredict_jc_years "$JC_REPREDICT_YEARS"
      eval_proxy
      ;;
    exp_restore_calib_20260314)
      if [[ ! -f "$CALIB_BACKUP_SRC" ]]; then
        echo "缺少校准备份: $CALIB_BACKUP_SRC" >&2
        exit 1
      fi
      cp -a "$CALIB_BACKUP_SRC" data/calibration_params.json
      echo "已用 $CALIB_BACKUP_SRC 覆盖 data/calibration_params.json（ML 未改）"
      repredict_jc_years "$JC_REPREDICT_YEARS"
      eval_proxy
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

cmd="${1:-}"
case "$cmd" in
  backup) backup_artifacts ;;
  eval-full) eval_full ;;
  eval-proxy) eval_proxy ;;
  run) run_exp "${2:-}" ;;
  -h|--help|"") usage ;;
  *) echo "未知命令: $cmd" >&2; usage; exit 1 ;;
esac
