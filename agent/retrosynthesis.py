"""agent 骨架 · 逆向合成推理（--stage retro）

路线 C 评分点"鼓励将逆向合成分析与 LLM 推理能力结合"的实现：
对 candidate_routes.jsonl 中每条路线的目标组成，LLM 输出逆合成树
（产物 → 中间体 → 原料），每个可执行步骤只能引用该路线已有的
record_id 证据（提示注入 + 写盘前强制校验，无效锚点剔除并标注）。

- 纯 LLM 推理 + 文献锚点路径（不依赖 RDKit；RDKit 可装时可另行增强）
- 增量执行：已有 retro_id 的路线跳过；追加写入，不覆盖
- 预算硬上限：storage.budget_guard("llm")
- 产出：data/04_routes/retrosynthesis.jsonl + retrosynthesis.md（可读简报）
"""
import json
import re
import sys
from datetime import datetime, timezone

from . import config, llm, storage
from .comparator import read_loose_jsonl

sys.stdout.reconfigure(encoding="utf-8")

RETRO_LOG = config.LOGS / "retro_log.jsonl"
RETRO_JSONL = config.ROUTES / "retrosynthesis.jsonl"
RETRO_MD = config.ROUTES / "retrosynthesis.md"

SYSTEM_PROMPT = """你是硫化物固态电解质逆向合成分析助手，领域为 argyrodite Li6PS5X（X=Cl/Br/I）及其掺杂/混卤变体。
给定一条候选合成路线（目标组成、前驱体、正合成步骤、可用文献证据编号），请做逆向合成分析：
从目标产物出发，逐级拆解为中间体和起始原料，并给出每步的正向操作建议。

规则：
1. 引用证据时只能使用"可用证据编号"列表中的 record_id，禁止编造其他编号；某步无合适证据时 evidence 留空并在 rationale 注明"化学原理支撑"。
2. 每一步给出：disconnection（逆合成切断描述）、forward_action（正向操作）、conditions（温度/时间/气氛，若证据支持）、evidence（record_id 列表）。
3. 原料必须是 commercially available 或文献常用的锂/磷/硫/卤素前驱体（Li2S、P2S5、LiCl、LiBr、LiI、Li2O、单质 S/P 等）。
4. 惰性气氛（Ar）贯穿全程；涉及升温的步骤必须给出温度范围。
5. 严格输出一个 JSON 对象：
{"target": "目标组成", "precursor_set": ["起始原料列表"],
 "pathways": [{"step": 1, "from": "原料/中间体", "to": "中间体/产物",
               "disconnection": "逆合成切断描述", "forward_action": "正向操作",
               "conditions": "条件", "evidence": ["record_id"], "rationale": "依据"}],
 "confidence": "高/中/低", "notes": "边界说明（如升温数据来自哪条记录的哪个温度口径）"}
不要输出 JSON 之外的文字。"""


def _evidence_index(record_ids: list[str], by_id: dict) -> str:
    lines = []
    for rid in record_ids:
        rec = by_id.get(rid)
        if not rec:
            continue
        lines.append(f"- {rid}: 体系={rec.get('system') or '?'} | 工艺={rec.get('synthesis_route') or '?'} "
                     f"| 温度={rec.get('annealing_temp') or '?'} | 时间={rec.get('annealing_time') or '?'} "
                     f"| 电导率={rec.get('conductivity') or '?'}（{rec.get('measurement_temp') or '未注明温度'}）")
    return "\n".join(lines) if lines else "（无可用证据编号）"


def _retro_id(existing: list[dict]) -> str:
    """retro 序号续编：retro_001+。"""
    nums = [int(m.group(1)) for r in existing if (m := re.match(r"retro_(\d+)", str(r.get("retro_id", ""))))]
    return f"retro_{max(nums) + 1 if nums else 1:03d}"


def run(args):
    by_id = {r.get("record_id"): r for r in storage.read_jsonl(config.RECORDS) if r.get("record_id")}
    routes = read_loose_jsonl(config.ROUTES / "candidate_routes.jsonl")
    done = {json.loads(line).get("route_id")
            for line in RETRO_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()} if RETRO_JSONL.exists() else set()
    done_rows = read_loose_jsonl(RETRO_JSONL) if RETRO_JSONL.exists() else []
    todo = [r for r in routes if r.get("route_id") and r["route_id"] not in done]
    if not todo:
        print(f"无需逆合成: 全部 {len(routes)} 条路线已有 retro 结果")
        return

    limit = getattr(args, "limit", None)
    if limit:
        todo = todo[:limit]
    print(f"逆向合成: 待处理 {len(todo)} 条路线（已有 {len(done)}）", flush=True)

    ok, fail = 0, 0
    for i, route in enumerate(todo, 1):
        rid = route.get("route_id")
        route_records = sorted({e for s in route.get("steps", []) for e in (s.get("evidence") or []) if e in by_id})
        user = (f"路线 {rid}：{route.get('route_name')}\n"
                f"目标组成：{route.get('target_composition')}\n"
                f"前驱体：{route.get('precursors')}\n"
                f"正合成步骤：\n" + "\n".join(
                    f"  {s.get('step')}. {s.get('action')}（{s.get('conditions')}）"
                    for s in route.get("steps", [])) +
                f"\n\n可用证据编号（只能引用这些 record_id）：\n{_evidence_index(route_records, by_id)}")
        try:
            storage.budget_guard("llm", ok + 1)
        except storage.BudgetExceededError as e:
            print(f"\n停跑: {e}")
            break
        try:
            tree = llm.chat_json(SYSTEM_PROMPT, user, max_tokens=4000)
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(todo)}] {rid} FAIL {type(e).__name__}: {str(e)[:80]}")
            continue

        # 写盘前强制校验证据锚点：无效 record_id 剔除并标注
        stripped = []
        for step in tree.get("pathways", []):
            valid = [e for e in (step.get("evidence") or []) if e in by_id]
            if len(valid) != len(step.get("evidence") or []):
                stripped.append({"step": step.get("step"),
                                 "removed": [e for e in step.get("evidence") or [] if e not in by_id]})
            step["evidence"] = valid
            step["anchor_verified"] = bool(valid)

        row = {
            "retro_id": _retro_id(done_rows),
            "route_id": rid,
            "hyp_id": route.get("hyp_id"),
            "target": tree.get("target") or route.get("target_composition"),
            "precursor_set": tree.get("precursor_set", []),
            "pathways": tree.get("pathways", []),
            "confidence": tree.get("confidence"),
            "notes": tree.get("notes"),
            "evidence_pool": route_records,
            "stripped_invalid_anchors": stripped,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.append_jsonl(RETRO_JSONL, [row])
        done_rows.append(row)
        storage.log("retro", action="retro_llm", route_id=rid,
                    steps=len(row["pathways"]), stripped=len(stripped))
        ok += 1
        print(f"[{i}/{len(todo)}] {rid} -> {row['retro_id']} 步骤 {len(row['pathways'])} "
              f"剔除无效锚点 {len(stripped)}", flush=True)

    print(f"\n逆向合成完成: 新增 {ok} | 失败 {fail}")
    if ok:
        write_md()


def write_md():
    """把 retro JSONL 渲染为可读简报（确定性，无 LLM）。"""
    rows = [json.loads(line) for line in RETRO_JSONL.read_text(encoding="utf-8").splitlines()
            if line.strip()] if RETRO_JSONL.exists() else []
    lines = ["# 逆向合成分析简报 · 路线 C（LLM 推理 + 文献锚点）",
             "",
             f"> 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}；"
             f"共 {len(rows)} 条路线。每条路径的 evidence 已通过 records.jsonl 锚点校验，"
             "被剔除的无效锚点见 stripped_invalid_anchors。候选路径为待证伪假设。", ""]
    for r in rows:
        lines.append(f"## {r['retro_id']} · {r['route_id']}（目标 {r['target']}，置信 {r['confidence']}）")
        lines.append("")
        lines.append(f"起始原料：{'、'.join(r['precursor_set']) or '未给出'}")
        lines.append("")
        for s in r["pathways"]:
            ev = "、".join(s.get("evidence", [])) or "化学原理支撑（无文献锚点）"
            lines.append(f"{s.get('step')}. **{s.get('from')} → {s.get('to')}**")
            lines.append(f"   - 逆合成切断：{s.get('disconnection')}")
            lines.append(f"   - 正向操作：{s.get('forward_action')}（{s.get('conditions')}）")
            lines.append(f"   - 证据：{ev}")
            if s.get("rationale"):
                lines.append(f"   - 依据：{s['rationale']}")
            lines.append("")
        if r.get("notes"):
            lines.append(f"边界说明：{r['notes']}")
        lines.append("")
    RETRO_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"[retro] 简报: {RETRO_MD}")


if __name__ == "__main__":
    run(None)
