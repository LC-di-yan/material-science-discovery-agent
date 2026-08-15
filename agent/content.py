"""agent 骨架 · 全文拉取

按 doc_id 拉取 Sciverse 全文到 content/, 增量跳过已存在文件。
源清单默认取最新 screened_*.jsonl。
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone

from . import config, storage

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.sciverse.space"


def fetch_content(key: str, doc_id: str, retries: int = 4) -> tuple[str | None, dict | None]:
    parts, offset, guard = [], None, 0
    while True:
        url = f"{BASE}/content?doc_id={doc_id}"
        if offset:
            url += f"&offset={offset}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}",
                                                   "User-Agent": config.UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and retries > 0:
                wait = 30 * (retries + 1)
                print(f"  [429] 限流, 等待 {wait}s 后重试 ...", flush=True)
                time.sleep(wait)
                retries -= 1
                continue
            return None, {"error": f"HTTP {e.code}"}
        except Exception as e:
            if retries > 0:
                time.sleep(5)
                retries -= 1
                continue
            return None, {"error": type(e).__name__}
        if payload.get("code") != "SUCCESS":
            return None, payload
        parts.append(payload.get("text", ""))
        if payload.get("more"):
            offset = payload.get("next_offset")
            guard += 1
            if guard > 20:
                break
        else:
            break
    return "".join(parts), None


def latest_screened() -> str:
    files = list(config.SCIVERSE_DIR.glob("screened_*.jsonl"))
    if not files:
        raise SystemExit("未找到 screened_*.jsonl, 请先运行 --stage screen")
    # 按修改时间取最新（避免 screened_50 这类旧清单被误选）
    return str(max(files, key=lambda f: f.stat().st_mtime))


def run(args):
    key = config.get_key("sciverse")
    src = getattr(args, "screened", None) or latest_screened()
    rows = storage.read_jsonl(src)
    limit = getattr(args, "limit", None)
    if limit:
        rows = rows[:limit]
    config.CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    ok, fail, skipped = 0, 0, 0
    log_rows = []
    for i, r in enumerate(rows, 1):
        doc = r.get("doc_id")
        if not doc:
            continue
        out = config.CONTENT_DIR / f"{doc}.json"
        if out.exists():
            skipped += 1
            continue
        try:
            storage.budget_guard("sciverse", ok + 1)
        except storage.BudgetExceededError as e:
            print(f"\n停跑: {e}", flush=True)
            break
        try:
            text, err = fetch_content(key, doc)
        except Exception as e:
            fail += 1
            print(f"[{i:02d}] FAIL {type(e).__name__} {e}")
            continue
        if err is not None or text is None:
            fail += 1
            print(f"[{i:02d}] ERR {err if err else 'empty'} doc={doc[:12]}")
            continue
        out.write_text(json.dumps({"doc_id": doc, "title": r.get("title"),
                                   "fetched_at": datetime.now(timezone.utc).isoformat(),
                                   "text": text}, ensure_ascii=False), encoding="utf-8")
        log_rows.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                         "platform": "sciverse", "action": "content", "doc_id": doc, "bytes": len(text)})
        ok += 1
        print(f"[{i:02d}/{len(rows)}] OK doc={doc[:12]} bytes={len(text)}", flush=True)
        time.sleep(0.6)
    storage.append_jsonl(config.QUERY_LOG, log_rows)
    storage.budget_update("sciverse", api_calls=ok)
    print(f"\n全文拉取: 新增 {ok} | 跳过已有 {skipped} | 失败 {fail}")
