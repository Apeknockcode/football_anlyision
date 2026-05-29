#!/usr/bin/env bash
# 补全四类数据：积分榜、伤停、赔率、历史场次与样本
# 用法：./scripts/backfill-missing-data.sh [--dry-run] [--no-odds] [--no-standings] [--no-injury] [--no-ingest]
# 需配置：API_FOOTBALL_KEY（积分榜/伤停/赔率）、FOOTBALL_DATA_API_KEY（历史场次）
# API 限频下耗时会较长，可分批执行或只开需要的步骤
#
# 数据是否已有：可先跑 ./scripts/backfill-check-data.sh 查看 DB 中已有数量；
# 再 --dry-run 看会拉取哪些日期。未配置 API key 时，赔率/积分榜/伤停可能拉不到新数据。
# 赔率步骤默认 --skip-existing：只拉 match_odds 中尚无赔率的日期，便于断点续拉（如昨日配额用完后今日继续）。

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DRY_RUN=""
SKIP_ODDS=""
SKIP_STANDINGS=""
SKIP_INJURY=""
SKIP_INGEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --no-odds)   SKIP_ODDS=1; shift ;;
    --no-standings) SKIP_STANDINGS=1; shift ;;
    --no-injury) SKIP_INJURY=1; shift ;;
    --no-ingest) SKIP_INGEST=1; shift ;;
    *) shift ;;
  esac
done

LOG_DIR="${PROJECT_ROOT}/data"
LOG_FILE="${LOG_DIR}/backfill_missing_data.log"
mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========== backfill-missing-data.sh 开始 =========="
[[ -n "$DRY_RUN" ]] && log "（仅打印/试跑，不实际拉取）"

# 1. 赔率：按 DB 比赛日补全 2024/2025/2026（--skip-existing 只拉缺失日期，断点续拉）
if [[ -z "$SKIP_ODDS" ]]; then
  log "--- 1. 赔率 odds-api-by-db --years 2024,2025,2026 --skip-existing ---"
  if [[ -n "$DRY_RUN" ]]; then
    python3 main.py odds-api-by-db --years 2024,2025,2026 --skip-existing --dry-run 2>&1 | tee -a "$LOG_FILE" || true
  else
    python3 main.py odds-api-by-db --years 2024,2025,2026 --skip-existing 2>&1 | tee -a "$LOG_FILE" || true
  fi
  log "赔率步骤结束"
else
  log "跳过赔率（--no-odds）"
fi

# 2. 积分榜：五大联赛 2024、2025 赛季（当前实现单文件，后跑的覆盖；预测主要用当前赛季，故先 2024 再 2025）
if [[ -z "$SKIP_STANDINGS" ]]; then
  log "--- 2. 积分榜 standings-api --season 2024 ---"
  [[ -z "$DRY_RUN" ]] && python3 main.py standings-api --season 2024 2>&1 | tee -a "$LOG_FILE" || true
  log "--- 2. 积分榜 standings-api --season 2025 ---"
  [[ -z "$DRY_RUN" ]] && python3 main.py standings-api --season 2025 2>&1 | tee -a "$LOG_FILE" || true
  log "积分榜步骤结束"
else
  log "跳过积分榜（--no-standings）"
fi

# 3. 伤停：按赛季断点续拉并 merge
if [[ -z "$SKIP_INJURY" ]]; then
  log "--- 3. 伤停 injury-api --season 2024 --merge ---"
  [[ -z "$DRY_RUN" ]] && python3 main.py injury-api --season 2024 --merge 2>&1 | tee -a "$LOG_FILE" || true
  log "--- 3. 伤停 injury-api --season 2025 --merge ---"
  [[ -z "$DRY_RUN" ]] && python3 main.py injury-api --season 2025 --merge 2>&1 | tee -a "$LOG_FILE" || true
  log "伤停步骤结束"
else
  log "跳过伤停（--no-injury）"
fi

# 4. 历史场次与样本：补全更多赛季（德甲/意甲/西甲/非洲杯等样本更稳）
if [[ -z "$SKIP_INGEST" ]]; then
  log "--- 4. 历史场次 ingest-fd --seasons 2021,2022,2023,2024,2025,2026 ---"
  [[ -z "$DRY_RUN" ]] && python3 main.py ingest-fd --seasons 2021,2022,2023,2024,2025,2026 2>&1 | tee -a "$LOG_FILE" || true
  log "历史场次步骤结束"
else
  log "跳过历史场次（--no-ingest）"
fi

log "========== backfill-missing-data.sh 结束 =========="
log "建议随后执行（仅用 DB）： python3 main.py repredict-history --db-only && python3 main.py predict-eval --by-competition"
