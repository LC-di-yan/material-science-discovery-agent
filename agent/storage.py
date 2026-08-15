"""agent 骨架 · 存储层（JSONL 读写 / 预算累计 / 审计日志）

所有动作增量落盘, 不覆盖历史 —— 证据链要求每次运行可回溯。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config

# 预算硬上限（复赛任务 G）：超限即停跑评估，防止意外费用。
# 扩充目标 1000+ 全文：sciverse 需覆盖检索 + 全文拉取，llm 覆盖抽取 + 对照 + discover。
BUDGET_LIMITS = {"llm": 1200, "sciverse": 3000, "materials_project": 100}


class BudgetExceededError(RuntimeError):
    """预算硬上限触发的停跑信号（任务 G）。"""


def budget_guard(platform: str, planned: int = 1) -> None:
    """API 调用前检查预算硬上限；超限抛 BudgetExceededError。

    planned 为本轮运行内已累计的计划调用数（含本次），用于弥补
    budget_update 在批处理结束时才落盘的滞后。
    """
    limit = BUDGET_LIMITS.get(platform)
    if limit is None:
        return
    used = int(get_budget(platform).get("api_calls", 0))
    if used + planned > limit:
        raise BudgetExceededError(
            f"{platform} 预算硬上限触发：已记录 {used} + 本轮计划 {planned} > 上限 {limit}。"
            f"请评估 data/99_logs/budget.json 后再继续。")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: str | Path, rows: list[dict]):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_jsonl(path: str | Path, rows: list[dict]):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def log(platform: str, **fields):
    """写一条审计日志（追加, 带时间戳）。"""
    append_jsonl(config.LOGS / f"{platform}_log.jsonl", [{"timestamp": now_iso(), **fields}])


def budget_update(platform: str, api_calls: int = 0, total_hits: int = 0,
                  unique_docs: int | None = None, cost: float | None = None,
                  extra: dict | None = None):
    """预算累计: 读旧值 + 新值, 不覆盖。兼容旧单平台格式 {platform, api_calls, ...}。"""
    path = config.BUDGET
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    if "platforms" not in existing and "platform" in existing:
        old = {k: v for k, v in existing.items() if k != "platform"}
        existing = {"platforms": {existing["platform"]: old}}
    platforms = existing.get("platforms", {})
    cur = dict(platforms.get(platform, {}))
    cur["api_calls"] = int(cur.get("api_calls", 0)) + api_calls
    cur["total_hits"] = int(cur.get("total_hits", 0)) + total_hits
    if unique_docs is not None:
        cur["unique_docs"] = unique_docs
    if cost is not None:
        cur["cost_usd"] = round(float(cur.get("cost_usd", 0)) + cost, 6)
    for k, v in (extra or {}).items():
        cur[k] = int(cur.get(k, 0)) + v
    platforms[platform] = cur
    out = {"platforms": platforms, "updated": now_iso()}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def get_budget(platform: str) -> dict:
    if not config.BUDGET.exists():
        return {}
    try:
        existing = json.loads(config.BUDGET.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if "platforms" not in existing and "platform" in existing:
        return {k: v for k, v in existing.items() if k != "platform"}
    return existing.get("platforms", {}).get(platform, {})
