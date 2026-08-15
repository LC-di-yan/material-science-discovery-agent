"""agent 骨架 · 配置层

职责:
- 项目路径常量
- API 密钥解析: 环境变量优先, fallback 到 _credentials.md（敏感文件, 勿提交 GitHub）
- LLM 默认配置（DeepSeek Anthropic 兼容端点）
"""
import os, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
CRED = ROOT / "_credentials.md"

DATA = ROOT / "data"
LIT = DATA / "01_literature"
SCIVERSE_DIR = LIT / "sciverse"
CONTENT_DIR = SCIVERSE_DIR / "content"
KNOWLEDGE = DATA / "02_knowledge"
CROSS = DATA / "03_cross_validate"
ROUTES = DATA / "04_routes"
COMPARE = DATA / "05_comparator"
LOGS = DATA / "99_logs"
QUERIES = ROOT / "queries"

HIT_CATALOG = SCIVERSE_DIR / "hit_catalog.jsonl"
RECORDS = KNOWLEDGE / "records.jsonl"
GAPS = KNOWLEDGE / "gaps.jsonl"
HYPOTHESES = KNOWLEDGE / "hypotheses.jsonl"
BUDGET = LOGS / "budget.json"
QUERY_LOG = LOGS / "query_log.jsonl"
EXTRACTION_LOG = LOGS / "extraction_log.jsonl"
VERIFICATION_LOG = LOGS / "verification_log.jsonl"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# LLM Anthropic 兼容端点默认值（tokenrhythm.studio /v1，deepseek-v4-flash-0731）
DEFAULT_LLM_BASE = "https://tokenrhythm.studio/v1"
DEFAULT_LLM_MODEL = "deepseek-v4-flash-0731"


def _cred_section(start_marker: str, end_marker: str | None = None) -> str:
    text = CRED.read_text(encoding="utf-8")
    start = text.split(start_marker, 1)[1]
    return start.split(end_marker, 1)[0] if end_marker else start


def _cred_field(section: str, key: str) -> str | None:
    m = re.search(rf"\|\s*{re.escape(key)}\s*\|\s*`([^`]+)`", section)
    return m.group(1) if m else None


def get_key(name: str) -> str:
    """按名称取 API key: 环境变量优先, fallback _credentials.md。"""
    envs = {
        "sciverse": ["SCIVERSE_API_TOKEN", "SCIVERSE_API_KEY"],
        "materials_project": ["MATERIALS_PROJECT_API_KEY", "MP_API_KEY"],
        "llm": ["LLM_API_KEY", "DEEPSEEK_API_KEY"],
    }
    for e in envs.get(name, []):
        v = os.environ.get(e)
        if v:
            return v
    if name == "sciverse":
        sec = _cred_section("## 1. Sciverse", "## 2.")
        key = _cred_field(sec, "API Key")
        if key:
            return key
    elif name == "materials_project":
        sec = _cred_section("## 2. Materials Project", "## 3.")
        key = _cred_field(sec, "API Key")
        if key:
            return key
    elif name == "llm":
        sec = _cred_section("## 4. LLM")
        key = _cred_field(sec, "API Key")
        if key:
            return key
    raise RuntimeError(f"未找到 API key: {name}（请设置环境变量或检查 {CRED.name}）")


def get_llm_config() -> dict:
    """返回 LLM 配置: base_url / api_key / model。"""
    base = os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    key = get_key("llm")
    return {"base_url": base, "api_key": key, "model": model}


def ensure_dirs():
    for d in (LIT, SCIVERSE_DIR, CONTENT_DIR, KNOWLEDGE, CROSS, ROUTES, COMPARE, LOGS, QUERIES):
        d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    ensure_dirs()
    print("Sciverse key:", (get_key("sciverse") or "?")[:6] + "...")
    print("MP key:", (get_key("materials_project") or "?")[:6] + "...")
    llm = get_llm_config()
    print("LLM base:", llm["base_url"], "| model:", llm["model"], "| key:", llm["api_key"][:6] + "...")
