#!/usr/bin/env bash
# 过一段时间执行：补历史赔率/积分榜 → 重算历史预测 → 全量评估与复盘（观察命中率是否提升）
# 用法：./scripts/periodic-backfill-eval.sh [--max-dates N]
# 建议：每周或积累一定新场次后执行一次；需配置 API_FOOTBALL_KEY，耗时会较长（API 限流）

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

MAX_DATES=15
while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-dates) MAX_DATES="${2:-15}"; shift 2 ;;
    *) shift ;;
  esac
done

LOG_DIR="${PROJECT_ROOT}/data"
LOG_FILE="${LOG_DIR}/periodic_run.log"
mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========== periodic-backfill-eval.sh 开始 (--max-dates $MAX_DATES) =========="

# 可选：按 DB 中 2024/2025/2026 比赛日期批量补赔率（首次或定期跑，耗时会较长）
# log "Step 0 (可选): odds-api-by-db --years 2024,2025,2026"
# python3 main.py odds-api-by-db --years 2024,2025,2026 2>&1 | tee -a "$LOG_FILE" || true

log "Step 1: backfill-log-support --max-dates $MAX_DATES"
if python3 main.py backfill-log-support --max-dates "$MAX_DATES" 2>&1 | tee -a "$LOG_FILE"; then
  log "Step 1 完成"
else
  log "Step 1 警告: backfill 可能部分失败（如 API 限流），继续执行重算与评估"
fi

log "Step 2: refresh-and-eval（repredict-history + eval-all）"
if python3 main.py refresh-and-eval 2>&1 | tee -a "$LOG_FILE"; then
  log "Step 2 完成"
else
  log "Step 2 警告: refresh-and-eval 可能部分失败，请查看上方输出"
  exit 1
fi

log "========== periodic-backfill-eval.sh 结束 =========="
log "可查看 data/daily_run.log 或上方输出中的 prediction-log recap 命中率。"
