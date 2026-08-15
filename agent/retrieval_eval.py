# -*- coding: utf-8 -*-
"""agent 骨架 · 检索质量自评估（--stage retrieval-eval）

从 search_*.jsonl 原始响应恢复 per-query 有序命中，用领域规则做"弱标注"相关性分级，
计算 P@K / 相对 Recall@K / nDCG@K，输出检索聚焦度自评报告。
纯本地确定性计算，无网络、无 LLM。

相关性分级（规则弱标注，非人工标注）：
- tier2 强相关：title/abstract/chunk 命中 STRONG_KW（argyrodite / Li6PS5Cl / 硫化物电解质强词）
- tier1 弱相关：命中 WEAK_KW ≥2
- tier0 无关：其余

边界：本评估的 P/R/nDCG 数值只用于"同一规则下的领域聚焦度自评估"，
不能与人工标注基准直接比较；升级路径 = 用 30–50 篇人工相关性标注集替换本弱标注。
"""
import json
import math
import sys
from datetime import datetime, timezone

from . import config, storage
from .screen import STRONG_KW, WEAK_KW

sys.stdout.reconfigure(encoding="utf-8")

K_VALUES = [5, 10, 20, 30]


def _text(hit) -> str:
    return f"{hit.get('title', '')} {hit.get('abstract', '')} {hit.get('chunk', '')}".lower()


def rel_tier(hit) -> int:
    t = _text(hit)
    if any(k in t for k in STRONG_KW):
        return 2
    weak = [k for k in WEAK_KW if k in t]
    return 1 if len(weak) >= 2 else 0


def _dcg(rels) -> float:
    return sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rels))


def _idcg(rels) -> float:
    return _dcg(sorted(rels, reverse=True))


def load_searches():
    """合并所有 search_*.jsonl；同 query 取最新一次调用。"""
    files = sorted(config.SCIVERSE_DIR.glob("search_*.jsonl"))
    by_query = {}
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            q = r.get("query")
            if q:
                by_query[q] = r
    return list(by_query.values()), [f.name for f in files]


def run_retrieval_eval(args):
    rows, files = load_searches()
    totals = {"tier2": 0, "tier1": 0, "tier0": 0}
    per_query = []
    for r in rows:
        payload = r.get("payload") or {}
        hits = payload.get("hits")
        if not isinstance(hits, list):
            continue
        rels = [rel_tier(h) for h in hits]
        if not rels:
            continue
        for x in rels:
            totals[("tier0", "tier1", "tier2")[x]] += 1
        rec = {"query": r.get("query"), "n": len(rels),
               "tier2": sum(x == 2 for x in rels), "tier1": sum(x == 1 for x in rels)}
        for K in K_VALUES:
            relK = rels[:K]
            kn = min(K, len(relK))
            rec[f"P_rel@{K}"] = round(sum(x >= 1 for x in relK) / kn, 4) if kn else 0.0
            rec[f"P_strong@{K}"] = round(sum(x == 2 for x in relK) / kn, 4) if kn else 0.0
            denom = max(1, sum(x >= 1 for x in rels))
            rec[f"recall_rel@{K}"] = round(sum(x >= 1 for x in relK) / denom, 4)
            rec[f"nDCG@{K}"] = round(_dcg(relK) / _idcg(relK), 4) if _idcg(relK) > 0 else 0.0
        per_query.append(rec)

    def avg(key):
        vals = [r[key] for r in per_query if key in r]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    summary = {
        "retrieval_eval_id": "rulebased_weak_label",
        "date": datetime.now(timezone.utc).isoformat(),
        "search_files": files,
        "n_queries": len(per_query),
        "n_hits": sum(totals.values()),
        "hit_distribution": totals,
        "note": "规则弱标注（STRONG_KW=强相关 tier2，WEAK_KW>=2=弱相关 tier1）；"
                "P@K/Recall@K(相对)/nDCG@K 仅为同一规则下的领域聚焦度自评估，非人工标注基准。",
        "avg": {k: avg(k) for k in
                [f"P_rel@{K}" for K in K_VALUES] + [f"P_strong@{K}" for K in K_VALUES] +
                [f"recall_rel@{K}" for K in K_VALUES] + [f"nDCG@{K}" for K in K_VALUES]},
    }
    out = config.LOGS / f"retrieval_eval_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    out.write_text(json.dumps({"summary": summary, "per_query": per_query},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    a = summary["avg"]
    print(f"[retrieval-eval] queries={len(per_query)} | 命中 {sum(totals.values())} 条 "
          f"(tier2 {totals['tier2']} / tier1 {totals['tier1']} / tier0 {totals['tier0']})")
    print(f"[retrieval-eval] P_rel@5/10/20 = {a['P_rel@5']:.3f}/{a['P_rel@10']:.3f}/{a['P_rel@20']:.3f}")
    print(f"[retrieval-eval] P_strong@5/10/20 = {a['P_strong@5']:.3f}/{a['P_strong@10']:.3f}/{a['P_strong@20']:.3f}")
    print(f"[retrieval-eval] recall_rel@5/10/20 = {a['recall_rel@5']:.3f}/{a['recall_rel@10']:.3f}/{a['recall_rel@20']:.3f}")
    print(f"[retrieval-eval] nDCG@5/10/20 = {a['nDCG@5']:.3f}/{a['nDCG@10']:.3f}/{a['nDCG@20']:.3f}")
    print(f"[retrieval-eval] 输出: {out}")
    return summary


if __name__ == "__main__":
    run_retrieval_eval(None)