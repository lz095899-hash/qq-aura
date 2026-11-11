"""真模型冒烟测试：假 QQ 网关 + 真模型（走本机 OpenAI 兼容网关）。

验证的不是"能不能请求"，而是：人设提示词 + 工具定义 + 多轮工具调用这套东西
在真模型上能不能跑通，产出的回复是不是正常中文短句。

跑法：python tests/real_llm.py [provider] [model]
默认 provider=magpie model=group/never-stuck（Hermes 本机网关 127.0.0.1:3425）。
"""
from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from aiohttp import web

from aura.config import load_config
from aura.daemon import build
from tests.mock_server import MockGateway


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def private_evt(uid, text):
    return {"post_type": "message", "message_type": "private", "sub_type": "friend",
            "message_id": 1, "user_id": uid, "self_id": 10001,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": uid, "nickname": "主人"}, "time": int(time.time())}


async def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "magpie"
    model = sys.argv[2] if len(sys.argv) > 2 else "group/never-stuck"

    gw = MockGateway()
    port = free_port()
    runner = web.AppRunner(gw.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    tmp = Path(tempfile.mkdtemp(prefix="aura-real-"))
    cfg_raw = {
        "onebot": {"http_url": f"http://127.0.0.1:{port}", "ws_url": f"ws://127.0.0.1:{port}/ws",
                   "self_id": 10001},
        "identity": {"owner_qq": 10000, "owner_name": "主人", "bot_name": "小鱼"},
        "llm": {"default": "real", "max_tool_rounds": 3, "timeout_s": 120,
                "providers": {"real": {"base_url": "http://127.0.0.1:3425/v1",
                                       "model": model, "api_key": "magpie"}}},
        "behavior": {"enabled": True, "dry_run": False, "debounce_ms": 200, "max_wait_ms": 600,
                     "group_mode": "mention_only", "reply_delay_ms": [30, 60], "history_turns": 10,
                     "proactive": {"enabled": False},
                     "memory": {"enabled": True, "db": str(tmp / "a.db")},
                     "qzone": {"enabled": True, "audience": "public"}},
        "control": {"port": free_port()},
        "persona": {"name": "小鱼", "age": 19, "gender": "女",
                    "bio": "你叫小鱼，19岁女生，住在主人的QQ里。有点黏人，但不谄媚。",
                    "style": "短句，像手机上打字。不列点，不用客服腔。"},
    }
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(cfg_path)
    cfg["llm"]["providers"]["real"]["model"] = model
    cfg["llm"]["providers"]["real"]["base_url"] = (
        "https://api.xiaomimimo.com/v1" if provider == "xiaomi" else "http://127.0.0.1:3425/v1")
    if provider == "xiaomi":
        import os
        cfg["llm"]["providers"]["real"]["api_key"] = os.environ.get("XIAOMIMIMO_V25_API_KEY", "")
        cfg["llm"]["providers"]["real"]["model"] = model

    engine = build(cfg)
    await engine.start()
    for _ in range(100):
        if gw.connected.is_set():
            break
        await asyncio.sleep(0.1)
    print(f"网关已连（{provider} / {model}），发一条私聊…")

    await gw.push(private_evt(10000, "在吗，今天加班到十点，累得要死"))
    for _ in range(600):
        if gw.sent_texts("send_private_msg"):
            break
        await asyncio.sleep(0.1)

    texts = gw.sent_texts("send_private_msg")
    print("\n她回的是：")
    for t in texts:
        print("  " + t)
    print("\n轮次统计：", engine.stats)
    if engine.last_error:
        print("最近错误：", engine.last_error)

    # 第二句：问一个需要翻记忆/需要人味的问题
    await gw.push(private_evt(10000, "你还记得我喜欢吃什么吗"))
    n = len(texts)
    for _ in range(600):
        if len(gw.sent_texts("send_private_msg")) > n:
            break
        await asyncio.sleep(0.1)
    print("\n第二轮回的是：")
    for t in gw.sent_texts("send_private_msg")[n:]:
        print("  " + t)

    await engine.stop()
    await runner.cleanup()
    ok = bool(texts) and all(len(t) > 1 for t in texts)
    print("\n结论：", "通过" if ok else "没拿到回复")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))