# Agent 骨架 · 固态电解质文献调研（方向三）

参加 **GOAI 赛道三 · 方向三（材料科学文献驱动的科学发现智能体）· 主题一（argyrodite Li₆PS₅Cl 合成路线与工艺优化）**。

本骨架把「检索 → 筛选 → 全文 → LLM 抽取 → 证据质量评估 → MP 验证」串成可复跑命令，全部产出落在 `data/`，并保留 `doc_id -> record_id` 的回溯关系。

## 当前数据快照（2026-08-04）

- 778 篇已保存全文；778 条成功结构化记录（remaining 0，无待重跑项）；核心 argyrodite 占比 33.3%。
- 本地评估确认 778/778 记录可回溯至全文；gaps 19 / hypotheses 20 / routes 12（route_A–L）/ retro 12；audit 判定 PASS。
- 语料在当前筛选口径下已到上限（理论 ~785），详见《项目进度与验收记录》。
- 候选路线是待证伪假设，不代表已完成湿实验或保证性能。

## 依赖与配置

- Python 3.10+；数据/审计层（search/screen/content/extract/validate/evaluate/gen/compare）仅使用标准库。`discover` 阶段额外依赖 AgentScope 2.0（Python ≥3.11，见根目录 `requirements.txt`），为唯一第三方运行时依赖（Apache-2.0）。
- API key 优先从环境变量读取；本地可回退至 `_credentials.md`，但该文件不得提交或同步：
  - `SCIVERSE_API_TOKEN`
  - `MATERIALS_PROJECT_API_KEY`
  - `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
- 默认 LLM：tokenrhythm.studio Anthropic 兼容端点 `https://tokenrhythm.studio/v1` 与 `deepseek-v4-flash-0731`。可替换为任何兼容实现；模型变化会影响抽取完整度，必须保留输入全文和输出审计。

## 用法

```bash
# 1. LLM 连通性冒烟（调用外部 API）
python -m agent.run --stage smoke-llm

# 2. 检索（查询词表每行一个查询；调用 Sciverse）
python -m agent.run --stage search --queries queries/expansion_v1.txt --top-k 30 --limit 5

# 3. 筛选（规则 + 去重 + 上限）
python -m agent.run --stage screen --max 300

# 4. 拉取全文（增量）
python -m agent.run --stage content

# 5. LLM 批量抽取（增量；先小批，后全量）
python -m agent.run --stage extract --limit 5
python -m agent.run --stage extract

# 服务恢复后，低并发重跑残留失败项；成功 doc_id 自动跳过
python -m agent.run --stage extract --workers 1

# 6. Materials Project thermo/core 背景查询（调用外部 API）
python -m agent.run --stage validate

# 7. 本地、确定性证据质量评估（无网络、无 LLM）
python -m agent.run --stage evaluate

# 8. 统一对照协议：生成/冻结四臂路线（纯 LLM 臂调用外部 API；其余臂确定性）
#    默认样本量：fixed=1、lit=12（4 条既有候选 + 8 条假设派生）、purellm=10、random=20
python -m agent.run --stage gen --arm all --seed 42
python -m agent.run --stage gen --arm purellm --routes 10   # 覆盖纯 LLM 臂条数
python -m agent.run --stage gen --arm random --routes-random 20   # 覆盖随机臂条数

# 9. 统一对照协议：确定性审计（无网络、无 LLM）
python -m agent.run --stage compare

# 9b. 电导率单位归一化 + 系统化跨文献矛盾检测（确定性，无网络无 LLM）
python -m agent.run --stage normalize        # -> data/02_knowledge/normalized_conductivity.jsonl
python -m agent.run --stage contradictions   # -> data/02_knowledge/contradictions.jsonl

# 9c. 确定性调研报告生成（无 LLM、无网络；数据全来自本地 JSON/JSONL）
python -m agent.run --stage report --snapshot 20260804   # -> 调研报告_auto_20260804.md

# 9d. 检索质量自评估（规则弱标注的 P@K/Recall@K/nDCG@K 领域聚焦度）
python -m agent.run --stage retrieval-eval   # -> data/99_logs/retrieval_eval_YYYYMMDD.json

# 10. discover 自主发现循环（AgentScope 2.0；调用外部 API，推理事件流落 discover_log.jsonl）
python -m agent.run --stage discover --goal "Li6PS5Cl 卤素混配+掺杂+工艺联合优化" --max-rounds 3

# 11. 一步全跑（search -> screen -> content -> extract）
python -m agent.run --stage full --queries queries/expansion_v1.txt
```

`discover` 循环规则：写工具强制证据锚点（record_id 必须已存在，否则拒写）、id 续编不覆盖历史记录；收敛条件为轮次上限或轮内零新增；确定性审计（evaluate/compare）不经过该循环。

## 数据目录与语义

| 目录 | 内容 |
|---|---|
| `data/01_literature/sciverse/` | 检索原始响应、命中目录、筛选清单、全文 JSON |
| `data/02_knowledge/` | records、gaps、hypotheses、原文审计，及归一化电导率（normalized_conductivity.jsonl）与跨文献矛盾清单（contradictions.jsonl） |
| `data/03_cross_validate/` | Materials Project thermo/core 交叉验证 |
| `data/04_routes/` | 候选合成路线与可读简报 |
| `data/05_comparator/` | 统一对照协议四臂冻结路线与摘要（生成后冻结；审计阶段零网络零 LLM） |
| `data/99_logs/` | 检索、筛选、抽取、验证、预算、本地质量评估和四臂对照审计 |

## 证据与限制

- **增量原则**：检索、记录、Gap、假设和路线采用追加式保存；成功 `doc_id` 不重复抽取。
- **证据规则**：性能或工艺断言必须关联 `record_id` 和 `doc_id`。理论值、引用他文数值、室温与升温测量不可混写。
- **MP 边界**：无 MP 条目仅表示数据库覆盖缺口；MP 不验证合成路线，也不能替代相分析或湿实验。
- **质量评估**：`evaluate` 是规则化证据质量基线，不是湿实验性能，也不声称已完成纯 LLM 或随机基线对照。对照协议和指标保存于评估 JSON；四臂实测见 `data/05_comparator/` 与 `data/99_logs/comparator_20260803.json`。
- **商业 API 与复现**：Sciverse 用于文献数据、DeepSeek 用于抽取、MP 用于数据库背景。必须披露费用、权限、替代方案及模型/服务波动对复现的影响。
