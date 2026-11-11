"""扩展能力：热榜/链接解析、天气、MiMo TTS、本地视觉素材。

所有外部调用都允许失败并返回可读错误，不阻塞 QQ 主消息链路。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx


URL_RE = re.compile(r"https?://[^\s<>\]\[）)]+", re.I)


class Extras:
    def __init__(self, cfg_getter):
        self.cfg = cfg_getter
        self._http = httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False,
                                       headers={"user-agent": "Mozilla/5.0 qq-aura/0.2"})

    def _behavior(self, key):
        return (self.cfg().get("behavior", {}).get(key) or {})

    @staticmethod
    def _secret(name):
        """读环境变量；Hermes 守护进程未继承环境时，安全回退到 ~/.hermes/.env。"""
        if not name:
            return ""
        value = os.environ.get(str(name), "")
        if value:
            return value
        env_file = Path("~/.hermes/.env").expanduser()
        if not env_file.is_file():
            return ""
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, val = raw.split("=", 1)
                if key.strip() == str(name):
                    return val.strip().strip('"').strip("'")
        except OSError:
            pass
        return ""

    # ---------- 外部内容 ----------
    async def bilibili_hot(self, count=8):
        count = max(1, min(int(count), 20))
        r = await self._http.get("https://api.bilibili.com/x/web-interface/popular",
                                 params={"pn": 1, "ps": count})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "B站接口失败")
        out = []
        for x in ((data.get("data") or {}).get("list") or [])[:count]:
            out.append({"platform": "bilibili", "title": x.get("title", ""),
                        "author": (x.get("owner") or {}).get("name", ""),
                        "url": x.get("short_link_v2") or f"https://www.bilibili.com/video/{x.get('bvid')}",
                        "summary": (x.get("desc") or "")[:240],
                        "views": (x.get("stat") or {}).get("view", 0),
                        "cover": x.get("pic", "")})
        return out

    async def douyin_hot(self, count=8):
        """抖音热榜。公开网页接口会变，失败时由调用方降级到 B 站。"""
        count = max(1, min(int(count), 20))
        urls = [
            "https://www.douyin.com/aweme/v1/web/hot/search/list/",
            "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        ]
        last = ""
        for url in urls:
            try:
                r = await self._http.get(url, headers={"referer": "https://www.douyin.com/"})
                r.raise_for_status()
                j = r.json()
                rows = ((j.get("data") or {}).get("word_list") or
                        (j.get("data") or {}).get("word_list_v2") or
                        j.get("word_list") or [])
                if rows:
                    return [{"platform": "douyin", "title": x.get("word") or x.get("sentence") or "",
                             "url": "https://www.douyin.com/search/" + quote(str(x.get("word") or x.get("sentence") or "")),
                             "hot_value": x.get("hot_value") or x.get("hot_value_text") or 0}
                            for x in rows[:count]]
            except Exception as e:
                last = str(e)
        raise RuntimeError(f"抖音公开热榜暂不可用：{last or '无数据'}")

    async def trending(self, platform="all", count=8):
        platform = (platform or "all").lower()
        if platform in ("bilibili", "b站", "bili"):
            return await self.bilibili_hot(count)
        if platform in ("douyin", "抖音"):
            return await self.douyin_hot(count)
        out, errors = [], []
        for fn in (self.douyin_hot, self.bilibili_hot):
            try:
                out.extend(await fn(max(2, count // 2)))
            except Exception as e:
                errors.append(str(e))
        if not out:
            raise RuntimeError("；".join(errors) or "热榜暂不可用")
        return out[:count]

    async def resolve_url(self, url):
        """解析抖音/B站链接；未知站点返回最终 URL 和网页标题。"""
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("不是有效链接")
        r = await self._http.get(url, headers={"referer": url})
        r.raise_for_status()
        final = str(r.url)
        host = urlparse(final).netloc.lower()

        bvid = ""
        m = re.search(r"/(BV[0-9A-Za-z]+)", final)
        if m:
            bvid = m.group(1)
        if bvid or "bilibili.com" in host:
            if not bvid:
                bvid_m = re.search(r'"bvid"\s*:\s*"(BV[0-9A-Za-z]+)"', r.text)
                bvid = bvid_m.group(1) if bvid_m else ""
            if bvid:
                d = await self._http.get("https://api.bilibili.com/x/web-interface/view",
                                         params={"bvid": bvid})
                j = d.json().get("data") or {}
                return {"platform": "bilibili", "url": final, "title": j.get("title", ""),
                        "author": (j.get("owner") or {}).get("name", ""),
                        "summary": (j.get("desc") or "")[:600], "cover": j.get("pic", "")}

        if any(x in host for x in ("douyin.com", "iesdouyin.com")):
            aweme = re.search(r"/(?:video|note)/(\d+)", final)
            aweme_id = aweme.group(1) if aweme else ""
            if aweme_id:
                detail = await self._http.get(
                    "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                    params={"aweme_id": aweme_id, "aid": 6383},
                    headers={"origin": "https://open.douyin.com",
                             "referer": "https://open.douyin.com/"})
                if detail.status_code == 200:
                    item = detail.json().get("aweme_detail") or {}
                    if item:
                        author = item.get("author") or {}
                        video = item.get("video") or {}
                        images = item.get("images") or []
                        media = ((video.get("play_addr") or {}).get("url_list") or [""])[0]
                        if images:
                            media = [((x.get("url_list") or [""])[0]) for x in images]
                        return {"platform": "douyin", "url": final, "aweme_id": aweme_id,
                                "title": (item.get("desc") or "")[:300],
                                "author": author.get("nickname", ""),
                                "media": media, "cover": ((video.get("cover") or {}).get("url_list") or [""])[0],
                                "duration": int(video.get("duration") or 0) // 1000,
                                "stats": item.get("statistics") or {}}
            title = self._html_title(r.text)
            desc = self._meta(r.text, "description")
            return {"platform": "douyin", "url": final, "aweme_id": aweme_id,
                    "title": title, "summary": desc[:600]}

        return {"platform": host, "url": final, "title": self._html_title(r.text),
                "summary": self._meta(r.text, "description")[:600]}

    @staticmethod
    def _html_title(text):
        m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.I | re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    @staticmethod
    def _meta(text, name):
        patterns = [rf'<meta[^>]+name=["\']{name}["\'][^>]+content=["\']([^"\']*)',
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{name}["\']']
        for p in patterns:
            m = re.search(p, text or "", re.I)
            if m:
                return m.group(1)
        return ""

    async def enrich_links(self, text, max_links=2):
        out = []
        for u in URL_RE.findall(text or "")[:max_links]:
            try:
                out.append(await self.resolve_url(u))
            except Exception as e:
                out.append({"url": u, "error": str(e)})
        return out

    # ---------- 天气 ----------
    async def weather(self, city=""):
        city = (city or self._behavior("weather").get("home_city") or "").strip()
        if not city:
            return {"ok": False, "message": "还不知道城市，请告诉我城市名"}
        g = await self._http.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": city, "count": 1, "language": "zh", "format": "json"})
        g.raise_for_status()
        rows = g.json().get("results") or []
        if not rows:
            return {"ok": False, "message": f"没找到城市：{city}"}
        p = rows[0]
        r = await self._http.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": p["latitude"], "longitude": p["longitude"],
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 2})
        r.raise_for_status()
        j = r.json()
        cur, daily = j.get("current") or {}, j.get("daily") or {}
        code = int(cur.get("weather_code") or 0)
        label = self._weather_label(code)
        return {"ok": True, "city": p.get("name", city), "region": p.get("admin1", ""),
                "weather": label, "temperature": cur.get("temperature_2m"),
                "feels_like": cur.get("apparent_temperature"), "wind": cur.get("wind_speed_10m"),
                "rain": cur.get("precipitation"),
                "today_high": (daily.get("temperature_2m_max") or [None])[0],
                "today_low": (daily.get("temperature_2m_min") or [None])[0],
                "rain_probability": (daily.get("precipitation_probability_max") or [None])[0]}

    @staticmethod
    def _weather_label(code):
        if code == 0: return "晴"
        if code <= 3: return "多云"
        if code <= 48: return "有雾"
        if code <= 67: return "下雨"
        if code <= 77: return "下雪"
        if code <= 82: return "阵雨"
        if code <= 86: return "阵雪"
        return "雷雨"

    # ---------- 图片理解 / 音乐 ----------
    async def describe_image(self, url, prompt=""):
        cfg = self._behavior("vision")
        if not cfg.get("enabled"):
            return {"ok": False, "message": "看图功能没开"}
        from .config import provider as provider_of
        p = provider_of(self.cfg(), cfg.get("provider") or None)
        model = cfg.get("model") or p.get("model")
        image_url = url
        if str(url).startswith(("http://", "https://")):
            image_res = await self._http.get(url, timeout=45)
            image_res.raise_for_status()
            mime = image_res.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            image_url = f"data:{mime};base64," + base64.b64encode(image_res.content).decode("ascii")
        body = {"model": model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt or "请用中文客观描述这张图，指出人物、场景、文字和明显情绪，100字以内。"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}], "temperature": 0.3, "stream": False}
        r = await self._http.post(p["base_url"].rstrip("/") + "/chat/completions", json=body,
                                  headers={"authorization": f"Bearer {p.get('api_key', '')}",
                                           "content-type": "application/json"}, timeout=90)
        if r.status_code >= 400:
            return {"ok": False, "message": f"看图模型 HTTP {r.status_code}: {r.text[:160]}"}
        try:
            text = ((r.json().get("choices") or [])[0].get("message") or {}).get("content") or ""
        except Exception:
            text = ""
        return {"ok": bool(text), "description": str(text).strip(),
                "message": "看清了" if text else "没看清图片"}

    async def search_music(self, query="", limit=5):
        query = (query or "甜甜的恋爱歌曲").strip()
        r = await self._http.post("https://music.163.com/api/search/get/web", data={
            "s": query, "type": 1, "offset": 0, "limit": max(1, min(int(limit), 10))})
        r.raise_for_status()
        rows = (r.json().get("result") or {}).get("songs") or []
        out = []
        for x in rows:
            artists = x.get("artists") or x.get("ar") or []
            out.append({"id": x.get("id"), "name": x.get("name", ""),
                        "artist": "/".join(a.get("name", "") for a in artists),
                        "url": f"https://music.163.com/#/song?id={x.get('id')}"})
        return out

    # ---------- TTS ----------
    async def tts(self, text, style="", voice=""):
        cfg = self._behavior("voice")
        if not cfg.get("enabled"):
            return {"ok": False, "message": "语音功能没开"}
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:500]
        if not text:
            return {"ok": False, "message": "没有要说的话"}
        api_key = cfg.get("api_key") or self._secret(str(cfg.get("api_key_env") or "XIAOMI_API_KEY"))
        if not api_key:
            return {"ok": False, "message": "没有配置小米 TTS API Key"}
        base = str(cfg.get("base_url") or "https://api.xiaomimimo.com/v1").rstrip("/")
        model = cfg.get("model") or "mimo-v2.5-tts"
        voice = voice or cfg.get("voice") or "茉莉"
        style = style or "年轻女生，亲昵自然，像给男朋友发的日常QQ语音，语速稍慢，软糯但不要做作"
        body = {"model": model,
                "messages": [{"role": "user", "content": style},
                             {"role": "assistant", "content": text}],
                "audio": {"format": "mp3"}, "stream": False}
        if "voicedesign" not in model:
            body["audio"]["voice"] = voice
        r = await self._http.post(f"{base}/chat/completions", json=body,
                                  headers={"authorization": f"Bearer {api_key}",
                                           "content-type": "application/json"}, timeout=90)
        if r.status_code >= 400:
            return {"ok": False, "message": f"小米TTS HTTP {r.status_code}: {r.text[:180]}"}
        try:
            msg = (r.json().get("choices") or [])[0].get("message") or {}
            audio = msg.get("audio") or {}
            raw = base64.b64decode(audio.get("data") or "", validate=True)
        except Exception as e:
            return {"ok": False, "message": f"TTS 响应没有有效音频：{e}"}
        if len(raw) < 1000:
            return {"ok": False, "message": "TTS 返回音频为空"}
        cache = Path(str(cfg.get("cache_dir") or "~/.aura/cache/tts")).expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1((text + str(time.time())).encode()).hexdigest()[:12]
        mp3_path = cache / f"{key}.mp3"
        pcm_path = cache / f"{key}.pcm"
        silk_path = cache / f"{key}.silk"
        mp3_path.write_bytes(raw)

        # macOS NapCat 的命令行 FFmpeg 后端没有实现 MP3→Tencent Silk，必须预转码。
        ffmpeg = shutil.which("ffmpeg") or str(Path.home() / ".hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg")
        if not Path(ffmpeg).is_file():
            return {"ok": False, "message": "找不到 ffmpeg，无法生成 QQ Silk 语音"}
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", str(mp3_path), "-vn", "-ar", "24000", "-ac", "1",
            "-f", "s16le", str(pcm_path), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not pcm_path.is_file():
            return {"ok": False, "message": f"MP3 转 PCM 失败：{err.decode(errors='ignore')[-180:]}"}
        try:
            import pilk
            duration_ms = await asyncio.to_thread(
                pilk.encode, str(pcm_path), str(silk_path), pcm_rate=24000, tencent=True)
        except Exception as e:
            return {"ok": False, "message": f"PCM 转 Silk 失败：{e}"}
        finally:
            pcm_path.unlink(missing_ok=True)
        silk = silk_path.read_bytes() if silk_path.is_file() else b""
        if not silk.startswith(b"\x02#!SILK_V3"):
            return {"ok": False, "message": "生成的语音不是 Tencent Silk v3"}
        if duration_ms > 60000:
            return {"ok": False, "message": "QQ 单条语音不能超过 60 秒"}
        return {"ok": True, "path": str(silk_path), "mp3_path": str(mp3_path),
                "bytes": len(silk), "duration_ms": duration_ms, "text": text,
                "base64": "base64://" + base64.b64encode(silk).decode("ascii")}

    async def generate_selfie(self, scene="日常"):
        """用固定参考脸即时生成自拍；失败由引擎降级到本地图库。"""
        cfg = self._behavior("selfie")
        if not cfg.get("live_generation"):
            return {"ok": False, "message": "即时自拍未开启"}
        from .config import provider as provider_of
        p = provider_of(self.cfg(), cfg.get("provider") or None)
        reference = Path(str(cfg.get("reference") or
                             "~/.aura/selfies/日常/morning-window.png")).expanduser()
        if not reference.is_file():
            return {"ok": False, "message": "缺少自拍固定脸参考图"}
        scene = re.sub(r"[^\w\u4e00-\u9fff，。 、-]", "", str(scene or "日常"))[:80]
        prompt = ("Preserve the exact same adult Chinese woman's identity, face, eyes, nose, mouth, "
                  "skin texture, hairstyle and dark hair color from the reference. Create one photorealistic "
                  f"casual smartphone selfie in this scene: {scene}. She is sending it privately to her boyfriend. "
                  "Natural realistic skin, believable phone camera, tasteful casual clothing, only one person, "
                  "no text, no watermark, no collage, no beauty-filter plastic skin.")
        files = {"image": (reference.name, reference.read_bytes(), "image/png")}
        data = {"model": cfg.get("model") or "codex/gpt-image-2", "prompt": prompt,
                "size": "1024x1024", "n": "1"}
        headers = {"authorization": f"Bearer {p.get('api_key', '')}"}
        r = await self._http.post(p["base_url"].rstrip("/") + "/images/edits",
                                  data=data, files=files, headers=headers, timeout=240)
        if r.status_code >= 400:
            return {"ok": False, "message": f"即时自拍 HTTP {r.status_code}: {r.text[:160]}"}
        rows = r.json().get("data") or []
        if not rows:
            return {"ok": False, "message": "即时自拍接口没有返回图片"}
        raw = b""
        if rows[0].get("b64_json"):
            raw = base64.b64decode(rows[0]["b64_json"])
        elif rows[0].get("url"):
            image = await self._http.get(rows[0]["url"], timeout=90)
            image.raise_for_status()
            raw = image.content
        if len(raw) < 1000:
            return {"ok": False, "message": "即时自拍返回空图片"}
        out_dir = Path(str(cfg.get("live_dir") or "~/.aura/selfies/live")).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"selfie-{int(time.time())}.png"
        path.write_bytes(raw)
        return {"ok": True, "path": str(path), "scene": scene, "bytes": len(raw)}

    # ---------- 本地图片素材 ----------
    def pick_asset(self, kind, mood=""):
        cfg = self._behavior(kind)
        root = Path(str(cfg.get("dir") or f"~/.aura/{kind}")).expanduser()
        if not root.exists():
            return None
        files = [p for p in root.rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}]
        if mood:
            matched = [p for p in files if mood.lower() in p.name.lower() or mood.lower() in p.parent.name.lower()]
            if matched:
                files = matched
        return random.choice(files) if files else None

    async def close(self):
        await self._http.aclose()
