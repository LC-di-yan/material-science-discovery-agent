"""agent 骨架 · LLM 客户端（Anthropic 兼容 /v1/messages）

默认走 tokenrhythm.studio（Anthropic 兼容端点, key 在 _credentials.md §4）。
base_url 可含或不含尾部 /v1，客户端统一追加 /v1/messages。
鉴权双试: 先 x-api-key, 401 后换 Authorization: Bearer。
"""
import json, random, re, sys, time, urllib.request, urllib.error

from . import config

sys.stdout.reconfigure(encoding="utf-8")


class EmptyResponse(RuntimeError):
    """API 返回了空 content（多半是限流/瞬时故障）。"""


def _call_once(cfg: dict, system: str, user: str, max_tokens: int, temperature: float,
               use_bearer: bool) -> dict:
    body = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},  # deepseek 系模型对抽取提示会陷入无界思维, 禁用后直接输出 text
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    url = re.sub(r"/v1$", "", cfg["base_url"].rstrip("/")) + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": config.UA,
    }
    if use_bearer:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    else:
        headers["x-api-key"] = cfg["api_key"]
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def chat(system: str, user: str, max_tokens: int = 4096, temperature: float = 0.2,
         retries: int = 3) -> str:
    """单次对话, 返回文本。鉴权双试 + 指数重试。"""
    cfg = config.get_llm_config()
    last_err = None
    for use_bearer in (False, True):
        for attempt in range(retries):
            try:
                payload = _call_once(cfg, system, user, max_tokens, temperature, use_bearer)
                parts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
                text = "".join(parts).strip()
                if not text:
                    raise EmptyResponse(payload)
                return text
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (401, 403):
                    if use_bearer:
                        break  # 两种鉴权都试过仍 401 -> 放弃重试
                    continue  # 换下一种鉴权方式
                if e.code >= 500:
                    time.sleep(2 * (attempt + 1))  # 服务端错误重试
                    continue
                raise  # 其他 4xx 直接抛
            except EmptyResponse as e:
                last_err = e
                time.sleep(5 * (attempt + 1) + random.uniform(0, 2))  # 空响应多半是限流, 加长退避+抖动
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败: {last_err}")


def _repair_embedded_newlines(text: str) -> str:
    """转义字符串值内未转义的换行/回车/制表符（LLM JSON 常见缺陷）。"""
    out = []
    in_str, esc = False, False
    for ch in text:
        if esc:
            out.append(ch)
            esc = False
        elif ch == "\\":
            out.append(ch)
            esc = True
        elif ch == '"':
            in_str = not in_str
            out.append(ch)
        elif in_str and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
        else:
            out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> dict:
    """多级容错解析: 全文 → ```json 围栏 → 首个{到末个} → 修补字符串内换行 → 逐行修补引号。"""
    candidates = [text]
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        candidates.append(m.group(1).strip())
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        candidates.append(text[s:e + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    for cand in candidates:
        repaired = _repair_embedded_newlines(cand) if cand else ""
        if not repaired or repaired == cand:
            continue
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            continue
    # 末级: 补残缺尾部（截断时去掉最后一个未闭合值）
    if s != -1 and e > s:
        cut = text[s:e + 1]
        cut = re.sub(r",\s*$", "", cut)          # 去尾逗号
        cut = re.sub(r'"\s*:\s*$', '"":', cut)  # 空值冒号
        try:
            return json.loads(cut)
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"LLM 输出非合法 JSON:\n{text[:500]}")


def chat_json(system: str, user: str, max_tokens: int = 4096, temperature: float = 0.2) -> dict:
    """对话并要求 JSON 输出, 解析容错。"""
    return _extract_json(chat(system, user, max_tokens=max_tokens, temperature=temperature))


def smoke() -> str:
    """冒烟验证: 一次最小调用。"""
    return chat("You are a helpful assistant.", "Reply with exactly: OK", max_tokens=32, temperature=0)


if __name__ == "__main__":
    print("LLM 冒烟:", smoke())
