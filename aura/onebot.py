"""OneBot v11 客户端：HTTP 调接口，WS(Universal) 收事件，断线自动重连。

和上游 dsh-aurabot-qq 的 src/onebot/client.js 行为对齐：
- 只有"确定没连上网关"才重试；超时/业务错误不重发，避免重复发消息；
- HTTP 为主通道，WS 只负责收事件；
- 每次连接现取端点，支持热改配置。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx
import websockets

log = logging.getLogger("aura.onebot")

RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0
RETRY_DELAYS = [1.0, 3.0, 5.0]
_NOT_CONNECTED = ("ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "EHOSTUNREACH", "ENETUNREACH")


class OneBotError(Exception):
    def __init__(self, action, message, *, transport=False, uncertain=False, data=None):
        super().__init__(f"{action} 失败：{message}")
        self.action = action
        self.transport = transport
        self.uncertain = uncertain
        self.data = data or {}


def text_of(message):
    """把 OneBot message（段数组或 CQ 串）里的纯文本提取出来。"""
    if isinstance(message, str):
        return message
    out = []
    for seg in message or []:
        t = seg.get("type")
        if t == "text":
            out.append(str(seg.get("data", {}).get("text", "")))
        elif t == "at":
            out.append("")
        elif t == "face":
            out.append("[表情]")
        elif t == "image":
            out.append("[图片]")
        elif t == "record":
            out.append("[语音]")
        elif t == "mface":
            out.append("[表情包]")
        elif t == "reply":
            continue
        else:
            out.append(f"[{t}]")
    return "".join(out).strip()


def media_of(message):
    """提取消息里的图片/语音/视频 URL，供多模态和链接解析使用。"""
    out = []
    for seg in message or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind in ("image", "record", "video"):
            url = data.get("url") or data.get("file") or ""
            if str(url).startswith(("http://", "https://")):
                out.append({"type": kind, "url": str(url)})
    return out


def mentions_me(message, self_id):
    for seg in message or []:
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == str(self_id):
            return True
    return False


def seg_text(text):
    return [{"type": "text", "data": {"text": text}}]


def seg_at(qq):
    return {"type": "at", "data": {"qq": str(qq)}}


class OneBot:
    def __init__(self, cfg_getter):
        self._cfg = cfg_getter  # callable -> dict
        self.state = "stopped"
        self.self_id = 0
        self._http = None
        self._task = None
        self._stopped = True
        self._on_event = None
        self._on_state = None
        self._session_ready = asyncio.Event()

    # ---------------- 端点 ----------------
    def _endpoint(self):
        ob = self._cfg()["onebot"]
        return {
            "http": ob.get("http_url", "").rstrip("/"),
            "ws": ob.get("ws_url", ""),
            "http_token": ob.get("http_token", ""),
            "ws_token": ob.get("ws_token", ""),
        }

    def _set_state(self, s, detail=""):
        if self.state == s:
            return
        self.state = s
        log.info("网关状态：%s %s", s, detail)
        if self._on_state:
            try:
                self._on_state(s, detail)
            except Exception:
                pass

    # ---------------- 调用 ----------------
    async def call(self, action, params=None, timeout=20.0, retry=True):
        params = params or {}
        ep = self._endpoint()
        if not ep["http"]:
            raise OneBotError(action, "没有配置 onebot.http_url", transport=True)
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=timeout, trust_env=False)
        headers = {"content-type": "application/json"}
        if ep["http_token"]:
            headers["authorization"] = f"Bearer {ep['http_token']}"

        delays = RETRY_DELAYS if retry else []
        attempt = 0
        while True:
            try:
                r = await self._http.post(f"{ep['http']}/{action}", json=params,
                                          headers=headers, timeout=timeout)
                try:
                    j = r.json()
                except Exception:
                    raise OneBotError(action, f"网关响应无法解析（HTTP {r.status}）", uncertain=True)
                if j.get("status") == "ok" or j.get("retcode") == 0:
                    return j.get("data")
                raise OneBotError(action, j.get("message") or j.get("wording") or
                                  f"retcode={j.get('retcode')}", data=j)
            except OneBotError:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt >= len(delays):
                    raise OneBotError(action, f"连不上 QQ 网关（{e}）", transport=True)
                await asyncio.sleep(delays[attempt])
                attempt += 1
            except httpx.TimeoutException:
                # 超时可能已经发出去了，不重试
                raise OneBotError(action, "超时", uncertain=True)
            except httpx.HTTPError as e:
                raise OneBotError(action, str(e), uncertain=True)

    # ---------------- 发送 ----------------
    async def send_private(self, user_id, segments, timeout=30.0):
        return await self.call("send_private_msg",
                               {"user_id": int(user_id), "message": segments}, timeout=timeout)

    async def send_group(self, group_id, segments, timeout=30.0):
        return await self.call("send_group_msg",
                               {"group_id": int(group_id), "message": segments}, timeout=timeout)

    async def send_text(self, chat, text, at=None):
        segs = []
        if chat["kind"] == "group" and at:
            segs.append(seg_at(at))
        segs.extend(seg_text(text))
        return await self.send_segments(chat, segs)

    async def send_segments(self, chat, segments, timeout=60.0):
        if chat["kind"] == "group":
            return await self.send_group(chat["id"], segments, timeout=timeout)
        return await self.send_private(chat["id"], segments, timeout=timeout)

    # ---------------- 探测 ----------------
    async def probe(self):
        info = await self.call("get_login_info", {}, timeout=10)
        self.self_id = int(info.get("user_id") or 0)
        return info

    # ---------------- 事件循环 ----------------
    async def start(self, on_event, on_state=None):
        self._on_event = on_event
        self._on_state = on_state
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="onebot-ws")

    async def stop(self):
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _loop(self):
        backoff = RECONNECT_MIN
        while not self._stopped:
            ep = self._endpoint()
            if not ep["ws"]:
                self._set_state("error", "没有配置 onebot.ws_url")
                await asyncio.sleep(5)
                continue
            url = ep["ws"]
            headers = {}
            if ep["ws_token"]:
                url = f"{url}{'&' if '?' in url else '?'}access_token={ep['ws_token']}"
                headers["Authorization"] = f"Bearer {ep['ws_token']}"
            try:
                self._set_state("connecting")
                async with websockets.connect(url, additional_headers=headers or None,
                                              max_size=None, ping_interval=25,
                                              open_timeout=12) as ws:
                    self._set_state("connected")
                    backoff = RECONNECT_MIN
                    await self._consume(ws)
                self._set_state("disconnected")
            except asyncio.CancelledError:
                raise
            except TypeError:
                # websockets < 14 用 extra_headers
                try:
                    async with websockets.connect(url, extra_headers=headers or None,
                                                  max_size=None, ping_interval=25) as ws:
                        self._set_state("connected")
                        backoff = RECONNECT_MIN
                        await self._consume(ws)
                    self._set_state("disconnected")
                except Exception as e:
                    self._set_state("error", str(e))
            except Exception as e:
                self._set_state("error", str(e)[:200])
            if self._stopped:
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 1.8, RECONNECT_MAX)
        self._set_state("stopped")

    async def _consume(self, ws):
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue
            post = evt.get("post_type")
            if post == "meta_event":
                continue
            if post == "message_sent":
                continue
            if self.self_id and str(evt.get("self_id", self.self_id)) == str(self.self_id):
                pass
            else:
                if evt.get("self_id"):
                    self.self_id = int(evt["self_id"])
            if post not in ("message",):
                continue
            try:
                await self._on_event(evt)
            except Exception:
                log.exception("事件处理失败")