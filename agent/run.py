"""agent 骨架 · CLI 编排入口

用法:
    python -m agent.run --stage smoke-llm                     # LLM 连通性冒烟
    python -m agent.run --stage search --queries queries/expansion_v1.txt --top-k 30 --limit 5
    python -m agent.run --stage screen --max 300
    python -m agent.run --stage content --screened <latest 自动>
    python -m agent.run --stage extract --limit 5             # 先小批验证
    python -m agent.run --stage extract                       # 全量（增量, 断点续跑）
    python -m agent.run --stage validate
    python -m agent.run --stage evaluate                      # 本地证据质量基线（无 API 调用）
    python -m agent.run --stage audit                         # 知识层质量审计（无 API 调用）
    python -m agent.run --stage retro --limit 1               # 逆向合成（LLM 推理 + 文献锚点）
    python -m agent.run --stage gen --arm purellm --routes 10 # 对照协议: 生成/冻结各臂路线
    python -m agent.run --stage gen --arm all --seed 42      # 或一次生成全部四臂（lit=12, random=20）
    python -m agent.run --stage compare                       # 对照协议: 确定性审计（无 API）
    python -m agent.run --stage discover --goal "Li6PS5Cl 卤素混配+掺杂+工艺联合优化" --max-rounds 3
    python -m agent.run --stage full                          # search→screen→content→extract
"""
import argparse, sys

from . import audit, comparator, config, content, evaluate, extract_batch, llm, normalize, report, retrieval_eval, screen, search, validate

sys.stdout.reconfigure(encoding="utf-8")


def main():
    config.ensure_dirs()
    p = argparse.ArgumentParser(prog="agent", description="方向三 固态电解质文献调研 Agent 骨架")
    p.add_argument("--stage", required=True, choices=["smoke-llm", "search", "screen",
                                                      "content", "extract", "validate", "evaluate",
                                                      "gen", "compare", "audit", "retro", "discover",
                                                      "normalize", "contradictions", "report",
                                                      "retrieval-eval", "full"])
    p.add_argument("--goal", default="Li6PS5Cl 卤素混配+掺杂+工艺联合优化",
                   help="discover 阶段的发现目标")
    p.add_argument("--max-rounds", type=int, default=8, help="discover 阶段最大轮次")
    p.add_argument("--queries", help="查询词表文件（相对根目录, 每行一个查询）")
    p.add_argument("--top-k", type=int, default=30)
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 条（验证用）")
    p.add_argument("--max", type=int, default=300, help="screen 阶段数量上限")
    p.add_argument("--screened", help="content 阶段的筛选清单文件（默认最新 screened_*）")
    p.add_argument("--sleep", type=float, default=0.2, help="抽取阶段每次 LLM 调用间隔秒数")
    p.add_argument("--workers", type=int, default=4, help="抽取阶段并发线程数")
    p.add_argument("--arm", default="all", help="gen 阶段生成臂: fixed/purellm/random/lit/all")
    p.add_argument("--routes", type=int, default=10, help="gen 阶段 purellm 臂路线条数")
    p.add_argument("--routes-random", type=int, default=20, help="gen 阶段 random 臂路线条数")
    p.add_argument("--seed", type=int, default=42, help="gen 阶段 random 臂随机种子")
    p.add_argument("--compare-date", default=None,
                   help="compare 阶段快照日期（默认今天；传 20260803 复现冻结基线口径）")
    p.add_argument("--snapshot", default=None,
                   help="evaluate 阶段语料快照日期（YYYYMMDD；默认今天）")
    p.add_argument("--force", action="store_true",
                   help="覆盖已存在的冻结快照/冻结路线（默认拒绝覆盖）")
    args = p.parse_args()

    stage = args.stage
    if stage == "smoke-llm":
        print("LLM 冒烟:", llm.smoke())
    elif stage == "search":
        search.run(args)
    elif stage == "screen":
        screen.run(args)
    elif stage == "content":
        content.run(args)
    elif stage == "extract":
        extract_batch.run(args)
    elif stage == "validate":
        validate.run(args)
    elif stage == "evaluate":
        evaluate.run(args)
    elif stage in ("gen", "compare"):
        comparator.run(args)
    elif stage == "audit":
        audit.run(args)
    elif stage == "normalize":
        normalize.run_conductivity(args)
    elif stage == "contradictions":
        normalize.run_contradictions(args)
    elif stage == "report":
        report.run_report(args)
    elif stage == "retrieval-eval":
        retrieval_eval.run_retrieval_eval(args)
    elif stage == "retro":
        from . import retrosynthesis
        retrosynthesis.run(args)
    elif stage == "discover":
        from . import agentic
        agentic.main(goal=args.goal, max_rounds=args.max_rounds)
    elif stage == "full":
        search.run(args)
        screen.run(args)
        content.run(args)
        extract_batch.run(args)


if __name__ == "__main__":
    main()
