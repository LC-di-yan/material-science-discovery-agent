"""agent 骨架 · LLM 批量抽取（核心）

从全文提取 method/property 片段, 调 LLM（Anthropic 兼容）逐篇抽取结构化记录,
增量追加 records.jsonl（保留历史, 已抽取 doc_id 跳过 = 断点续跑）。
"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config, storage, llm

sys.stdout.reconfigure(encoding="utf-8")

METHOD_KW = re.compile(r"\b(ball[- ]mill|mechanochem|stoichiometric|precursor|mixed|milling|anneal|sinter|grind|reagent|was synthesized|were synthesized|atmosphere|Li2S|P2S5|LiCl|LiBr|LiI)\b", re.IGNORECASE)
PROP_KW = re.compile(r"\b(ionic conduct|S cm|S/cm|mS cm|activation energy|conductivity|air.stab|moisture|H2S|phase purity|diffusion)\b", re.IGNORECASE)

SYSTEM_PROMPT = """你是材料科学文献信息抽取器，专注无机固态电解质，尤其是硫化物固态电解质（argyrodite Li6PS5X（X=Cl/Br/I）、Li3PS4、LPS（Li2S-P2S5）、LGPS、Li7PS6、氧硫化物、卤化物等）的合成与工艺。从给定文献段落抽取"体系-工艺-性能"结构化字段。严格输出一个 JSON 对象，不要输出其他文字。

字段：
{
 "system": 材料体系（如 "Li6PS5Cl" / "Li6PS5Cl0.75Br0.25" / "Li3PS4" / "0.75Li2S-0.25P2S5"），片段未明确体系则 null,
 "precursor": 前驱体（如 "Li2S, P2S5, LiCl"），无则 null,
 "synthesis_route": 合成路线（如 高能球磨 / 机械化学 / 直接退火 / 固相烧结 / 微波辅助），无则 null,
 "ball_milling": 球磨条件（转速/时间/罐体），无则 null,
 "annealing_temp": 退火/热处理温度（含单位），无则 null,
 "annealing_time": 热处理时间，无则 null,
 "atmosphere": 合成/处理气氛，无则 null,
 "conductivity": 离子电导率（数值+单位，如 "4.96e-3 S/cm"），无则 null,
 "measurement_temp": 电导率测试温度，无则 null,
 "activation_energy": 活化能（数值+单位），无则 null,
 "dopant": 掺杂元素或卤素混配组成，无则 null,
 "air_stability": 空气/水分稳定性描述，无则 null,
 "evidence_level": "experimental" 或 "review" 或 "theoretical",
 "notes": 简述该文体系、核心工艺/性能发现（中文，1-2 句）；信息不足则写 "信息不足"
}

规则：
1. 只要片段属于固态电解质/硫化物电解质领域，就尽力抽取；只有与固态电解质完全无关（如生物、药物、纯电化学器件）才全部置 null 且 notes 写 "不相关"。
2. 所有字段必须来自原文，未提及填 null。
3. 电导率/温度/活化能保留数值与单位，禁止改写。
4. 禁止编造任何字段。"""


def extract_passages(text: str) -> tuple[str, str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z][a-z]+: )", text) if len(p.strip()) > 60]
    def pick(kw, max_paras=2):
        hits = sorted([(p, len(kw.findall(p))) for p in paras if kw.findall(p)], key=lambda x: -x[1])
        return "\n".join(p for p, _ in hits[:max_paras])
    return pick(METHOD_KW)[:1600], pick(PROP_KW)[:1200]


def next_record_id(existing: list[dict]) -> str:
    nums = []
    for r in existing:
        m = re.match(r"rec_(\d+)", str(r.get("record_id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"rec_{max(nums) + 1 if nums else 1:03d}"


def normalize(rec: dict, src: dict, rid: str, evidence_chunk: str) -> dict:
    return {
        "record_id": rid,
        "doc_id": src.get("doc_id"),
        "title": src.get("title"),
        "year": src.get("year"),
        "journal": src.get("journal"),
        "system": rec.get("system"),
        "precursor": rec.get("precursor"),
        "synthesis_route": rec.get("synthesis_route"),
        "ball_milling": rec.get("ball_milling"),
        "annealing_temp": rec.get("annealing_temp"),
        "annealing_time": rec.get("annealing_time"),
        "atmosphere": rec.get("atmosphere"),
        "conductivity": rec.get("conductivity"),
        "measurement_temp": rec.get("measurement_temp"),
        "activation_energy": rec.get("activation_energy"),
        "dopant": rec.get("dopant"),
        "air_stability": rec.get("air_stability"),
        "evidence_level": rec.get("evidence_level") if rec.get("evidence_level") in ("experimental", "review", "theoretical") else "experimental",
        "notes": rec.get("notes"),
        "source_chunk": evidence_chunk[:500],
    }


def _process_one(fp):
    data = json.loads(fp.read_text(encoding="utf-8"))
    method, prop = extract_passages(data.get("text", ""))
    user = (f"标题: {data.get('title','')}\n年份: {data.get('year') or ''}\n期刊: {data.get('journal') or ''}\n"
            f"=== 合成方法段落 ===\n{method}\n=== 性能段落 ===\n{prop}")
    try:
        rec = llm.chat_json(SYSTEM_PROMPT, user, max_tokens=2000)
        # Persist the exact evidence sent for extraction, not an optional source `chunk` field.
        return data, rec, method + "\n" + prop, None
    except Exception as e:
        return data, None, "", e


def run(args):
    existing = storage.read_jsonl(config.RECORDS)
    done = {r.get("doc_id") for r in existing if r.get("doc_id")}
    files = sorted(config.CONTENT_DIR.glob("*.json"))
    todo = [f for f in files if f.stem not in done]
    limit = getattr(args, "limit", None)
    if limit:
        todo = todo[:limit]
    if not todo:
        print(f"无需抽取: 已有 {len(done)} 篇记录, content 无新 doc_id", flush=True)
        return

    workers = getattr(args, "workers", 2)
    # 预分配 record_id, 逐条即时落盘（可断点续跑 + 实时监控）
    rid = next_record_id(existing)
    print(f"待抽取 {len(todo)} 篇 (content 共 {len(files)}, 已记录 {len(done)}, 并发 {workers})", flush=True)

    ok, fail = 0, 0
    fails = []
    log_rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, fp in enumerate(todo, 1):
            try:
                storage.budget_guard("llm", ok + 1)
            except storage.BudgetExceededError as e:
                print(f"\n停跑: {e}", flush=True)
                break
            if i > 1:
                time.sleep(getattr(args, "sleep", 0.2))  # 串行提交, 间隔调用以规避 LLM 限流
            fut = ex.submit(_process_one, fp)
            data, rec, evidence_chunk, err = fut.result()
            if err:
                fail += 1
                fails.append(data["doc_id"])
                print(f"[{i:03d}/{len(todo)}] FAIL {data['doc_id'][:12]} {type(err).__name__}: {str(err)[:90]}", flush=True)
                continue
            norm = normalize(rec, data, rid, evidence_chunk)
            storage.append_jsonl(config.RECORDS, [norm])
            log_rows.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                             "action": "extract_llm", "record_id": rid, "doc_id": data["doc_id"],
                             "fields": sum(1 for v in (norm.get(k) for k in
                                ("system", "precursor", "synthesis_route", "conductivity", "activation_energy", "dopant")) if v)})
            ok += 1
            print(f"[{i:03d}/{len(todo)}] {rid} doc={data['doc_id'][:12]} system={norm.get('system')} cond={norm.get('conductivity')}", flush=True)
            rid = f"rec_{int(rid[4:]) + 1:03d}"

    storage.append_jsonl(config.EXTRACTION_LOG, log_rows)
    storage.budget_update("llm", api_calls=ok)
    print(f"\n抽取完成: 新增 {ok} 条 | 失败 {fail} | 累计记录 {len(storage.read_jsonl(config.RECORDS))}", flush=True)
    if fails:
        print("失败 doc_id 前若干:", [f[:12] for f in fails][:10], flush=True)
