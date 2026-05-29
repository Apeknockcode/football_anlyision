# API-Football 可拉取数据与预测用途

基于 api-sports.io 的 **API-Football** 接口，整理：**项目已用**的数据、**尚未接入**但可拉取的数据，以及其中**对模型预测**有潜力的部分。

**总览文档（推荐后续统一查阅）**：[API-Football接口与使用说明.md](API-Football接口与使用说明.md) — 已请求/未请求接口一览、调用位置、后续使用建议与速查表。

---

## 一、项目已拉取且已参与预测的 API-Football 数据

| 接口/数据 | 项目中的用法 | 预测中的角色 |
|-----------|--------------|--------------|
| **fixtures**（赛程） | `ingest-api-history`、`jc-match-analysis-backfill-fixture-id` | 赛程 + fixture_id 关联后续接口 |
| **fixtures/statistics**（单场统计） | `statistics-api` → `api_football_statistics` / JSON | xG 或射正/射门代理 → `PREDICT_USE_XG` 时与期望进球混合 |
| **fixtures/lineups**（阵容） | `lineups-by-jingcai` → `match_lineups` | 复盘、关键缺阵分析；**不参与**当次预测（预测用伤停+预计首发） |
| **predictions**（API 预测） | `fill-api-predictions`、竞彩预测时可选融合 | 与规则模型概率线性融合 |
| **odds**（赔率） | `odds-api`、`odds-api-by-db` → `match_odds` | 胜平负隐含概率与模型概率融合 |
| **standings**（积分榜） | `standings-api`、`standings-backfill` | 主客队排名 → 期望进球微调（PREDICT_STANDINGS_NUDGE） |
| **injuries**（伤停） | `injury-api`、`injury-api-by-db` → `match_injury` | 缺阵人数 + 关键球员 → 期望进球下调 |
| **fixtures/headtohead**（历史交锋） | `h2h-backfill` | 主客直接对话 → 与近期期望进球融合 |

---

## 二、API-Football 已提供、项目尚未拉取的数据

以下均为官方文档中存在的端点，当前项目**未**接入；标注 ✅ 的表示对**模型预测**有直接或间接价值。

| 接口 | 说明 | 对预测的价值 | 优先级建议 |
|------|------|--------------|------------|
| **fixtures/events** | 单场事件（进球、黄红牌、换人、射门等，含时间与球员） | 可做：单场/近期「真实 xG」汇总、红牌/点球倾向、大机会次数；需解析 event 类型与明细 | 高：补强 xG/场面特征 |
| **players/statistics**（按 fixture） | 单场**球员级**统计（射门、xG、传球等） | 主客队近期「核心球员 xG/射门」→ 关键球员状态或权重；与伤停结合更细 | 高：关键球员/状态 |
| **players/topscorers** | 联赛射手榜（前 N 名） | 识别「关键球员」、或作伤停权重的补充名单 | 中：可替代/补充 jingcai 关键球员 |
| **coaches** / **coaches/statistics** | 教练信息、执教统计 | 换帅、任期 → 稳定性/风格粗特征；需设计规则 | 中低：特征设计成本高 |
| **teams/statistics** | 球队赛季级统计（联赛+赛季维度） | 赛季场均控球、射门、xG 等 → 风格/压制力；可与单场统计互补 | 中：风格/长期表现 |
| **squads/players**（按队） | 球队当前阵容名单（球员 id） | 与 lineups/events 结合做「谁常首发、谁缺阵」更准 | 中：配合伤停/阵容 |
| **livescore** | 实时比分/状态 | 仅适合赛中/直播产品，**不用于**赛前预测 | 不做 |
| **leagues** / **countries** | 联赛、国家列表与元数据 | 做筛选、展示用，对预测公式无直接作用 | 低 |

---

## 三、优先建议拉取并用于模型的数据（按性价比）

### 1. fixtures/events（单场事件）

- **能拿到什么**：每场的进球、射门、黄红牌、换人、部分数据源带 per-shot xG。
- **预测用途**：  
  - 按主客队聚合**近期场均 xG**（若 API 返回 xG 相关字段），比单用「射正/射门」代理更准。  
  - 红牌/点球次数 → 可做「裁判/风格」的轻量特征或过滤。  
- **实现要点**：新 etl 模块按 `fixture_id` 拉取并落库；预测前按 team+日期聚合近期 N 场 events，算场均 xG/红牌等，传入 `predict_match` 或新特征层。  
- **注意**：免费档/不同联赛对 events 的覆盖可能不同，需先试拉再决定是否全量用。

### 2. players/statistics（按 fixture，球员单场统计）

- **能拿到什么**：每场每位球员的射门、xG、传球、对抗等（视 API 版本而定）。
- **预测用途**：  
  - 主客队「核心球员」（如射手榜前几名）的**近期场均 xG/射门** → 作为「关键球员状态」特征，或与现有伤停权重结合。  
  - 比只用「是否缺阵」更细：能区分「在阵但状态差」vs「在阵且状态好」。  
- **实现要点**：按 fixture 拉取并落库；按 team 聚合近期场次中「关键球员」的均值，作为可选输入传入预测；需先定义「关键球员」来源（topscorers 或 jingcai 名单）。

### 3. 单场统计的深度使用（fixtures/statistics，已有拉取）

- **现状**：已拉取并用于 xG 代理（及部分复盘）；`PREDICT_USE_XG` 开启时参与期望进球混合。
- **可加强**：  
  - 控球率、角球、禁区内射门等 → 做主客队**近期场均**，作为「风格/压制力」的轻量特征（如控球高且射门多 → 略抬期望进球或仅作展示）。  
  - 文档 [可补充数据与模型提升.md](可补充数据与模型提升.md) 已建议「单场统计聚合为近期特征」。  
- **实现要点**：在现有 `api_football_statistics` 上按 team+日期算近期均值，在 `predict_match` 或联赛覆盖里增加可选参数（如 possession_last5、shots_last5），小权重或仅高置信场次使用。

### 4. topscorers（射手榜）

- **能拿到什么**：联赛赛季射手榜（前 20 等）。
- **预测用途**：  
  - 与 jingcai「关键球员」交叉：若伤停名单里的人在 topscorers 中，可提高该队伤停权重或单独标「核心缺阵」。  
  - 或作为「关键球员」名单的补充来源（当 jingcai 无关键球员时）。  
- **实现要点**：按 league+season 拉取并缓存；在 `parse_jingcai_analysis_for_predict` 或伤停逻辑里，用该名单对 home/away 缺阵做加权或标记。

### 5. teams/statistics（球队赛季统计）

- **能拿到什么**：某联赛某赛季某队的场均进球、失球、控球、射门等。
- **预测用途**：  
  - 与「近期 N 场」互补：赛季统计更稳，适合样本少的队或赛季初。  
  - 可做「联赛内实力/风格」的粗指标，参与收缩或先验。  
- **实现要点**：按 league+season+team 拉取并落库；预测时若近期场次不足，用该队赛季场均与联赛平均做收缩（类似现有 PREDICT_LEAGUE_SHRINKAGE，但用球队维度）。

---

## 四、小结（按「易落地 → 需设计」排序）

| 优先级 | 数据 | 用途 | 说明 |
|--------|------|------|------|
| 1 | **fixtures/statistics 近期聚合** | 控球/角球/射门等 → 风格特征 | 已有数据，只差聚合与入参 |
| 2 | **fixtures/events** | 更准 xG、红牌/点球倾向 | 新拉取 + 聚合逻辑；需确认 API 返回字段与档位 |
| 3 | **players/statistics（per fixture）** | 关键球员近期状态 | 新拉取 + 与伤停/关键球员名单结合 |
| 4 | **topscorers** | 关键球员名单或伤停加权 | 新拉取 + 与现有伤停逻辑结合 |
| 5 | **teams/statistics** | 赛季场均 → 收缩/先验 | 新拉取；样本少时补强 |
| 6 | **coaches** | 换帅/稳定性 | 特征设计成本高，可后置 |

以上均可通过现有 `injury_api_football._request` 或统一封装的 API 调用函数拉取；涉及「近期场均」的，需按 team+日期在 DB 或缓存中聚合后再传入 `predict_match` 或扩展的联赛/覆盖参数。  
若需要，我可以把其中某一项拆成「接口字段 + 落库表结构 + 预测入参」的具体任务列表（含文件与配置点）。
