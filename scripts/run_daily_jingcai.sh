#!/usr/bin/env bash
# 每日竞足彩预测：拉取当日五大联赛场次并写入 prediction_log。
# 建议配合 cron 在晚上 7 点执行：0 19 * * * /path/to/project/scripts/run_daily_jingcai.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DATE="$(date +%Y-%m-%d)"
python3 main.py daily-jingcai --date "$DATE"
