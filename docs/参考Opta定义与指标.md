# 参考 Opta 的定义与指标

项目在统计口径与高级指标上**对齐 [Opta](https://www.optasports.com/) / [Opta Analyst](https://theanalyst.com/) 的公开定义**，便于与行业一致、扩展新指标时有所依据。以下为参考来源与在本项目中的用法。

---

## 参考来源

| 主题 | 链接 | 说明 |
|------|------|------|
| **统计定义总览** | [Opta Football Stats Definitions](https://theanalyst.com/articles/opta-football-stats-definitions) | 射门、传球、对抗等事件与统计的官方定义 |
| **预期进球 (xG)** | [What Is Expected Goals (xG)?](https://theanalyst.com/articles/what-is-expected-goals-xg) | xG 含义、计算思路与常见误区 |
| **xG 演进** | [Evolving Expected Goals (xG)](https://theanalyst.com/articles/evolving-expected-goals-xg) | 现代 xG 模型考虑的因素（距离、角度、门将位置、压迫等） |

---

## 本项目已采用的指标

- **预期进球 (Expected Goals, xG)**  
  - **定义**：衡量机会质量，每脚射门对应 0～1 的得分概率，单场/球队 xG 为所有射门概率之和。  
  - **用法**：预测时优先使用 API-Football 单场统计中的 `expected_goals` 字段（若数据源为 Opta 或同类，即与上述定义一致）；无该字段时用「射正 / 总射门 × 系数」作为代理。详见 [按优先级加特征与评估实操.md](按优先级加特征与评估实操.md) 中的 xG 小节。  
- **射门相关**（来自 API 单场统计，命名与 Opta 对齐）：  
  - **Shots on Goal**：射正（含被门将扑出、被最后一名防守球员封堵等）。  
  - **Total Shots**：总射门（含被封堵、偏出等）。  
  - 上述两项在无 `expected_goals` 时用于 xG 代理计算。  
- **控球**：使用 API 中的 **Ball Possession** 等字段，用于复盘或展示；定义与 Opta 的控球统计一致。

展示层面：来自 API 预测接口的「预测进球」（如盘口 -2.5/-1.5）与 **xG（预期进球）** 区分命名，避免混淆。

---

## 后续可考虑的指标（依赖数据）

- **Expected Goals on Target (xGOT)**  
  - 射门后的期望进球（考虑射门落点），用于评估终结能力与门将表现。若数据源提供 xGOT 或 SGA，可作为赛前期望的微调特征。  
  - 参考：[What Are Expected Goals on Target (xGOT)?](https://theanalyst.com/articles/what-are-expected-goals-on-target-xgot)  
- **Sequences / Possessions**  
  - 序列与控球段、high turnovers、10+ 脚传递、build-up attacks、direct attacks 等，用于风格与场面分析；需序列级数据。  
  - 参考：[Possessions and Sequences in Football](https://theanalyst.com/articles/possessions-and-sequences-in-football)  
- **Live Win Probability (LWP) / Match Momentum**  
  - 赛中实时胜平负概率与场面势头，依赖实时事件；若未来做赛中/直播类功能可参考。  
  - 参考：[Live Win Probability](https://theanalyst.com/articles/live-win-probability)、[What is Match Momentum?](https://theanalyst.com/articles/what-is-match-momentum)

---

## 代码中的对齐方式

- 单场统计的指标类型名（如 `expected_goals`、`Shots on Goal`、`Total Shots`）在 `etl/api_football_statistics.py` 中以常量或统一命名使用，与 API 返回及 Opta 常用命名一致。  
- 新增统计类型时，优先对照 [Opta Football Stats Definitions](https://theanalyst.com/articles/opta-football-stats-definitions) 命名与含义，再写入本模块与本文档。
