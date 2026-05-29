# 竞彩：重算写库 + 校准/ML + 2026 验证（推荐流程）

前提：`.env` 已设为当前最优默认（**E3 权重 + `JC_TIMESLICE_PROFILE=off`**）。

---

## 方案 A：一条命令（最省事，耗时最长）

会依次：**全量复盘 → 更新校准与 ML → 主库 `repredict-history` + 竞彩按年重算 → 再全量评估**。

```bash
cd /path/to/football_anlyision
python3 main.py jc-recap-refresh-eval
```

- 若库中年份很多、只想先刷近几年竞彩，可缩小重算年份（**注意**：仍会先跑主库 `repredict-history`，与是否传 `--years` 无关）：

```bash
python3 main.py jc-recap-refresh-eval --years 2024,2025,2026
```

---

## 方案 B：只关心竞彩写库（不跑主库 repredict-history）

`jc-recap-refresh-eval` 内部固定会调 `repredict-all`，**无法跳过主库**。若你**只要刷 `jc_predictions`**：

```bash
python3 main.py calibrate-probs
python3 main.py train-outcome-ml
# 按年重算竞彩（示例：含 2021–2026 则逐年执行）
for y in 2021 2022 2023 2024 2025 2026; do
  python3 main.py predict-jc-from-self-improvement --year "$y" --include-finished
done
python3 main.py jc-predictions-eval --single-rec-only --all-time
```

或用现有脚本（自动读 `.env`，务必 `--profile off` 与默认一致）：

```bash
bash scripts/predict_2026_timesliced.sh --profile off --dates 2021-01-01,2025-12-31
bash scripts/predict_2026_timesliced.sh --profile off --year 2026
```

---

## 2026 单独验证（训练期外、只看验证年）

重算完成后：

```bash
python3 main.py jc-predictions-eval --single-rec-only --year 2026
```

---

## 再往后：阈值与联赛过滤

1. **阈值**：在 `.env` 调高 `SINGLE_REC_MIN_PROB` / `JC_REC_DEFAULT_MIN_PROB` → 推荐场次变少，命中率常升。
2. **联赛过滤**：用 `jc-segment-auto` 或联赛参数 JSON，只对高命中联赛出推荐（见项目内其它文档）。

---

## 跑完后

- 若 API 未自动reload，**重启 uvicorn**，避免读到旧环境变量。
- 若全量「单场推荐」明显下降，做**对照实验**定位是校准 / ML / 权重：见 **`docs/jc_ablation_experiments.md`**，脚本 **`scripts/jc_ablation_compare.sh`**。

## 当天竞彩优先命中率（当前 .env 最优默认）

- `SINGLE_REC_MIN_PROB=0.68`、`JC_REC_DEFAULT_MIN_PROB=0.68`、`JC_REC_HIGH_CONFIDENCE_MIN_PROB=0.72`：少推、提高展示命中。  
- `JC_REC_USE_SEGMENT_STATS=true`：用 `data/jc_rec_segment_hit_rates.json`（不存在则自动不生效）。更新细分可跑：`python3 main.py jc-segment-auto` 或带 `--export-segment-stats` 的评估。  
- 改阈值后需对**当日**重新拉预测/写库，页面才一致。
