"""Deterministic evidence-quality evaluation for the literature Agent.

This stage reads only local JSON/JSONL evidence and produces a reproducible snapshot.
It does not call external APIs or change the scientific decision layer.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

CORE_ARGYRODITE_RE = re.compile(r"Li\s*[0-9.+-]*PS[0-9.+-]*(?:Cl|Br|I|F)", re.IGNORECASE)
KEY_FIELDS = (
    "system",
    "precursor",
    "synthesis_route",
    "ball_milling",
    "annealing_temp",
    "annealing_time",
    "conductivity",
    "measurement_temp",
    "activation_energy",
    "dopant",
    "air_stability",
)


def _rate(numerator: int, denominator: int) -> dict:
    return {
        "count": numerator,
        "total": denominator,
        "percent": round((100 * numerator / denominator) if denominator else 0.0, 1),
    }


def build_snapshot(snap: str = "20260804") -> dict:
    records = storage.read_jsonl(config.RECORDS)
    content_ids = {path.stem for path in config.CONTENT_DIR.glob("*.json")}
    record_doc_ids = [record.get("doc_id") for record in records if record.get("doc_id")]
    unique_record_ids = {record.get("record_id") for record in records if record.get("record_id")}
    unique_doc_ids = set(record_doc_ids)

    audit_path = config.KNOWLEDGE / "extraction_quality_audit_20260802.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    audit_records = audit.get("records", [])
    direct_audit = sum(item.get("classification") == "core_argyrodite_direct" for item in audit_records)
    limited_audit = len(audit_records) - direct_audit

    core_records = [
        record for record in records
        if CORE_ARGYRODITE_RE.search(str(record.get("system") or ""))
    ]
    field_coverage = {
        field: _rate(sum(bool(record.get(field)) for record in records), len(records))
        for field in KEY_FIELDS
    }
    source_anchors = sum(
        bool(record.get("doc_id")) and record.get("doc_id") in content_ids
        for record in records
    )
    persisted_chunks = sum(bool(str(record.get("source_chunk") or "").strip()) for record in records)

    date_fmt = f"{snap[:4]}-{snap[4:6]}-{snap[6:]}"
    return {
        "evaluation_id": f"evidence_quality_{snap}_v2",
        "revision": f"v2 - 协议指标与统一对照协议对齐；语料快照日期 {date_fmt}",
        "evaluation_type": "deterministic_local_evidence_baseline",
        "scope": "Local JSON/JSONL corpus only; no LLM or network calls.",
        "corpus": {
            "content_files": len(content_ids),
            "records": len(records),
            "unique_record_ids": len(unique_record_ids),
            "unique_record_doc_ids": len(unique_doc_ids),
            "unextracted_content_files": len(content_ids - unique_doc_ids),
            "core_argyrodite_records": _rate(len(core_records), len(records)),
        },
        "traceability": {
            "record_doc_id_present_in_local_content": _rate(source_anchors, len(records)),
            "persisted_source_chunk": _rate(persisted_chunks, len(records)),
            "note": (
                "Legacy records predate prospective source_chunk persistence. Their original "
                "content JSON remains the provenance source; see extraction_quality_audit_20260802.json."
            ),
        },
        "field_coverage": field_coverage,
        "audit_sample": {
            "reviewed_records": len(audit_records),
            "direct_core_evidence": direct_audit,
            "restricted_or_theoretical_evidence": limited_audit,
            "audit_file": str(audit_path.relative_to(config.ROOT)),
        },
        "pre_registered_comparators": {
            "fixed_process": "Literature-standard Li6PS5Cl route; compare only when composition, compaction, thermal history, and measurement temperature are recorded.",
            "pure_llm": "Generate routes without retrieval, then measure supported-step rate and unsupported-claim rate against the same local corpus.",
            "random_composition_process": "Randomly combine documented components and conditions, then measure chemical/evidence validity against the same rules.",
            "metrics": [
                "supported_step_rate",
                "unsupported_claim_rate",
                "measurement_temperature_label_compliance",
                "composition_process_constraint_pass_rate",
                "precedent_hit_rate",
                "hypothesis_retraction_or_narrowing_rate_after_counterevidence",
            ],
            "result_status": "Protocol defined; comparator executed on 2026-08-03 (see data/99_logs/comparator_20260803.json).",
        },
        "interpretation": (
            "This is a data-quality and traceability baseline, not a wet-lab performance result "
            "or a model-vs-model benchmark."
        ),
    }


def run(args):
    snap = getattr(args, "snapshot", None) or datetime.now(timezone.utc).strftime("%Y%m%d")
    output = config.LOGS / f"evaluation_{snap}.json"
    if output.exists() and not getattr(args, "force", False):
        raise SystemExit(f"评估快照已存在（冻结保护）: {output.name}；如需覆盖请加 --force")
    snapshot = build_snapshot(snap)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    corpus = snapshot["corpus"]
    print(
        "Evidence evaluation: "
        f"records {corpus['records']} | content {corpus['content_files']} | "
        f"remaining {corpus['unextracted_content_files']} | output {output}"
    )


if __name__ == "__main__":
    run(None)
