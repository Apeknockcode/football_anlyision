# Web API 接口说明

启动服务：`python -m uvicorn api.app:app --reload`，默认 `http://127.0.0.1:8000`。  
交互文档：**http://127.0.0.1:8000/docs**（Swagger UI，按标签分组）。

---

## 一、根 / 健康

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务信息与版本 `{ service, version }` |

---

## 二、预测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/predictions` | 指定日期的五大联赛场次 + prediction_log 合并展示。Query: `date_str`(YYYY-MM-DD)、`mode`(exact/until)。数据来自 DB（API/ingest）。 |
| POST | `/predictions/run` | 对指定日期的 MongoDB/DB 五大联赛场次跑模型预测，结果写入 prediction_log（及 predictions），供仪表盘展示。Query: `date_str`(YYYY-MM-DD)。 |

---

## 三、比赛

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/matches` | DB 比赛列表。Query: `competition_id`、`season_id`、`limit`(默认 50)。 |
| GET | `/match-detail` | 单场详情。Query: `match_id`。体彩详情已废弃，仅返回空 `sporttery_detail`。 |

---

## 四、报告

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/report-list` | 已有复盘报告列表。Query: `date_str` 可筛某比赛日。返回 `[{ report_id, url }]`。 |
| GET | `/reports/{report_id}` | 获取报告文件。Query: `format=md|html`。 |
| POST | `/reports/generate` | 按 DB match_id 生成复盘报告（MD+HTML）。Query: `match_id`。 |
| POST | `/reports/generate-by-fixture` | 按日期+主客队名匹配场次并生成报告。Query: `date_str`、`home_team`、`away_team`。 |

---

## 五、伤停

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/injury` | DB（match_injury）伤停列表。Query: `year`、`league`(英超/西甲/意甲/德甲/法甲)、`limit`。 |
| POST | `/injury-crawl` | 从公众号伤停文章抓图 OCR，写入 DB。Query: `date_str`、`url`。 |
| POST | `/injury-api` | 从 API-FOOTBALL 拉取伤停并写入 DB。Query: `date_str`、`merge`。需配置 `API_FOOTBALL_KEY`。 |

---

## 六、复盘

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/recap-stats` | prediction_log 复盘统计：场次、胜平负/比分命中率、MAE、按联赛拆解。 |

---

## 七、已废弃（返回 410）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/sync-sporttery` | 已废弃，数据请从 API 获取。 |
| POST | `/sync-sporttery-from-www` | 已废弃，数据请从 API 获取。 |

---

## 八、前端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dashboard` | 返回仪表盘静态页 `api/static/index.html`。 |

---

## 常用调用示例

```bash
# 今日预测列表（比赛日 = 所选日期+1）
curl "http://127.0.0.1:8000/predictions?date_str=2026-02-21"

# 复盘统计
curl "http://127.0.0.1:8000/recap-stats"

# 伤停列表（当年、英超）
curl "http://127.0.0.1:8000/injury?league=英超"

# 报告列表（某日）
curl "http://127.0.0.1:8000/report-list?date_str=2026-02-20"

# 生成复盘报告（指定 match_id）
curl -X POST "http://127.0.0.1:8000/reports/generate?match_id=123"
```
