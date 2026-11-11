"""命令行入口：python -m aura <init|run|mcp|doctor|whoami|status>"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cmd_init(args):
    from .config import DEFAULT_CONFIG_PATH
    target = Path(os.environ.get("AURA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force:
        print(f"配置已存在：{target}（加 --force 覆盖）")
    else:
        src = ROOT / "config.example.yaml"
        shutil.copyfile(src, target)
        print(f"写好配置：{target}\n接着改里面 identity / onebot / llm 三段，再跑：aura doctor")
    db = Path("~/.aura/aura.db").expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    return 0


def cmd_doctor(args):
    from .config import load_config, provider
    from .store import Store
    cfg = load_config(args.config)
    print(f"配置文件：{cfg['_path']}（{'存在' if cfg['_exists'] else '不存在'}）")
    try:
        p = provider(cfg)
        print(f"模型：{p['name']} / {p['model']} @ {p['base_url']}")
        print(f"API key：{'已配' if p.get('api_key') else '没配（' + str(p.get('api_key_env')) + '）'}")
        print(f"可用 provider：{', '.join(cfg['llm'].get('providers') or {})}")
    except Exception as e:
        print(f"模型配置有问题：{e}")
    store = Store(cfg["behavior"]["memory"]["db"])
    print(f"数据库：{store.path}（记忆 {store.memory_count()} 条，FTS {'开' if store.fts else '关'}）")
    print(f"总开关：{cfg['behavior']['enabled']}　试运行：{cfg['behavior']['dry_run']}　"
          f"群聊档位：{cfg['behavior'].get('group_mode')}")
    print(f"主人 QQ：{cfg['identity'].get('owner_qq') or '没配'}")
    from .onebot import OneBot
    ob = OneBot(lambda: cfg)
    try:
        info = asyncio.run(ob.probe())
        print(f"网关：通（{cfg['onebot']['http_url']}）机器人 QQ {info.get('user_id')} {info.get('nickname', '')}")
    except Exception as e:
        print(f"网关：不通（{e}）\n  检查 NapCat/SnowLuma 是否在跑，以及 HTTP/WS 端口和 token")
    return 0


def cmd_whoami(args):
    from .config import load_config
    from .onebot import OneBot
    cfg = load_config(args.config)
    ob = OneBot(lambda: cfg)
    info = asyncio.run(ob.probe())
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    import httpx
    from .config import load_config
    cfg = load_config(args.config)
    c = cfg["control"]
    url = f"http://{c.get('host', '127.0.0.1')}:{int(c.get('port', 8799))}/status"
    try:
        r = httpx.get(url, timeout=5, trust_env=False,
                      headers={"x-aura-token": c.get("token", "")} if c.get("token") else None)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"常驻进程没在跑（{e}）。启动：python -m aura run")
        return 1
    return 0


def cmd_run(args):
    from .daemon import main as run_main
    import os
    if args.config:
        os.environ["AURA_CONFIG"] = str(Path(args.config).expanduser())
    run_main()
    return 0


def cmd_mcp(args):
    from .mcp_server import mcp
    logging.basicConfig(level=logging.WARNING)
    mcp.run()
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser("aura")
    ap.add_argument("--config", "-c", default=None, help="配置文件路径")
    sub = ap.add_subparsers(dest="cmd")
    p_init = sub.add_parser("init")
    p_init.add_argument("--force", action="store_true")
    for name in ("run", "mcp", "doctor", "whoami", "status"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    cmd = args.cmd or "status"
    fn = globals().get(f"cmd_{cmd}")
    if not fn:
        ap.print_help()
        return 2
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())