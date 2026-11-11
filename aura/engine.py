"""引擎：收消息 → 攒一段 → 叫模型 → 说话/沉默/发空间/记东西。

这一层是替换掉 DeepSeek Harness 的部分：上游把"想不想回、回什么、调哪个工具"交给 DSH 会话，
这里自己实现同一套判断，模型只负责生成。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from pathlib import Path

from . import tools as tools_mod
from .onebot import OneBotError, mentions_me, text_of
from .persona import diary_prompt, now_text, system_prompt

log = logging.getLogger("aura.engine")

AUDIENCE = {"public": 1, "all": 1, "friends": 4, "selected": 16,
            "self": 64, "private": 64, "excluded": 128}


class Engine:
    def __init__(self, cfg_getter, store, memory, llm, onebot):
        self.cfg = cfg_getter
        self.store = store
        self.memory = memory
        self.llm = llm
        self.onebot = onebot
        self._bufs = {}
        self._suppress = False
        self._last_proactive = 0.0
        self._last_social = 0.0
        self._scheduler_task = None
        self._stopped = False
        self.stats = {"recv": 0, "sent": 0, "turns": 0, "silent": 0, "errors": 0}
        self.last_error = ""

    # ---------------- 基础 ----------------
    @property
    def owner_qq(self):
        return str(self.cfg()["identity"].get("owner_qq") or "")

    def chat_label(self, ctx):
        return ctx.get("chat_id", "")

    def _enabled(self):
        return bool(self.cfg()["behavior"].get("enabled"))

    def _dry(self):
        return bool(self.cfg()["behavior"].get("dry_run"))

    def chat_key(self, evt):
        if evt.get("message_type") == "group":
            return f"group:{evt.get('group_id')}"
        return f"private:{evt.get('user_id')}"

    # ---------------- 收消息 ----------------
    async def on_event(self, evt):
        if evt.get("message_type") not in ("private", "group"):
            return
        uid = str(evt.get("user_id") or "")
        if not uid or uid == str(self.onebot.self_id):
            return
        text = text_of(evt.get("message"))
        if not text:
            return
        sender = evt.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or uid
        key = self.chat_key(evt)
        self.store.add_message(key, "user", text, uid, name)
        self.stats["recv"] += 1

        if not self._enabled():
            return

        is_group = evt.get("message_type") == "group"
        b = self.cfg()["behavior"]
        bot_name = (self.cfg().get("persona", {}).get("name") or "").strip()
        called = mentions_me(evt.get("message"), self.onebot.self_id)
        if not called and bot_name and bot_name in text:
            called = True

        if is_group:
            mode = b.get("group_mode", "mention_only")
            is_owner_msg = uid == self.owner_qq
            if not called and not is_owner_msg:
                if mode == "mention_only":
                    return
                p = 0.45 if mode == "chatty" else float(b.get("group_reply_probability", 0.25))
                if random.random() > p:
                    return
        await self._buffer({
            "key": key, "chat_id": key, "kind": "group" if is_group else "private",
            "id": str(evt.get("group_id") if is_group else uid), "raw_id": str(evt.get("group_id") or uid),
            "name": name, "speaker_id": uid, "speaker_name": name,
            "is_owner": uid == self.owner_qq,
        }, text, called)

    async def _buffer(self, chat, text, called):
        b = self.cfg()["behavior"]
        now = time.time()
        buf = self._bufs.get(chat["key"])
        if buf is None:
            buf = {"chat": chat, "msgs": [], "first": now, "timer": None, "called": called}
            self._bufs[chat["key"]] = buf
        buf["chat"] = chat
        buf["called"] = buf["called"] or called
        buf["msgs"].append(text)
        if buf["timer"]:
            buf["timer"].cancel()
        elapsed = (now - buf["first"]) * 1000
        wait = max(0.3, min(b.get("debounce_ms", 2500) / 1000.0,
                            (b.get("max_wait_ms", 9000) - elapsed) / 1000.0))
        buf["timer"] = asyncio.create_task(self._flush_later(chat["key"], wait))

    async def _flush_later(self, key, wait):
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        buf = self._bufs.pop(key, None)
        if not buf:
            return
        try:
            await self._turn(buf["chat"], buf["msgs"], buf.get("called"))
        except Exception as e:
            self.stats["errors"] += 1
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("这一轮没能处理")

    # ---------------- 一轮对话 ----------------
    def _ctx(self, chat):
        return {
            "chat_id": chat["chat_id"], "kind": chat["kind"], "id": chat["id"],
            "raw_id": chat["raw_id"], "speaker_id": chat["speaker_id"],
            "speaker_name": chat["speaker_name"],
            "is_owner": str(chat["speaker_id"]) == self.owner_qq,
        }

    def _history_messages(self, chat, limit):
        rows = self.store.history(chat["chat_id"], limit)
        out = []
        for r in rows:
            text = r["text"]
            if r["role"] == "assistant":
                out.append({"role": "assistant", "content": text})
            else:
                label = f"{r['speaker_name']}: " if chat["kind"] == "group" and r["speaker_name"] else ""
                out.append({"role": "user", "content": label + text})
        return out

    async def _turn(self, chat, batch, called):
        ctx = self._ctx(chat)
        b = self.cfg()["behavior"]
        limit = int(b.get("history_turns", 24)) * 2
        mem_block = self.memory.context_block(chat["chat_id"], query_text=" ".join(batch[-3:]))
        extra = ""
        if chat["kind"] == "group":
            extra = (f"这是群聊（群号 {chat['raw_id']}）。刚才叫到你的点是"
                     f"{'有人在叫你' if called else '你自己想插话'}。没兴趣就 do_nothing。")
        sysmsg = system_prompt(self.cfg(), self.store.get_mood(), mem_block, chat, extra)
        messages = [{"role": "system", "content": sysmsg}]
        messages.extend(self._history_messages(chat, limit))
        messages.append({"role": "user", "content":
                         "（接着往下。要不要回、回几条由你决定；不想说话就调 do_nothing。）"})

        tool_schemas = tools_mod.build(ctx)
        content, used = await self._run_tool_loop(messages, tool_schemas, ctx)
        self.stats["turns"] += 1

        if self._suppress:
            self._suppress = False
            self.stats["silent"] += 1
            log.info("[%s] 决定不说", chat["chat_id"])
            return
        if not content:
            self.stats["silent"] += 1
            return
        if content.strip().upper() in ("NO_REPLY", "NO_REPLY."):
            self.stats["silent"] += 1
            return

        await self._send_reply(chat, content, at=chat["speaker_id"])

    async def _run_tool_loop(self, messages, tool_schemas, ctx):
        rounds = int(self.cfg()["llm"].get("max_tool_rounds", 4))
        used = []
        content = ""
        for _ in range(rounds):
            res = await self.llm.chat(messages, tools=tool_schemas or None)
            if res["tool_calls"]:
                messages.append({
                    "role": "assistant", "content": res["content"] or "",
                    "tool_calls": [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"],
                                                 "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                                   for c in res["tool_calls"]],
                })
                for call in res["tool_calls"]:
                    try:
                        out = await tools_mod.dispatch(self, call["name"], call["arguments"], ctx)
                    except Exception as e:
                        out = {"ok": False, "message": str(e)}
                    used.append(call["name"])
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": tools_mod.to_json(out)})
                if self._suppress:
                    return "", used
                content = res["content"] or content
                continue
            content = res["content"]
            break
        return content, used

    def _split(self, text):
        b = self.cfg()["behavior"]
        max_segs = int(b.get("max_reply_segments", 3))
        max_chars = int(b.get("max_segment_chars", 400))
        text = (text or "").replace("**", "").replace("`", "").strip()
        lines = [l.strip(" -•\t") for l in text.split("\n") if l.strip(" -•\t")]
        segs = lines or [text]
        out = []
        for s in segs:
            while len(s) > max_chars and len(out) < max_segs:
                out.append(s[:max_chars])
                s = s[max_chars:]
            if len(out) >= max_segs:
                break
            out.append(s)
        return [x for x in out[:max_segs] if x.strip()]

    async def _send_reply(self, chat, content, at=None):
        segs = self._split(content)
        if not segs:
            return
        if self._dry():
            log.info("[dry_run] 本来要发给 %s：%s", chat["chat_id"], " / ".join(segs))
            return
        lo, hi = (self.cfg()["behavior"].get("reply_delay_ms") or [600, 2200])[:2]
        for i, seg in enumerate(segs):
            if i:
                await asyncio.sleep(random.uniform(float(lo), float(hi)) / 1000.0)
            try:
                await self.onebot.send_text(chat, seg, at=at)
                self.stats["sent"] += 1
            except OneBotError as e:
                self.stats["errors"] += 1
                self.last_error = str(e)
                log.warning("发送失败：%s", e)
                break
        self.store.add_message(chat["chat_id"], "assistant", content,
                               self.onebot.self_id, self.cfg()["persona"].get("name", ""))

    # ---------------- 主动 / 空间 ----------------
    async def proactive(self, force=False):
        b = self.cfg()["behavior"]
        p = b.get("proactive") or {}
        if not p.get("enabled") and not force:
            return {"ok": False, "message": "主动搭话没开"}
        if not self.owner_qq:
            return {"ok": False, "message": "没配 identity.owner_qq"}
        key = f"private:{self.owner_qq}"
        idle_h = (time.time() - (self.store.last_user_ts(key) or 0)) / 3600.0
        if not force and idle_h < float(p.get("idle_hours", 5)):
            return {"ok": False, "message": f"主人才 {idle_h:.1f} 小时没说话，还不到时候"}
        if not force and not self._in_awake(p.get("awake") or []):
            return {"ok": False, "message": "不在她醒着的时间段"}
        if not force and self.store.count_today("proactive") >= int(p.get("max_per_day", 3)):
            return {"ok": False, "message": "今天主动找过够多次了"}
        if not force and random.random() > float(p.get("probability", 0.25)):
            return {"ok": False, "message": "这次没想说话"}

        chat = {"key": key, "chat_id": key, "kind": "private", "id": self.owner_qq,
                "raw_id": self.owner_qq, "name": "主人", "speaker_id": self.owner_qq,
                "speaker_name": self.cfg()["identity"].get("owner_name", "主人")}
        mem_block = self.memory.context_block(key)
        sysmsg = system_prompt(self.cfg(), self.store.get_mood(), mem_block, chat)
        messages = [{"role": "system", "content": sysmsg}]
        messages.extend(self._history_messages(chat, 12))
        messages.append({"role": "user", "content":
                         f"（已经 {idle_h:.1f} 小时没人跟你说话了。想说点什么就说一句，"
                         "不想说就只回 NO_REPLY。）"})
        content, _ = await self._run_tool_loop(messages, tools_mod.build(self._ctx(chat)),
                                               self._ctx(chat))
        self._suppress = False
        if not content or content.strip().upper().startswith("NO_REPLY"):
            return {"ok": False, "message": "她想了想，没说话"}
        await self._send_reply(chat, content)
        self.store.bump("proactive")
        return {"ok": True, "message": "主动发了", "text": content}

    def _in_awake(self, awake):
        if not awake:
            return True
        try:
            start = time.strptime(str(awake[0]), "%H:%M")
            end = time.strptime(str(awake[1]) if len(awake) > 1 else "23:59", "%H:%M")
        except ValueError:
            return True
        now = time.localtime()
        cur = (now.tm_hour, now.tm_min)
        a = (start.tm_hour, start.tm_min)
        b = (end.tm_hour, end.tm_min)
        return a <= cur <= b if a <= b else (cur >= a or cur <= b)

    async def post_qzone(self, text, visibility="", ctx=None):
        text = (text or "").strip()
        if not text:
            return {"ok": False, "message": "内容为空"}
        qz = self.cfg()["behavior"].get("qzone") or {}
        if not qz.get("enabled"):
            return {"ok": False, "message": "空间功能没开（behavior.qzone.enabled）"}
        mode = visibility or qz.get("audience", "public")
        right = AUDIENCE.get(mode, 1)
        if self._dry():
            log.info("[dry_run] 本来要发说说：%s", text[:200])
            return {"ok": True, "message": "试运行，没真发", "dry_run": True}
        try:
            r = await self.onebot.call("send_qzone_msg", {"content": text, "ugc_right": right},
                                       timeout=60)
        except OneBotError as e:
            return {"ok": False, "message": str(e)}
        tid = str((r or {}).get("tid") or (r or {}).get("id") or r or "")
        self.store.add_qzone(tid, text)
        return {"ok": True, "message": f"说说发出去了（tid {tid or '?'}）", "tid": tid}

    async def change_avatar(self, source="", force=False, ctx=None):
        """更换 QQ 头像。source 可为 HTTPS URL，或 avatar_dir 内的文件名。"""
        cfg = self.cfg()["behavior"].get("avatar") or {}
        if not cfg.get("enabled"):
            return {"ok": False, "message": "换头像功能没开（behavior.avatar.enabled）"}
        source = str(source or "").strip()

        # 如果是主人触发，允许直接更换跳过冷却
        if ctx and ctx.get("is_owner"):
            force = True

        avatar_dir = Path(str(cfg.get("dir") or "~/.aura/avatars")).expanduser().resolve()
        choices = sorted(p.name for p in avatar_dir.glob("*") if p.suffix.lower() in
                         {".jpg", ".jpeg", ".png", ".webp"}) if avatar_dir.exists() else []

        # 留空或泛指时，自动从图库选择
        if not source or source.lower() in ("default", "auto", "none", "null") or "头像" in source:
            if "xiaoyu-thoughtful.png" in choices:
                source = "xiaoyu-thoughtful.png"
            elif choices:
                source = choices[0]
            else:
                return {"ok": False, "message": "缺少头像图片，图库中无可用文件"}

        cooldown_h = max(0.0, float(cfg.get("cooldown_hours", 168)))
        last = float(self.store.get("avatar_last_changed", 0) or 0)
        remaining = cooldown_h * 3600 - (time.time() - last)
        if remaining > 0 and not force:
            return {"ok": False, "message": f"头像冷却中，还要 {remaining / 3600:.1f} 小时"}

        if source.startswith("https://"):
            payload = source
            shown = source
        elif source.startswith("http://"):
            return {"ok": False, "message": "远程头像只允许 HTTPS URL"}
        else:
            candidate = Path(source).expanduser()
            path = candidate.resolve() if candidate.is_absolute() else (avatar_dir / candidate).resolve()
            try:
                path.relative_to(avatar_dir)
            except ValueError:
                return {"ok": False, "message": "本地头像必须位于配置的 avatar.dir 目录内"}
            if not path.is_file():
                matched = [c for c in choices if Path(source).stem.lower() in c.lower()]
                if matched:
                    path = avatar_dir / matched[0]
                elif "xiaoyu-thoughtful.png" in choices:
                    path = avatar_dir / "xiaoyu-thoughtful.png"
                else:
                    return {"ok": False, "message": "头像文件不存在", "available": choices[:30]}
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                return {"ok": False, "message": "头像只支持 jpg/jpeg/png/webp"}
            if path.stat().st_size > int(cfg.get("max_bytes", 5 * 1024 * 1024)):
                return {"ok": False, "message": "头像文件过大"}
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            payload = f"base64://{encoded}"
            shown = path.name

        if self._dry():
            log.info("[dry_run] 本来要换头像：%s", shown)
            return {"ok": True, "message": "试运行，没真换", "dry_run": True}
        try:
            await self.onebot.call("set_qq_avatar", {"file": payload}, timeout=60)
        except OneBotError as e:
            return {"ok": False, "message": str(e)}
        self.store.set("avatar_last_changed", time.time())
        self.store.set("avatar_current", shown)
        return {"ok": True, "message": f"头像已更换：{shown}", "source": shown}

    async def write_diary(self, ctx=None):
        key = f"private:{self.owner_qq}"
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        rows = []
        for chat_id in self.memory._chat_ids_since(start):
            rows.extend(self.store.messages_between(chat_id, start, time.time() + 60))
        if len(rows) < 4:
            return {"ok": False, "message": "今天聊得太少，写不出东西"}
        lines = []
        for r in rows[-300:]:
            who = r["speaker_name"] or ("我" if r["role"] == "assistant" else "对方")
            lines.append(f"{who}: {r['text']}")
        res = await self.llm.chat(
            [{"role": "system", "content": "你只输出一段说说正文，不要标题、不要引号、不要解释。"},
             {"role": "user", "content": diary_prompt(self.cfg(), "\n".join(lines),
                                                      time.strftime("%Y-%m-%d"))}])
        text = (res["content"] or "").strip()
        if not text:
            return {"ok": False, "message": "模型没写出东西"}
        out = await self.post_qzone(text, ctx=ctx)
        if out.get("ok"):
            self.store.bump("diary")
            out["text"] = text
        return out

    # ---------------- 定时 ----------------
    async def scheduler(self):
        self._stopped = False
        while not self._stopped:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("定时任务出错")
            await asyncio.sleep(30)

    async def _tick(self):
        b = self.cfg()["behavior"]
        # 心情慢慢回落到平静
        row = self.store.get_mood()
        v, e = float(row["valence"]), float(row["energy"])
        if abs(v - 0.1) > 0.01 or abs(e - 0.5) > 0.01:
            self.store.set_mood(v + (0.1 - v) * 0.05, e + (0.5 - e) * 0.03, row["label"], row["note"])

        # 主动搭话
        p = b.get("proactive") or {}
        if p.get("enabled") and time.time() - self._last_proactive >= float(p.get("tick_seconds", 300)):
            self._last_proactive = time.time()
            await self.proactive()

        # 空间日记
        qz = b.get("qzone") or {}
        if qz.get("enabled") and self._enabled() and self.store.count_today("diary") < int(qz.get("max_per_day", 1)):
            for t in qz.get("diary_times") or []:
                if self._near(t, 20) and self.store.count_today("diary") == 0:
                    await self.write_diary()
                    break

        # 记忆整理
        mem = b.get("memory") or {}
        if mem.get("enabled") and self._near(mem.get("consolidate_at", "03:30"), 30):
            yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            if not self.store.was_consolidated(yesterday):
                await self.memory.consolidate(yesterday, int(mem.get("consolidate_days", 3)))

        # 空间社交
        social = (qz.get("social") or {})
        if qz.get("enabled") and social.get("enabled") and self._enabled() \
                and time.time() - self._last_social > 3600:
            self._last_social = time.time()
            await self.browse_qzone_social(social)

    def _near(self, hhmm, window_min=10):
        try:
            h, m = [int(x) for x in str(hhmm).split(":")]
        except Exception:
            return False
        now = time.localtime()
        return now.tm_hour == h and abs(now.tm_min - m) <= window_min

    async def browse_qzone_social(self, social):
        """逛好友空间：按概率点赞/评论。失败就当没逛，不影响主流程。"""
        try:
            res = await self.onebot.call("get_qzone_feeds", {"count": 15}, timeout=25)
        except OneBotError as e:
            log.info("逛空间失败：%s", e)
            return {"ok": False, "message": str(e)}
        done_like = done_comment = 0
        for feed in (res or {}).get("feeds") or []:
            key = str(feed.get("key") or feed.get("tid") or "")
            uin = feed.get("uin")
            if not key or self.store.seen(f"qzone:{key}"):
                continue
            self.store.mark_seen(f"qzone:{key}")
            brief = str(feed.get("html") or "").replace("<", " <")[:400]
            try:
                if random.random() < float(social.get("like_probability", 0.35)) and done_like < 2:
                    await self.onebot.call("like_qzone", {"tid": key, "target_uin": int(uin)}, timeout=20)
                    done_like += 1
                if random.random() < float(social.get("comment_probability", 0.15)) and done_comment < 1:
                    res2 = await self.llm.chat([
                        {"role": "system", "content": "你在 QQ 空间看到一个好友的说说，用一句话留个言。"
                                                      "口语、自然、不超过 20 字，不夸、不套话，直接输出这句话。"},
                        {"role": "user", "content": brief}])
                    c = (res2["content"] or "").strip()[:40]
                    if c:
                        await self.onebot.call("comment_qzone", {"tid": key, "content": c,
                                                                 "target_uin": int(uin)}, timeout=25)
                        done_comment += 1
            except Exception as e:
                log.info("空间互动跳过 %s：%s", key, e)
        return {"ok": True, "like": done_like, "comment": done_comment}

    async def start(self):
        await self.onebot.start(self.on_event)
        self._scheduler_task = asyncio.create_task(self.scheduler(), name="aura-scheduler")

    async def stop(self):
        self._stopped = True
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
        for buf in self._bufs.values():
            if buf["timer"]:
                buf["timer"].cancel()
        self._bufs.clear()
        await self.onebot.stop()
        await self.llm.close()

    # ---------------- 对外 ----------------
    async def send_manual(self, chat_id, text, at=None):
        """外部（MCP/控制接口）让我发一条。"""
        kind, _, rid = str(chat_id).partition(":")
        chat = {"key": chat_id, "chat_id": chat_id, "kind": kind, "id": rid, "raw_id": rid,
                "speaker_id": "", "speaker_name": ""}
        if self._dry():
            return {"ok": False, "message": "当前是 dry_run，没真发"}
        try:
            await self.onebot.send_text(chat, text, at=at)
        except OneBotError as e:
            return {"ok": False, "message": str(e)}
        self.store.add_message(chat_id, "assistant", text, self.onebot.self_id, "manual")
        self.stats["sent"] += 1
        return {"ok": True, "message": "发出去了"}

    def status(self):
        from .config import provider
        try:
            p = provider(self.cfg())
            model = f"{p['name']}/{p['model']}"
            key_set = bool(p.get("api_key"))
        except Exception as e:
            model = f"（配置有误：{e}）"
            key_set = False
        mood = self.store.get_mood()
        return {
            "enabled": self._enabled(),
            "dry_run": self._dry(),
            "gateway": self.onebot.state,
            "self_id": self.onebot.self_id,
            "owner_qq": self.owner_qq,
            "model": model,
            "api_key_set": key_set,
            "persona": self.cfg()["persona"].get("name"),
            "mood": {"label": mood["label"], "valence": round(float(mood["valence"]), 2),
                     "energy": round(float(mood["energy"]), 2)},
            "memories": self.store.memory_count(),
            "stats": self.stats,
            "last_error": self.last_error,
            "db": str(self.store.path),
        }