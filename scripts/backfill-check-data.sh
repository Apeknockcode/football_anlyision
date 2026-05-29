#!/usr/bin/env bash
# 查看「补全数据」是否已有：自动识别当前仓库走 Mongo/SQLite，并分别统计
# 用法：./scripts/backfill-check-data.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB="${PROJECT_ROOT}/data/football.db"

echo "========== 当前仓库 DB 路由 =========="
python3 - << 'PY'
from db import repo
from config import MONGO_URI, MONGO_DB_NAME, DB_PATH

use_mongo = repo.use_mongo()
print(f"当前读写后端: {'MongoDB' if use_mongo else 'SQLite'}")
print(f"USE_MONGO 生效: {use_mongo}")
if use_mongo:
    print(f"MONGO_URI 已配置: {bool((MONGO_URI or '').strip())}")
    print(f"MONGO_DB_NAME: {MONGO_DB_NAME}")
else:
    print(f"SQLite 文件: {DB_PATH}")
PY

if [[ -f "$DB" ]]; then
  echo ""
  echo "========== SQLite: DB 中已有数据量（补全相关）=========="
  sqlite3 "$DB" << 'SQL'
.mode column
.headers on
SELECT '赔率 match_odds' AS 类型, COUNT(*) AS 总条数 FROM match_odds;
SELECT '  - 2024年' AS 类型, COUNT(*) AS 条数 FROM match_odds WHERE log_date LIKE '2024%';
SELECT '  - 2025年' AS 类型, COUNT(*) AS 条数 FROM match_odds WHERE log_date LIKE '2025%';
SELECT '积分榜 league_standings' AS 类型, COUNT(*) AS 总条数 FROM league_standings;
SELECT '  - 2024赛季' AS 类型, COUNT(*) AS 条数 FROM league_standings WHERE season = 2024;
SELECT '  - 2025赛季' AS 类型, COUNT(*) AS 条数 FROM league_standings WHERE season = 2025;
SELECT '伤停 match_injury' AS 类型, COUNT(*) AS 总条数 FROM match_injury;
SELECT '  - 2024年' AS 类型, COUNT(*) AS 条数 FROM match_injury WHERE log_date LIKE '2024%';
SELECT '比赛 matches 已完赛' AS 类型, COUNT(*) AS 总场次 FROM matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL;
SELECT '  - 2024年' AS 类型, COUNT(*) AS 场次 FROM matches WHERE strftime('%Y', match_date) = '2024' AND home_score IS NOT NULL;
SQL

  echo ""
  echo "========== SQLite: 五大联赛（英超/西甲/意甲/德甲/法甲）专项 =========="
  sqlite3 "$DB" << 'BIG5'
.mode column
.headers on
-- 五大联赛已完赛场次（主 ID：3021/3019/3014/3015/3002）
SELECT c.name AS 联赛, COUNT(m.id) AS 已完赛场次
FROM matches m
JOIN competitions c ON m.competition_id = c.id
WHERE m.home_score IS NOT NULL AND c.id IN (3021,3019,3014,3015,3002)
GROUP BY c.id, c.name ORDER BY 已完赛场次 DESC;
BIG5

  echo ""
  echo "SQLite: 赔率 match_odds 按联赛(league_cn)："
  sqlite3 "$DB" "SELECT league_cn AS 联赛, COUNT(*) AS 条数 FROM match_odds GROUP BY league_cn ORDER BY 条数 DESC;"

  echo ""
  echo "SQLite: 伤停 match_injury 按联赛："
  sqlite3 "$DB" "SELECT league_cn AS 联赛, COUNT(*) AS 条数 FROM match_injury GROUP BY league_cn ORDER BY 条数 DESC;"

  echo ""
  echo "SQLite: 积分榜 league_standings 按赛季："
  sqlite3 "$DB" "SELECT season AS 赛季, COUNT(*) AS 条数 FROM league_standings GROUP BY season ORDER BY season DESC;"
else
  echo ""
  echo "SQLite 文件不存在: $DB"
fi

echo ""
echo "========== MongoDB: DB 中已有数据量（补全相关）=========="
python3 - << 'PY'
from db import repo

if not repo.use_mongo():
    print("当前未启用 MongoDB（USE_MONGO=False 或未配置 MONGO_URI），跳过 Mongo 统计。")
    raise SystemExit(0)

db = repo._db()

print(f"赔率 match_odds: {db['match_odds'].count_documents({})}")
print(f"  - 2024年: {db['match_odds'].count_documents({'log_date': {'$regex': '^2024'}})}")
print(f"  - 2025年: {db['match_odds'].count_documents({'log_date': {'$regex': '^2025'}})}")
print(f"积分榜 league_standings: {db['league_standings'].count_documents({})}")
print(f"  - 2024赛季: {db['league_standings'].count_documents({'season': 2024})}")
print(f"  - 2025赛季: {db['league_standings'].count_documents({'season': 2025})}")
print(f"伤停 match_injury: {db['match_injury'].count_documents({})}")
print(f"  - 2024年: {db['match_injury'].count_documents({'log_date': {'$regex': '^2024'}})}")
print(
    "比赛 matches 已完赛: "
    f"{db['matches'].count_documents({'home_score': {'$ne': None}, 'away_score': {'$ne': None}})}"
)
print(
    "  - 2024年: "
    f"{db['matches'].count_documents({'match_date': {'$regex': '^2024'}, 'home_score': {'$ne': None}})}"
)

print("")
print("Mongo: 五大联赛（英超/西甲/意甲/德甲/法甲）已完赛场次：")
pipeline_big5 = [
    {'$match': {'home_score': {'$ne': None}, 'competition_id': {'$in': [3021, 3019, 3014, 3015, 3002]}}},
    {'$group': {'_id': '$competition_id', '已完赛场次': {'$sum': 1}}},
    {'$sort': {'已完赛场次': -1}},
]
for row in db['matches'].aggregate(pipeline_big5):
    print(f"  赛事ID {row['_id']}: {row['已完赛场次']}")

print("")
print("Mongo: 赔率 match_odds 按联赛(league_cn)：")
for row in db['match_odds'].aggregate([
    {'$group': {'_id': '$league_cn', '条数': {'$sum': 1}}},
    {'$sort': {'条数': -1}},
]):
    print(f"  {row.get('_id') or '(空)'}: {row['条数']}")

print("")
print("Mongo: 伤停 match_injury 按联赛：")
for row in db['match_injury'].aggregate([
    {'$group': {'_id': '$league_cn', '条数': {'$sum': 1}}},
    {'$sort': {'条数': -1}},
]):
    print(f"  {row.get('_id') or '(空)'}: {row['条数']}")

print("")
print("Mongo: 积分榜 league_standings 按赛季：")
for row in db['league_standings'].aggregate([
    {'$group': {'_id': '$season', '条数': {'$sum': 1}}},
    {'$sort': {'_id': -1}},
]):
    print(f"  {row.get('_id')}: {row['条数']}")
PY

echo ""
echo "若当前后端（Mongo/SQLite）下 2024/2025 数量已不少，说明补全数据多半已有；若为 0 或很少，可跑："
echo "  ./scripts/backfill-missing-data.sh          # 全量补（需 API_FOOTBALL_KEY、FOOTBALL_DATA_API_KEY）"
echo "  ./scripts/backfill-missing-data.sh --dry-run   # 仅看会拉哪些日期、不实际请求"
echo "  ./scripts/backfill-missing-data.sh --no-ingest # 只补赔率+积分榜+伤停，不拉历史场次"
