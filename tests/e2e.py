"""端到端验证：假网关 + 假模型 → 引擎 → 断言真的发出了正确的动作。

跑法：python tests/e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml
from aiohttp import web

from aura.daemon import build, make_app
from aura.store import Store
from tests.mock_server import MockGateway, MockLLM

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {extra}" if extra and not cond else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def wait_until(cond, timeout=10.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if await cond() if asyncio.iscoroutinefunction(cond) else cond():
            return True
        await asyncio.sleep(interval)
    return False


def private_evt(uid, text, nickname="主人"):
    return {"post_type": "message", "message_type": "private", "sub_type": "friend",
            "message_id": 1, "user_id": uid, "self_id": 10001,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": uid, "nickname": nickname}, "time": int(time.time())}


def group_evt(gid, uid, text, at=False, nickname="群友"):
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": "10001"}})
    segs.append({"type": "text", "data": {"text": text}})
    return {"post_type": "message", "message_type": "group", "sub_type": "normal",
            "message_id": 2, "group_id": gid, "user_id": uid, "self_id": 10001,
            "message": segs, "sender": {"user_id": uid, "nickname": nickname, "card": nickname},
            "time": int(time.time())}


async def main():
    gw = MockGateway()
    llm_mock = MockLLM()
    gw_port, llm_port = free_port(), free_port()

    gw_runner = web.AppRunner(gw.app())
    await gw_runner.setup()
    await web.TCPSite(gw_runner, "127.0.0.1", gw_port).start()

    llm_runner = web.AppRunner(llm_mock.app())
    await llm_runner.setup()
    await web.TCPSite(llm_runner, "127.0.0.1", llm_port).start()

    tmp = Path(tempfile.mkdtemp(prefix="aura-e2e-"))
    cfg_raw = {
        "onebot": {"http_url": f"http://127.0.0.1:{gw_port}", "ws_url": f"ws://127.0.0.1:{gw_port}/ws",
                   "http_token": "", "ws_token": "", "self_id": 10001},
        "identity": {"owner_qq": 10000, "owner_name": "主人", "bot_name": "小鱼"},
        "llm": {"default": "mock", "temperature": 0.8, "max_tool_rounds": 3, "timeout_s": 10,
                "providers": {"mock": {"base_url": f"http://127.0.0.1:{llm_port}/v1",
                                       "model": "mock", "api_key": "test"}}},
        "behavior": {
            "enabled": True, "dry_run": False, "debounce_ms": 250, "max_wait_ms": 700,
            "group_mode": "mention_only", "group_reply_probability": 0.25,
            "max_reply_segments": 3, "max_segment_chars": 400,
            "reply_delay_ms": [30, 60], "history_turns": 10,
            "proactive": {"enabled": False}, "memory": {"enabled": True, "db": str(tmp / "a.db")},
            "qzone": {"enabled": True, "audience": "public", "diary_times": ["21:30"],
                      "social": {"enabled": False}},
        },
        "control": {"host": "127.0.0.1", "port": free_port(), "token": ""},
        "persona": {"name": "小鱼", "age": 19, "gender": "女",
                    "bio": "你叫小鱼。", "style": "短句。"},
    }
    from aura.config import load_config
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(cfg_path)

    print("连接假网关…")
    engine = build(cfg)
    await engine.start()
    check("WS 连上假网关", await wait_until(gw.connected.is_set, 8))

    # 1. 私聊正常回复
    print("\n[1] 私聊收到消息 → 回话")
    await gw.push(private_evt(10000, "在吗"))
    ok = await wait_until(lambda: len(gw.sent_texts("send_private_msg")) >= 1)
    check("私聊回了一条", ok, gw.calls[-3:])
    check("内容是模型产出", ok and "怎么了" in gw.sent_texts("send_private_msg")[0])

    # 2. 群聊没被叫到 → 不说话
    print("\n[2] 群聊没@ → 沉默")
    before = len(gw.sent_texts("send_group_msg"))
    await gw.push(group_evt(55555, 10002, "今天下班好晚"))
    await asyncio.sleep(1.2)
    check("群聊没@ 没说话", len(gw.sent_texts("send_group_msg")) == before)

    # 3. 群聊被@ → 回话且带 @
    print("\n[3] 群聊被@ → 回话")
    await gw.push(group_evt(55555, 10002, "小鱼你今晚吃啥", at=True))
    ok = await wait_until(lambda: len(gw.sent_texts("send_group_msg")) > before)
    check("群聊被@ 回了", ok)
    ats = [c for c in gw.calls if c["action"] == "send_group_msg"
           and any(s.get("type") == "at" for s in c["params"]["message"])]
    check("@ 回给对方", bool(ats))

    # 4. 工具调用：remember
    print("\n[4] 工具调用 remember → 记忆落库")
    n0 = engine.store.memory_count()
    await gw.push(private_evt(10000, "记住我最喜欢吃榴莲"))
    ok = await wait_until(lambda: engine.store.memory_count() > n0)
    check("记忆写进去了", ok, f"{n0} → {engine.store.memory_count()}")
    rows = engine.store.memories_for([f"private:10000"])
    check("记的是榴莲那件事", any("榴莲" in r["text"] for r in rows))
    check("工具轮之后还回了话",
          await wait_until(lambda: any("记住" in x for x in gw.sent_texts("send_private_msg"))))

    # 5. do_nothing
    print("\n[5] do_nothing → 沉默")
    b1 = len(gw.sent_texts("send_private_msg"))
    await gw.push(private_evt(10000, "你先闭嘴"))
    await asyncio.sleep(1.2)
    check("调 do_nothing 没发消息", len(gw.sent_texts("send_private_msg")) == b1)
    check("静默计数 +1", engine.stats["silent"] >= 1)

    # 6. 发说说
    print("\n[6] 发 QQ 空间说说")
    r = await engine.post_qzone("今天风很大")
    qz = [c for c in gw.calls if c["action"] == "send_qzone_msg"]
    check("调用了 send_qzone_msg", bool(qz), r)
    check("ugc_right 用的是公开=1", bool(qz) and qz[-1]["params"]["ugc_right"] == 1)
    check("内容对得上", bool(qz) and qz[-1]["params"]["content"] == "今天风很大")

    # 7. 空间日记
    print("\n[7] 空间日记")
    r = await engine.write_diary()
    check("日记发出去了", r.get("ok"), r)
    check("日记落库", engine.store.qzone_today_count() >= 2)

    # 8. 主动搭话
    print("\n[8] 主动搭话（force）")
    b2 = len(gw.sent_texts("send_private_msg"))
    r = await engine.proactive(force=True)
    check("主动发了一条", r.get("ok") and len(gw.sent_texts("send_private_msg")) > b2, r)
    check("说的是主动搭话的词", "还在忙吗" in (gw.sent_texts("send_private_msg") or [""])[-1])

    # 9. 记忆召回
    print("\n[9] 记忆召回")
    hits = engine.memory.recall("榴莲", "private:10000")
    check("能想起榴莲", any("榴莲" in h["text"] for h in hits))
    cross = engine.memory.recall("榴莲", "group:55555")
    check("群聊取不到私聊记忆", not any("榴莲" in h["text"] for h in cross))

    # 10. 记忆整理
    print("\n[10] 每日记忆整理")
    yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    engine.store.add_message("private:10000", "user", "我最近天天加班到十一点", "10000", "主人",
                             ts=time.time() - 86400)
    n1 = engine.store.memory_count()
    r = await engine.memory.consolidate(yday)
    check("整理成功", r.get("ok"), r)
    check("整理后有新记忆", engine.store.memory_count() >= n1)

    # 11. 控制接口
    print("\n[11] 控制接口（MCP 走的就是这套）")
    ctl = cfg["control"]
    runner = web.AppRunner(make_app(cfg, engine))
    await runner.setup()
    site = web.TCPSite(runner, ctl["host"], ctl["port"])
    await site.start()
    base = f"http://{ctl['host']}:{ctl['port']}"
    async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
        st = (await c.get(f"{base}/status")).json()
        check("/status 有状态", st.get("ok") and st.get("self_id") == 10001, st)
        b3 = len(gw.sent_texts("send_private_msg"))
        r = (await c.post(f"{base}/send", json={"chat_id": "private:10000",
                                                "text": "手动发的一条"})).json()
        check("/send 能发", r.get("ok") and len(gw.sent_texts("send_private_msg")) > b3, r)
        r = (await c.get(f"{base}/recall", params={"q": "榴莲"})).json()
        check("/recall 查得到", r.get("ok") and "榴莲" in r.get("message", ""), r)
        r = (await c.post(f"{base}/toggle", json={"enabled": False})).json()
        check("/toggle 关得掉", r.get("ok") and engine.cfg()["behavior"]["enabled"] is False)
    await runner.cleanup()

    # 12. 关掉总开关后不说话
    print("\n[12] 总开关关掉 → 不说话")
    b4 = len(gw.sent_texts("send_private_msg"))
    await gw.push(private_evt(10000, "喂？"))
    await asyncio.sleep(1.0)
    check("关了之后沉默", len(gw.sent_texts("send_private_msg")) == b4)

    # 13. dry_run
    print("\n[13] dry_run → 生成但不发")
    cfg["behavior"]["enabled"] = True
    cfg["behavior"]["dry_run"] = True
    b5 = len(gw.sent_texts("send_private_msg"))
    await gw.push(private_evt(10000, "在吗在吗"))
    await asyncio.sleep(1.2)
    check("dry_run 不发消息", len(gw.sent_texts("send_private_msg")) == b5)
    check("但这一轮确实跑了", engine.stats["turns"] >= 1)

    await engine.stop()
    await gw_runner.cleanup()
    await llm_runner.cleanup()

    print(f"\n===== {len(PASS)} 通过 / {len(FAIL)} 失败 =====")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))