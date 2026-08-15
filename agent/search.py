"""agent 骨架 · 检索阶段

Sciverse /agentic-search 语义检索, 查询词表从文件读, 命中增量合并 hit_catalog（跨运行去重累计）。
"""
import json, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_QUERIES = [
    "argyrodite Li6PS5Cl synthesis ionic conductivity",
    "argyrodite Li6PS5Cl Cl Br halogen substitution ionic conductivity",
    "Li6PS5Br lithium thiophosphate argyrodite ionic conductivity",
    "Li6PS5I iodide argyrodite solid electrolyte",
    "Li-rich argyrodite Li6+xPS5-xCl1+x lithium ion conductor",
    "argyrodite cation doping sulfide solid electrolyte",
    "Li6PS5Cl ball milling mechanochemical synthesis",
    "Li6PS5Cl annealing temperature sintering solid electrolyte",
    "argyrodite air stability H2S moisture sensitivity",
    "Li6PS5Cl hot pressing densification ionic conductivity",
    "argyrodite halide mixing Cl Br activation energy",
    "sulfide solid electrolyte Li2S excess precursor synthesis",
    "argyrodite phase purity XRD structural stability",
    "Li6PS5Cl electrochemical stability lithium metal anode",
    "chloride doped argyrodite solid-state batteries electrolyte",
    "mechanochemical synthesis argyrodite process optimization",
]


def load_queries(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_QUERIES
    p = config.ROOT / path if not str(path).startswith(str(config.ROOT)) else config.ROOT / path
    lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if not lines:
        raise SystemExit(f"查询词表为空: {path}")
    return lines


def call_agentic_search(key: str, query: str, top_k: int, retries: int = 3) -> dict:
    body = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
    last = None
    for _ in range(retries):
        req = urllib.request.Request(
            "https://api.sciverse.space/agentic-search",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": config.UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            break  # HTTP 状态错误不再重试
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def extract_hits(payload: dict) -> tuple[list, str]:
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for k in ("hits", "documents", "items", "results", "records"):
            if k in data:
                return data[k], k
    if isinstance(data, list):
        return data, "data[]"
    return [], "unknown"


def hit_id(hit) -> str | None:
    for k in ("doc_id", "paper_id", "_id", "id", "uid"):
        if isinstance(hit, dict) and hit.get(k):
            return str(hit[k])
    return None


def hit_field(hit, *keys, default=""):
    if not isinstance(hit, dict):
        return default
    for k in keys:
        v = hit.get(k)
        if isinstance(v, (str, int, float)) and v not in ("", None):
            return str(v)
        if isinstance(v, dict) and k == "metadata":
            for k2 in ("title", "year", "journal"):
                if v.get(k2):
                    return str(v[k2])
    return default


def run(args):
    key = config.get_key("sciverse")
    queries = load_queries(getattr(args, "queries", None))
    if getattr(args, "limit", None):
        queries = queries[: args.limit]

    # 现有 catalog（跨运行去重累计）
    catalog: dict[str, dict] = {}
    for r in storage.read_jsonl(config.HIT_CATALOG):
        if r.get("doc_id"):
            catalog[r["doc_id"]] = r

    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    search_file = config.SCIVERSE_DIR / f"search_{now_str}.jsonl"
    n_calls, n_hits, errors, new_docs = 0, 0, 0, 0
    log_rows, new_entries = [], []

    for qi, q in enumerate(queries, 1):
        try:
            storage.budget_guard("sciverse", n_calls + 1)
        except storage.BudgetExceededError as e:
            print(f"\n停跑: {e}")
            break
        try:
            payload = call_agentic_search(key, q, args.top_k)
        except urllib.error.HTTPError as e:
            msg = f"[{qi}] HTTP {e.code} {q!r}: {e.read().decode('utf-8', 'replace')[:160]}"
            print("ERR", msg); errors += 1; continue
        except Exception as e:
            print("ERR", f"[{qi}] {type(e).__name__} {q!r}: {e}"); errors += 1; continue

        ts = datetime.now(timezone.utc).isoformat()
        hits, kind = extract_hits(payload)
        storage.append_jsonl(search_file, [{"timestamp": ts, "query": q, "top_k": args.top_k,
                                            "hit_kind": kind, "payload": payload}])
        doc_ids = []
        for h in hits:
            hid = hit_id(h)
            doc_ids.append(hid)
            if hid and hid not in catalog:
                entry = {
                    "doc_id": hid,
                    "title": hit_field(h, "title"),
                    "year": hit_field(h, "publication_published_year"),
                    "journal": hit_field(h, "publication_venue_name_unified"),
                    "authors": h.get("author", []) if isinstance(h, dict) else [],
                    "citation_count": hit_field(h, "citation_count"),
                    "score": hit_field(h, "score"),
                    "chunk": hit_field(h, "chunk", "evidence", "snippet", "content", "excerpt"),
                    "queries": [q],
                }
                catalog[hid] = entry
                new_entries.append(entry)
                new_docs += 1
            elif hid and q not in catalog[hid].get("queries", []):
                catalog[hid].setdefault("queries", []).append(q)
        n_hits += len(hits)
        n_calls += 1
        log_rows.append({"timestamp": ts, "platform": "sciverse", "query": q,
                         "top_k": args.top_k, "hits": len(hits), "doc_ids": doc_ids[:50]})
        print(f"[{qi}/{len(queries)}] hits={len(hits)} new={sum(1 for d in doc_ids if d and d in catalog)} q={q!r}")
        time.sleep(0.5)

    # 全量写回 catalog（含历史 + 本次新增）
    storage.write_jsonl(config.HIT_CATALOG, list(catalog.values()))
    storage.append_jsonl(config.QUERY_LOG, log_rows)
    storage.budget_update("sciverse", api_calls=n_calls, total_hits=n_hits,
                          unique_docs=len(catalog), extra={"queries_failed": errors})
    print(f"\n检索完成: 本次调用 {n_calls} | 命中 {n_hits} | 新增 {new_docs} | 失败 {errors} | 累计唯一 {len(catalog)}")
    print("原始响应:", search_file)
    print("命中清单(累计):", config.HIT_CATALOG)
