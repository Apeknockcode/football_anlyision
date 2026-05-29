# 足球分析 AI 工具（MVP）

面向球迷的足球比赛复盘报告生成工具：使用公开数据（StatsBomb Open Data）+ 规则统计 + 可选 LLM 生成可读报告。**个人自用，仅 CLI，不对外提供 API。**

## 功能

- **数据**：从 StatsBomb 开放数据（GitHub）与 football-data.org 拉取比赛与事件数据，ETL 后写入 SQLite。
- **分析**：事件统计（传球、射门、控球率等）、简单战术/复盘逻辑（关键瞬间、谁主导）。
- **报告**：将结构化指标组装成 payload，经 LLM 生成球迷向 Markdown/HTML 报告（无 API Key 时使用 Mock 模板）。
- **预测**：基于历史比分对「主队 vs 客队」做简单预测（期望进球、胜/平/负概率）；可配合 `fixtures` 查看未来赛程。

**统计定义**：射门、xG、控球等指标与 [Opta 定义](https://theanalyst.com/articles/opta-football-stats-definitions) 对齐，详见 [docs/参考Opta定义与指标.md](docs/参考Opta定义与指标.md)。

**项目结构与数据**：目录布局、**数据来源**（StatsBomb / football-data.org / API-Football / 公众号 / 竞彩等）、**数据依赖**（谁读谁、谁写谁）、**数据库**（SQLite / MongoDB / 阿里云 RDS）的对应关系见 [docs/项目结构.md](docs/项目结构.md)。

## 环境要求

- Python 3.10+
- 网络（首次拉取 StatsBomb 数据）

## 安装

```bash
cd football_anlyision
pip install -r requirements.txt
```

**使用真实 LLM 生成报告**：需额外安装 `openai` 并配置环境变量（见下方「配置」）：
```bash
pip install openai
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
```

## 配置

- **数据库**：默认 `data/football.db`（SQLite）。可通过环境变量 `DATABASE_URL` 覆盖。
- **LLM**：
  - `LLM_PROVIDER=mock`（默认）：不调用真实 API，输出模板报告。
  - `LLM_PROVIDER=openai` 且设置 `OPENAI_API_KEY` 或 `LLM_API_KEY`：使用 OpenAI 或兼容接口。
  - `LLM_BASE_URL`、`LLM_MODEL`：自定义 base URL 与模型名（如国产大模型）。

## 使用

### 1. 拉取数据并落库

```bash
python main.py ingest
```

默认拉取 La Liga 2020/21 **前 30 场**。可指定 `--competition`、`--season`。要**拉满该赛季全部比赛**可加 `--all`：
```bash
python main.py ingest --all
```
若某场因网络超时未拉到事件，可稍后重跑并加 `--force-events` 补拉：
```bash
python main.py ingest --max-matches 50 --force-events
```

**拉取 2023、2024、2025、2026 等近年数据**：StatsBomb 开放数据只包含部分赛事/赛季，并非所有联赛都有近年数据。先查看有哪些可用：
```bash
python main.py seasons
```
会列出 2023–2026 相关赛事的 `competition_id`、`season_id`、赛事名、赛季名。再按需拉取，例如：
```bash
# 德甲 2023/24（comp_id=9, season_id=281）
python main.py ingest --competition 9 --season 281 --all
# 欧洲杯 2024（comp_id=55, season_id=282）
python main.py ingest --competition 55 --season 282 --all
```
指定其他年份：`python main.py seasons --years 2022,2021`。

**五大联赛（英超/西甲/意甲/德甲/法甲）**：使用 football-data.org 免费 API，需在 [football-data.org](https://www.football-data.org/) 注册并设置 `FOOTBALL_DATA_API_KEY`（或使用项目内默认 Key）。拉取 2023–2026 赛季：
```bash
python main.py ingest-fd
# 或指定赛季
python main.py ingest-fd --seasons 2023,2024,2025
```
数据为比赛级（比分、进球者），与 StatsBomb 事件数据并存于同一库；`list` / `report` 通用。  
**未来赛程**：`ingest-fd` 会同时写入未开赛场次（SCHEDULED/TIMED），`list` 中比分显示为「待赛」；比赛结束后再次运行 `ingest-fd` 会更新该场比分并写入进球。`report` 仅支持已结束比赛。

**欧冠**（免费档可用）：用 `--leagues CL` 拉取并更新欧冠数据，可与五大联赛一并入库。建议拉全近年赛季以保持完整：
```bash
python main.py ingest-fd --leagues CL --seasons 2023,2024,2025,2026
```
欧联、德国杯、意杯等杯赛在 football-data.org 免费方案下不可用（403），本项目不再拉取。

**补全单场统计与半场比分**：对已入库的 football-data 场次，可运行 `enrich-fd` 请求单场详情接口，将射门、控球、犯规等与半场比分写入 `raw_meta`，报告中的五大联赛场次即可展示完整统计与半场比分：
```bash
python main.py enrich-fd --limit 20
# 或只补全一场
python main.py enrich-fd --match-id 12345
```
免费 API 约 10 次/分钟，全量补全可分批执行（多次 `--limit 20`）。

### 2. 未来比赛预测（基于历史比分）

基于双方**近期已结束比赛**的场均进球/失球，用简单模型算期望进球与胜/平/负概率（Poisson 近似）：

```bash
python main.py predict "Arsenal FC" "Liverpool FC"
python main.py predict "阿森纳" "利物浦" --competition "Premier League" --last-n 10
```

- `--competition`：可选，限定在某一赛事内取最近 N 场（如 Premier League）。
- `--last-n`：参考最近几场，默认 10。

球队名支持模糊匹配（如 `Arsenal` 可匹配 `Arsenal FC`）。需先有历史数据（如 `ingest-fd` 拉取五大联赛）。仪表盘中「预测结果」旁标有**高置信**的场次表示某项概率 ≥45%，建议优先参考高置信或概率明显高于 33% 的场次。

**查看未来赛程**（便于选一场做预测）：

```bash
python main.py fixtures --league PL --season 2025
```

`--league` 可选：PL（英超）、PD（西甲）、SA（意甲）、BL1（德甲）、FL1（法甲）。会请求 football-data.org 并列出未开赛场次。

**用 football 模型预测 self_improvement_football 中的竞彩列表**（需 MongoDB）：若已将某日竞彩列表保存到 `self_improvement_football.jc_matches`（如通过仪表盘或 `POST /api/jc-today/save`），可对该日五大联赛场次跑模型预测并写入 `jc_predictions`，历史数据仍从主库 football 读取：
```bash
python main.py predict-jc-from-self-improvement --date 2026-02-27 --last-n 10
```
队名会按主客的英文/中文在 football 的 teams 中解析；无法解析或非五大联赛的场次会跳过。

**预测事后分析**（预测 vs 完赛结果）：若希望对「今天预测的几场」或历史预测做误差分析，可先用 `predict-match` 对指定比赛落库预测，完赛后用 `predict-eval` 查看期望进球 MAE、胜平负准确率与 Brier 分数：

```bash
# 对 list 中的某场（match_id）做预测并写入 predictions 表
python main.py predict-match 12345 --last-n 10

# 对比已落库预测与完赛结果（全量历史，或 --today 仅最近 24 小时内创建的预测）
python main.py predict-eval
python main.py predict-eval --today
```

输出示例：期望进球 MAE（主/客/整体）、胜平负猜对条数与准确率、Brier 分数（概率校准，越小越好）。不自动改预测参数，仅做事后分析供人工参考。

**每日预测结果统计与复盘**（推荐流程）：用 `prediction-log` 记录当天预测，完赛后填实际比分并做复盘（预测 vs 实际、MAE、胜平负准确率、不一致原因简述）：

```bash
# 1. 当天要预测几场时：每场执行一次 add（会调用预测并写入 data/prediction_log.json）
python main.py prediction-log add "Chelsea FC" "Leeds United FC" --competition "Premier League"
python main.py prediction-log add "Tottenham Hotspur FC" "Newcastle United FC" --competition "Premier League"

# 2. 查看已记录的预测（序号用于后面填比分）
python main.py prediction-log list
python main.py prediction-log list --date 2026-02-11   # 只列某天

# 3. 完赛后按序号填写实际比分（序号从 list 输出第一列）
python main.py prediction-log set-result 0 2 1    # 第 0 场 主队 2 - 客队 1
python main.py prediction-log set-result 1 0 2

# 4. 复盘：看预测 vs 实际、期望进球 MAE、胜平负准确率、每场不一致原因
python main.py prediction-log recap
python main.py prediction-log recap --date 2026-02-11   # 只复盘某天
```

日志文件：`data/prediction_log.json`，可手动编辑补实际比分或交给 AI 复盘时使用。

**调参与赔率融合驱动流程**（用 eval 与 recap 反馈改进预测）：

1. **看指标**：跑 `python main.py eval-all`（等价于 predict-eval + predict-eval --by-competition + prediction-log recap + recap --by-competition），或分别跑四条命令，记录胜平负准确率、MAE、Brier。
2. **改参数**：根据复盘结论改 `config.py` 或 `.env` 中的 `PREDICT_*`（如 `PREDICT_HOME_ADVANTAGE`、`PREDICT_DRAW_BOOST_COEF`、`PREDICT_LEAGUE_SHRINKAGE_MIN_SAMPLES`），或改 `data/predict_league_params.json` 对某联赛单独调 `last_n`、收缩等。赔率融合可试 `PREDICT_USE_ODDS_FUSION=false` vs `true`、`PREDICT_ODDS_FUSION_WEIGHT_MODEL=0.3/0.5/0.7`。**每场边界命中**：势均力敌且主队略优时可用 `PREDICT_BOUNDARY_HOME_SPREAD`、`PREDICT_BOUNDARY_HOME_NUDGE` 略抬主队期望（默认 0.28 / 0.03），减少「预测平、实际主」的漏判。
3. **重算历史**：执行 `python main.py repredict-history`，用当前参数对「已有预测且已完赛」的场次重算并写回 DB 与 prediction_log。
4. **再评估**：运行 `python main.py refresh-and-eval`（= repredict-history + eval-all），或单独跑 `eval-all`，对比前后指标。**样本增加后**可先跑 `python main.py refresh-models`（calibrate-probs + train-outcome-ml），再跑 `refresh-and-eval` 看新指标。

**提高复盘命中率（补历史赔率/积分榜）**：若希望重算时用上更多赔率与积分榜，可先跑 `python main.py backfill-log-support [--max-dates N]`（从 prediction_log 已填比分的日期拉取 API-Football 赔率与最新一季积分榜，默认最近 15 个日期），再跑 `repredict-history` 与 `prediction-log recap`（及 `recap --by-competition`）观察命中率变化。需配置 `API_FOOTBALL_KEY`，请求受限流影响可适当减小 `--max-dates`。backfill 完成后可一键执行 `./scripts/after-backfill-recap.sh` 做重算与复盘。

**低置信度标记**：当主/客近期样本不足或期望进球几乎持平时，预测结果会带 `confidence: "low"`（API 与 prediction_log 中为 `confidence` 字段），前端可展示为「仅供参考」。阈值由 `PREDICT_LOW_CONFIDENCE_MIN_SAMPLES`、`PREDICT_LOW_CONFIDENCE_MAX_SPREAD` 控制。

**概率校准**：用历史「预测概率 vs 实际结果」拟合 Platt 缩放，使输出概率更贴近实际发生频率（可改善 Brier）。步骤：先积累足够已填比分的预测（建议 20 条以上），运行 `python main.py calibrate-probs` 生成 `data/calibration_params.json`，再设置 `PREDICT_USE_CALIBRATION=true`，之后新预测将自动应用校准。

**预测逻辑流程图**：完整预测链路（期望进球 → 概率 → 赔率/校准/ML 融合）见 [docs/预测流程图.md](docs/预测流程图.md)（含 Mermaid 流程图与当前命中率说明）。

**加大模型**：2.1 多 last_n（可选 5/10/15 三档融合）、联赛主客细分、xG 混合与伤停关键球员权重已接入。2.2 可在规则输出上接 ML 修正层：运行 `python main.py train-outcome-ml`（需 scikit-learn），再设 `PREDICT_USE_OUTCOME_ML=true`、`PREDICT_OUTCOME_ML_WEIGHT=0.3`。

**数据不足时**：若 `calibrate-probs` / `train-outcome-ml` 报样本不足，或某联赛 recap 样本很少，请多跑 `auto-accumulate-log` 并填比分以积累数据。

**多积累 prediction_log**：recap 的准确率与按联赛/伤停统计都依赖「已填实际比分」的条数。可运行 `python main.py auto-accumulate-log`（默认拉取昨日/今日/明日场次：未开赛的自动写入预测，已完赛的自动同步赛果并回填 log）。也可手动用 `prediction-log add` / `predict-jingcai` 记预测，用 `set-result` 填比分。

**后续可提升方向**：运行 `python main.py suggest-improvements` 可输出**数据积累**、**API 预测融合**、**联赛级调参**三类建议（基于当前样本与按联赛统计）。验证 API 预测接口可用：`python main.py test-api-predictions --date YYYY-MM-DD [--home 主队 --away 客队]`。

**单变量微调建议**：每次只改一个参数，改完跑 `repredict-history` 再跑 `prediction-log recap --by-competition` 对比。已做过的单变量对比（recap 96 场均保持 39/96 胜平负）：边界 NUDGE/SPREAD、意甲/法甲 HOME/DRAW_BOOST、赔率权重 0.47→0.45、API 权重 0.28→0.30 均未提升。当前保留：NUDGE=0.03、SPREAD=0.28、赔率 0.47、**API 权重 0.30**、联赛参数见 `data/predict_league_params.json`。

**还能继续提升的方向**（三条路，在不动模型结构的前提下仍可提升）：

1. **多补历史赔率**  
   定期或偶尔跑：`backfill-log-support --max-dates 25`（或 30、40，视 API 配额而定），然后按上面两条命令 **repredict + recap**。补的日期越多，参与赔率/API 融合的 recap 场次越多，越有机会提升。

2. **多积累新场次**  
   每天跑 `auto-accumulate-log` 并填比分，让 log 里近期、带赔率/API 的场次变多；过一阵再 backfill → repredict → recap，recap 会更多反映「有数据的场次」，命中率更容易上去。

3. **样本多了再重训**  
   当 DB 和 prediction_log 的已填比分明显增多（例如再多个几十场），跑一次：`refresh-models`（校准 + outcome ML 重训），再 `refresh-and-eval`，一般会有 1～2 个点的空间。

**查看数据量**（数据增多后迭代模型前可先看规模）：

```bash
python3 main.py data-stats
```

输出主库比赛数/已完赛/预测数；当主库即竞彩库（`MONGO_DB_NAME=self_improvement_football`）时显示 `jingcai_match_analysis` 与 `jc_predictions` 条数。当「预测 + 已完赛」足够（如 > 30）时再跑 `python3 main.py refresh-models` 与 `python3 main.py refresh-and-eval`。

**单场推荐命中率**：`jc-predictions-eval` 会输出「单场推荐分档」——仅统计推荐概率≥50%/55%/60% 时的命中率，便于只推高置信场次。默认今日推荐已提高门槛（`min_prob=0.50`、`min_spread=0.06`）；实力差距大时优先选胜/负而非让球（可设 `SINGLE_REC_MIN_PROB=0.52` 进一步提高高置信门槛）。ML 修正层可调 `PREDICT_OUTCOME_ML_N_ESTIMATORS`、`PREDICT_OUTCOME_ML_MAX_DEPTH` 重训后观察分档命中率。

**结论**：提升主要依赖 **更多场次有赔率/API**（backfill + 日常积累）以及 **样本增多后重训校准和 ML**。当前配置（含 API 0.30）已按「能提升的方向」设好；等 backfill 跑完，按上面顺序做一次 repredict + recap 即可看到最新效果。**预测表条数与可评估场次**：若预测表有数千条而 `predict-eval` 仅统计到少量场次，说明只有「已完赛且已填比分」的预测才参与评估；补预测与补比分可增加可评估样本，详见 [docs/提升准确率实操.md](docs/提升准确率实操.md)。**若对当前命中率仍不满足**，可参考 [docs/竞彩命中率后续可做清单.md](docs/竞彩命中率后续可做清单.md) 按优先级逐项执行（重训 ML、补数据、主推高置信等）。

### 3. 查看已入库比赛（获取 report 用的 match_id）

```bash
python main.py list
```

输出每场的 id、日期、主客队、比分；未开赛场次比分为「待赛」。用其中的 **id** 作为 `report` 的 `<match_id>`（仅已结束比赛可生成报告）。

### 4. 生成单场报告

```bash
python main.py report 1
```

其中 `1` 为 `list` 或 ingest 日志中的比赛 ID。报告写入 `data/reports/match_1.md` 与 `match_1.html`。

```bash
python main.py report 1 --open
```

生成后自动在浏览器中打开 HTML 报告。

### 5. 端到端示例

```bash
# 拉取数据（默认 30 场；或 --all 拉满该赛季）
python main.py ingest

# 查看有哪些比赛及对应 id
python main.py list

# 为某场生成报告（用 list 里的 id）
python main.py report 1 --open
```

## 项目结构

```
football_anlyision/
├── config.py          # 路径、DB、LLM 配置
├── data/              # 数据库与报告输出目录
├── etl/               # 数据接入与 ETL（schema、ingest）
├── engine/            # 统计、战术逻辑、报告 payload
├── report/            # 报告组装、LLM 客户端、MD/HTML 输出
├── main.py            # CLI 入口（ingest / list / report / seasons）
├── requirements.txt
└── README.md
```

## 数据来源与引用

**数据来源**：仪表盘场次/比分、预测、伤停、复盘报告等来自 DB（matches、predictions、match_injury 等）及 API（体彩同步、API-Football 等），详见代码与配置。

本工具使用 [StatsBomb Open Data](https://github.com/statsbomb/open-data)。若基于该数据发表分析，请按 StatsBomb 要求注明来源并使用其 Media Pack 中的 logo。五大联赛数据来自 [football-data.org](https://www.football-data.org/)。

### 数据是否够做 AI 分析？

**可以。** 当前数据已支持为任意已入库比赛生成 AI 复盘报告。

| 数据来源 | 报告可用内容 |
|----------|--------------|
| **football-data.org** | 比分、赛事/赛季、进球时刻（谁在何时进球）。无传球/控球/射门数等事件统计，报告会注明「仅比分与进球时刻」。 |
| **StatsBomb** | 同上 + 传球、射门、控球率、犯规等统计，以及更细的关键瞬间与战术倾向。 |

生成报告时，payload 会带上 `data_source` 字段，LLM 会根据来源决定表述方式，避免编造未提供的统计。若希望单场分析更细（如控球、传球对比），可优先对 StatsBomb 有事件的比赛做 `report`。

### 6. 日常数据与预测

需要时可手动执行：**同步昨日赛果** + **今日竞彩预测** + **auto-accumulate-log**（未开赛写预测、已完赛回填）+ **伤停 API**（拉取当日伤停并 merge）+ **suggest-improvements**。若仅需「拉取场次并预测」，可运行 `python main.py daily-jingcai --date 2026-02-13`。

**下注单推荐**：根据当日竞彩场次与 `jc_predictions` 模型预测，筛选出概率达标的场次作为下注参考（默认仅展示 max(主胜/平/主负)≥45% 的场次，按概率降序）。命令行：`python main.py recommend-bet-slip [--date YYYY-MM-DD] [--min-prob 0.45] [--top N]`；仪表盘竞彩页顶部有「今日下注推荐」区块；接口：`GET /api/jc-today/recommend?date=YYYY-MM-DD&min_prob=0.45&top=N`。需先对该日有竞彩列表并已运行预测（如 `predict-jc-from-self-improvement` 或网页「运行预测」）。

**投注花费与收益统计**：在数据库表 `bet_records` 中记录每笔花费与回报，便于统计盈亏。命令行：`python main.py bet-record add --date 2026-03-06 --spend 100 [--return 0] [--note "今日一单"]`、`bet-record list [--from YYYY-MM-DD] [--to YYYY-MM-DD]`、`bet-record stats [--from] [--to]`。SQLite 时表在 `data/football.db`，Mongo 时在 `bet_records` 集合。

### 7. 从体彩网同步比赛数据

当第三方数据源（如 football-data.org）暂无某日完赛记录时，可从 [体彩网 m.sporttery.cn](https://m.sporttery.cn/mjc/zqsj/?tab=concern) 抓取该日比赛比分，写入 DB 并更新 `prediction_log` 的实际比分，便于网页点「复盘」生成完整报告。

**命令行：**
```bash
python main.py sync-sporttery --date 2026-02-11
```

**若体彩页面需登录或反爬**：在浏览器打开体彩页面，F12 → Network，找到返回 `matchList` 的请求，复制 Response 保存为 `data/sporttery_20260211.json`，然后：
```bash
python main.py sync-sporttery --date 2026-02-11 --from-json data/sporttery_20260211.json
```

参考 JSON 格式见 `data/sporttery_sample.json`。体彩中文队名（如切尔西、利兹联）会自动映射到系统英文队名（Chelsea FC、Leeds United FC）。

**Web 仪表盘**：选择日期后点「从体彩同步」可同样触发同步。仪表盘入口：`python -m uvicorn api.app:app --reload`，访问 `/dashboard`。

### 8. 伤停数据拉取（来源：亚鸣笔记等公众号）

伤停数据来自公众号伤停文章中的**图片表格**，通过抓图 + OCR 解析后写入 `data/injury_suspension.json`，仪表盘按场次号/联赛自动匹配展示。默认来源为**亚鸣笔记**，可在配置中切换或按日期指定不同文章。

**按日期自动换文章**：使用 `data/injury_urls.json` 配置（可复制 `data/injury_urls_sample.json` 为 `injury_urls.json` 后编辑）：

- `source_name`：来源名称（如「亚鸣笔记」），仅用于展示。
- `default_url`：未按日期命中时使用的公众号文章链接。
- `by_date`：`"YYYY-MM-DD": "文章链接"`，某日拉取伤停时优先使用该日链接。

仪表盘点击「拉取伤停」时会带上**当前选择的查询日期**，后端按该日期从 `by_date` 或 `default_url` 取链接；拉取成功后会把本次使用的 URL 写回 `by_date`，下次同一天会优先用同一篇文章。

**仪表盘**：选择日期后点击 **「拉取伤停」**，即按该日期对应的文章链接抓取并解析。

**命令行**：
```bash
# 按今天日期从 injury_urls.json / INJURY_ARTICLE_URL 取链接并拉取
python3 main.py injury-crawl

# 指定比赛日，按该日期选文章（并写回 by_date 便于下次复用）
python3 main.py injury-crawl --date 2026-02-14

# 直接指定公众号文章链接
python3 main.py injury-crawl --url "https://mp.weixin.qq.com/s/xxx"

# 公众号环境异常时：用本地伤停图片解析
python3 main.py injury-crawl --from-image data/raw/injury_0.png
```

若公众号出现「环境异常」无法自动抓取，请用 `--from-image` 指定本地图片，或把当日文章链接填入 `data/injury_urls.json` 的 `by_date["YYYY-MM-DD"]` 或 `default_url` 后重试。

**伤停来源二：API-FOOTBALL（五大联赛）**

在 [api-sports.io](https://api-sports.io) 注册后可使用 API-FOOTBALL。配置 `API_FOOTBALL_KEY` 后可用以下方式拉取伤停：

- **命令行**：`python3 main.py injury-api [--date YYYY-MM-DD] [--merge]`
- **按赛季断点续拉（免费档拉 2024 等）**：`python3 main.py injury-api --season 2024`。会从 `data/injury_api_progress.json` 记录的上次日期之后继续拉取，每日自动 merge 到 `injury_suspension.json`；遇 429 限额会保存进度，次日再执行同一命令即可继续。若你之前已手动拉过部分日期，可先创建进度文件避免重复请求，例如 `{"season_2024": "2024-11-20"}` 表示已拉到 2024-11-20（含），下次从 2024-11-21 起拉。
- **仪表盘**：点击「伤停(API)」或调用 `POST /injury-api?date_str=YYYY-MM-DD`。
- **配置**：在 `.env` 中设置 `API_FOOTBALL_KEY=你的密钥`（勿提交到 Git）。
- **加快拉取（付费档）**：在 `.env` 中设置 `API_FOOTBALL_RATE_LIMIT_SEC=0.5`，或命令行加 `--rate-limit 0.5`，单日伤停拉取可从约 1 分钟降到数秒。
- **按 DB 比赛日批量补伤停**：`python3 main.py injury-api-by-db --years 2024,2025,2023,2026 [--skip-existing]`。只拉 DB 里已有比赛的日期，适合新导入赛程后补全伤停。
  - **执行频率**：不必每天跑。初次或新赛季可跑全量；日常建议**每周一次**用 `--skip-existing` 补缺失日期，或每天只拉当天：`python3 main.py injury-api --date 今天`。
  - **加速**：① 付费档在 `.env` 设 `API_FOOTBALL_RATE_LIMIT_SEC=0.05` 或命令行加 `--rate-limit 0.05`，可大幅缩短每请求间隔。② 只补最近几天时加 `--limit N`（如 `--limit 7`）只处理最近 N 个缺失日期，避免一次拉几百天。

**免费档限制（当前实测）**：  
- **Fixtures 按日期**：仅开放很窄的“近期”日期窗口（例如 2026-02-12～2026-02-14），其他日期返回 0。  
- **Injuries 按联赛+赛季**：仅开放 **2022、2023、2024** 赛季，2025/2026 赛季会报错「Free plans do not have access to this season」。  
因此免费档下，**无法**同时拿到「当前/未来比赛日赛程 + 当季伤停」；若日期在开放窗口内，赛程能拉到，但伤停会因赛季受限而为空。需要 2025/2026 当季伤停需使用付费方案。单次拉取约 6 次请求（1 次 fixtures + 5 次 injuries），免费档另有 10 次/分钟、100 次/天限制。

**伤停对预测命中率的影响**：仪表盘和接口在展示预测时，会对**有伤停**的场次做期望进球下调（主/客队缺阵人数越多下调越多，上限约 15%），再重算胜平负与让球预测。要查看「伤停是否真的提高了命中率」，可在完赛并填写实际比分后运行复盘，会按**有伤停数据的场次**单独统计：原始预测命中数 vs 伤停调整后命中数。例如：
```bash
python3 main.py prediction-log recap [--date 2026-02-14]
```
输出中会包含一行：`伤停：有伤停数据的场次 N 场 — 原始预测命中 A，伤停调整后命中 B（有提升/无提升）`。补充更多、更准的伤停数据后，复盘样本变大，可长期观察 B 是否稳定高于 A。

**API-FOOTBALL 赔率与积分榜（多源融合与排名微调）**

- **赔率**：`python3 main.py odds-api [--date YYYY-MM-DD] [--merge]` 拉取指定日期五大联赛胜平负赔率，写入 `data/api_football_odds.json`。预测时若体彩无赔率会自动用该缓存做赔率融合；也可与体彩赔率互补。
- **赔率按 DB 补全**：按 DB 中 2024/2025/2026 的比赛日期（或按赛季日期范围）批量拉取并 merge，可在现有比赛数据上把赔率补全：`python3 main.py odds-api-by-db [--years 2024,2025,2026]` 或 `python3 scripts/fetch_odds_by_match_dates.py [--years 2024,2025,2026]`；可选 `--limit N`、`--dry-run`。**注意**：API-FOOTBALL 文档约定 Odds 仅提供赛前 1～14 天、且只保留 **7 天历史**，故无法拉取数月/整年历史；长期历史赔率需竞彩等其它源。若 dashboard 报「赔率端点不存在」需在后台开通 Odds。
- **积分榜**：`python3 main.py standings-api [--season 2025]` 拉取五大联赛当季积分榜，写入 `data/api_football_standings.json`。预测时会按主客队排名做小幅期望进球微调（主队排名更靠前则略升主队、降客队，由 `PREDICT_USE_STANDINGS_NUDGE`、`PREDICT_STANDINGS_NUDGE_FACTOR` 控制）。建议赛季中定期执行一次 `standings-api` 以更新排名。
- **统计**：`python3 main.py statistics-api [--date YYYY-MM-DD] [--merge]` 拉取指定日期场次的射门/控球等统计，写入 `data/api_football_statistics.json`，可用于复盘或后续特征。
- **阵容（上场人员）**：按 DB 比赛批量拉取完整阵容并落库，**断点续拉**、可限次数（适配 7500 次/天）：`python3 main.py lineups-by-db --years 2024,2025 [--skip-existing] [--limit 500] [--rate-limit 0.05]`。默认跳过 `match_lineups` 中已有记录的场次；`--limit N` 本次最多拉 N 场；付费档可加 `--rate-limit 0.05` 加速。`--dry-run` 仅打印待拉场次数。
- **阵容（赛前预计）**：主源为 **懂球帝** 预计首发，预测时优先读 `dongqiudi_lineups`，无则回退 `transfermarkt_lineups`（Transfermarkt 网页/接口访问受限，暂作回退）。命令：`python3 main.py dongqiudi-lineups [--date YYYY-MM-DD] [--no-save] [--rate-limit SEC] [--max-matches N]`。与体彩射手名单结合，将「关键球员不在预计首发且未在伤停」的人数叠加到伤停 key_count。**防封**：默认 2.5 秒间隔（`DONGQIUDI_RATE_LIMIT_SEC`）、单次最多 25 场（`DONGQIUDI_MAX_REQUESTS_PER_RUN`）。备用：`python3 main.py transfermarkt-lineups ...` 写 `transfermarkt_lineups`（需能访问 TM 时使用）。
- **API 预测融合**：设置 `PREDICT_USE_API_PREDICTIONS=true`、`PREDICT_API_PREDICTIONS_WEIGHT=0.25` 后，在 `prediction-log add` 等流程中会请求 API-Football 的 predictions 接口，将其胜平负预测与模型概率线性融合（每场约 2 次请求：fixtures + predictions）。

## 后续扩展

- 更多联赛/赛季、更多数据源  
- 对话式问答、本地仪表盘  
- 需要时再启用 `api/` 做本地调试（如 `python -m uvicorn api.app:app --reload`）