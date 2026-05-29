#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
用法：
  bash scripts/predict_2026_timesliced.sh [--year YYYY | --dates YYYY-MM-DD,YYYY-MM-DD] [--profile PROFILE]

说明：
  - 不传参数时，默认重算 2026-01-01~2026-06-30，并评估 2026。
  - --year 与 --dates 二选一。
  - --profile 可覆盖本次运行使用的时间切片 profile（不改 .env）；profile=off 表示不应用切片（与 .env 中 off 一致）。
  - 预测过程会自动按 .env 里的 JC_TIMESLICE_PROFILE + JC_TIMESLICE_PARAMS_PATH 应用切片参数。
EOF
}

year=""
dates=""
profile=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --year)
      year="${2:-}"
      shift 2
      ;;
    --dates)
      dates="${2:-}"
      shift 2
      ;;
    --profile)
      profile="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -n "$year" && -n "$dates" ]]; then
  echo "错误：--year 与 --dates 不能同时使用。" >&2
  usage
  exit 1
fi

predict_args=(python3 main.py predict-jc-from-self-improvement --include-finished)
eval_args=(python3 main.py jc-predictions-eval --single-rec-only)
range_label=""
profile_label="${profile:-${JC_TIMESLICE_PROFILE:-from_env}}"

if [[ -n "$year" ]]; then
  predict_args+=(--year "$year")
  eval_args+=(--year "$year")
  range_label="year=$year"
elif [[ -n "$dates" ]]; then
  if [[ "$dates" != *,* ]]; then
    echo "错误：--dates 格式应为 YYYY-MM-DD,YYYY-MM-DD" >&2
    exit 1
  fi
  date_from="${dates%%,*}"
  date_to="${dates##*,}"
  predict_args+=(--dates "$dates")
  eval_args+=(--date-from "$date_from" --date-to "$date_to")
  range_label="dates=$dates"
else
  predict_args+=(--dates "2026-01-01,2026-06-30")
  eval_args+=(--year "2026")
  range_label="default(2026-01-01~2026-06-30)"
fi

echo "[1/2] 重算 ${range_label}（自动按日期应用切片参数）"
echo "      使用 profile=${profile_label}"
if [[ -n "$profile" ]]; then
  export JC_TIMESLICE_PROFILE="$profile"
fi
"${predict_args[@]}"

echo "[2/2] 输出 ${range_label} 单场推荐命中率"
"${eval_args[@]}"

echo "完成：已按正式切片配置自动重算并输出单场推荐命中率。"
