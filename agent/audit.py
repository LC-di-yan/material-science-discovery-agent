"""agent 骨架 · 知识层质量审计（--stage audit）

对 gaps / hypotheses / routes 做确定性审计（零网络、零 LLM）：
- 证据锚点完整性：record_id / doc_id 存在且可回溯至本地全文
- 关联一致性：linked_gap / hyp_id 引用有效
- 近似重复检测：标题/描述字符二元组 Jaccard
- 温度标签合规：性能声明绑定测量温度
- ID 续编完整性：无重复、无跳号

用法:  python -m agent.run --stage audit
输出:  data/99_logs/audit_YYYYMMDD.json
"""
import json
import re
import sys
from datetime import datetime, timezone

from . import config, storage
from .comparator import COND_UNIT_RE, TEMP_LABEL_RE, read_loose_jsonl

sys.stdout.reconfigure(encoding="utf-8")


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", str(text or "")).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else {t} if t else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _traceable(record: dict, content_ids: set[str]) -> bool:
    return bool(record.get("doc_id")) and record["doc_id"] in content_ids


def run(args):
    records = storage.read_jsonl(config.RECORDS)
    by_id = {r.get("record_id"): r for r in records if r.get("record_id")}
    content_ids = {p.stem for p in config.CONTENT_DIR.glob("*.json")}
    gaps = storage.read_jsonl(config.GAPS)
    hyps = storage.read_jsonl(config.HYPOTHESES)
    routes = read_loose_jsonl(config.ROUTES / "candidate_routes.jsonl")
    gap_ids = {g.get("gap_id") for g in gaps}
    hyp_ids = {h.get("hyp_id") for h in hyps}

    report = {
        "audit_id": "knowledge_quality",
        "audit_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "audit_type": "deterministic_local_knowledge_audit",
        "scope": "gaps/hypotheses/routes anchor integrity, duplication, temp labels; no LLM or network.",
        "corpus": {"records": len(records), "content_files": len(content_ids),
                   "gaps": len(gaps), "hypotheses": len(hyps), "routes": len(routes)},
    }

    def check_anchors(ids: list, label: str) -> list[dict]:
        issues = []
        for rid in ids or []:
            rec = by_id.get(rid)
            if rec is None:
                issues.append({"kind": label, "record_id": rid, "issue": "record_id 不存在"})
            elif not _traceable(rec, content_ids):
                issues.append({"kind": label, "record_id": rid,
                               "issue": f"doc_id {rec.get('doc_id')} 无本地全文"})
        return issues

    # 1. Gap 锚点与 ID
    gap_issues, gap_anchor_ok = [], 0
    for g in gaps:
        ids = g.get("evidence_record_ids") or []
        found = check_anchors(ids, "gap")
        gap_issues.extend({"gap_id": g.get("gap_id"), **i} for i in found)
        if ids and not found:
            gap_anchor_ok += 1
        dup_doc = len(ids) != len({by_id.get(r, {}).get("doc_id") for r in ids if r in by_id})
        if dup_doc:
            gap_issues.append({"gap_id": g.get("gap_id"), "issue": "多条 record 指向同一 doc_id（非错误，提示复核）"})
        # 配对一致性：evidence_doc_ids 必须与 record_id 反查的真实 doc_id 逐对一致（防复用/伪造）
        dids = g.get("evidence_doc_ids") or []
        if len(ids) != len(dids):
            gap_issues.append({"gap_id": g.get("gap_id"),
                               "issue": "evidence_doc_ids 与 evidence_record_ids 数量不一致"})
        else:
            for rid, did in zip(ids, dids):
                rec = by_id.get(rid)
                if rec and rec.get("doc_id") != did:
                    gap_issues.append({"gap_id": g.get("gap_id"),
                                       "issue": f"{rid} 的 evidence_doc_ids({str(did)[:12]}…) "
                                                f"≠ records 真实 doc_id({str(rec.get('doc_id'))[:12]}…)"})
    report["gaps"] = {"total": len(gaps), "anchor_ok": gap_anchor_ok,
                      "anchor_ok_rate": round(gap_anchor_ok / len(gaps), 4) if gaps else None,
                      "issues": gap_issues}

    # 2. 假设锚点 + linked_gap
    hyp_issues, hyp_anchor_ok = [], 0
    for h in hyps:
        hid = h.get("hyp_id")
        found = check_anchors(h.get("supporting_records") or [], "hypothesis")
        hyp_issues.extend({"hyp_id": hid, **i} for i in found)
        if h.get("supporting_records") and not found:
            hyp_anchor_ok += 1
        if h.get("linked_gap") and h["linked_gap"] not in gap_ids:
            hyp_issues.append({"hyp_id": hid, "issue": f"linked_gap {h['linked_gap']} 不存在"})
    report["hypotheses"] = {"total": len(hyps), "anchor_ok": hyp_anchor_ok,
                            "anchor_ok_rate": round(hyp_anchor_ok / len(hyps), 4) if hyps else None,
                            "issues": hyp_issues}

    # 3. 路线锚点 + 引用 + 温度绑定
    route_issues, route_anchor_ok = [], 0
    for r in routes:
        rid = r.get("route_id")
        step_ids = sorted({e for s in r.get("steps", []) for e in (s.get("evidence") or [])})
        found = check_anchors(step_ids, "route")
        route_issues.extend({"route_id": rid, **i} for i in found)
        if step_ids and not found:
            route_anchor_ok += 1
        if r.get("hyp_id") and r["hyp_id"] not in hyp_ids and r.get("source") != "hypothesis_derived":
            route_issues.append({"route_id": rid, "issue": f"hyp_id {r['hyp_id']} 不存在"})
        if r.get("linked_gap") and r["linked_gap"] not in gap_ids:
            route_issues.append({"route_id": rid, "issue": f"linked_gap {r['linked_gap']} 不存在"})
        perf = str(r.get("expected_performance") or "")
        if COND_UNIT_RE.search(perf) and not TEMP_LABEL_RE.search(perf):
            route_issues.append({"route_id": rid, "issue": "expected_performance 声称电导率但缺温度标签"})
    report["routes"] = {"total": len(routes), "anchor_ok": route_anchor_ok,
                        "anchor_ok_rate": round(route_anchor_ok / len(routes), 4) if routes else None,
                        "issues": route_issues}

    # 4. 近似重复（Gap 之间两两比较，规模小可全量）
    near_dup = []
    grams = [(g.get("gap_id"), _bigrams(str(g.get("title", "")) + str(g.get("description", "")))) for g in gaps]
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            sim = _jaccard(grams[i][1], grams[j][1])
            if sim >= 0.6:
                near_dup.append({"a": grams[i][0], "b": grams[j][0], "similarity": round(sim, 3)})
    report["near_duplicates"] = near_dup

    # 5. ID 续编完整性
    def id_check(rows, key, prefix, pattern):
        ids = [str(r.get(key, "")) for r in rows]
        nums = sorted(int(m.group(1)) for i in ids if (m := re.match(pattern, i)))
        dup = sorted({i for i in ids if ids.count(i) > 1})
        gaps_in_seq = [n for n in range(nums[0], nums[-1] + 1) if n not in nums] if nums else []
        return {"total": len(rows), "numbered": len(nums), "duplicates": dup,
                "missing_numbers": gaps_in_seq, "next_expected": f"{prefix}_{nums[-1] + 1:03d}" if nums else f"{prefix}_001"}

    report["id_sequences"] = {
        "gaps": id_check(gaps, "gap_id", "gap", r"gap_(\d+)"),
        "hypotheses": id_check(hyps, "hyp_id", "hyp", r"hyp_(\d+)"),
    }
    route_letters = sorted(str(r.get("route_id", "")) for r in routes)
    report["id_sequences"]["routes"] = {"total": len(routes), "route_ids": route_letters}

    total_issues = len(gap_issues) + len(hyp_issues) + len(route_issues) + len(near_dup)
    report["summary"] = {
        "total_issues": total_issues,
        "verdict": "PASS" if total_issues == 0 else "REVIEW",
        "interpretation": ("全部知识层条目锚点有效且无重复/引用缺陷。"
                           if total_issues == 0 else
                           "存在需复核条目，见各节 issues；REVIEW 不等于数据无效，多数为提示级。"),
    }

    out = config.LOGS / f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[audit] gaps {report['gaps']['anchor_ok']}/{len(gaps)} | "
          f"hyps {report['hypotheses']['anchor_ok']}/{len(hyps)} | "
          f"routes {report['routes']['anchor_ok']}/{len(routes)} | "
          f"近似重复 {len(near_dup)} | 判定 {report['summary']['verdict']}")
    print(f"[audit] 报告: {out}")
    return report


if __name__ == "__main__":
    run(None)
