"""配置加载：YAML + 环境变量覆盖，缺字段用默认值补齐。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(os.environ.get("AURA_CONFIG", "~/.aura/config.yaml")).expanduser()

DEFAULTS = {
    "onebot": {"http_url": "http://127.0.0.1:3000", "ws_url": "ws://127.0.0.1:3001",
               "http_token": "", "ws_token": "", "self_id": 0},
    "identity": {"owner_qq": 0, "owner_name": "主人", "bot_name": "小鱼"},
    "llm": {"default": "mimo", "temperature": 0.85, "max_tool_rounds": 4, "timeout_s": 90, "providers": {}},
    "behavior": {
        "enabled": False, "dry_run": False,
        "debounce_ms": 2500, "max_wait_ms": 9000,
        "group_mode": "mention_only", "group_reply_probability": 0.25,
        "max_reply_segments": 3, "max_segment_chars": 400,
        "reply_delay_ms": [600, 2200], "history_turns": 24,
        "proactive": {"enabled": False, "idle_hours": 5, "awake": ["09:00", "23:30"],
                      "probability": 0.25, "max_per_day": 3, "tick_seconds": 300},
        "qzone": {"enabled": False, "diary_times": ["21:30"], "max_per_day": 1, "audience": "public",
                  "social": {"enabled": False, "like_probability": 0.35,
                             "comment_probability": 0.15, "budget_per_hour": 4}},
        "memory": {"enabled": True, "consolidate_at": "03:30", "consolidate_days": 3,
                   "db": "~/.aura/aura.db"},
    },
    "control": {"host": "127.0.0.1", "port": 8799, "token": ""},
    "persona": {"name": "小鱼", "age": 19, "gender": "女", "bio": "", "style": ""},
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    p = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    raw = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, raw)
    cfg["_path"] = str(p)
    cfg["_exists"] = p.exists()

    # 环境变量覆盖（只覆盖最常改的那几个）
    env_map = {
        "AURA_HTTP_URL": ("onebot", "http_url"),
        "AURA_WS_URL": ("onebot", "ws_url"),
        "AURA_HTTP_TOKEN": ("onebot", "http_token"),
        "AURA_WS_TOKEN": ("onebot", "ws_token"),
        "AURA_OWNER_QQ": ("identity", "owner_qq"),
        "AURA_LLM_PROVIDER": ("llm", "default"),
        "AURA_DB": ("behavior", "memory", "db"),
        "AURA_CONTROL_PORT": ("control", "port"),
    }
    for env, keys in env_map.items():
        val = os.environ.get(env)
        if val in (None, ""):
            continue
        node = cfg
        for k in keys[:-1]:
            node = node[k]
        if keys[-1] in ("owner_qq", "self_id", "port"):
            val = int(val)
        node[keys[-1]] = val
    return cfg


def save_config(cfg, path=None):
    p = Path(path or cfg.get("_path") or DEFAULT_CONFIG_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def provider(cfg, name=None):
    """取一个 provider 配置，并把 api_key_env 解析成真正的 key。"""
    llm = cfg["llm"]
    name = name or llm.get("default")
    provs = llm.get("providers") or {}
    if name not in provs:
        raise ConfigError(f"llm.providers 里没有 {name!r}，现有：{', '.join(provs) or '（空）'}")
    p = dict(provs[name])
    p["name"] = name
    if not p.get("api_key"):
        env = p.get("api_key_env")
        if env:
            p["api_key"] = os.environ.get(env, "")
    p.setdefault("temperature", llm.get("temperature", 0.85))
    p.setdefault("timeout_s", llm.get("timeout_s", 90))
    p.setdefault("base_url", "https://api.openai.com/v1")
    p.setdefault("model", "gpt-4o-mini")
    return p


class ConfigError(Exception):
    pass