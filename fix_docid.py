# -*- coding: utf-8 -*-
"""回填 gaps.jsonl 的 evidence_doc_ids（按 record_id 从 records.jsonl 反查真实 doc_id）。

- 修复前自动备份 gaps.jsonl -> gaps.jsonl.pre_fixdocid（仅当备份不存在时）。
- 不信任历史填写的 doc_id；对每条 gap 的 evidence_record_ids 逐一反查真实 doc_id 并回填。
- 被改写的 gap 增加 evidence_doc_ids_repaired=true 留痕。

用法（仓库根目录）:
    python fix_docid.py
"""
import shutil

from agent import storage, config


def main():
    src = config.GAPS
    bak = src.with_name(src.name + ".pre_fixdocid")
    if not bak.exists():
        shutil.copy2(src, bak)
        print(f"已备份原始 gaps.jsonl -> {bak.name}")

    records = storage.read_jsonl(config.RECORDS)
    doc_of = {r["record_id"]: r.get("doc_id") for r in records if r.get("record_id")}

    gaps = storage.read_jsonl(src)
    repaired = 0
    for g in gaps:
        rids = g.get("evidence_record_ids") or []
        if not rids:
            continue
        new_dids = [doc_of[r] for r in rids if r in doc_of]
        if new_dids and new_dids != (g.get("evidence_doc_ids") or []):
            g["evidence_doc_ids"] = new_dids
            g["evidence_doc_ids_repaired"] = True
            repaired += 1
    storage.write_jsonl(src, gaps)
    print(f"修复 gap 条数: {repaired}（总 {len(gaps)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())