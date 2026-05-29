# 竞彩命中率回退 / 对照实验说明

`jc-recap-refresh-eval` 之后若全量「单场推荐」明显下降，通常来自三类变化的**叠加**：

1. **新校准** `data/calibration_params.json` → 三向概率形变，「按权重最高项」的推荐项会变。  
2. **新 ML** `data/outcome_ml_model.joblib` → 融合后结果会变。  
3. **`.env` 融合权重 + 切片**（如 E3、`off`）→ 与重算前不一致时也会变。

对照思路：**一次只改一类**（或显式关校准/关 ML），重算竞彩后再看指标，定位主因。

---

## 快速代理（推荐先做）

全量 2021–2026 重算每次约数十分钟～数小时。可先用**单年**当风向标（默认 `AB_YEAR=2025`）：

```bash
export AB_YEAR=2025
bash scripts/jc_ablation_compare.sh backup
bash scripts/jc_ablation_compare.sh run exp_current          # 仅评估，不重算（看当前库）
bash scripts/jc_ablation_compare.sh run exp_no_calib         # 关校准 + 只重算 AB_YEAR + 评估该年
bash scripts/jc_ablation_compare.sh run exp_no_ml            # 关 ML + …
bash scripts/jc_ablation_compare.sh run exp_no_calib_no_ml   # 两项都关 + …
bash scripts/jc_ablation_compare.sh run exp_old_weights      # 旧权重 0.53/0.21 + …
bash scripts/jc_ablation_compare.sh run exp_restore_calib_20260314  # 恢复旧校准文件 + …
```

每次 `run exp_*` 会打印 **「单场推荐」** 在该年的结果，对比哪条最接近你期望。

---

## 全量确认（定位清楚后再做）

```bash
export JC_REPREDICT_YEARS=2021,2022,2023,2024,2025,2026
bash scripts/jc_ablation_compare.sh run exp_no_calib   # 示例：确认关校准后全量回升
bash scripts/jc_ablation_compare.sh eval-full           # 全量单场推荐（不重算）
```

---

## 恢复「当前」校准/ML（实验搞乱后）

每次 `backup` 会生成 `data/backup/ablation_时间戳/`。把其中文件拷回 `data/` 即可。

---

## 仓库里已有的历史备份

- `data/backup/calibration_params.json.20260314_141607`  
- `data/backup/calibration_params.json.20260310_220954`  
- ML 仅有 `.meta.json` 备份，**没有**保证同目录有对应 `.joblib`；`exp_restore_calib_20260314` **只回退校准**，不回退 ML。

若确认「新 ML」有害，可：`PREDICT_USE_OUTCOME_ML=false` 重算，或自行从别处拷贝旧 `outcome_ml_model.joblib` 到 `data/`。

---

## 决策建议

| 现象 | 可尝试 |
|------|--------|
| 关校准后指标明显回升 | 保留旧校准或缩小 `calibrate-probs` 训练窗、检查是否过拟合 |
| 关 ML 后回升 | 降低 `PREDICT_OUTCOME_ML_WEIGHT` 或暂时关 ML |
| 仅旧权重回升 | `.env` 回到 0.53/0.21（或网格再搜），切片按需 |
| 都要关才回升 | 新校准与新 ML 与当前权重不匹配，需分步重训或固定一套基线 |
