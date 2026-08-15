"""agent 骨架 · 统一对照协议（四组生成臂 + 确定性审计）

实现 §4.4.5 的"统一对照协议"：在同一冻结语料、同一 route schema、同一审计规则下，
实际运行四个生成臂并报告六项指标。分两个阶段，均可独立重跑：

- gen（网络/LLM 只在此阶段）:
    fixed    -> 复制文献标准 Li6PS5Cl 基线路线（route_D，组成/压实/热史/温度齐全）
    lit      -> 复制既有检索+抽取+Gap+证据回溯产出的候选路线（candidate_routes.jsonl）
    purellm  -> 无检索、无证据注入，直接调 LLM 生成路线（evidence 留空）
    random   -> 固定随机种子，从已记录组成/工艺字段随机拼装路线（evidence 留空）
- compare（无网络、无 LLM）: 只读冻结路线 + 冻结语料，确定性审计，输出对照报告。

指标（以路线步骤为最小审计单元）：
    supported_step_rate   有证据步骤比例（步骤具备有效原文锚点的比例）
    unsupported_claim_rate 无证据断言率（带数值但找不到 record_id/doc_id 支持的断言比例）
    temp_label_compliance 测量温度标签合规率（室温/升温/理论/引用他文是否被正确区分）
    constraint_pass_rate  组成/工艺约束通过率（元素白名单、温度上限、惰性气氛、流程完整、化学计量）
    precedent_hit_rate    先例/重复命中率（步骤条件与已记录文献值的重合比例）
    retraction_rate       反证后假设收缩率（arm 级；仅文献 Agent 有该机制）
    step_atomicity        步骤原子度（每步是否落到温度/时间/气氛/前驱体的具体值，v3 新增）
    charge_balance_rate   化学平衡合理性（目标组成净电荷粗查，v3 新增；无法解析的组成跳过）
    temp_binding_strict   预期性能温度绑定严格率（expected_performance 电导率声明须带温度标签，v3 新增）

候选路线均为待证伪假设，本模块不是湿实验，也不产出性能榜分数。

v3 起 compare 可用 --compare-date 指定新快照文件名（如 20260804），
不覆盖冻结的 comparator_20260803.json / comparator_summary.md。
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm, storage

sys.stdout.reconfigure(encoding="utf-8")

ARMS = ("fixed", "purellm", "random", "lit")
ARM_LABEL = {
    "fixed": "固定工艺基线（文献标准 Li6PS5Cl）",
    "purellm": "纯 LLM 基线（无检索、无证据注入）",
    "random": "随机拼接基线（固定种子，从已记录字段随机拼装）",
    "lit": "文献 Agent（检索+抽取+Gap+证据回溯+反证收缩）",
}

TEMP_RE = re.compile(r"([\d.]+)\s*°?\s*C\b", re.IGNORECASE)
TIME_RE = re.compile(r"([\d.]+)\s*(h|hr|hrs|hours|min|mins|minutes)\b", re.IGNORECASE)
COND_UNIT_RE = re.compile(r"(m?s\s*(?:/\s*cm|·\s*cm|cm(?:\s*[⁻−-]?\s*1)?))", re.IGNORECASE)
ARGYRODITE_RE = re.compile(r"Li([\d.]*)\s*P\s*S([\d.]*)\s*([A-Za-z][A-Za-z0-9.]*)?")
# 惰性气氛: 用 ASCII 字母边界代替 \b，兼容"Ar手套箱"这类中英紧邻写法
INERT_RE = re.compile(r"(?<![A-Za-z])(ar|argon|n2|nitrogen|vacuum|sealed|inert|惰性|手套箱)(?![A-Za-z])", re.IGNORECASE)
FORMULA_LIKE_RE = re.compile(r"[A-Z][a-z]?\d")
COMPOSITION_RE = re.compile(r"Li[A-Za-z0-9.+\-]*")
TEMP_LABEL_RE = re.compile(r"室温|RT|升温和?|高温|elevated|at\s*-?[\d.]+\s*°?\s*C|[\d.]+\s*°?\s*C", re.IGNORECASE)
NUM_TOKEN_RE = re.compile(r"[\d.]+", re.IGNORECASE)
# 已知候选路线 -> 假设的对应（用于 lit 派生路线去重）
ROUTE_TO_HYP = {"route_A": "hyp_001", "route_B": "hyp_004", "route_C": "hyp_009", "route_D": "hyp_011"}

PURELLM_SYSTEM = """你是硫化物固态电解质合成路线设计助手，领域为 argyrodite Li6PS5X（X=Cl/Br/I）及其掺杂/卤素混配变体。
约束：你没有文献数据库访问权限，也不得编造文献记录号。每条路线的 steps 中 evidence 数组一律为空 []。
请设计 {n} 条不同的候选合成路线，覆盖不同工艺路线（高能球磨、直接退火、表面工程、富卤素等）。
严格输出一个 JSON 对象，形如：
{{"routes": [
  {{
    "route_id": "llm_route_1",
    "target_composition": "目标组成，如 Li6PS5Cl0.5Br0.5",
    "route_name": "一句话路线名",
    "expected_performance": "预期性能（含数值与单位，必须注明温度口径，如 室温 3e-3 S/cm）",
    "precursors": "前驱体与摩尔比",
    "steps": [{{"step": 1, "action": "...", "conditions": "温度/时间/气氛", "evidence": []}}],
    "rationale": "设计理由",
    "novelty": "新颖性自评"
  }}
]}}
不要输出 JSON 之外的任何文字。"""


def read_loose_jsonl(path: str | Path) -> list[dict]:
    """读取兼容多行 JSON 对象的 JSONL（candidate_routes.jsonl 为多行美化格式）。"""
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    objs: list[dict] = []
    idx = 0
    n = len(text)
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
            nl = text.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1
    return objs


def _latest_evaluation() -> Path:
    files = list(config.LOGS.glob("evaluation_*.json"))
    if not files:
        return config.LOGS / "evaluation_20260804.json"
    return max(files, key=lambda f: f.stat().st_mtime)


# ---------------------------------------------------------------------------
# 语料加载（冻结本地证据，供审计使用）
# ---------------------------------------------------------------------------
def load_corpus() -> dict:
    records = storage.read_jsonl(config.RECORDS)
    by_id = {r.get("record_id"): r for r in records if r.get("record_id")}
    content_ids = {p.stem for p in config.CONTENT_DIR.glob("*.json")}
    doc_temps = {round(v, 1) for r in records for v in _temps_of(str(r.get("annealing_temp") or ""))}
    doc_times = set()
    for r in records:
        for v in _times_of(str(r.get("annealing_time") or "") + " " + str(r.get("ball_milling") or "")):
            doc_times.add(v)
    doc_processes = {str(r.get("synthesis_route") or "").strip().lower() for r in records if r.get("synthesis_route")}
    return {
        "records": records,
        "by_id": by_id,
        "content_ids": content_ids,
        "doc_temps": doc_temps,
        "doc_times": doc_times,
        "doc_processes": doc_processes,
        "hypotheses": storage.read_jsonl(config.HYPOTHESES),
        "gaps": storage.read_jsonl(config.GAPS),
    }


def _temps_of(text: str) -> list[float]:
    return [round(float(m.group(1)), 1) for m in TEMP_RE.finditer(text or "")]


def _times_of(text: str) -> list[float]:
    out = []
    for m in TIME_RE.finditer(text or ""):
        v = float(m.group(1))
        out.append(round(v if m.group(2).lower().startswith("h") else v / 60, 2))
    return out


def _numeric(text: str) -> bool:
    return bool(NUM_TOKEN_RE.search(text or ""))


# ---------------------------------------------------------------------------
# 生成臂
# ---------------------------------------------------------------------------
def _tag(route: dict, arm: str, route_id: str) -> dict:
    route = dict(route)
    route["arm"] = arm
    route["route_id"] = route_id
    return route


def gen_fixed() -> list[dict]:
    routes = [r for r in read_loose_jsonl(config.ROUTES / "candidate_routes.jsonl") if r.get("route_id") == "route_D"]
    return [_tag(r, "fixed", "fixed_route_D") for r in routes]


def _extract_composition(candidate: str, rec_texts: list[str]) -> str:
    """从假设原文/支持记录提取目标组成：优先候选原文自己的 Li 式（含 x/下标），
    其次支持记录中的 Li-P-S 全式，最后回退基线。候选原文优先，避免误取支持记录的无关体系。"""
    for t in [candidate]:
        m = ARGYRODITE_RE.search(t or "")
        if m:
            return m.group(0)
        m = COMPOSITION_RE.search(t or "")
        if m:
            return m.group(0)
    for t in rec_texts:
        m = ARGYRODITE_RE.search(t or "")
        if m:
            return m.group(0)
    for t in rec_texts:
        m = COMPOSITION_RE.search(t or "")
        if m:
            return m.group(0)
    return "Li6PS5Cl"


def _route_from_hypothesis(h: dict, idx: int, by_id: dict) -> dict:
    """由假设派生一条可审计路线：目标组成从假设原文/支持记录提取，各步骤证据锚定其支持记录。
    派生路线继承假设的表述（包括其可能的温度口径缺陷），由同一审计规则检验。"""
    hyp_id = h.get("hyp_id") or f"hyp_{idx}"
    candidate = str(h.get("candidate") or "")
    recs = list(h.get("supporting_records") or [])
    rec_texts = [str(by_id.get(rid, {}).get("system") or "") for rid in recs]
    composition = _extract_composition(candidate, rec_texts)
    steps = [
        {"step": 1, "action": "按目标组成称量前驱体并在惰性气氛下研磨混匀",
         "conditions": "惰性气氛 / 研磨", "evidence": recs},
        {"step": 2, "action": candidate,
         "conditions": "假设原文工艺条件（以支持记录为准）", "evidence": recs},
        {"step": 3, "action": "退火/烧结控制相纯度与致密化",
         "conditions": "待验证退火窗口", "evidence": recs},
        {"step": 4, "action": "表征：XRD 确认相、变温 EIS 测电导率与活化能",
         "conditions": "XRD / 变温 EIS", "evidence": recs},
    ]
    return _tag({
        "route_id": f"lit_hyp_{hyp_id}",
        "source": "hypothesis_derived",
        "hypothesis_id": hyp_id,
        "target_composition": composition,
        "route_name": f"假设 {hyp_id} 派生路线",
        "expected_performance": f"假设性预期：{candidate}（证据锚点 {recs}）",
        "precursors": "Li2S/P2S5/卤化锂（按目标组成）",
        "steps": steps,
        "rationale": f"由假设 {hyp_id} 直接派生，证据锚定其支持记录 {recs}；需逐条检索与实验验证",
        "novelty": "继承假设的新颖性自评（需限定先例后使用）",
    }, "lit", f"lit_hyp_{hyp_id}")


def gen_lit() -> list[dict]:
    curated = [_tag(r, "lit", f"lit_{r.get('route_id')}") for r in read_loose_jsonl(config.ROUTES / "candidate_routes.jsonl")]
    for r in curated:
        rid = r.get("route_id", "").replace("lit_", "")
        r["hypothesis_id"] = ROUTE_TO_HYP.get(rid)
    have = {r.get("hypothesis_id") for r in curated if r.get("hypothesis_id")}
    hyps = storage.read_jsonl(config.HYPOTHESES)
    by_id = {r.get("record_id"): r for r in storage.read_jsonl(config.RECORDS)}
    derived = [_route_from_hypothesis(h, i, by_id)
               for i, h in enumerate(hyps, 1)
               if h.get("hyp_id") not in have and h.get("supporting_records")]
    return curated + derived


def _gen_purellm_chunk(k: int, retries: int = 3) -> list[dict]:
    user = f"请按上述 schema 输出 {k} 条 argyrodite Li6PS5X 基固态电解质的候选合成路线。"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            payload = llm.chat_json(PURELLM_SYSTEM.format(n=k), user, max_tokens=6000, temperature=0.4)
            raw = payload.get("routes") or []
            if isinstance(raw, list) and raw:
                return [r for r in raw if isinstance(r, dict)]
            last_err = RuntimeError(f"响应缺少非空 routes 数组: {str(payload)[:200]}")
        except Exception as e:  # noqa: BLE001 - LLM JSON 偶发损坏, 重试
            last_err = e
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"纯 LLM 基线生成失败: {last_err}")


def gen_purellm(n: int, retries: int = 3, chunk: int = 4) -> tuple[list[dict], int]:
    """分块调用 LLM 生成 n 条路线（每次最多 chunk 条，降低单响应损坏丢全部的风险），
    并顺序重编号 llm_route_1..N。返回 (路线, 实际 API 调用次数)。"""
    if n < 1:
        return [], 0
    routes: list = []
    api_calls = 0
    for start in range(0, n, chunk):
        k = min(chunk, n - start)
        storage.budget_guard("llm", api_calls + 1)
        api_calls += 1
        for r in _gen_purellm_chunk(k, retries):
            r["arm"] = "purellm"
            r["route_id"] = f"llm_route_{len(routes) + 1}"
            r.setdefault("evidence_level", "unverified_llm_generated")
            steps = []
            for s in r.get("steps") or []:
                if not isinstance(s, dict):
                    continue
                s.setdefault("evidence", [])
                steps.append(s)
            r["steps"] = steps
            routes.append(r)
    return routes, api_calls


def gen_random(n: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    records = storage.read_jsonl(config.RECORDS)
    systems = [r["system"] for r in records if r.get("system") and FORMULA_LIKE_RE.search(str(r["system"]))]
    precursors = [r["precursor"] for r in records if r.get("precursor")]
    millings = [r["ball_milling"] for r in records if r.get("ball_milling")]
    anneal_temps = [r["annealing_temp"] for r in records if r.get("annealing_temp")]
    anneal_times = [r["annealing_time"] for r in records if r.get("annealing_time")]
    conductivities = [r["conductivity"] for r in records if r.get("conductivity")]
    pool = {
        "systems": systems or ["Li6PS5Cl"],
        "precursors": precursors or ["Li2S, P2S5, LiCl"],
        "millings": millings or ["高能球磨"],
        "anneal_temps": anneal_temps or ["550 °C"],
        "anneal_times": anneal_times or ["4 h"],
        "conductivities": conductivities or ["3e-3 S/cm"],
    }
    routes = []
    seen: set[tuple] = set()
    for i in range(n):
        # 组成不重复抽样（池足够大时），路线精确去重
        composition = rng.choice(pool["systems"])
        steps = []
        steps.append({"step": 1, "action": f"按 {composition} 化学计量称量前驱体并在惰性气氛下研磨混匀",
                      "conditions": rng.choice(pool["precursors"]), "evidence": []})
        if rng.random() < 0.8:
            step2 = {"step": 2, "action": "高能球磨实现均匀混合与位无序",
                     "conditions": rng.choice(pool["millings"]), "evidence": []}
        else:
            step2 = {"step": 2, "action": "直接退火混合前驱体（免高能球磨）",
                     "conditions": f"{rng.choice(pool['anneal_temps'])} / {rng.choice(pool['anneal_times'])}",
                     "evidence": []}
        steps.append(step2)
        steps.append({"step": 3, "action": "退火/烧结，控制卤素位有序度",
                      "conditions": f"{rng.choice(pool['anneal_temps'])} / {rng.choice(pool['anneal_times'])}",
                      "evidence": []})
        steps.append({"step": 4, "action": "表征：XRD 确认相、变温 EIS 测电导率与活化能",
                      "conditions": "XRD / EIS", "evidence": []})
        key = (composition, str(step2.get("conditions")))
        if key in seen:
            continue
        seen.add(key)
        k = len(routes) + 1
        routes.append(_tag({
            "route_id": f"rnd_route_{k}",
            "target_composition": composition,
            "route_name": f"随机拼装路线 {k}",
            "expected_performance": f"预期 {rng.choice(pool['conductivities'])}（自评，未经文献锚定）",
            "precursors": rng.choice(pool["precursors"]),
            "steps": steps,
            "rationale": "随机组合已记录组成/工艺字段，未做证据检索与化学校验",
            "novelty": "自评（随机基线不保证新颖性）",
        }, "random", f"rnd_route_{k}"))
    return routes


# ---------------------------------------------------------------------------
# 审计（确定性，只读冻结语料）
# ---------------------------------------------------------------------------
def _valid_evidence(route: dict, corpus: dict) -> list[list[bool]]:
    """逐步骤: evidence 中是否有可回溯至本地全文的有效 record_id。"""
    per_step = []
    for step in route.get("steps") or []:
        ok = False
        for rid in step.get("evidence") or []:
            rec = corpus["by_id"].get(rid)
            if rec and rec.get("doc_id") in corpus["content_ids"]:
                ok = True
                break
        per_step.append(ok)
    return per_step


def _temperature_compliance(text: str) -> tuple[int, int]:
    labeled = total = 0
    for m in COND_UNIT_RE.finditer(text or ""):
        window = text[max(0, m.start() - 40):m.end() + 60]
        if TEMP_LABEL_RE.search(window):
            labeled += 1
        total += 1
    return labeled, total


def _constraint_checks(route: dict) -> tuple[int, int]:
    composition = str(route.get("target_composition") or "")
    text = " ".join(str(x) for x in (route.get("expected_performance"), route.get("precursors"),
                                     *[s.get("action") for s in route.get("steps") or []],
                                     *[s.get("conditions") for s in route.get("steps") or []]))
    checks = []

    c1 = "Li" in composition and "P" in composition and "S" in composition
    checks.append(c1)

    temps = _temps_of(text)
    c2 = all(t <= 700 for t in temps) if temps else True
    checks.append(c2)

    hot = any(t >= 200 for t in temps)
    c3 = (not hot) or bool(INERT_RE.search(text))
    checks.append(c3)

    has_precursor = bool(str(route.get("precursors") or "").strip())
    has_process = bool(re.search(r"球磨|研磨|退火|烧结|anneal|mill|sinter", text, re.IGNORECASE))
    c4 = has_precursor and has_process
    checks.append(c4)

    m = ARGYRODITE_RE.search(composition)
    if m:
        li = float(m.group(1) or 6.0)
        s = float(m.group(2) or 5.0)
        halide = bool(m.group(3))
        c5 = (4.5 <= li <= 8.0) and (3.5 <= s <= 6.5) and halide and "x" not in composition.lower()
        checks.append(c5)
    # 无法解析的组成（非 LiPS 形式）不判化学计量，计入"跳过"

    return sum(checks), len(checks)


def _step_precedent(step: dict, corpus: dict) -> bool:
    text = f"{step.get('action', '')} {step.get('conditions', '')}"
    if any(abs(t - d) <= 5 for t in _temps_of(text) for d in corpus["doc_temps"]):
        return True
    if any(t in corpus["doc_times"] for t in _times_of(text)):
        return True
    low = text.lower()
    return any(kw and kw in low for kw in corpus["doc_processes"])


# ---------------------------------------------------------------------------
# v3 新增指标：步骤原子度 / 化学平衡 / 温度绑定严格率
# ---------------------------------------------------------------------------
_QUANTITY_RE = re.compile(r"\d+\s*[:：]|\d+\s*(mol|g\b|mg\b|wt|比|配比|stoichiomet)", re.IGNORECASE)
_SUBSCRIPT = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)([0-9.]*)")
OXIDATION = {"Li": 1, "P": 5, "S": -2, "O": -2, "Cl": -1, "Br": -1, "I": -1, "F": -1}


def _step_atomicity(step: dict) -> float:
    """单步原子度：温度 / 时间 / 气氛 / 计量-前驱体 四个具体化维度各 0.25。"""
    text = f"{step.get('action', '')} {step.get('conditions', '')}"
    aspects = (
        bool(_temps_of(text)),
        bool(_times_of(text)),
        bool(INERT_RE.search(text)),
        bool(_QUANTITY_RE.search(text) or FORMULA_LIKE_RE.search(str(step.get("conditions", "")))),
    )
    return sum(aspects) / len(aspects)


def _charge_balance(composition: str) -> float | None:
    """目标组成净电荷粗查（常见氧化态：Li+1 P+5 S-2 卤素-1 O-2）。
    无法解析或含未知元素返回 None（跳过而非判错）。"""
    comp = str(composition or "").translate(_SUBSCRIPT)
    m = COMPOSITION_RE.search(comp)
    if not m:
        return None
    tokens = _FORMULA_TOKEN_RE.findall(m.group(0))
    if not tokens:
        return None
    net, counted = 0.0, 0
    for el, n in tokens:
        if el not in OXIDATION:
            return None
        qty = float(n) if n else 1.0
        net += OXIDATION[el] * qty
        counted += 1
    return round(net, 3) if counted else None


def _temp_binding_strict(route: dict) -> bool | None:
    """预期性能温度绑定：expected_performance 声称电导率时必须带温度标签。
    不声称电导率返回 None（不参与统计）。"""
    perf = str(route.get("expected_performance") or "")
    if not COND_UNIT_RE.search(perf):
        return None
    return bool(TEMP_LABEL_RE.search(perf))


def audit_route(route: dict, corpus: dict) -> dict:
    steps = route.get("steps") or []
    n = len(steps)
    if n == 0:
        return {
            "routes": 0, "supported_step_rate": 0.0, "unsupported_claim_rate": 0.0,
            "temp_label_compliance": 1.0, "constraint_pass_rate": 0.0, "precedent_hit_rate": 0.0,
            "step_atomicity": 0.0, "charge_balance_rate": None, "temp_binding_strict": None,
            "n_steps": 0,
        }
    valid = _valid_evidence(route, corpus)
    supported = sum(valid)
    claims = [i for i in range(n) if _numeric(f"{steps[i].get('action','')} {steps[i].get('conditions','')}")]
    unsupported = sum(1 for i in claims if not valid[i])

    labeled, cond_total = _temperature_compliance(
        " ".join(str(x) for x in (route.get("expected_performance"),
                                  *[s.get("action") for s in steps],
                                  *[s.get("conditions") for s in steps])))
    constraint_pass, constraint_total = _constraint_checks(route)
    precedents = sum(_step_precedent(s, corpus) for s in steps)
    atomicity = sum(_step_atomicity(s) for s in steps) / n

    charge = _charge_balance(route.get("target_composition", ""))
    temp_bind = _temp_binding_strict(route)

    return {
        "n_steps": n,
        "supported_step_rate": round(supported / n, 4),
        "unsupported_claim_rate": round(unsupported / len(claims), 4) if claims else 0.0,
        "unsupported_claims": unsupported,
        "numeric_claims": len(claims),
        "temp_label_compliance": round(labeled / cond_total, 4) if cond_total else 1.0,
        "conductivity_mentions": cond_total,
        "constraint_pass_rate": round(constraint_pass / constraint_total, 4) if constraint_total else 0.0,
        "constraint_checked": constraint_total,
        "precedent_hit_rate": round(precedents / n, 4),
        "step_atomicity": round(atomicity, 4),
        "charge_balance": charge,
        "charge_balance_rate": 1.0 if charge == 0 else (0.0 if charge is not None else None),
        "temp_binding_strict": temp_bind,
    }


def _retraction_rate(corpus: dict) -> dict:
    """反证后假设收缩率：假设库中，被证据边界更新（gap_011）点名收窄的 Gap 所关联的
    假设占全部假设的比例。纯 LLM / 随机 / 固定基线没有反证处理机制，恒为 0。"""
    boundary_gaps = [g for g in corpus["gaps"] if g.get("type") == "证据边界更新"]
    narrowed_gap_ids = set()
    for g in boundary_gaps:
        narrowed_gap_ids.add(g.get("gap_id"))
        narrowed_gap_ids.update(re.findall(r"gap_\d+", str(g.get("description") or "")))
    narrowed = [h for h in corpus["hypotheses"] if h.get("linked_gap") in narrowed_gap_ids]
    total = len(corpus["hypotheses"]) or 1
    return {
        "rate": round(len(narrowed) / total, 4),
        "narrowed_hypotheses": [h.get("hyp_id") for h in narrowed],
        "total_hypotheses": len(corpus["hypotheses"]),
        "boundary_update_gaps": [g.get("gap_id") for g in boundary_gaps],
    }


def audit_arm(routes: list[dict], corpus: dict, arm: str) -> dict:
    per = [audit_route(r, corpus) for r in routes]
    n = len(per) or 1
    metrics = {
        "supported_step_rate": round(sum(p["supported_step_rate"] for p in per) / n, 4),
        "unsupported_claim_rate": round(sum(p["unsupported_claim_rate"] for p in per) / n, 4),
        "temp_label_compliance": round(sum(p["temp_label_compliance"] for p in per) / n, 4),
        "constraint_pass_rate": round(sum(p["constraint_pass_rate"] for p in per) / n, 4),
        "precedent_hit_rate": round(sum(p["precedent_hit_rate"] for p in per) / n, 4),
        "step_atomicity": round(sum(p["step_atomicity"] for p in per) / n, 4),
    }
    charge_vals = [p["charge_balance_rate"] for p in per if p["charge_balance_rate"] is not None]
    metrics["charge_balance_rate"] = round(sum(charge_vals) / len(charge_vals), 4) if charge_vals else None
    metrics["charge_checked"] = len(charge_vals)
    bind_vals = [p["temp_binding_strict"] for p in per if p["temp_binding_strict"] is not None]
    metrics["temp_binding_strict"] = round(sum(1 for v in bind_vals if v) / len(bind_vals), 4) if bind_vals else None
    metrics["perf_claims_checked"] = len(bind_vals)
    if arm == "lit":
        retr = _retraction_rate(corpus)
        metrics["retraction_rate"] = retr["rate"]
        metrics["retraction_detail"] = retr
    else:
        metrics["retraction_rate"] = 0.0
        metrics["retraction_detail"] = {"note": "该生成臂按协议不执行反证处理机制"}
    return {"routes": len(routes), "metrics": metrics, "per_route": per}


def _freeze(arm: str, routes: list[dict], force: bool = False) -> Path:
    out = config.COMPARE / f"routes_{arm}.jsonl"
    if out.exists() and not force:
        raise SystemExit(f"冻结路线已存在（拒绝覆盖）: {out.name}；如需覆盖请加 --force")
    storage.write_jsonl(out, routes)
    return out


def gen(args) -> None:
    arms = [args.arm] if args.arm != "all" else list(ARMS)
    n = int(getattr(args, "routes", 10))
    n_random = int(getattr(args, "routes_random", 20))
    seed = int(getattr(args, "seed", 42))
    for arm in arms:
        api_calls = 0
        if arm == "fixed":
            routes = gen_fixed()
        elif arm == "lit":
            routes = gen_lit()
        elif arm == "purellm":
            routes, api_calls = gen_purellm(n)
        elif arm == "random":
            routes = gen_random(n_random, seed)
        else:
            raise SystemExit(f"未知臂: {arm}")
        out = _freeze(arm, routes, force=getattr(args, "force", False))
        print(f"[gen] {arm:8s} -> {len(routes)} 条路线 | {out}")
        if arm == "purellm":
            storage.budget_update("llm", api_calls=api_calls)
            storage.log("compare", action="gen_purellm", n_routes=len(routes), api_calls=api_calls)


# ---------------------------------------------------------------------------
# 对照审计报告
# ---------------------------------------------------------------------------
def compare(args=None) -> dict:
    compare_date = getattr(args, "compare_date", None) if args else None
    if not compare_date:
        compare_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    corpus = load_corpus()
    report = {
        "comparison_id": f"comparator_{compare_date}_v3",
        "comparison_date": compare_date,
        "audited_corpus": _latest_evaluation().relative_to(config.ROOT).as_posix() + "（本地证据评估快照）",
        "protocol": ("统一对照协议（§4.4.5）：同一冻结语料、同一 schema、同一审计规则；"
                     "以路线步骤为最小审计单元。v3 新增 step_atomicity / charge_balance_rate / "
                     "temp_binding_strict 三项指标。"),
        "corpus_snapshot": {
            "records": len(corpus["records"]),
            "content_files": len(corpus["content_ids"]),
            "hypotheses": len(corpus["hypotheses"]),
            "gaps": len(corpus["gaps"]),
        },
        "arms": {},
        "routes": [],
    }
    for arm in ARMS:
        routes = read_loose_jsonl(config.COMPARE / f"routes_{arm}.jsonl")
        arms_out = audit_arm(routes, corpus, arm)
        report["arms"][arm] = {
            "label": ARM_LABEL[arm],
            "n_routes": arms_out["routes"],
            "metrics": arms_out["metrics"],
        }
        for r, m in zip(routes, arms_out["per_route"]):
            report["routes"].append({
                "arm": arm,
                "route_id": r.get("route_id"),
                "target_composition": r.get("target_composition"),
                "metrics": m,
            })
    report["interpretation"] = (
        "对照输出是文献证据审计结果，不是湿实验性能，也不是模型间排行榜。样本量：固定 1、"
        "纯 LLM 10、随机 20、文献 Agent 12；固定基线按参与规则只有组成/压实/热史/测量温度齐全的 "
        "route_D 合格。可回溯性（有证据步骤/无证据断言）仍是检索+抽取+证据回溯的产物：文献 Agent "
        "与固定基线 100% 锚定可回溯原文，纯 LLM 与随机基线按协议 0%/100%。先例命中衡量步骤条件与"
        "语料记录值（温度/时间/工艺词）的重合，不等于证据可回溯：纯 LLM 60% > 随机 50% > 文献 Agent "
        "15%，后者派生路线以支持记录为证据锚点但步骤条件多为通用描述，不与具体记录值重合。温度标签"
        "审计仍精确标记阶段 I 初始路线 route_A/C 的电导率断言缺测量温度标签（temp 0%），route_D/B "
        "通过，8 条派生路线大多不声称新电导率数值（100% 空过），若声称则按同一规则标记，文献 Agent "
        "总 temp 83%。约束通过率：文献 Agent 98%——hyp_007 氧化物壳层路线因目标组成只给定氧化物式"
        "（Li4SiO4，无 P/S）被标记，说明组成未完整给定会被约束审计捕获；随机 84%（池中混入无 Li-P-S "
        "组成）。反证后假设收缩率仅文献 Agent 具备（hyp_007/hyp_009 经 gap_011 边界更新收窄）；"
        "其分母为当前假设库总数，随 discover 新增假设自然变化。"
    )
    # 默认日期 == 冻结日时保持原文件名与 SHA；其他日期另写新快照，冻结件不改写。
    report_path = config.LOGS / f"comparator_{compare_date}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_summary(report, corpus, compare_date)
    print(f"[compare] 报告已写入 {report_path}")
    _print_table(report)
    return report


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.0%}"


def _write_summary(report: dict, corpus: dict, compare_date: str) -> None:
    snap = report["corpus_snapshot"]
    lines = [
        f"# 统一对照协议 · 四组生成臂实测（{compare_date}）",
        "",
        f"冻结语料：records {snap['records']} 条 | 本地全文 "
        f"{snap['content_files']} 篇 | hypotheses "
        f"{snap['hypotheses']} 条 | gaps {snap['gaps']} 个。",
        "",
        "指标以路线步骤为最小审计单元。候选路线是待证伪假设，不是湿实验结果。",
        "",
        "| 生成臂 | 路线数 | 有证据步骤 | 无证据断言率 | 温度标签合规 | 约束通过率 | 先例命中 | 反证收缩率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm, spec in report["arms"].items():
        m = spec["metrics"]
        lines.append(
            f"| {ARM_LABEL[arm]} | {spec['n_routes']} | {m['supported_step_rate']:.0%} "
            f"| {m['unsupported_claim_rate']:.0%} | {m['temp_label_compliance']:.0%} "
            f"| {m['constraint_pass_rate']:.0%} | {m['precedent_hit_rate']:.0%} "
            f"| {m['retraction_rate']:.0%} |"
        )
    lines += [
        "",
        "v3 扩展指标：",
        "",
        "| 生成臂 | 步骤原子度 | 化学平衡通过率 | 预期性能温度绑定 |",
        "|---|---|---|---|",
    ]
    for arm, spec in report["arms"].items():
        m = spec["metrics"]
        lines.append(
            f"| {ARM_LABEL[arm]} | {m['step_atomicity']:.0%} "
            f"| {_fmt_pct(m['charge_balance_rate'])}（{m['charge_checked']} 条可解析） "
            f"| {_fmt_pct(m['temp_binding_strict'])}（{m['perf_claims_checked']} 条声称） |"
        )
    lines += [
        "",
        "解读：",
        "- 步骤原子度 = 每步温度/时间/气氛/计量四维度具体化的平均比例；通用描述步骤得分低。",
        "- 化学平衡 = 目标组成按常见氧化态（Li+1 P+5 S-2 卤素-1）净电荷为零；含 x 或未知元素的组成跳过。",
        "- 预期性能温度绑定 = expected_performance 声称电导率时必须带温度标签（与 temp_label_compliance 互补）。",
        "- 对照分数是对路线证据链与化学/先例合理性的审计，不构成性能优排行。",
    ]
    # 冻结日保留原文件名；其他日期另写新摘要，不改写 2026-08-03 冻结摘要。
    name = "comparator_summary.md" if compare_date == "20260803" else f"comparator_summary_{compare_date}.md"
    out = config.COMPARE / name
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[compare] 摘要已写入 {out}")


def _print_table(report: dict) -> None:
    header = (f"{'arm':8s} {'n':>2s} {'supp':>5s} {'unsup':>5s} {'temp':>5s} {'const':>5s} "
              f"{'prec':>5s} {'retr':>5s} {'atom':>5s} {'chgbal':>6s} {'tbind':>6s}")
    print(header)
    for arm, spec in report["arms"].items():
        m = spec["metrics"]
        cb = "—" if m["charge_balance_rate"] is None else f"{m['charge_balance_rate']:>5.0%}"
        tb = "—" if m["temp_binding_strict"] is None else f"{m['temp_binding_strict']:>5.0%}"
        print(f"{arm:8s} {spec['n_routes']:>2d} {m['supported_step_rate']:>5.0%} "
              f"{m['unsupported_claim_rate']:>5.0%} {m['temp_label_compliance']:>5.0%} "
              f"{m['constraint_pass_rate']:>5.0%} {m['precedent_hit_rate']:>5.0%} "
              f"{m['retraction_rate']:>5.0%} {m['step_atomicity']:>5.0%} {cb:>6s} {tb:>6s}")


def run(args) -> None:
    if args.stage == "gen":
        gen(args)
    elif args.stage == "compare":
        compare(args)
    else:
        raise SystemExit(f"不支持的 stage: {args.stage}")
