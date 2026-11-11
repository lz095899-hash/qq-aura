"""任意 OpenAI 兼容接口的调用层（DeepSeek / 小米 MiMo / OpenAI / Ollama / 自建中转）。

只做三件事：拼请求、重试、把 tool_calls 归一成统一结构。
上层（engine）不关心是哪家模型。
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

log = logging.getLogger("aura.llm")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, cfg_getter):
        self._cfg = cfg_getter
        self._clients = {}

    def _client(self, base_url, timeout):
        key = (base_url, timeout)
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(timeout=timeout, trust_env=False)
        return self._clients[key]

    def providers(self):
        return sorted((self._cfg()["llm"].get("providers") or {}).keys())

    async def chat(self, messages, tools=None, provider_name=None, max_retries=3, force_json=False):
        """返回 {'content': str, 'tool_calls': [{id,name,arguments(dict)}], 'raw': {...}}"""
        from .config import provider as provider_of
        p = provider_of(self._cfg(), provider_name)
        base = p["base_url"].rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"content-type": "application/json"}
        if p.get("api_key"):
            headers["authorization"] = f"Bearer {p['api_key']}"
        body = {
            "model": p["model"],
            "messages": messages,
            "temperature": float(p.get("temperature", 0.85)),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if force_json:
            body["response_format"] = {"type": "json_object"}

        client = self._client(base, float(p.get("timeout_s", 90)))
        last = None
        for attempt in range(max_retries):
            try:
                r = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                last = "请求超时"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except httpx.HTTPError as e:
                last = f"网络错误：{e}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in RETRY_STATUS:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                await asyncio.sleep(min(8.0, 1.2 * (attempt + 1) ** 2))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{p['name']} HTTP {r.status_code}: {r.text[:400]}")
            try:
                data = r.json()
            except Exception:
                last = "响应不是 JSON"
                continue
            choices = data.get("choices") or []
            if not choices:
                # MiMo 偶发空 choices：重试一次就好
                last = "响应没有 choices"
                await asyncio.sleep(1.0)
                continue
            msg = choices[0].get("message") or {}
            calls = []
            for c in msg.get("tool_calls") or []:
                fn = c.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {"_raw": raw_args}
                else:
                    args = raw_args
                calls.append({"id": c.get("id") or f"call_{len(calls)}",
                              "name": fn.get("name", ""), "arguments": args})
            return {"content": (msg.get("content") or "").strip(), "tool_calls": calls,
                    "provider": p["name"], "model": p["model"], "raw": data}
        raise LLMError(f"模型调用失败（{p['name']} / {p['model']}）：{last}")

    async def close(self):
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()