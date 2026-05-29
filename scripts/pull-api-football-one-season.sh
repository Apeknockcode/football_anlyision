#!/usr/bin/env bash
# 按「单个赛季」拉 API-FOOTBALL：射手榜 → 球队赛季统计 → 阵容 → 教练（与 main.py 子命令一致）。
# 适合配额有限时「一年一年 / 一天一季」执行，避免一次 for 循环把当天额度打光。
#
# 用法:
#   ./scripts/pull-api-football-one-season.sh 2024
#   ./scripts/pull-api-football-one-season.sh 2024 0.5
# 第二个参数为每次请求后的休眠秒数（默认 0.3）。遇限流会非 0 退出，次日对同年再跑即可断点续拉。
#
# 需配置: .env 中 MONGO_URI、API key 等（与 main.py 其它 api 子命令相同）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

usage() {
  echo "用法: $0 <赛季年> [rate_limit_sec]" >&2
  echo "示例: $0 2024" >&2
  echo "      $0 2024 0.5" >&2
  exit 1
}

[[ $# -lt 1 ]] && usage

SEASON="$1"
RATE="${2:-0.3}"

if ! [[ "$SEASON" =~ ^[0-9]{4}$ ]]; then
  echo "赛季年须为四位数字，例如 2024" >&2
  exit 1
fi

if ! [[ "$RATE" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "rate_limit 须为非负数字，例如 0.3" >&2
  exit 1
fi

echo "=========================================="
echo "赛季 ${SEASON}  |  rate-limit ${RATE}s"
echo "=========================================="

run() {
  echo ""
  echo ">>> $*"
  python3 main.py "$@"
}

run topscorers-api --season "$SEASON" --rate-limit "$RATE"
run teams-statistics-api --season "$SEASON" --rate-limit "$RATE"
run squads-api --season "$SEASON" --rate-limit "$RATE"
run coaches-api --season "$SEASON" --rate-limit "$RATE"

echo ""
echo "赛季 ${SEASON} 四项已完成。"
