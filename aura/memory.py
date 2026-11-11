"""长期记忆：存、想、整理。

作用域铁律（和上游一致）：记忆按 chat 隔离，群聊永远看不到私聊里记的事。
召回分数 = 0.55*相关度 + 0.25*重要度 + 0.12*新鲜度 + 0.08*被想起来的次数。
相关度用中文字符二元组重合算，不依赖分词器和外部向量服务。
"""
from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger("aura.memory")

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _bigrams(text):
    s = re.sub(r"\s+", "", (text or "").lower())
    if not s:
        return set()
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    words = [w for w in re.split(r"[^\w\u4e00-\u9fff]+", s) if len(w) >= 2]
    grams.update(words)
    return grams


def relevance(query, text):
    a, b = _bigrams(query), _bigrams(text)
    if not a or not b:
        return 0.0
    hit = len(a & b)
    return min(1.0, hit / max(1, len(a)) * 1.6)


def scopes_for(chat_id, owner_chat=None):
    """本会话能看到的记忆作用域：自己这个会话 + 全局（她自己的事）。"""
    out = ["global"]
    if chat_id:
        out.insert(0, chat_id)
    return out


def _freshness(created_at, days=120.0):
    age = max(0.0, (time.time() - float(created_at or 0)) / 86400.0)
    return pow(0.5, age / days)


def score_row(row, query):
    rel = relevance(query, row["text"]) if query else 0.0
    imp = float(row["importance"] or 0.5)
    fresh = _freshness(row["created_at"])
    revive = 1.0 if (row["last_recalled"] or 0) > time.time() - 7 * 86400 else 0.0
    return 0.55 * rel + 0.25 * imp + 0.12 * fresh + 0.08 * revive


def valid_now(row):
    today = time.strftime("%Y-%m-%d")
    if row["valid_from"] and row["valid_from"] > today:
        return False
    if row["valid_until"] and row["valid_until"] < today:
        return False
    return True


class Memory:
    def __init__(self, store, llm=None, cfg_getter=None):
        self.store = store
        self.llm = llm
        self._cfg = cfg_getter or (lambda: {})

    # ---------------- 写 ----------------
    def add(self, text, about_name="", about_id="", scope_chat="global",
            importance=0.5, source="tool", tags=""):
        text = (text or "").strip()
        if not text:
            return {"ok": False, "message": "内容为空"}
        if len(text) > 300:
            text = text[:300]
        mid = self.store.add_memory(text, about_name, about_id, scope_chat,
                                    importance, source, tags)
        return {"ok": True, "id": mid, "message": "记下了"}

    # ---------------- 读 ----------------
    def recall(self, query, chat_id, about_ids=None, exclude_about=(), limit=8, min_score=0.12):
        rows = self.store.memories_for(scopes_for(chat_id), 500)
        hits = []
        for row in rows:
            if not valid_now(row):
                continue
            if row["about_id"] and str(row["about_id"]) in {str(x) for x in exclude_about}:
                continue
            if about_ids is not None and row["about_id"] and str(row["about_id"]) not in {str(x) for x in about_ids}:
                continue
            s = score_row(row, query)
            if s >= min_score:
                hits.append((s, row))
        hits.sort(key=lambda x: -x[0])
        out = []
        for s, row in hits[:limit]:
            self.store.touch_memory(row["id"])
            out.append({"id": row["id"], "text": row["text"], "about": row["about_name"],
                        "importance": row["importance"], "score": round(s, 3),
                        "created_at": row["created_at"]})
        return out

    def recall_text(self, query, chat_id, limit=6, **kw):
        hits = self.recall(query, chat_id, limit=limit, **kw)
        if not hits:
            return "没想起相关的事"
        return "；".join(f"{h['text']}" for h in hits)

    def lookup_person(self, who, chat_id):
        who = (who or "").strip()
        rows = self.store.memories_for(scopes_for(chat_id), 500)
        matched = [r for r in rows if who and (who in (r["about_name"] or "") or who == str(r["about_id"] or ""))]
        if not matched:
            return {"ok": True, "message": f"对「{who}」还没有清晰的印象"}
        matched.sort(key=lambda r: (-float(r["importance"] or 0), -(r["created_at"] or 0)))
        lines = [f"{r['text']}（重要度 {float(r['importance']):.2f}）" for r in matched[:10]]
        return {"ok": True, "message": "\n".join(lines), "count": len(matched)}

    def overview(self, chat_id, limit=12):
        rows = self.store.memories_for(scopes_for(chat_id), 500)
        rows = [r for r in rows if valid_now(r)]
        rows.sort(key=lambda r: -(float(r["importance"] or 0) * (0.5 + 0.5 * _freshness(r["created_at"]))))
        return [r["text"] for r in rows[:limit]]

    def context_block(self, chat_id, query_text="", limit=10):
        if not self._cfg().get("behavior", {}).get("memory", {}).get("enabled", True):
            return ""
        items = []
        if query_text.strip():
            for h in self.recall(query_text, chat_id, limit=4):
                items.append(h["text"])
        for t in self.overview(chat_id, limit):
            if t not in items:
                items.append(t)
        if not items:
            return ""
        return "你记得的事：\n" + "\n".join(f"- {t}" for t in items[:limit])

    # ---------------- 每日整理 ----------------
    async def consolidate(self, day=None, days=3):
        """把最近几天没整理过的聊天流水提炼成长期记忆。"""
        day = day or time.strftime("%Y-%m-%d")
        if self.store.was_consolidated(day):
            return {"ok": True, "message": f"{day} 已经整理过"}
        target_ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
        start = target_ts - max(1, days) * 86400
        rows = []
        for chat_id in self._chat_ids_since(start):
            rows.extend(self.store.messages_between(chat_id, start, target_ts + 86400))

        if len(rows) < 8:
            self.store.mark_consolidated(day)
            return {"ok": True, "message": "这段时间聊天太少，不用整理"}

        by_chat = {}
        for r in rows:
            by_chat.setdefault(r["chat_id"], []).append(r)

        saved = 0
        for chat_id, msgs in by_chat.items():
            lines = []
            for m in msgs[-400:]:
                who = m["speaker_name"] or ("我" if m["role"] == "assistant" else "对方")
                lines.append(f"{who}: {m['text']}")
            prompt = (
                "下面是一段聊天流水。请提炼出值得长期记住的事实，只记关于人本身的事"
                "（名字、身份、喜好、习惯、约定、重要经历、关系），不要记日常寒暄和闲聊细节。\n"
                "输出 JSON：{\"memories\":[{\"text\":\"一句话事实，主语写清楚\",\"about\":\"关于谁的名字或称呼\","
                "\"importance\":0.0到1.0}]}。最多 8 条，没有就返回空数组。\n\n" + "\n".join(lines)
            )
            try:
                res = await self.llm.chat(
                    [{"role": "system", "content": "你是一个只输出 JSON 的记忆整理器，不要寒暄。"},
                     {"role": "user", "content": prompt}],
                    force_json=True)
                data = json.loads(res["content"] or "{}")
            except Exception as e:
                log.warning("整理 %s 失败：%s", chat_id, e)
                continue
            for item in (data.get("memories") or [])[:8]:
                text = str(item.get("text") or "").strip()
                if len(text) < 4:
                    continue
                if self._duplicate(text, chat_id):
                    continue
                self.add(text, about_name=str(item.get("about") or ""), scope_chat=chat_id,
                         importance=float(item.get("importance") or 0.5), source="consolidate")
                saved += 1
        self.store.mark_consolidated(day)
        log.info("记忆整理完成：%s，新增 %d 条", day, saved)
        return {"ok": True, "message": f"整理完 {day}，新增 {saved} 条记忆", "saved": saved}

    def _chat_ids_since(self, start_ts):
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT DISTINCT chat_id FROM messages WHERE created_at>=?", (start_ts,)).fetchall()
        return [r["chat_id"] for r in rows]

    def _duplicate(self, text, chat_id):
        rows = self.store.memories_for(scopes_for(chat_id), 300)
        for r in rows:
            if relevance(text, r["text"]) > 0.8 and abs(len(r["text"]) - len(text)) < 40:
                return True
        return False