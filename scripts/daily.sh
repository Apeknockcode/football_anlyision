#!/usr/bin/env bash
# 每日必跑（日常）：① 今日竞彩预测（API 场次）  ② auto-accumulate-log（从 DB 取场次，写预测/回填比分）  ③ 伤停 API  ④ suggest-improvements
# 数据均从 API 获取（体彩网已废弃）；请确保 ingest 等已回填赛果，auto-accumulate-log 会用 DB 比分更新 prediction_log。
# 用法：./scripts/daily.sh  或  cron: 0 19 * * * /path/to/football_anlyision/scripts/daily.sh
# 伤停：不设 INJURY_DAILY_SEASON 时拉取当日伤停并 merge；设 INJURY_DAILY_SEASON=2024 时按赛季断点续拉（免费档补历史用）
# 过一段时间：补历史赔率/积分榜 + 重算与复盘见 scripts/periodic-backfill-eval.sh，详见 docs/schedule.md

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 用 Python 算昨天/今天，兼容 Mac 与 Linux
TODAY=$(python3 -c "from datetime import date; print(date.today().isoformat())")
YESTERDAY=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=1)).isoformat())")

LOG_DIR="${PROJECT_ROOT}/data"
LOG_FILE="${LOG_DIR}/daily_run.log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========== daily.sh 开始 =========="
log "今日: $TODAY  昨日: $YESTERDAY"

# 1) 今日竞彩：从 API（football-data）拉取明日比赛场次并预测
log "Step 1: 今日竞彩场次与预测 daily-jingcai --date $TODAY"
if python3 main.py daily-jingcai --date "$TODAY" 2>&1 | tee -a "$LOG_FILE"; then
    log "Step 1 完成"
else
    log "Step 1 警告: daily-jingcai 失败，尝试仅用 jingcai_fixtures 预测明日"
    TOMORROW=$(python3 -c "from datetime import date, timedelta; print((date.today() + timedelta(days=1)).isoformat())")
    python3 main.py predict-jingcai --date "$TOMORROW" 2>&1 | tee -a "$LOG_FILE" || true
fi

# 2) 日常：从 DB 取昨日/今日/明日场次，未开赛写预测、已完赛用 DB 比分回填 log（数据来自 API/ingest）
log "Step 2: auto-accumulate-log 昨日/今日/明日（从 DB 积累）"
if python3 main.py auto-accumulate-log 2>&1 | tee -a "$LOG_FILE"; then
    log "Step 2 完成"
else
    log "Step 2 说明: auto-accumulate-log 可能无新数据，继续执行"
fi

# 3) API-FOOTBALL 伤停
# 付费且已拉全量：每日仅拉当天伤停并合并（默认）。设 INJURY_DAILY_SEASON=2024 可改为按赛季断点续拉（免费档补历史用）。
if [ -n "${INJURY_DAILY_SEASON:-}" ]; then
    log "Step 3: 伤停(API) --season $INJURY_DAILY_SEASON 断点续拉"
    if python3 main.py injury-api --season "$INJURY_DAILY_SEASON" 2>&1 | tee -a "$LOG_FILE"; then
        log "Step 3 完成"
    else
        log "Step 3 说明: 伤停 API 可能已达今日限额或赛季已拉完，明日 cron 会继续"
    fi
else
    log "Step 3: 伤停(API) 当日增量 --date $TODAY --merge"
    if python3 main.py injury-api --date "$TODAY" --merge 2>&1 | tee -a "$LOG_FILE"; then
        log "Step 3 完成"
    else
        log "Step 3 说明: 当日伤停拉取可能无新数据或网络异常，不影响主流程"
    fi
fi

# 4) 后续可提升方向建议（数据积累、API 预测融合、联赛级调参）
log "Step 4: suggest-improvements"
if python3 main.py suggest-improvements 2>&1 | tee -a "$LOG_FILE"; then
    log "Step 4 完成"
else
    log "Step 4 说明: suggest-improvements 可能因 DB/路径异常未执行，不影响主流程"
fi

log "========== daily.sh 结束 =========="
