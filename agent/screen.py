"""agent 骨架 · 筛选阶段

规则筛选: 领域关键词命中（强词≥1 或弱词≥2）+ 排除已抽取 doc_id + 数量上限。
输出 screened_{ts}.jsonl（保留 hit_catalog 字段 + matched_kw）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

# 强相关词（argyrodite / 硫化物电解质专属）
STRONG_KW = [
    "argyrodite", "li6ps5cl", "li6ps5br", "li6ps5i", "li6ps5", "ps5cl", "ps5br",
    "li2s-p2s5", "thio-lisicon", "thiophosphate", "lithium thiophosphate",
    "sulfide solid electrolyte", "sulfide electrolyte", "sulfide-based electrolyte",
]
# 弱相关词（需累计 ≥2）
WEAK_KW = [
    "solid-state electrolyte", "solid state electrolyte", "lithium ion conductor",
    "ionic conductivity", "ball milling", "mechanochemical", "li2s", "p2s5",
    "halogen", "halide", "air stability", "moisture", "h2s", "anode", "cathode",
]


def _hit_text(r: dict) -> str:
    return f"{r.get('title', '')} {r.get('chunk', '')}".lower()


def matches(r: dict) -> list[str]:
    text = _hit_text(r)
    strong = [k for k in STRONG_KW if k in text]
    if strong:
        return strong
    weak = [k for k in WEAK_KW if k in text]
    return weak if len(weak) >= 2 else []


def sort_key(r: dict):
    try:
        return -float(r.get("score") or 0)
    except (TypeError, ValueError):
        try:
            return -float(r.get("citation_count") or 0)
        except (TypeError, ValueError):
            return 0


def run(args):
    all_hits = storage.read_jsonl(config.HIT_CATALOG)
    # 已抽取 doc_id（增量: 不再重复筛入）
    done = {r.get("doc_id") for r in storage.read_jsonl(config.RECORDS) if r.get("doc_id")}

    picked, no_match = [], 0
    for r in all_hits:
        doc = r.get("doc_id")
        if doc in done:
            continue
        kw = matches(r)
        if not kw:
            no_match += 1
            continue
        picked.append({**r, "matched_kw": kw})
    picked.sort(key=sort_key)
    max_n = getattr(args, "max", 300)
    if max_n and len(picked) > max_n:
        picked = picked[:max_n]

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = config.SCIVERSE_DIR / f"screened_{ts}.jsonl"
    storage.write_jsonl(out, picked)
    storage.log("screen", action="screen", n_hits=len(all_hits), n_new=len(picked),
                no_match=no_match, output=str(out))
    print(f"\n筛选完成: 候选 {len(picked)} 篇 | 无关键词命中 {no_match} | 已抽取跳过 {len(done)}")
    print("输出:", out)
    return out
