"""agent 骨架 · Materials Project 交叉验证

从 hypotheses.jsonl 派生目标组成, 增量查询 thermo/core（synthesis 已知不可靠, 默认跳过）。
输出增量追加 mp_*.jsonl + verification_log。
"""
import json, re, sys, time, urllib.request, urllib.error

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.materialsproject.org"
FORMULA_RE = re.compile(r"Li[0-9A-Za-z.\-+x()]*PS[0-9A-Za-z.\-+x()]*")


def extract_formulas(text: str) -> list[str]:
    m = FORMULA_RE.search(text or "")
    return [m.group(0)] if m else []


def mp_get(key: str, path: str, fields: list[str] | None = None, retries: int = 3) -> dict:
    url = f"{BASE}{path}"
    if fields:
        url += ("&" if "?" in path else "?") + "_fields=" + ",".join(fields)
    last = None
    for _ in range(retries):
        req = urllib.request.Request(url, headers={"X-API-KEY": key, "User-Agent": config.UA,
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            break  # HTTP 状态错误不再重试
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def run(args):
    key = config.get_key("materials_project")
    hyps = storage.read_jsonl(config.HYPOTHESES)
    targets = []
    for h in hyps:
        for f in extract_formulas(h.get("candidate", "")):
            # MP formula queries require concrete compositions, not symbolic templates.
            if any(token in f for token in ("x", "y", "(", ")", "+", "-")):
                continue
            if f not in targets:
                targets.append(f)
    # 补 baseline（若不存在）
    for b in ("Li6PS5Cl", "Li6PS5Br", "Li6PS5I"):
        if b not in targets:
            targets.append(b)

    # 已查过的 target（增量跳过）
    thermo_done = {r.get("target") for r in storage.read_jsonl(config.CROSS / "mp_thermo.jsonl")}
    core_done = {r.get("target") for r in storage.read_jsonl(config.CROSS / "mp_core.jsonl")}
    new_thermo, new_core, log_rows = [], [], []
    n_calls = 0

    for t in targets:
        if t in thermo_done:
            continue
        try:
            storage.budget_guard("materials_project", n_calls + 1)
        except storage.BudgetExceededError as e:
            print(f"\n停跑: {e}")
            break
        try:
            p2 = mp_get(key, f"/materials/thermo/?formula={t}",
                        fields=["material_id", "formula_pretty", "energy_above_hull", "is_stable"])
            d2 = p2.get("data", [])
            new_thermo.append({"target": t, "num": len(d2),
                               "records": [{k: d.get(k) for k in ("material_id", "formula_pretty", "energy_above_hull", "is_stable")} for d in d2[:5]]})
            n_calls += 1
            print(f"[thermo] {t:26s} 记录 {len(d2)}")
            time.sleep(0.3)
        except Exception as e:
            print(f"[thermo] {t} FAIL {type(e).__name__}")
        if t in core_done:
            continue
        try:
            p3 = mp_get(key, f"/materials/core/?formula={t}", fields=["material_id", "formula_pretty"])
            d3 = p3.get("data", [])
            new_core.append({"target": t, "num": len(d3),
                             "records": [{"material_id": d.get("material_id"), "formula_pretty": d.get("formula_pretty")} for d in d3[:5]]})
            n_calls += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[core] {t} FAIL {type(e).__name__}")

    storage.append_jsonl(config.CROSS / "mp_thermo.jsonl", new_thermo)
    storage.append_jsonl(config.CROSS / "mp_core.jsonl", new_core)
    if log_rows:
        storage.append_jsonl(config.VERIFICATION_LOG, log_rows)
    storage.budget_update("materials_project", api_calls=n_calls)
    print(f"\nMP 验证: 新增 thermo {len(new_thermo)} | core {len(new_core)} | 调用 {n_calls}")
