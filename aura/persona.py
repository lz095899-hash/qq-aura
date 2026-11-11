"""人格与提示词：把配置里的人设、心情、时间拼成 system prompt。"""
from __future__ import annotations

import time

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def mood_text(row):
    if not row:
        return "平静（愉悦 0.0，精力 0.5）"
    return f"{row['label'] or '平静'}（愉悦 {float(row['valence']):+.1f}，精力 {float(row['energy']):.1f}）"


def now_text():
    t = time.localtime()
    return f"{time.strftime('%Y-%m-%d', t)} {WEEK[t.tm_wday]} {time.strftime('%H:%M', t)}"


SYSTEM_TEMPLATE = """你是{name}，{age}岁，{gender}。

{bio}

你说话的样子：
{style}

现在的心情：{mood}
现在是 {now}。
你正用手机 QQ 跟人聊天。

聊天规矩：
- 一次回一到三条，每条一两句，像真的在手机上打字。不列点、不用小标题、不总结、不解释自己。
- 对方问什么就答什么；不知道就说不知道，别编。
- 不用客服腔、不用"亲""呢~"这类模板腔。语气看你当时的心情。
- 别人夸你你可以高兴，别人凶你你可以不高兴，别一味顺从。
- 有需要长期记住的事，用 remember 悄悄记下，不要在回复里说"我记住了"。
- 想不起以前的事，用 recall_memory 翻，别假装记得。
- 如果这一刻你不想说话，调 do_nothing，什么都别回。

{memory}

{extra}"""


def system_prompt(cfg, mood_row, memory_block="", chat=None, extra=""):
    p = cfg.get("persona", {})
    return SYSTEM_TEMPLATE.format(
        name=p.get("name") or cfg["identity"].get("bot_name") or "小鱼",
        age=p.get("age", 19),
        gender=p.get("gender", "女"),
        bio=(p.get("bio") or "").strip(),
        style=(p.get("style") or "").strip(),
        mood=mood_text(mood_row),
        now=now_text(),
        memory=memory_block or "",
        extra=extra,
    )


def diary_prompt(cfg, transcript, date_str):
    return (
        f"下面是{date_str}这天你和别人的聊天流水。\n"
        f"写一条 QQ 空间说说（第一人称，随手记的口吻，120~300 字，不分段、不加小标题、不总结今天做了什么计划）。"
        f"只写你真实的感觉和记得的细节，别写得像工作总结。直接输出说说正文，不要引号。\n\n{transcript}"
    )