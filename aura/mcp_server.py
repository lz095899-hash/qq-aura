"""MCP 层：把 qq-aura 暴露给 Hermes / Claude / 任何 MCP 客户端。

它只是个控制面板——真正的收发、记忆、定时都在 `aura run` 那个常驻进程里，
所以 Hermes 关掉她也不会掉线。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from .config import load_config

ROOT = Path(__file__).resolve().parent.parent
mcp = FastMCP("qq-aura")


def _cfg():
    return load_config()


def _base():
    c = _cfg()["control"]
    return f"http://{c.get('host', '127.0.0.1')}:{int(c.get('port', 8799))}"


def _headers():
    token = _cfg()["control"].get("token") or ""
    return {"x-aura-token": token} if token else {}


class Down(Exception):
    pass


async def _req(method, path, body=None, params=None, timeout=30.0):
    url = _base() + path
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
        try:
            r = await c.request(method, url, json=body, params=params, headers=_headers())
        except httpx.HTTPError as e:
            raise Down(f"连不上 aura 常驻进程（{_base()}）：{e}")
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
    return data


def _fmt(d):
    if isinstance(d, str):
        return d
    if d.get("ok") is False:
        return "✗ " + str(d.get("message") or "没成功")
    if "message" in d:
        return str(d["message"])
    return str(d)


async def _safe(fn, *a, **kw):
    try:
        return _fmt(await fn(*a, **kw))
    except Down as e:
        return (f"✗ {e}\n先跑一次 aura_start，或在终端执行：{sys.executable} -m aura run")
    except httpx.HTTPError as e:
        return f"✗ 请求出错：{e}"


# ---------------- 状态 / 启动 ----------------
@mcp.tool()
async def aura_status() -> str:
    """查她现在的状态：是否上线、网关通不通、用哪个模型、心情、记忆条数、收发计数。"""
    d = await _req("GET", "/status")
    if not d.get("ok"):
        return _fmt(d)
    s = d
    return "\n".join([
        f"总开关：{'开' if s['enabled'] else '关'}　试运行：{'是' if s['dry_run'] else '否'}",
        f"网关：{s['gateway']}　机器人 QQ：{s['self_id'] or '未探测'}　主人：{s['owner_qq']}",
        f"模型：{s['model']}（key {'已配' if s['api_key_set'] else '没配'}）　人设：{s['persona']}",
        f"心情：{s['mood']['label']}（{s['mood']['valence']:+}/{s['mood']['energy']}）　记忆：{s['memories']} 条",
        f"收 {s['stats']['recv']} / 发 {s['stats']['sent']} / 轮次 {s['stats']['turns']} / 沉默 {s['stats']['silent']} / 错 {s['stats']['errors']}",
        f"进程 {s['pid']}　DB {s['db']}" + (f"\n最近错误：{s['last_error']}" if s.get("last_error") else ""),
    ])


@mcp.tool()
async def aura_start() -> str:
    """如果常驻进程没在跑，就把它在后台拉起来（不占当前会话）。已经在线则直接返回状态。"""
    try:
        d = await _req("GET", "/health", timeout=3)
        if d.get("ok"):
            return f"她已经在线（网关 {d.get('gateway')}）"
    except Down:
        pass
    cfg_path = os.environ.get("AURA_CONFIG") or str(Path("~/.aura/config.yaml").expanduser())
    if not Path(cfg_path).exists():
        return f"✗ 没有配置文件 {cfg_path}，先跑 python -m aura init"
    log_dir = Path("~/.aura").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "aura.out", "ab")
    env = dict(os.environ, AURA_CONFIG=cfg_path)
    subprocess.Popen([sys.executable, "-m", "aura", "run"], cwd=str(ROOT), env=env,
                     stdout=out, stderr=out, start_new_session=True)
    for _ in range(20):
        await asyncio.sleep(1)
        try:
            d = await _req("GET", "/health", timeout=2)
            if d.get("ok"):
                st = await _req("GET", "/status")
                return f"已拉起（pid {st.get('pid')}，网关 {d.get('gateway')}）"
        except Down:
            continue
    return "✗ 拉起来了但 20 秒内没响应，看 ~/.aura/aura.log"


# ---------------- 发言 ----------------
@mcp.tool()
async def aura_send(chat_id: str, text: str, at: str = "") -> str:
    """用她的号发一条消息。chat_id 形如 private:123456 或 group:987654；at 填要@的QQ号（群聊可选）。"""
    return await _safe(_req, "POST", "/send", {"chat_id": chat_id, "text": text,
                                               "at": at or None})


@mcp.tool()
async def aura_qzone_post(text: str, visibility: str = "") -> str:
    """用她的号发一条 QQ 空间说说。visibility 可选 public / friends / self，留空按配置。"""
    return await _safe(_req, "POST", "/qzone", {"text": text, "visibility": visibility or None})


@mcp.tool()
async def aura_diary() -> str:
    """让她按今天的聊天流水写一篇空间日记并发出去。"""
    return await _safe(_req, "POST", "/diary", {}, timeout=180)


@mcp.tool()
async def aura_proactive(force: bool = True) -> str:
    """让她主动找主人说句话。force=true 时忽略「多久没说话/概率/次数」这些条件。"""
    return await _safe(_req, "POST", "/proactive", {"force": force}, timeout=120)


# ---------------- 记忆 ----------------
@mcp.tool()
async def aura_remember(text: str, about: str = "", chat_id: str = "", importance: float = 0.6) -> str:
    """往她的长期记忆里写一条（作用域默认是主人私聊；chat_id 可指定某个群/某个人）。"""
    return await _safe(_req, "POST", "/remember", {"text": text, "about": about,
                                                   "chat_id": chat_id or None,
                                                   "importance": importance})


@mcp.tool()
async def aura_recall(query: str, chat_id: str = "", limit: int = 8) -> str:
    """翻她的记忆库。作用域默认主人私聊，跨会话的记忆取不到。"""
    d = await _req("GET", "/recall", params={"q": query, "chat_id": chat_id or
                                             f"private:{_cfg()['identity']['owner_qq']}",
                                             "limit": limit})
    if not d.get("ok"):
        return _fmt(d)
    if not d.get("items"):
        return "没想起来"
    return "\n".join(f"- {i['text']}（{i['about'] or '—'}，{i['score']}）" for i in d["items"])


@mcp.tool()
async def aura_history(chat_id: str = "", limit: int = 20) -> str:
    """看某段会话最近的聊天记录。默认主人私聊。"""
    d = await _req("GET", "/history", params={"chat_id": chat_id or
                                              f"private:{_cfg()['identity']['owner_qq']}",
                                              "limit": limit})
    if not d.get("ok"):
        return _fmt(d)
    if not d.get("items"):
        return "还没有记录"
    return "\n".join(f"[{i['at']}] {i['speaker'] or i['role']}：{i['text']}" for i in d["items"])


# ---------------- 开关 / 模型 / 人设 ----------------
@mcp.tool()
async def aura_toggle(enabled: bool = None, dry_run: bool = None, group_mode: str = "",
                      persist: bool = True) -> str:
    """改总开关 / 试运行 / 群聊档位（mention_only|normal|chatty）。persist=true 写回配置文件。"""
    body = {"persist": persist}
    if enabled is not None:
        body["enabled"] = enabled
    if dry_run is not None:
        body["dry_run"] = dry_run
    if group_mode:
        body["group_mode"] = group_mode
    return await _safe(_req, "POST", "/toggle", body)


@mcp.tool()
async def aura_model(name: str = "") -> str:
    """看当前模型，或切换到某个 provider（配置里 llm.providers 的名字，如 mimo / deepseek / openai / local）。"""
    d = await _safe(_req, "POST", "/model", {"name": name, "persist": bool(name)})
    return d


@mcp.tool()
async def aura_persona(name: str = "", bio: str = "", style: str = "", age: int = None,
                       gender: str = "", persist: bool = True) -> str:
    """看或改她的人设。留空参数=只看；传了就改（persist=true 写回配置）。"""
    if not any([name, bio, style, age, gender]):
        d = await _req("GET", "/persona")
        p = d.get("persona", {})
        return (f"名字：{p.get('name')}　{age or p.get('age')}岁 {p.get('gender')}\n"
                f"人设：\n{(p.get('bio') or '').strip()}\n说话方式：\n{(p.get('style') or '').strip()}")
    body = {"persist": persist}
    for k, v in (("name", name), ("bio", bio), ("style", style), ("gender", gender)):
        if v:
            body[k] = v
    if age:
        body["age"] = age
    return await _safe(_req, "POST", "/persona", body)


# ---------------- 通讯录 / 空间 ----------------
@mcp.tool()
async def aura_friends() -> str:
    """列出她 QQ 号上的好友（要网关在线）。"""
    d = await _req("GET", "/friends")
    if not d.get("ok"):
        return _fmt(d)
    items = d.get("items") or []
    if not items:
        return "好友列表是空的"
    return "\n".join(f"{i['user_id']}　{i.get('remark') or i.get('nickname') or ''}" for i in items)


@mcp.tool()
async def aura_feeds() -> str:
    """看她 QQ 空间好友动态（要网关支持 get_qzone_feeds）。"""
    d = await _req("GET", "/feeds")
    if not d.get("ok"):
        return _fmt(d)
    items = d.get("items") or []
    if not items:
        return "没有新动态"
    return "\n".join(f"[{i.get('uin')}] {i['text'][:120]}" for i in items)


if __name__ == "__main__":
    mcp.run()