# -*- coding: utf-8 -*-
"""agent 骨架 · AgentScope 2.0 工具适配层（薄封装）

把现有十阶段函数包成 FunctionTool，供 LiteratureDiscoveryAgent 在 discover 循环中调用。
原则：
- 不改任何现有 stage 源码（search/screen/content/extract_batch/validate/storage）；
- 数据/审计层（evaluate/compare）不在此暴露——确定性审计不经过 AgentScope；
- 写工具强制证据锚点（record_id 必须已存在于 records.jsonl，无锚点拒写）；
- id 续编（gap_012+ / hyp_013+ / route_E+），旧记录绝不覆盖。
"""
import re
from datetime import datetime, timezone

from agentscope.tool import FunctionTool

from . import config, content, extract_batch, screen, search, storage, validate
from .comparator import read_loose_jsonl

FIELDS_SUMMARY = ("record_id", "doc_id", "title", "system", "dopant",
                  "synthesis_route", "conductivity", "annealing_temp", "notes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_num_id(existing: list[dict], key: str, prefix: str) -> str:
    nums = []
    for r in existing:
        m = re.match(rf"{prefix}_(\d+)", str(r.get(key, "")))
        if m:
            nums.append(int(m.group(1)))
    return f"{prefix}_{max(nums) + 1 if nums else 1:03d}"


def _record_ids() -> set[str]:
    return {r.get("record_id") for r in storage.read_jsonl(config.RECORDS)
            if r.get("record_id")}


# ---------------------------------------------------------------------------
# 检索 / 筛选 / 全文 / 抽取 / 验证（包装现有 stage）
# ---------------------------------------------------------------------------

def search_literature(query: str, top_k: int = 30) -> dict:
    """在 Sciverse 对单条查询做语义检索，命中增量合并进 hit_catalog（跨运行去重）。

    Args:
        query: 英文检索语句，如 "argyrodite Li6PS5Cl Cl Br halogen mixing ionic conductivity"。
        top_k: 返回条数上限（默认 30）。
    """
    key = config.get_key("sciverse")
    try:
        storage.budget_guard("sciverse")
    except storage.BudgetExceededError as e:
        return {"error": str(e), "hits": 0}
    catalog = {r["doc_id"]: r for r in storage.read_jsonl(config.HIT_CATALOG) if r.get("doc_id")}
    payload = search.call_agentic_search(key, query, top_k)
    hits, kind = search.extract_hits(payload)
    new_entries = []
    for h in hits:
        hid = search.hit_id(h)
        if hid and hid not in catalog:
            catalog[hid] = {
                "doc_id": hid, "title": search.hit_field(h, "title"),
                "year": search.hit_field(h, "publication_published_year"),
                "journal": search.hit_field(h, "publication_venue_name_unified"),
                "authors": h.get("author", []) if isinstance(h, dict) else [],
                "citation_count": search.hit_field(h, "citation_count"),
                "score": search.hit_field(h, "score"),
                "chunk": search.hit_field(h, "chunk", "evidence", "snippet", "content", "excerpt"),
                "queries": [query],
            }
            new_entries.append(catalog[hid])
        elif hid and query not in catalog[hid].get("queries", []):
            catalog[hid].setdefault("queries", []).append(query)
    storage.write_jsonl(config.HIT_CATALOG, list(catalog.values()))
    storage.append_jsonl(config.QUERY_LOG, [
        {"timestamp": _now(), "platform": "sciverse", "query": query,
         "top_k": top_k, "hits": len(hits), "source": "discover_agent"}])
    storage.budget_update("sciverse", api_calls=1, total_hits=len(hits),
                          unique_docs=len(catalog))
    return {"query": query, "hits": len(hits), "new_docs": len(new_entries),
            "new_doc_ids": [e["doc_id"] for e in new_entries][:20],
            "catalog_total": len(catalog)}


def screen_hits(max_n: int = 50) -> dict:
    """对 hit_catalog 做规则筛选（领域关键词 + 排除已抽取），输出最新 screened 清单。

    Args:
        max_n: 本次筛选清单数量上限（默认 50）。
    """
    import argparse
    out = screen.run(argparse.Namespace(max=max_n))
    rows = storage.read_jsonl(out)
    return {"screened_file": str(out), "candidates": len(rows),
            "top_doc_ids": [r.get("doc_id") for r in rows[:20]]}


def fetch_content(doc_ids: list[str]) -> dict:
    """按 doc_id 列表拉取 Sciverse 全文到本地 content/（增量，已存在跳过）。

    Args:
        doc_ids: 要拉取全文的 doc_id 列表（来自 search_literature / screen_hits）。
    """
    key = config.get_key("sciverse")
    config.CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    import json as _json
    ok, skipped, fail = 0, 0, []
    for doc in doc_ids:
        out = config.CONTENT_DIR / f"{doc}.json"
        if out.exists():
            skipped += 1
            continue
        try:
            storage.budget_guard("sciverse", ok + 1)
        except storage.BudgetExceededError as e:
            fail.append(doc)
            continue
        try:
            text, err = content.fetch_content(key, doc)
        except Exception as e:
            fail.append(doc)
            continue
        if err is not None or text is None:
            fail.append(doc)
            continue
        import json as _json
        out.write_text(_json.dumps({"doc_id": doc, "fetched_at": _now(), "text": text},
                                   ensure_ascii=False), encoding="utf-8")
        ok += 1
    if ok:
        storage.budget_update("sciverse", api_calls=ok)
    storage.log("content", action="content_agent", ok=ok, skipped=skipped, failed=len(fail))
    return {"fetched": ok, "skipped_existing": skipped, "failed": fail[:10]}


def extract_records(limit: int = 5) -> dict:
    """对 content/ 中未抽取的全文做 LLM 结构化抽取，增量追加 records.jsonl（断点续跑）。

    Args:
        limit: 本次最多抽取多少篇（默认 5，验证用小批量）。
    """
    import argparse
    extract_batch.run(argparse.Namespace(limit=limit, workers=1, sleep=0.2))
    records = storage.read_jsonl(config.RECORDS)
    return {"records_total": len(records),
            "last_record_ids": [r.get("record_id") for r in records[-limit:]]}


def validate_mp() -> dict:
    """从 hypotheses.jsonl 派生目标组成，向 Materials Project 查询 thermo/core 交叉验证。"""
    import argparse
    validate.run(argparse.Namespace())
    thermo = storage.read_jsonl(config.CROSS / "mp_thermo.jsonl")
    core = storage.read_jsonl(config.CROSS / "mp_core.jsonl")
    return {"mp_thermo_targets": len(thermo), "mp_core_targets": len(core),
            "latest": [r.get("target") for r in thermo[-5:]]}


# ---------------------------------------------------------------------------
# 知识库读写（供 agent 推理与写回）
# ---------------------------------------------------------------------------

def read_knowledge(topic: str | None = None, limit: int = 50) -> dict:
    """读取知识库摘要（records/gaps/hypotheses），供发现循环推理。

    Args:
        topic: 可选过滤词（在 system/dopant/notes/title 中子串匹配，大小写不敏感）。
        limit: records 返回条数上限（默认 50；gaps/hypotheses 全量返回，数量少）。
    """
    records = storage.read_jsonl(config.RECORDS)
    if topic:
        t = topic.lower()
        records = [r for r in records if t in " ".join(
            str(r.get(k) or "") for k in ("system", "dopant", "notes", "title")).lower()]
    gaps = storage.read_jsonl(config.GAPS)
    hyps = storage.read_jsonl(config.HYPOTHESES)
    return {
        "records_total": len(storage.read_jsonl(config.RECORDS)),
        "records": [{k: r.get(k) for k in FIELDS_SUMMARY} for r in records[-limit:]],
        "gaps": gaps,
        "hypotheses": hyps,
    }


def write_gap(title: str, description: str, evidence_record_ids: list[str],
              evidence_doc_ids: list[str], novelty: str) -> dict:
    """追加一条研究 Gap 到 gaps.jsonl（id 自动续编；无有效证据锚点则拒写）。

    Args:
        title: Gap 一句话标题。
        description: 描述：哪些已有报道（引 record_id）、缺什么联合报道。
        evidence_record_ids: 支撑该 Gap 的 record_id 列表（必须已存在于 records.jsonl）。
        evidence_doc_ids: 对应 doc_id 列表（与 record_id 对应）。
        novelty: 新颖性自评（高/中/低 + 一句理由）。
    """
    known = _record_ids()
    bad = [rid for rid in evidence_record_ids if rid not in known]
    if not evidence_record_ids or bad:
        return {"error": "证据锚点无效（无锚点不写）", "unknown_record_ids": bad,
                "hint": "先用 read_knowledge 查已有 record_id"}
    # doc_id 由 records 反查回填，不信任调用方传入的值（防止复用/伪造他人 doc_id）
    recs = {r.get("record_id"): r for r in storage.read_jsonl(config.RECORDS)}
    evidence_doc_ids = [recs[rid].get("doc_id") for rid in evidence_record_ids]
    gap_id = _next_num_id(storage.read_jsonl(config.GAPS), "gap_id", "gap")
    row = {"gap_id": gap_id, "type": "未联合报道组合", "title": title,
           "description": description, "evidence_record_ids": evidence_record_ids,
           "evidence_doc_ids": evidence_doc_ids, "novelty": novelty,
           "created_by": "LiteratureDiscoveryAgent", "created_at": _now()}
    storage.append_jsonl(config.GAPS, [row])
    return {"gap_id": gap_id, "written": True}


def write_hypothesis(candidate: str, route_type: str, rationale: str,
                     supporting_records: list[str], expected_mechanism: str,
                     novelty: str, linked_gap: str) -> dict:
    """追加一条假设到 hypotheses.jsonl（id 自动续编；无有效证据锚点则拒写）。

    Args:
        candidate: 候选材料/组成+工艺，如 "Li6PS5Cl0.5Br0.5 混卤 + 直接退火 550°C 10 h"。
        route_type: 路线类型标签，如 "卤素混配 × 直接退火工艺"。
        rationale: 理由：每步引用 record_id 说明文献支撑，指出未被联合报道之处。
        supporting_records: 支撑 record_id 列表（必须已存在于 records.jsonl）。
        expected_mechanism: 预期机理。
        novelty: 新颖性自评（高/中/低）。
        linked_gap: 关联的 gap_id（必须已存在于 gaps.jsonl）。
    """
    known = _record_ids()
    bad = [rid for rid in supporting_records if rid not in known]
    if not supporting_records or bad:
        return {"error": "证据锚点无效（无锚点不写）", "unknown_record_ids": bad,
                "hint": "先用 read_knowledge 查已有 record_id"}
    gap_ids = {g.get("gap_id") for g in storage.read_jsonl(config.GAPS)}
    if linked_gap not in gap_ids:
        return {"error": f"linked_gap {linked_gap} 不存在", "hint": "先 write_gap 或查 read_knowledge"}
    hyp_id = _next_num_id(storage.read_jsonl(config.HYPOTHESES), "hyp_id", "hyp")
    row = {"hyp_id": hyp_id, "candidate": candidate, "route_type": route_type,
           "rationale": rationale, "supporting_records": supporting_records,
           "expected_mechanism": expected_mechanism, "novelty": novelty,
           "linked_gap": linked_gap,
           "created_by": "LiteratureDiscoveryAgent", "created_at": _now()}
    storage.append_jsonl(config.HYPOTHESES, [row])
    return {"hyp_id": hyp_id, "written": True}


def write_route(route_name: str, hyp_id: str, target_composition: str,
                expected_performance: str, precursors: str,
                steps: list[dict], linked_gap: str) -> dict:
    """追加一条候选合成路线到 04_routes/candidate_routes.jsonl（route_id 自动续编 E+）。

    Args:
        route_name: 路线名称。
        hyp_id: 关联假设 id（必须已存在于 hypotheses.jsonl）。
        target_composition: 目标组成，如 "Li6PS5Cl0.5Br0.5"。
        expected_performance: 预期性能（引用 record_id 的量化依据）。
        precursors: 前驱体与配比。
        steps: 步骤列表，每项 {"step": 序号, "action": 操作, "conditions": 条件, "evidence": [record_id 列表]}。
        linked_gap: 关联 gap_id。
    """
    hyp_ids = {h.get("hyp_id") for h in storage.read_jsonl(config.HYPOTHESES)}
    if hyp_id not in hyp_ids:
        return {"error": f"hyp_id {hyp_id} 不存在", "hint": "先 write_hypothesis"}
    known = _record_ids()
    for s in steps:
        bad = [e for e in (s.get("evidence") or []) if e not in known]
        if bad:
            return {"error": f"step {s.get('step')} 证据锚点无效", "unknown_record_ids": bad}
    route_file = config.ROUTES / "candidate_routes.jsonl"
    letters = []
    for r in read_loose_jsonl(route_file):
        m = re.match(r"route_([A-Z])", str(r.get("route_id", "")))
        if m:
            letters.append(m.group(1))
    route_id = "route_" + (chr(max(ord(c) for c in letters) + 1) if letters else "A")
    row = {"route_id": route_id, "hyp_id": hyp_id, "target_composition": target_composition,
           "route_name": route_name, "priority": len(letters) + 1, "linked_gap": linked_gap,
           "expected_performance": expected_performance, "precursors": precursors,
           "steps": steps, "created_by": "LiteratureDiscoveryAgent", "created_at": _now()}
    import json as _json
    with route_file.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    return {"route_id": route_id, "written": True}


def budget_check() -> dict:
    """读取 API 预算累计（sciverse/llm/materials_project 调用数），供收敛/停止判断。"""
    path = config.BUDGET
    if not path.exists():
        return {"platforms": {}}
    import json as _json
    return _json.loads(path.read_text(encoding="utf-8"))


TOOLS = [
    FunctionTool(search_literature),
    FunctionTool(screen_hits),
    FunctionTool(fetch_content),
    FunctionTool(extract_records),
    FunctionTool(validate_mp),
    FunctionTool(read_knowledge),
    FunctionTool(write_gap),
    FunctionTool(write_hypothesis),
    FunctionTool(write_route),
    FunctionTool(budget_check),
]
