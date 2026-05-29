# self_improvement_football 库表说明

当主库使用 MongoDB 且 `MONGO_DB_NAME=self_improvement_football` 时，竞彩相关数据与「主库」共用此库。以下为库内各集合的用途与关系。

---

## 集合一览

| 集合名 | 用途 | 唯一键 / 主键 |
|--------|------|----------------|
| **jingcai_match_analysis** | 竞彩对阵详情（体彩详情接口拉取）：主客队、联赛、赔率、伤停、比分、api_football_fixture_id 等 | `sporttery_match_id`（upsert） |
| **jingcai_match_anlysis** | 同上，拼写兼容；若存在且有条目则优先用此集合 | 同左 |
| **jc_matches** | 某「竞彩日」的列表快照（按日一条文档，`matches` 数组），用于按日展示/预测 | `date`（竞彩日 YYYY-MM-DD） |
| **jc_predictions** | 本模型对竞彩场次的预测结果（期望进球、胜平负概率等） | `(date, sporttery_match_id)` 或 `(date, home, away)` |
| **jc_api_predictions** | API-Football 接口的单场预测（第三方预测） | `api_football_fixture_id` |
| **jingcai_teams** | 竞彩队名中文 → 英文等映射，供解析主客队、预测时查 teams | `name_cn` |
| **api_football_statistics** | API-Football 单场统计（射门、射正、控球等），供 xG 代理与复盘 | `fixture_id` |
| **transfermarkt_lineups** | Transfermarkt 赛前用户阵型（vorbericht），预计首发名单，预测时无懂球帝时回退使用 | `sporttery_match_id` |
| **pre_match_lineups** | 赛前预计阵容，与 match_lineups 相同结构（home_starting/away_starting 等），便于与赛后实际阵容对比 | `match_id`（即 sporttery_match_id） |
| **dongqiudi_lineups** | 懂球帝预计首发，预测时优先使用（主源） | `sporttery_match_id` |

---

## 每场数据存放一览

当前每场竞彩对阵的以下数据均已落库，供仪表盘展示与预测使用：

| 数据类型 | 存储位置 | 字段/说明 |
|----------|----------|------------|
| **伤停** | jingcai_match_analysis | `injury_suspension`（体彩 getInjurySuspensionV1，主客队缺阵名单） |
| **赔率** | jingcai_match_analysis；另见 match_odds | 文档内 `odds`（胜平负、让球、比分、总进球、半全场等）；match_odds 按日期+主客+联赛存体彩/API 赔率 |
| **历史交锋** | jingcai_match_analysis | `result_history_all`（全部赛制）、`result_history_same_tournament`（同赛制） |
| **未来赛事** | jingcai_match_analysis | `future_matches`（体彩 getFutureMatchesV1，主客队未来赛程） |
| **单场统计**（射门、控球、角球等） | api_football_statistics | 按 `fixture_id` 存，每场 `teams: [主队统计, 客队统计]`；竞彩场次通过 `api_football_fixture_id` 关联 |
| **阵容（赛前预计）** | dongqiudi_lineups（优先）、transfermarkt_lineups（回退） | `predicted_home_starting`、`predicted_away_starting` 等；预测时用于叠加「关键球员未在预计首发」到伤停 key_count |

- 伤停/赔率/历史交锋/未来赛事：由体彩详情接口拉取后写入 **jingcai_match_analysis**，仪表盘详情展开时直接读该文档。
- 单场统计：由 API-Football statistics 拉取后写入 **api_football_statistics**；竞彩场次若有 `api_football_fixture_id` 则可关联展示或用于 xG/复盘。
- 阵容（赛前）：由 `dongqiudi-lineups` / `transfermarkt-lineups` 命令拉取，预测时优先读 **dongqiudi_lineups**，无则读 **transfermarkt_lineups**。

### 竞彩预测时这些数据如何参与

运行 **predict-jc-from-self-improvement**（或 API 竞彩预测）时，会按 `sporttery_match_id` 从 **jingcai_match_analysis** 加载单场详情，并解析后传入 `predict_match`：

| 数据 | 用途 |
|------|------|
| **伤停**（`injury_suspension`） | 主客队缺阵名单 → 下调该队期望进球；与射手/关键球员名单结合算 `home_absent_key_count` / `away_absent_key_count`，关键球员缺阵权重更高。 |
| **赔率**（`odds`） | 胜平负 spf_win/draw/loss → 若开启 `PREDICT_USE_ODDS_FUSION`，与规则模型概率线性融合。 |
| **历史交锋**（`result_history_same_tournament` 或 `result_history_all`） | 解析为 (主队场均进/失, 客队场均进/失, 场数)，用于期望进球的历史交锋融合（替代或补充主库 matches 的 h2h）。 |
| **未来赛事**（`future_matches`） | 主客队未来 7 天内场次数 → 赛程密度/疲劳调整（场次多则略降该队期望）。 |

**api_football_statistics**：在配置 `PREDICT_USE_XG=true` 时，会按主客队近期比赛的 `fixture_id` 从本表（或 JSON）读取单场统计，用 xG/射门等算近期场均 xG，与形式期望进球混合。竞彩场次若已回填 `api_football_fixture_id` 且拉过统计，则同样参与。单场统计也用于**赛后复盘**（射门、控球等展示）。

---

## 各表含义与数据流

- **jingcai_match_analysis**  
  - 体彩详情接口按 `sporttery_match_id` 拉取并 upsert。  
  - 含：主客队中英文、联赛、赔率、伤停、未来场次、比分（完赛后有 home_score/away_score）、可选 `api_football_fixture_id`。  
  - 按日查「该竞彩日场次」用 `jc_date` / `date`；repredict 的「已完赛」列表来自这里（有比分的文档）。

- **jc_matches**  
  - 按竞彩日存列表快照（`date` + `matches` 数组）。  
  - 与 jingcai_match_analysis 可能重叠：列表拉取会写 jc_matches，详情拉取会写 jingcai_match_analysis。按日读赛程时优先用 jingcai_match_analysis（按 jc_date），无则用 jc_matches。

- **jc_predictions**  
  - 本模型预测结果，按 `(date, sporttery_match_id)` 或 `(date, home, away)` upsert。  
  - 评估、复盘、命中率都基于 jc_predictions + jingcai_match_analysis 的比分。

- **jc_api_predictions**  
  - API-Football 预测接口结果，按 `api_football_fixture_id` 存。  
  - 与 jingcai_match_analysis 的 `api_football_fixture_id` 关联，用于展示「API 预测」列或融合。

- **jingcai_teams**  
  - 竞彩中文队名 → 英文等，用于解析主客队、写入主库 teams（当主库即本库时）。

- **api_football_statistics**  
  - 按 fixture 存的射门/射正等，用于近期 xG 代理、单场统计复盘。  
  - 预测时若开启 xG，会从本表（或 JSON）读统计。

---

## 与主库重合时的注意点

当 `MONGO_DB_NAME=self_improvement_football` 时，主库的「matches / predictions / teams」等也在此库：

- 若**没有**单独建 `matches`、`predictions` 集合，则主库的 repredict 会走「竞彩路径」：用 **jingcai_match_analysis 已完赛** 列表重算并写 **jc_predictions**（见 `repredict_jc_evaluable_matches`）。
- **teams**：竞彩预测会通过 jingcai_teams + 主库 teams 解析队名；若本库作主库，需保证有 **teams** 集合（或由 ensure_team 按需写入）。
- **competitions**：按联赛名解析时可能用到主库 **competitions**，若本库作主库可同样在此库建 competitions。

---

## 可能欠缺或可补充的

1. **matches / predictions（主库形态）**  
   若希望与「传统主库」完全一致（按 match_id 的 predictions 表、按 match 的评估），可在本库建 `matches`、`predictions`，并把 jingcai 场次同步为带 `source_id=af_<fixture_id>` 的 match 文档，便于用主库的 predict-eval、xG 近期场次等。

2. **competitions**  
   若以本库为主库且要做按联赛的排名/评估，建议有 **competitions** 集合（或从别处同步）。

3. **去重策略**  
   `jc_match_analysis_list_by_jc_date` 已改为**不做 sporttery_match_id 去重**：同一日、同一 mid 若在主集合与备用集合各有一条，或同一 mid 多条文档，都会计入。repredict 会逐条处理，写入 jc_predictions 时仍按 (date, sporttery_match_id) upsert，故最终每场仍一条预测，但「已完赛场次数」会与文档数一致、不因去重变少。

4. **单场统计覆盖**  
   确保需要 xG 的场次已拉取并写入 **api_football_statistics**（或 JSON）；竞彩按年拉统计可用 `statistics-api --jingcai-years 2024,2025,2026`。

---

## 小结

- **核心数据**：jingcai_match_analysis（赛程+详情+比分）、jc_predictions（本模型预测）、jingcai_teams（队名映射）。  
- **辅助**：jc_matches（列表快照）、jc_api_predictions（API 预测）、api_football_statistics（单场统计/xG）。  
- **主库同库时**：视情况补 teams、competitions，或同步 matches/predictions 以兼容主库评估与 xG 逻辑。
