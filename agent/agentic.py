# -*- coding: utf-8 -*-
"""agent 骨架 · AgentScope 2.0 编排层（discover 循环）

LiteratureDiscoveryAgent：自主执行「读知识库 → 生成 Gap → 触发补检 →
生成假设/路线 → 写回 JSONL」的发现循环。

设计要点（见《方案_引入AgentScope2.0改造.md》）：
- 编排层只负责决策与工具调用；数据/审计层（evaluate/compare）不经过 AgentScope；
- 权限引擎设为 BYPASS（工具自动放行，无人值守运行）；
- 事件流 reply_stream 全量落 data/99_logs/discover_log.jsonl（推理过程可审计）；
- 收敛三重保险：max_rounds 轮次上限 + budget_check 预算工具 + 轮内零新增即停。
"""
import asyncio
import os
from datetime import datetime, timezone

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import DeepSeekCredential
from agentscope.message import UserMsg
from agentscope.model import DeepSeekChatModel
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from . import config, storage, tools
from .comparator import read_loose_jsonl

DISCOVER_LOG = config.LOGS / "discover_log.jsonl"
ROUTE_FILE = config.ROUTES / "candidate_routes.jsonl"

DISCOVER_SYSTEM_PROMPT = """你是 LiteratureDiscoveryAgent：材料文献科学发现 Agent，围绕 Li6PS5Cl（argyrodite 硫化物固态电解质）的"掺杂×工艺"联合优化做发现循环。

规则：
1. 只消费/写入本仓库 data/ 下的 records/gaps/hypotheses/routes JSONL，遵守增量原则（绝不覆盖已有 id 与记录）；
2. 每条 Gap/假设/路线必须锚定已存在的 record_id（+doc_id），无锚点不写；写入前先用 read_knowledge 核实 record_id 确实存在；
3. 若补检/阅读发现先例（该组合已被联合报道），必须收缩表述（降低新颖性评级、改述为细化/复现问题），禁止放大新颖性；
4. 目标：识别"各步骤均有文献支撑、但未被联合报道"的掺杂×工艺组合；
5. 优先基于已有 records（当前规模见每轮消息）做推理；只有证据明显不足时才 search_literature → fetch_content → extract_records 补检（注意 API 预算）；
6. 每轮聚焦一个小主题；轮末用中文简要总结：本轮新增了什么、依据哪些 record_id、为何继续或收敛。"""


def build_model() -> DeepSeekChatModel:
    """LLM OpenAI 兼容端点（门禁冒烟已验证工具调用可靠）；thinking 默认关。"""
    key = config.get_key("llm")
    base_url = os.environ.get("DISCOVER_BASE_URL") or "https://tokenrhythm.studio/v1"
    cred = DeepSeekCredential(api_key=key, base_url=base_url)
    model = os.environ.get("DISCOVER_MODEL") or "deepseek-v4-flash-0731"
    return DeepSeekChatModel(
        credential=cred,
        model=model,
        parameters=DeepSeekChatModel.Parameters(thinking_enable=False, temperature=0.2),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _counts() -> dict:
    return {
        "records": len(storage.read_jsonl(config.RECORDS)),
        "gaps": len(storage.read_jsonl(config.GAPS)),
        "hypotheses": len(storage.read_jsonl(config.HYPOTHESES)),
        "routes": len(read_loose_jsonl(ROUTE_FILE)),
    }


def _log_event(round_no: int, evt) -> None:
    try:
        dump = evt.model_dump(mode="json")
    except Exception:
        dump = {"raw": str(evt)[:2000]}
    storage.append_jsonl(DISCOVER_LOG, [
        {"timestamp": _now(), "round": round_no,
         "event_type": type(evt).__name__, "event": dump}])


def _round_message(round_no: int, max_rounds: int, goal: str, prev_counts: dict) -> str:
    return (
        f"第 {round_no}/{max_rounds} 轮发现循环。总目标：{goal}\n"
        f"当前知识库规模：records={prev_counts['records']} gaps={prev_counts['gaps']} "
        f"hypotheses={prev_counts['hypotheses']} routes={prev_counts['routes']}。\n"
        "本轮请按系统规则执行：先 read_knowledge（可带 topic 过滤）了解现状，再决定——\n"
        "- 证据不足：search_literature → screen_hits → fetch_content → extract_records 补检（注意预算）；\n"
        "- 证据充分：识别未被联合报道的掺杂×工艺组合，write_gap → write_hypothesis，必要时 write_route；\n"
        "- 本轮聚焦一个与之前轮次不同的小主题，避免重复已有 gap/hypothesis；\n"
        "- 轮末用中文总结本轮产出与收敛判断。"
    )


async def run_discover(goal: str = "Li6PS5Cl 卤素混配+掺杂+工艺联合优化",
                       max_rounds: int = 8, max_iters: int = 15,
                       min_rounds: int = 2) -> dict:
    """discover 主循环：多轮 reply_stream，每轮事件流全量落日志，轮间检查收敛。"""
    config.ensure_dirs()
    counts_before = _counts()
    agent = Agent(
        name="LiteratureDiscoveryAgent",
        system_prompt=DISCOVER_SYSTEM_PROMPT,
        model=build_model(),
        toolkit=Toolkit(tools=tools.TOOLS),
        react_config=ReActConfig(max_iters=max_iters),
        state=AgentState(
            permission_context=PermissionContext(mode=PermissionMode.BYPASS),
        ),
    )

    rounds, prev_counts, stop_reason = [], counts_before, None
    for r in range(1, max_rounds + 1):
        round_info = {"round": r, "tool_calls": [], "answer": ""}
        msg = _round_message(r, max_rounds, goal, prev_counts)
        async for evt in agent.reply_stream(UserMsg("user", msg)):
            _log_event(r, evt)
            t = type(evt).__name__
            if t == "ToolCallStartEvent":
                round_info["tool_calls"].append(evt.tool_call_name)
            elif t == "TextBlockDeltaEvent":
                round_info["answer"] += evt.delta
        after = _counts()
        round_info["new_this_round"] = {k: after[k] - prev_counts[k] for k in after}
        rounds.append(round_info)
        prev_counts = after
        added = sum(v for v in round_info["new_this_round"].values())
        print(f"[discover] round {r}: tools={len(round_info['tool_calls'])} "
              f"new={round_info['new_this_round']}", flush=True)
        if r >= min_rounds and added == 0:
            stop_reason = f"轮内零新增，收敛于第 {r} 轮"
            break
    if stop_reason is None:
        stop_reason = f"达到最大轮次 {max_rounds}"

    counts_after = _counts()
    summary = {
        "goal": goal,
        "rounds_run": len(rounds),
        "stop_reason": stop_reason,
        "counts_before": counts_before,
        "counts_after": counts_after,
        "new_total": {k: counts_after[k] - counts_before[k] for k in counts_after},
        "rounds": [{"round": ri["round"], "tool_calls": ri["tool_calls"],
                    "new": ri["new_this_round"]} for ri in rounds],
        "log_file": str(DISCOVER_LOG),
    }
    storage.append_jsonl(DISCOVER_LOG, [
        {"timestamp": _now(), "event_type": "DiscoverSummary", "event": summary}])
    return summary


def main(goal: str, max_rounds: int) -> None:
    import json
    summary = asyncio.run(run_discover(goal=goal, max_rounds=max_rounds))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
