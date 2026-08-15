# -*- coding: utf-8 -*-
"""agent 骨架 · 调研报告生成器（--stage report）

从本地 JSON/JSONL 证据确定性渲染 Markdown 报告，无网络、无 LLM。
数据只来自 records/gaps/hypotheses/routes/normalized_conductivity/contradictions/
audit/comparator，保证"报告不是 LLM 自由发挥"，每条结论可回溯到 record_id/doc_id。

用法:
    python -m agent.run --stage report --snapshot 20260804
输出:
    <ROOT>/调研报告_auto_<snapshot>.md
"""
import json
import re
import sys
from datetime import datetime, timezone

from . import config, storage
from .evaluate import CORE_ARGYRODITE_RE, KEY_FIELDS

sys.stdout.reconfigure(encoding="utf-8")

MIN_ROWS = 15  # 矛盾清单正文展示上限


def _rate(n: int, d: int) -> str:
    return f"{n}/{d}（{100 * n / d:.1f}%）" if d else "0/0（-）"


def _latest(glob: str):
    files = sorted(config.LOGS.glob(glob))
    return files[-1] if files else None


def run_report(args):
    snap = getattr(args, "snapshot", None) or datetime.now(timezone.utc).strftime("%Y%m%d")
    date_fmt = f"{snap[:4]}-{snap[4:6]}-{snap[6:]}"

    records = storage.read_jsonl(config.RECORDS)
    gaps = storage.read_jsonl(config.GAPS)
    hyps = storage.read_jsonl(config.HYPOTHESES)
    routes = _read_routes(config.ROUTES / "candidate_routes.jsonl")
    content_ids = {p.stem for p in config.CONTENT_DIR.glob("*.json")}
    norm = storage.read_jsonl(config.KNOWLEDGE / "normalized_conductivity.jsonl")
    contra = storage.read_jsonl(config.KNOWLEDGE / "contradictions.jsonl")

    core = [r for r in records if CORE_ARGYRODITE_RE.search(str(r.get("system") or ""))]

    # 归一化状态分布
    norm_stat = {}
    for p in norm:
        norm_stat[p["status"]] = norm_stat.get(p["status"], 0) + 1
    exact_vals = sorted(p["value_s_cm"] for p in norm if p["status"] == "exact")

    # 审计结论
    audit_file = _latest("audit_20*.json")
    audit = json.loads(audit_file.read_text(encoding="utf-8")) if audit_file else {}

    # 四臂对照摘要
    comp_file = _latest("comparator_20*.json")
    comp = json.loads(comp_file.read_text(encoding="utf-8")) if comp_file else {}

    L = []
    A = L.append
    A("# 材料科学文献调研自动报告：Li6PS5Cl argyrodite 合成路线与工艺")
    A("")
    A(f"> 由 `python -m agent.run --stage report` 确定性生成（无 LLM、无网络）。")
    A(f"> 语料快照：{date_fmt}｜生成时间：{datetime.now(timezone.utc).isoformat()}｜"
      f"records {len(records)} 条 / 本地全文 {len(content_ids)} 篇。")
    A("")

    # 1 语料与字段覆盖
    A("## 1. 语料与字段覆盖")
    A("")
    A("| 指标 | 值 |")
    A("| --- | --- |")
    A(f"| 结构化记录 / 本地全文 | {len(records)} / {len(content_ids)} |")
    A(f"| 核心 argyrodite 记录（evaluate 正则口径） | {_rate(len(core), len(records))} |")
    A(f"| Research Gap / 候选假设 / 候选路线 | {len(gaps)} / {len(hyps)} / {len(routes)} |")
    A("")
    A("| 字段 | 非空比例 |")
    A("| --- | --- |")
    for f in KEY_FIELDS:
        A(f"| {f} | {_rate(sum(bool(r.get(f)) for r in records), len(records))} |")
    A("")

    # 2 归一化
    A("## 2. 电导率单位归一化（S/cm）")
    A("")
    A("| 状态 | 数量 |")
    A("| --- | ---: |")
    for k in ("exact", "multi", "geq", "leq", "approx", "unparseable", "empty"):
        if k in norm_stat:
            A(f"| {k} | {norm_stat[k]} |")
    if exact_vals:
        n = len(exact_vals)
        A("")
        A(f"可归一单值 **{n}** 条，值域 {exact_vals[0]:.2e} ~ {exact_vals[-1]:.2e} S/cm，中位 {exact_vals[n // 2]:.2e} S/cm。")
    A("")

    # 3 Gap
    A("## 3. Research Gap（%d 个）" % len(gaps))
    A("")
    A("| gap_id | 标题 | 依据 record_id | 新颖性 |")
    A("| --- | --- | --- | --- |")
    for g in gaps:
        ev = "、".join((g.get("evidence_record_ids") or [])[:4])
        A(f"| {g.get('gap_id')} | {g.get('title','')} | {ev} | {g.get('novelty','')} |")
    A("")

    # 4 假设
    A("## 4. 候选假设（%d 条）" % len(hyps))
    A("")
    A("| hyp_id | 候选 | 路线类型 | linked_gap | 新颖性 |")
    A("| --- | --- | --- | --- | --- |")
    for h in hyps:
        A(f"| {h.get('hyp_id')} | {h.get('candidate','')} | {h.get('route_type','')} | "
          f"{h.get('linked_gap','')} | {h.get('novelty','')} |")
    A("")

    # 5 路线
    A("## 5. 候选合成路线（%d 条）" % len(routes))
    A("")
    A("| route_id | 目标组成 | 路线名 | hyp_id |")
    A("| --- | --- | --- | --- |")
    for r in routes:
        A(f"| {r.get('route_id')} | {r.get('target_composition') or r.get('target') or ''} | "
          f"{r.get('route_name','')} | {r.get('hyp_id','')} |")
    A("")

    # 6 矛盾
    A("## 6. 跨文献矛盾清单（候选 %d 对，展示前 %d）" % (len(contra), min(len(contra), MIN_ROWS)))
    A("")
    A("> 未对齐测量温度的初筛：同一归一体系内电导率相差 ≥10 倍记为候选矛盾，需人工复核温度口径。")
    A("")
    A("| 体系 | 高值 S/cm | 低值 S/cm | 倍数 | record(高/低) | 温度(高/低) |")
    A("| --- | ---: | ---: | ---: | --- | --- |")
    for c in contra[:MIN_ROWS]:
        A(f"| {c.get('system_norm','')} | {c['value_hi_s_cm']:.2e} | {c['value_lo_s_cm']:.2e} | "
          f"x{c.get('ratio')} | {c.get('record_id_hi')}/{c.get('record_id_lo')} | "
          f"{c.get('temp_hi') or '-'} / {c.get('temp_lo') or '-'} |")
    A("")

    # 7 对照
    A("## 7. 四臂对照摘要")
    A("")
    arms = comp.get("arms", {})
    if arms:
        A("| 生成臂 | 路线数 | 有证据步骤 | 无证据断言 | 温度标签合规 | 约束通过率 | 先例命中 | 反证收缩率 |")
        A("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for arm, spec in arms.items():
            m = spec.get("metrics", {})
            A(f"| {spec.get('label', arm)} | {spec.get('n_routes', '-')} | "
              f"{_pct(m.get('supported_step_rate'))} | {_pct(m.get('unsupported_claim_rate'))} | "
              f"{_pct(m.get('temp_label_compliance'))} | {_pct(m.get('constraint_pass_rate'))} | "
              f"{_pct(m.get('precedent_hit_rate'))} | {_pct(m.get('retraction_rate'))} |")
    A("")

    # 8 审计
    A("## 8. 证据链审计")
    A("")
    summ = audit.get("summary", {})
    A(f"- 判定：**{summ.get('verdict', '未知')}**（{summ.get('interpretation', '')}）")
    for k in ("gaps", "hypotheses", "routes"):
        sec = audit.get(k, {})
        if "anchor_ok" in sec:
            A(f"- {k} 锚点有效率：{sec.get('anchor_ok')}/{sec.get('total', 0)}")
    A("")

    # 9 边界
    A("## 9. 边界声明")
    A("")
    A("- 候选 Gap/假设/路线均为待证伪假设，不代表湿实验结论或性能保证。")
    A("- 矛盾检测是同一归一体系内电导率相差 ≥10 倍的初筛，**未对齐测量温度/致密度/热史**，需逐条复核。")
    A("- 归一化只对『单值 + 单位』的明确写法生效；范围/多值/纯描述一律标记，不猜测数值。")

    out = config.ROOT / f"调研报告_auto_{snap}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"[report] 已输出: {out}")
    print(f"[report] 规模: records {len(records)} / gaps {len(gaps)} / hyps {len(hyps)} / "
          f"routes {len(routes)} / 矛盾 {len(contra)} / 归一化 exact {norm_stat.get('exact', 0)}")
    return out


def _pct(v) -> str:
    return "—" if v is None else f"{100 * float(v):.0f}%"


def _read_routes(path):
    """兼容多行 JSON 对象的路由文件读取。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    objs, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \n\r\t":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(text, idx)
            objs.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx += 1
    return objs


if __name__ == "__main__":
    class _A:
        snapshot = None
    run_report(_A())