# API-Football 接口与使用说明

本文档汇总项目对 **API-Football（api-sports.io）** 的接口使用情况，便于后续扩展与维护。  
- **已请求**：路径、参数、代码位置、预测用途  
- **未请求**：接口列表、说明、对预测的潜力  
- **后续使用**：优先级、实现要点、相关命令索引  

请求统一通过 `etl/injury_api_football._request(path, params)` 发起，base URL 见 `config.API_FOOTBALL_BASE`，鉴权为 `x-apisports-key`。

---

## 一、项目已请求的接口

以下路径均在 `etl/` 中实际调用过。

| 接口路径 | 常用参数 | 调用位置（etl/） | 预测/业务用途 |
|----------|----------|------------------|----------------|
| **fixtures** | `date` / `id` / `league`+`season` | injury_api_football, api_football_odds, api_football_match_ingest | 赛程、fixture_id 关联、按日/按联赛拉场次 |
| **fixtures/lineups** | `fixture` | api_football_lineups | 阵容 → match_lineups，复盘/关键缺阵分析（不参与当次预测） |
| **fixtures/statistics** | `fixture` | api_football_statistics | 单场统计 → xG 代理，PREDICT_USE_XG 时与期望进球混合 |
| **fixtures/headtohead** | `h2h`(team1-team2)、可选 `league`/`season` | api_football_h2h | 历史交锋 → h2h-backfill，与近期期望进球融合 |
| **predictions** | `fixture` | api_football_predictions | API 预测概率 → 与规则模型概率融合 |
| **odds** | `fixture` 或 `date`+`league`+`season` | api_football_odds | 赔率 → match_odds，隐含概率与模型融合 |
| **standings** | `league`, `season` | api_football_standings | 积分榜 → 主客排名，PREDICT_STANDINGS_NUDGE 微调期望进球 |
| **injuries** | `league`, `season` | injury_api_football | 伤停 → match_injury，缺阵/关键球员 → 期望进球下调 |
| **fixtures/events** | `fixture` | api_football_events | 单场事件 → api_football_events，复盘/xG 汇总或预测特征；命令 `events-by-jingcai` |

**相关命令（main.py）**：  
`ingest-api-history` / `jc-match-analysis-backfill-fixture-id`（fixtures）、`lineups-by-jingcai`（lineups）、`statistics-api`（statistics）、`events-by-jingcai`（fixtures/events）、`h2h-backfill`（headtohead）、`fill-api-predictions`（predictions）、`odds-api` / `odds-api-by-db`（odds）、`standings-api` / `standings-backfill`（standings）、`injury-api` / `injury-api-by-db`（injuries）。

---

## 二、未请求过的接口（按类别）

以下为 API-Football 文档中存在、**项目尚未调用**的接口，按类别整理便于后续选用。

### 2.1 比赛/场次

（暂无：**fixtures/events** 已接入，见「一、已请求的接口」。）

### 2.2 球队

| 接口 | 说明 | 对预测的潜力 |
|------|------|--------------|
| **teams** | 球队信息（id、name、logo、venue 等） | 低：元数据/展示 |
| **teams/statistics** | 球队在指定联赛+赛季下的统计（场均进球、控球、射门等） | 中：赛季风格/样本少时收缩 |

### 2.3 球员

| 接口 | 说明 | 对预测的潜力 |
|------|------|--------------|
| **players** | 球员信息（可按 team、id、search 等查） | 低：元数据 |
| **players/statistics** | 球员在联赛+赛季或单场下的统计（射门、xG 等） | 高：关键球员近期状态 |
| **players/topscorers** | 联赛射手榜（前 N 名） | 中：关键球员名单/伤停加权 |
| **players/squads** | 球队阵容名单（按 team+season） | 中：配合伤停/阵容 |

### 2.4 联赛/赛事

| 接口 | 说明 | 对预测的潜力 |
|------|------|--------------|
| **leagues** | 联赛列表与元数据 | 低：筛选/展示 |
| **leagues/seasons** 或 **seasons** | 可用赛季列表 | 低：配置/展示 |
| **countries** | 国家列表 | 低：筛选 |

### 2.5 教练/转会/场馆/其他

| 接口 | 说明 | 对预测的潜力 |
|------|------|--------------|
| **coaches** | 教练信息（可按 team、id 等） | 中低：换帅/稳定性 |
| **transfers** | 转会记录 | 低：展示/补强名单 |
| **trophies** | 球员/教练荣誉 | 低：展示 |
| **venues** | 场馆信息 | 低：展示 |
| **bookmakers** | 博彩商列表 | 低：与 odds 搭配 |
| **livescore** | 实时比分（若单独存在） | 不做：赛前预测不用 |

---

## 三、后续使用建议

### 3.1 优先级（按「易落地 → 需设计」）

| 优先级 | 接口/数据 | 用途 | 说明 |
|--------|-----------|------|------|
| 1 | **fixtures/statistics 近期聚合** | 控球/角球/射门 → 风格特征 | 已有拉取，只差按 team+日期聚合与入参 |
| 2 | **fixtures/events** | 更准 xG、红牌/点球倾向 | 新 etl 按 fixture 拉取并落库，再聚合 |
| 3 | **players/statistics**（按 fixture） | 关键球员近期状态 | 新拉取，与伤停/关键球员名单结合 |
| 4 | **players/topscorers** | 关键球员名单或伤停加权 | 按 league+season 拉取并缓存 |
| 5 | **teams/statistics** | 赛季场均 → 收缩/先验 | 样本少或赛季初补强 |
| 6 | **coaches** | 换帅/稳定性 | 特征设计成本高，可后置 |

### 3.2 实现要点（通用）

- **调用方式**：使用 `etl/injury_api_football._request(path, params)`，注意 `config.API_FOOTBALL_KEYS` 与限流（如 `API_FOOTBALL_RATE_LIMIT_SEC`）。
- **落库**：新数据建议写入 MongoDB（如 `self_improvement_football` 下新集合）或 JSON 到 `data/`，便于预测管线读取。
- **预测入参**：涉及「近期场均」的，需按 team+日期在 DB/缓存中聚合后，传入 `engine/predict.predict_match` 或联赛覆盖参数。

### 3.3 相关文档索引

| 文档 | 内容 |
|------|------|
| [API-Football可拉取数据与预测用途.md](API-Football可拉取数据与预测用途.md) | 已用/未用数据、预测价值、各接口实现要点 |
| [可补充数据与模型提升.md](可补充数据与模型提升.md) | 模型可补充数据总览（含非 API-Football 来源） |

---

## 四、速查表：已用 vs 未用

| 接口 | 项目是否已请求 |
|------|----------------|
| fixtures | ✅ 已用 |
| fixtures/lineups | ✅ 已用 |
| fixtures/statistics | ✅ 已用 |
| fixtures/headtohead | ✅ 已用 |
| fixtures/events | ✅ 已用（`events-by-jingcai` → api_football_events） |
| predictions | ✅ 已用 |
| odds | ✅ 已用 |
| standings | ✅ 已用 |
| injuries | ✅ 已用 |
| teams | ❌ 未用 |
| teams/statistics | ❌ 未用 |
| players | ❌ 未用 |
| players/statistics | ❌ 未用 |
| players/topscorers | ❌ 未用 |
| players/squads | ❌ 未用 |
| leagues | ❌ 未用 |
| seasons | ❌ 未用 |
| countries | ❌ 未用 |
| coaches | ❌ 未用 |
| transfers | ❌ 未用 |
| trophies | ❌ 未用 |
| venues | ❌ 未用 |
| bookmakers | ❌ 未用 |

---

*文档维护：新增接口调用时，在「一、已请求」中补充路径与调用位置；新考虑未请求接口时，在「二、未请求」中补充并标注预测潜力。*
