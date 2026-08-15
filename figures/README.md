# 提交图表

这些图表由 `scripts/generate_submission_figures.py` 从本地已存档证据确定性生成；不调用网络、LLM 或图像生成模型。

## 再生成

```bash
python scripts/generate_submission_figures.py
```

输出同时包含 PNG（Markdown 预览）和 SVG（可缩放提交/排版）。

## 图表与来源

| 图表 | 用途 | 主要数据来源 |
| --- | --- | --- |
| `fig_01_phase_progress` | 阶段 I 最小闭环与阶段 II 语料扩充规模 | `data/99_logs/evaluation_20260802.json` 与方案中已审计阶段 I/II 计数 |
| `fig_02_field_coverage` | 关键字段覆盖率与跨文献可比性限制 | `data/99_logs/evaluation_20260802.json` |
| `fig_03_evidence_architecture` | 自研流水线、数据目录和证据锚点 | 根目录 `archive/开发报告_Agent选型与架构.md`、现有 `data/` 目录 |
| `fig_04_counterevidence_narrowing` | 反证触发的假设收缩 | `data/02_knowledge/extraction_quality_audit_20260802.json`、`gap_011` |
| `fig_05_route_D_doe` | `route_D` 的可比性 DOE 和测量边界 | `data/04_routes/candidate_routes.jsonl`、`rec_107`、`rec_217` |
| `fig_06_comparator` | 统一对照协议四臂的确定性审计 | `data/99_logs/comparator_20260803.json` |

## 解释边界

- 图 1、图 2中的数量是 2026-08-02 的本地语料快照；346 篇全文全部完成结构化抽取，无待重试项。
- 图 3是工程架构图，不表示所有模块都已作为独立多代理服务上线；当前可运行 CLI 入口见 `agent/run.py`。
- 图 4展示的是已有先例如何限制早期过宽的新颖性表述，不将其解释为候选材料失败或性能下降。
- 图 5是待验证的实验设计，不是已完成湿实验，也不预设性能提升。
- 图 6是对路线证据链与化学/先例合理性的确定性审计，不是湿实验性能，也不是模型间排行；审计日 2026-08-03，语料快照 2026-08-02。
- Materials Project 在图表和方案中均只作为结构/热力学背景；数据库无条目不能用于证明路线可行性、新颖性或材料稳定性。
