# 竞彩时间切片 vs 统一参数（实验小结）

## 结论（2021-01-01～2025-12-31，单场推荐「全部」）

| 方案 | 约命中率 | 备注 |
|------|-----------|------|
| **统一参数 E3**（`model=0.50, api=0.24, ml=0.16`） | **~54.3%**（9391/17306） | 当前推荐默认基线 |
| `rolling_halfyear_v1` 等两段切片 | ~54.1% | 未超过 E3 |
| 两段 E3 微网格（`e3_slice_mg_*`） | 最高约 54.2% | 未超过 E3 |
| 三段切片（`e3_three_seg_*`） | 最高约 54.2%（`e3_three_seg_late_api`） | 未超过 E3 |

## 默认配置（已写入 `.env`）

- 融合权重对齐 **E3**。
- `JC_TIMESLICE_PROFILE=off`：`data/jc_timeslice_params.json` 中 `off` 为空列表，**不做按日覆盖**，预测与 `.env` 一致。

## 若仍要做切片实验

- 命令行可临时指定 profile（不改 `.env`）：

  ```bash
  bash scripts/predict_2026_timesliced.sh --dates 2021-01-01,2025-12-31 --profile rolling_halfyear_v1
  ```

- **三段 profile 已补 `07-01～12-31` 的 E3 段**：避免仅 1–6 月命中切片、7–12 月误用旧 `.env` 权重导致评估口径混杂。

## 可选：仍要半年切片上线时

将 `JC_TIMESLICE_PROFILE` 改回 `rolling_halfyear_v1`（或你验证过的 `e3_three_seg_late_api` 等），并 **重算** 对应区间预测后再评估。
