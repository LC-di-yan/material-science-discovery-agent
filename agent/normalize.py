# -*- coding: utf-8 -*-
"""agent 骨架 · 电导率单位归一化 + 系统化跨文献矛盾检测

把 records.jsonl 里非结构化的 conductivity 字符串归一为统一单位 S/cm 的数值；
并对"同一归一体系内、电导率相差 ≥1 个数量级"的记录对做确定性矛盾检测。
全部为本地确定性计算，无网络、无 LLM。

-  --stage normalize        -> data/02_knowledge/normalized_conductivity.jsonl
-  --stage contradictions   -> data/02_knowledge/contradictions.jsonl

设计边界（诚实原则）：
- 只归一"单值 + 单位"的明确写法；范围/多值/纯描述一律标记，不强行猜测数值。
- 矛盾检测是"未对齐测量温度"的初筛：同一归一体系内比值 ≥10 倍记为候选矛盾，
  每条矛盾保留双方 measurement_temp，供后续人工复核。
"""
import json
import re
import sys
from datetime import datetime, timezone

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

# ---- 文本预处理：全角/上标/负号 -> ASCII ----
_TRANS = str.maketrans({
    "×": "x", "＊": "*", "．": ".",
    "−": "-", "⁻": "-",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})


def _to_sci(s: str) -> str:
    """把 10 的幂与缺乘号写法统一为 e 计数（负号必须与指数绑定，避免丢号）。"""
    # 剥离不确定度括号 3.0(2) -> 3.0
    s = re.sub(r"(\d(?:\.\d+)?)\(\d+\)", r"\1", s)
    # 有系数带乘号：5.21x10^-3 / 5.21*10-3 -> 5.21e-3
    s = re.sub(r"(\d(?:\.\d+)?)\s*[xX*×]\s*10\s*(?:\^\s*)?(-?\d+)", r"\1e\2", s)
    # 有系数缺乘号：3 10-4 -> 3e-4
    s = re.sub(r"(\d(?:\.\d+)?)\s+10\s*(?:\^\s*)?(-?\d+)", r"\1e\2", s)
    # 裸 10 带 ^：10^-3 / 10^{-3} -> 1e-3
    s = re.sub(r"(?<![\d.])10\s*\^\s*\{?\s*(-?\d+)\s*\}?", r"1e\1", s)
    # 裸 10 无 ^：10-3 -> 1e-3
    s = re.sub(r"(?<![\d.])10\s*(-\d+)", r"1e\1", s)
    return s


# 主 token：数值(可科学计数) + 单位(m?S) + 每cm（/cm、·cm、cm^-1、cm-1）
_TOKEN = re.compile(
    r"(?P<pre>[<>~≈]\s*)?"
    r"(?P<m>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*(?P<u>m?S)\s*(?:/\s*cm|·\s*cm|cm(?:\^?\s*-?\s*1)?)",
    re.IGNORECASE,
)


def parse_conductivity(text) -> dict:
    """解析 conductivity 字段并归一为 S/cm 数值。

    status: empty / unparseable / multi / range / exact / geq / leq
    """
    raw = str(text or "").strip()
    if not raw:
        return {"status": "empty", "value_s_cm": None, "raw": raw}
    s = _to_sci(raw.translate(_TRANS))
    tokens = list(_TOKEN.finditer(s))
    if not tokens:
        return {"status": "unparseable", "value_s_cm": None, "raw": raw}
    if len(tokens) > 1:
        return {"status": "multi", "value_s_cm": None, "raw": raw,
                "n_values": len(tokens)}
    t = tokens[0]
    m = float(t.group("m"))
    unit = (t.group("u") or "S").strip().lower()
    factor = 1e-3 if unit.startswith("m") else 1.0
    value = m * factor
    pre = (t.group("pre") or "").strip()
    if not pre:
        status = "exact"
    elif ">" in pre:
        status = "geq"
    elif "<" in pre:
        status = "leq"
    else:
        status = "approx"
    return {"status": status, "value_s_cm": value, "raw": raw,
            "matched": t.group(0).strip(), "unit": unit, "prefix": pre}


# 矛盾检测的合理离子电导范围（S/cm）：低于 1e-7 多为电子电导/测量噪声，高于 10 视为解析异常
COND_MIN = 1e-7
COND_MAX = 10.0

# 常见缩写别名（归一后小写、无空格）
_ALIAS = {
    "lpscl": "li6ps5cl",
    "lpsbr": "li6ps5br",
    "lpsi": "li6ps5i",
    "lps": "li2s-p2s5",
    "lgps": "li10gep2s12",
}


def normalize_system(text) -> str:
    """体系名基础归一：去括号注释、小写、去空格、别名映射。"""
    s = str(text or "").translate(_TRANS)
    outer = re.sub(r"\([^)]*\)|（[^）]*）", "", s).strip()
    if not outer:
        m = re.search(r"[\(（]([^)）]*)[\)）]", s)
        outer = (m.group(1).strip() if m else s)
    s = outer.lower().replace("−", "-").replace("–", "-")
    s = re.sub(r"\s+", "", s)
    return _ALIAS.get(s, s)


def run_conductivity(args):
    records = storage.read_jsonl(config.RECORDS)
    rows = []
    stats = {}
    for r in records:
        p = parse_conductivity(r.get("conductivity"))
        p["record_id"] = r.get("record_id")
        p["doc_id"] = r.get("doc_id")
        p["system"] = r.get("system")
        p["system_norm"] = normalize_system(r.get("system")) or None
        rows.append(p)
        stats[p["status"]] = stats.get(p["status"], 0) + 1

    out = config.KNOWLEDGE / "normalized_conductivity.jsonl"
    storage.write_jsonl(out, rows)
    exact = [p for p in rows if p["status"] == "exact"]
    print(f"[normalize] 输出: {out}")
    print(f"[normalize] 状态分布: {stats}")
    if exact:
        vals = sorted(p["value_s_cm"] for p in exact)
        n = len(vals)
        print(f"[normalize] 可归一(exact) {n} 条，值域 "
              f"{vals[0]:.3e} ~ {vals[-1]:.3e} S/cm，中位 {vals[n // 2]:.3e}")
    return rows


def run_contradictions(args):
    records = storage.read_jsonl(config.RECORDS)
    groups: dict[str, list] = {}
    for r in records:
        p = parse_conductivity(r.get("conductivity"))
        if p["status"] != "exact":
            continue
        if not (COND_MIN <= p["value_s_cm"] <= COND_MAX):
            continue
        sysn = normalize_system(r.get("system"))
        if not sysn or sysn in ("argyrodite", "li-argyrodite", ""):
            continue
        item = {"record_id": r.get("record_id"), "doc_id": r.get("doc_id"),
                "conductivity": r.get("conductivity"), "value_s_cm": p["value_s_cm"],
                "measurement_temp": r.get("measurement_temp")}
        groups.setdefault(sysn, []).append(item)

    rows = []
    pair_sets = set()
    for sysn, items in groups.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                va, vb = a["value_s_cm"], b["value_s_cm"]
                if va <= 0 or vb <= 0:
                    continue
                ratio = max(va, vb) / min(va, vb)
                if ratio < 10:
                    continue
                key = tuple(sorted([a["record_id"], b["record_id"]]))
                if key in pair_sets:
                    continue
                pair_sets.add(key)
                rows.append({
                    "system_norm": sysn,
                    "record_id_hi": (a if va >= vb else b)["record_id"],
                    "doc_id_hi": (a if va >= vb else b)["doc_id"],
                    "cond_hi": (a if va >= vb else b)["conductivity"],
                    "temp_hi": (a if va >= vb else b)["measurement_temp"],
                    "value_hi_s_cm": max(va, vb),
                    "record_id_lo": (b if va >= vb else a)["record_id"],
                    "doc_id_lo": (b if va >= vb else a)["doc_id"],
                    "cond_lo": (b if va >= vb else a)["conductivity"],
                    "temp_lo": (b if va >= vb else a)["measurement_temp"],
                    "value_lo_s_cm": min(va, vb),
                    "ratio": round(ratio, 2),
                })

    rows.sort(key=lambda x: -x["ratio"])
    out = config.KNOWLEDGE / "contradictions.jsonl"
    storage.write_jsonl(out, rows)
    print(f"[contradictions] 输出: {out}")
    print(f"[contradictions] 参与比较的归一体系数: {sum(1 for _, v in groups.items() if len(v) >= 2)}")
    print(f"[contradictions] 候选矛盾对(≥10x): {len(rows)}")
    for r in rows[:15]:
        print(f"  {r['system_norm']:22s} {r['value_hi_s_cm']:.2e} vs {r['value_lo_s_cm']:.2e} "
              f"S/cm (x{r['ratio']}) | {r['record_id_hi']}/{r['record_id_lo']}")
    return rows


if __name__ == "__main__":
    run_conductivity(None)
    run_contradictions(None)