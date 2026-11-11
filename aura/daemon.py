"""常驻进程 + 本机控制接口。

`aura run` 起的就是这个：OneBot WS + 引擎 + 定时任务 + 127.0.0.1 控制 API。
MCP 层（mcp_server.py）通过这个 API 操作它，两者解耦：Hermes 不开的时候她照样在线。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from aiohttp import web

from .config import load_config, provider, save_config
from .engine import Engine
from .llm import LLM
from .memory import Memory
from .onebot import OneBot, OneBotError
from .store import Store

log = logging.getLogger("aura.daemon")


def build(cfg):
    store = Store(cfg["behavior"]["memory"]["db"])
    llm = LLM(lambda: cfg)
    memory = Memory(store, llm, lambda: cfg)
    onebot = OneBot(lambda: cfg)
    engine = Engine(lambda: cfg, store, memory, llm, onebot)
    return engine


def _auth(request):
    token = request.app["cfg"]["control"].get("token") or ""
    if not token:
        return True
    given = request.headers.get("x-aura-token") or request.query.get("token") or ""
    return given == token


@web.middleware
async def guard(request, handler):
    peer = request.remote
    if peer not in ("127.0.0.1", "::1", "localhost"):
        return web.json_response({"ok": False, "message": "只允许本机访问"}, status=403)
    if not _auth(request):
        return web.json_response({"ok": False, "message": "token 不对"}, status=401)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        log.exception("接口出错")
        return web.json_response({"ok": False, "message": f"{type(e).__name__}: {e}"}, status=500)


def make_app(cfg, engine):
    app = web.Application(middlewares=[guard])
    app["cfg"] = cfg
    app["engine"] = engine

    async def status(request):
        return web.json_response({"ok": True, **engine.status(), "ts": time.time(),
                                  "pid": os.getpid(), "config": cfg.get("_path")})

    async def send(request):
        b = await request.json()
        text = str(b.get("text") or "").strip()
        chat_id = str(b.get("chat_id") or "").strip()
        if not chat_id and b.get("user_id"):
            chat_id = f"private:{b['user_id']}"
        if not chat_id and b.get("group_id"):
            chat_id = f"group:{b['group_id']}"
        if not text or not chat_id:
            return web.json_response({"ok": False, "message": "要 chat_id 和 text"}, status=400)
        return web.json_response(await engine.send_manual(chat_id, text, at=b.get("at")))

    async def qzone(request):
        b = await request.json()
        return web.json_response(await engine.post_qzone(str(b.get("text") or ""),
                                                         str(b.get("visibility") or "")))

    async def diary(request):
        return web.json_response(await engine.write_diary())

    async def proactive(request):
        b = {}
        try:
            b = await request.json()
        except Exception:
            pass
        return web.json_response(await engine.proactive(force=bool(b.get("force"))))

    async def remember(request):
        b = await request.json()
        chat_id = str(b.get("chat_id") or f"private:{engine.owner_qq}")
        return web.json_response(engine.memory.add(
            str(b.get("text") or ""), about_name=str(b.get("about") or ""),
            about_id=str(b.get("about_id") or ""), scope_chat=chat_id,
            importance=float(b.get("importance") or 0.6), source="mcp"))

    async def recall(request):
        q = request.query.get("q") or request.query.get("query") or ""
        chat_id = request.query.get("chat_id") or f"private:{engine.owner_qq}"
        hits = engine.memory.recall(q, chat_id, limit=int(request.query.get("limit") or 10))
        return web.json_response({"ok": True, "items": hits,
                                  "message": "；".join(h["text"] for h in hits) or "没想起来"})

    async def history(request):
        chat_id = request.query.get("chat_id") or f"private:{engine.owner_qq}"
        rows = engine.store.history(chat_id, int(request.query.get("limit") or 30))
        return web.json_response({"ok": True, "items": [
            {"role": r["role"], "speaker": r["speaker_name"], "text": r["text"],
             "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))} for r in rows]})

    async def toggle(request):
        b = await request.json()
        changed = {}
        if "enabled" in b:
            cfg["behavior"]["enabled"] = bool(b["enabled"])
            changed["enabled"] = bool(b["enabled"])
        if "dry_run" in b:
            cfg["behavior"]["dry_run"] = bool(b["dry_run"])
            changed["dry_run"] = bool(b["dry_run"])
        if "group_mode" in b:
            cfg["behavior"]["group_mode"] = str(b["group_mode"])
            changed["group_mode"] = cfg["behavior"]["group_mode"]
        if b.get("persist"):
            save_config(cfg)
        return web.json_response({"ok": True, "changed": changed,
                                  "message": "，".join(f"{k}={v}" for k, v in changed.items())})

    async def model(request):
        b = await request.json()
        name = str(b.get("name") or "")
        if name:
            if name not in (cfg["llm"].get("providers") or {}):
                return web.json_response({"ok": False, "message": f"没有 provider {name}",
                                          "providers": list(cfg["llm"].get("providers") or {})})
            cfg["llm"]["default"] = name
            if b.get("persist"):
                save_config(cfg)
        p = provider(cfg)
        return web.json_response({"ok": True, "provider": p["name"], "model": p["model"],
                                  "message": f"现在用 {p['name']} / {p['model']}",
                                  "providers": list(cfg["llm"].get("providers") or {})})

    async def persona_get(request):
        return web.json_response({"ok": True, "persona": cfg.get("persona", {})})

    async def persona_set(request):
        b = await request.json()
        for k in ("name", "age", "gender", "bio", "style"):
            if k in b:
                cfg["persona"][k] = b[k]
        if b.get("persist"):
            save_config(cfg)
        return web.json_response({"ok": True, "persona": cfg["persona"], "message": "人设已更新"})

    async def friends(request):
        try:
            data = await engine.onebot.call("get_friend_list", {}, timeout=20)
        except OneBotError as e:
            return web.json_response({"ok": False, "message": str(e)})
        return web.json_response({"ok": True, "items": [
            {"user_id": f.get("user_id"), "nickname": f.get("nickname"), "remark": f.get("remark")}
            for f in data or []]})

    async def groups(request):
        try:
            data = await engine.onebot.call("get_group_list", {}, timeout=20)
        except OneBotError as e:
            return web.json_response({"ok": False, "message": str(e)})
        return web.json_response({"ok": True, "items": [
            {"group_id": g.get("group_id"), "group_name": g.get("group_name"),
             "member_count": g.get("member_count")} for g in data or []]})

    async def feeds(request):
        try:
            data = await engine.onebot.call("get_qzone_feeds", {"count": 10}, timeout=25)
        except OneBotError as e:
            return web.json_response({"ok": False, "message": str(e)})
        items = []
        for f in (data or {}).get("feeds") or []:
            items.append({"tid": f.get("key") or f.get("tid"), "uin": f.get("uin"),
                          "text": str(f.get("html") or "")[:300]})
        return web.json_response({"ok": True, "items": items})

    async def health(request):
        return web.json_response({"ok": True, "gateway": engine.onebot.state})

    app.add_routes([
        web.get("/status", status), web.get("/health", health),
        web.post("/send", send), web.post("/qzone", qzone), web.post("/diary", diary),
        web.post("/proactive", proactive), web.post("/remember", remember),
        web.get("/recall", recall), web.get("/history", history),
        web.post("/toggle", toggle), web.post("/model", model),
        web.get("/persona", persona_get), web.post("/persona", persona_set),
        web.get("/friends", friends), web.get("/groups", groups), web.get("/feeds", feeds),
    ])
    return app


async def serve(config_path=None):
    cfg = load_config(config_path)
    if not cfg.get("_exists"):
        raise SystemExit(f"找不到配置文件 {cfg['_path']}。先跑：python -m aura init")
    engine = build(cfg)
    try:
        info = await engine.onebot.probe()
        engine.onebot.self_id = int(info.get("user_id") or cfg["onebot"].get("self_id") or 0)
        log.info("网关已连上，机器人 QQ：%s（%s）", engine.onebot.self_id, info.get("nickname", ""))
    except OneBotError as e:
        log.warning("暂时连不上 QQ 网关（%s）；进程继续跑，网关恢复后自动重连", e)

    await engine.start()
    log.info("qq-aura 起来了：总开关=%s 试运行=%s 模型=%s",
             cfg["behavior"]["enabled"], cfg["behavior"]["dry_run"],
             provider(cfg)["model"])
    ctl = cfg["control"]
    app = make_app(cfg, engine)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, ctl.get("host", "127.0.0.1"), int(ctl.get("port", 8799)))
    await site.start()
    log.info("控制接口：http://%s:%s/status", ctl.get("host", "127.0.0.1"), ctl.get("port", 8799))
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await engine.stop()


def setup_logging(quiet=False):
    base = Path(os.environ.get("AURA_HOME", "~/.aura")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(base / "aura.log", encoding="utf-8")]
    if not quiet:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)


def main():
    setup_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()