"""假的 QQ 网关 + 假的模型接口，用来在没有真 QQ 的环境里跑通全链路。

- HTTP 侧按 OneBot v11 的返回格式回包，所有动作记进 calls 列表；
- WS 侧等客户端连上后，由测试脚本主动 push 事件。
- LLM 侧按消息内容决定回文本 / 回 tool_calls / 回 JSON。
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web


class MockGateway:
    def __init__(self):
        self.calls = []
        self.ws = None
        self.connected = asyncio.Event()
        self._n = 0

    def sent_texts(self, action=None):
        out = []
        for c in self.calls:
            if action and c["action"] != action:
                continue
            if c["action"] in ("send_private_msg", "send_group_msg"):
                segs = c["params"].get("message") or []
                out.append("".join(s.get("data", {}).get("text", "") if s.get("type") == "text"
                                   else ("@" if s.get("type") == "at" else "")
                                   for s in segs))
        return out

    async def handle_action(self, request):
        action = request.match_info["action"]
        try:
            params = await request.json()
        except Exception:
            params = {}
        self.calls.append({"action": action, "params": params})
        data = {}
        if action == "get_login_info":
            data = {"user_id": 10001, "nickname": "小鱼"}
        elif action in ("send_private_msg", "send_group_msg"):
            self._n += 1
            data = {"message_id": self._n}
        elif action == "send_qzone_msg":
            data = {"tid": "TID123"}
        elif action == "set_qq_avatar":
            data = {"result": "ok"}
        elif action == "get_qzone_feeds":
            data = {"feeds": [{"key": "F1", "uin": 10002, "html": "<p>今天加班到十点</p>"},
                              {"key": "F2", "uin": 10003, "html": "<p>新买的猫窝到了</p>"}]}
        elif action == "get_friend_list":
            data = [{"user_id": 10000, "nickname": "主人", "remark": "主人"},
                    {"user_id": 10002, "nickname": "阿黄"}]
        elif action == "get_group_list":
            data = [{"group_id": 55555, "group_name": "测试群", "member_count": 3}]
        return web.json_response({"status": "ok", "retcode": 0, "data": data})

    async def handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    evt = json.loads(msg.data)
                except Exception:
                    continue
                if evt.get("echo"):
                    await ws.send_json({"status": "ok", "retcode": 0,
                                        "echo": evt["echo"], "data": {}})
        return ws

    async def push(self, evt):
        assert self.ws is not None, "WS 还没连上"
        evt.setdefault("self_id", 10001)
        evt.setdefault("time", 0)
        await self.ws.send_json(evt)

    def app(self):
        app = web.Application()
        app.router.add_post("/{action}", self.handle_action)
        app.router.add_get("/ws", self.handle_ws)
        return app


class MockLLM:
    """按消息内容决定回什么，够覆盖：正常回复、工具调用、心情、日记、记忆整理。"""

    def __init__(self):
        self.requests = []

    async def chat(self, request):
        body = await request.json()
        self.requests.append(body)
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        tool_names = [t.get("function", {}).get("name") for t in (body.get("tools") or [])]

        # 记忆整理
        if "记忆整理器" in joined:
            content = json.dumps({"memories": [
                {"text": "主人喜欢吃榴莲", "about": "主人", "importance": 0.7}]},
                ensure_ascii=False)
            return self._json(self._resp(content))

        # 日记
        if "说说正文" in joined:
            return self._json(self._resp("今天听到一句话，说人要在能跑的时候多跑两步。我记下了。"))

        # 空间评论
        if "留个言" in joined:
            return self._json(self._resp("十点了还不睡啊"))

        # 主动搭话
        if "已经" in str(last.get("content")) and "没人跟你说话" in str(last.get("content")):
            return self._json(self._resp("喂，还在忙吗"))

        user_msgs = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
        trigger = user_msgs[-2] if len(user_msgs) > 1 else (user_msgs[-1] if user_msgs else "")

        # 工具调用：用户说"记住"
        if "记住" in trigger and not has_tool_result and "remember" in tool_names:
            return self._json(self._resp("", tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "remember",
                             "arguments": json.dumps({"text": "主人喜欢吃榴莲", "about": "主人",
                                                      "importance": 0.8}, ensure_ascii=False)}}]))

        # 工具调用：do_nothing
        if "闭嘴" in trigger and "do_nothing" in tool_names and not has_tool_result:
            return self._json(self._resp("", tool_calls=[{
                "id": "call_2", "type": "function",
                "function": {"name": "do_nothing", "arguments": "{}"}}]))

        if has_tool_result:
            return self._json(self._resp("嗯，记住了"))
        return self._json(self._resp("在的，怎么了？"))

    def _json(self, payload):
        return web.json_response(payload)

    def _resp(self, content, tool_calls=None):
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return {"id": "mock-1", "object": "chat.completion", "model": "mock",
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": "tool_calls" if tool_calls else "stop"}]}

    def app(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        return app