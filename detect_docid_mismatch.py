# -*- coding: utf-8 -*-
"""检测 gaps.jsonl 里 evidence_doc_ids 与 records.jsonl 真实 doc_id 不一致（复用/伪造）。

用法（仓库根目录）:
    python detect_docid_mismatch.py
出口码: 0 = 无 mismatch；1 = 存在 mismatch。
"""
import sys

from agent import storage, config


def main():
    records = storage.read_jsonl(config.RECORDS)
    doc_of = {r["record_id"]: r.get("doc_id") for r in records if r.get("record_id")}
    gaps = storage.read_jsonl(config.GAPS)

    bad = 0
    for g in gaps:
        rids = g.get("evidence_record_ids") or []
        dids = g.get("evidence_doc_ids") or []
        gid = g.get("gap_id")
        if len(rids) != len(dids):
            print(f"[长度不一致] {gid}: record_ids={len(rids)} doc_ids={len(dids)}")
            bad += 1
            continue
        for i, (rid, did) in enumerate(zip(rids, dids)):
            true = doc_of.get(rid)
            if true is None:
                print(f"[record 不存在] {gid} #{i} {rid}")
                bad += 1
            elif true != did:
                print(f"[MISMATCH] {gid} #{i} {rid}: 填={did[:12] if did else '-'}… 真实={true[:12]}…")
                bad += 1
    print(f"\nMISMATCH 总数: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())