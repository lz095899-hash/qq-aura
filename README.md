# qq-aura

> 把「住在 QQ 里的 AI 伙伴」（原 [`aureth-labs/dsh-aurabot-qq`](https://github.com/aureth-labs/dsh-aurabot-qq)「大肥鱼」）从 **DeepSeek Harness 私有环境**彻底解耦，改造成**独立常驻后台服务 + 通用 MCP Server**。

模型不再与特定平台绑定：小米 MiMo、DeepSeek、OpenAI、本地 Ollama 或自建 OpenAI 兼容网关，改一行配置即可无缝热切换。

---

## 目录
- [核心特性](#核心特性)
- [架构设计与工作流](#架构设计与工作流)
- [能力对照表](#能力对照表)
- [保姆级部署教程](#保姆级部署教程)
  - [第 1 步：安装与配置 QQ 协议端（NapCat）](#第-1-步安装与配置-qq-协议端napcat)
  - [第 2 步：安装 qq-aura 与环境准备](#第-2-步安装-qq-aura-与环境准备)
  - [第 3 步：配置模型与安全秘钥](#第-3-步配置模型与安全秘钥)
  - [第 4 步：健康体检与安全试运行（dry_run）](#第-4-步健康体检与安全试运行dry_run)
  - [第 5 步：正式上线](#第-5-步正式上线)
  - [第 6 步：接入 MCP 客户端（Hermes / Claude / Cursor）](#第-6-步接入-mcp-客户端hermes--claude--cursor)
- [MCP 暴露工具列表](#mcp-暴露工具列表)
- [记忆隔离与数据安全设计](#记忆隔离与数据安全设计)
- [风控与安全建议（必读）](#风控与安全建议必读)
- [常见问题与排障手册](#常见问题与排障手册)
- [开源协议](#开源协议)

---

## 核心特性

- **真人交互节奏**：内置消息防抖（默认 2.5 秒，最长攒 9 秒）。等对方发完一段再一口气回复，并按拟人语速随机延迟分段发送，绝无机器人客服的秒回感或长篇大论。
- **动态情绪演化**：内置情感坐标系（`valence` 愉悦度 / `energy` 活跃度），随对话自然波动并随时间平滑衰减，直接反映在回复语气与标点习惯上。
- **分层记忆系统（SQLite + FTS5）**：
  - **短期缓冲**：最近对话流水滑动窗口。
  - **长期事实记忆**：基于重要度 + 新鲜度 + 检索相关度（BM25 全文检索）动态召回。
  - **自动提炼整合**：每日凌晨自动对前日对话进行归纳去重，提炼为精简事实。
  - **物理级会话隔离**：群聊记忆与私聊记忆严格隔离，主人私密记忆绝不泄漏到任何群聊。
- **QQ 空间深度互动**：
  - 支持发文字/图文说说（可配置公开、好友、仅自己可见）。
  - 支持每日根据聊天经历定时撰写空间日记。
  - 支持好友空间动态巡视、智能点赞与评论。
- **多模型支持与一键热切换**：
  - 原生支持所有 OpenAI 兼容 API：小米 MiMo（`mimo-v2.6-flash`）、DeepSeek（`deepseek-chat`）、OpenAI（`gpt-4o` 等）、本地 Ollama（`qwen2.5` 等）。
  - 运行时可通过 MCP 指令或本地控制接口热切换模型，无需重启后台进程。
- **安全与解耦**：
  - **零硬编码秘钥**：所有 API Key 均支持环境变量映射（`api_key_env`），配置文件不落明文。
  - **服务常驻**：作为独立 Daemon 进程运行，Hermes / IDE 客户端重启或关闭不会导致 QQ 掉线。

---

## 架构设计与工作流

```
                    ┌─────────────────────────┐
                    │     手机/桌面 QQ 用户    │
                    └────────────┬────────────┘
                                 │
                         (腾讯 QQ 协议链路)
                                 │
                    ┌────────────▼────────────┐
                    │ OneBot v11 网关 (NapCat) │
                    │ HTTP:3000   WS:3001     │
                    └────────────┬────────────┘
                                 │
           ┌─────────────────────┼─────────────────────┐
           │ (WebSocket 事件推送) │ (HTTP Action 动作调用) │
           ▼                                           ▲
┌──────────────────────────────────────────────────────┴────────┐
│                        qq-aura 常驻守护进程                     │
│                                                               │
│   ┌──────────────┐      ┌──────────────┐      ┌───────────┐   │
│   │  防抖缓冲队列  ├─────►│  人设/情绪/  ├─────►│ 模型推理   │   │
│   │ (Debounce)   │      │  记忆装载    │      │ (MiMo等)  │   │
│   └──────────────┘      └──────┬───────┘      └─────┬─────┘   │
│                                │                    │         │
│                         ┌──────▼───────┐            │         │
│                         │ SQLite+FTS5  │            │         │
│                         │ (记忆/心情/库)│            │         │
│                         └──────────────┘            │         │
│                                                     ▼         │
│   ┌─────────────────────────────────────────────────────┐     │
│   │ 拟人分段打字延迟发送 / 空间发表 / 空间互动              │     │
│   └─────────────────────────────────────────────────────┘     │
│                                ▲                              │
│                                │ 本地回环控制 (127.0.0.1:8799)   │
└────────────────────────────────┼──────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │    FastMCP 接口适配层    │
                    └────────────┬────────────┘
                                 │ stdio
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ Hermes Agent │         │Claude Desktop│         │  Cursor/IDE  │
└──────────────┘         └──────────────┘         └──────────────┘
```

---

## 能力对照表

| 模块 | 功能 | 状态 | 实现机制 |
|---|---|:---:|---|
| **消息交互** | 私聊/群聊智能收发 | ✅ | `send_private_msg` / `send_group_msg`，支持 array 分段格式 |
| | 真人防抖打字机制 | ✅ | 防抖 2.5s、最长攒 9s，多段随机间隔模拟打字 |
| | 群聊门禁策略 | ✅ | 支持 `mention_only`（仅 @ 或叫名字）、`normal`（按概率）、`chatty`（活跃） |
| | 主动日常搭话 | ✅ | 结合闲置时长、醒着时段、触发概率与每日频次上限主动找主人 |
| **记忆与认知** | 长期事实记忆 | ✅ | SQLite FTS5，融合「相关度 + 重要度 + 新鲜度 + 回忆频次」打分 |
| | 每日记忆提炼整理 | ✅ | 每天凌晨对昨日聊天流水归纳去重入库 |
| | 人物印象与关系沉淀 | ✅ | `lookup_person`，根据对方行为持续沉淀相处经验 |
| | 动态情绪系统 | ✅ | `valence`（愉悦度）与 `energy`（能量）随对话动态起伏并缓慢衰减 |
| **QQ 空间** | 发表说说 | ✅ | 扩展 action `send_qzone_msg`，权限（公开=1/好友=4/私密=64）可控 |
| | 空间日记 | ✅ | 定时按今日经历自动创作日记并发布 |
| | 好友空间巡视点赞 | ✅ | 获取好友动态流水，按预算概率自动点赞、评论互动 |
| **账号形象** | 自主更换头像 | ✅ | 主人私聊可触发；支持 HTTPS 或本地图库，默认 7 天冷却 |
| **多模型支持** | 小米 MiMo / DeepSeek / OpenAI / Ollama | ✅ | 标准 OpenAI 兼容接口封装，支持运行时动态热切换 |
| **客户端集成** | MCP 通用协议接入 | ✅ | FastMCP 提供 15 个控制工具，开箱即用 |

---

## 保姆级部署教程

### 第 1 步：安装与配置 QQ 协议端（NapCat）

本项目依赖 OneBot v11 协议标准。推荐使用最新版 **[NapCatQQ](https://github.com/NapNeko/NapCatQQ)**。

#### 1. 安装 NapCat（以 macOS 为例）
1. 确保系统安装了官方 QQ（路径为 `/Applications/QQ.app`）。
2. 从 [NapCat Release](https://github.com/NapNeko/NapCatQQ/releases) 下载 `NapCatInstaller.app`。
3. 若打开提示损坏或被 Gatekeeper 拦截，在终端执行：
   ```bash
   xattr -dr com.apple.quarantine /path/to/NapCatInstaller.app
   ```
4. 打开安装器，依次执行「安装补丁」与「启动 NapCat」。
5. **权限注意**：当系统提示授权访问 QQ 数据目录时，必须点击「授权访问」挂载数据路径；在「系统设置 → 隐私与安全性 → App 管理」中允许安装器管理 QQ。

#### 2. 扫码登录与网络配置
1. 启动后终端将输出黑白二维码，使用**专门准备的 QQ 小号**扫码登录。
2. 打开 NapCat WebUI（默认地址：`http://127.0.0.1:6099/webui`）。Token 可在终端启动首屏或配置文件中找到。
3. 进入 **「网络配置」**，添加以下两项配置：
   - **HTTP 服务器**：
     - 主机：`127.0.0.1`
     - 端口：`3000`
     - 消息格式：必须选择 **array**（严禁选择 CQ 码字符串，否则复杂富文本解析失败）
     - Token：留空（或自设后同步填入配置文件）
   - **WebSocket 服务器（正向）**：
     - 主机：`127.0.0.1`
     - 端口：`3001`
     - 消息格式：**array**
     - Token：同上

---

### 第 2 步：安装 qq-aura 与环境准备

推荐使用现代 Python 包管理器 `uv`（也可以使用原生 `python3.12 -m venv`）：

```bash
# 1. 克隆代码仓库
git clone https://github.com/lz095899-hash/qq-aura.git
cd qq-aura

# 2. 创建并激活虚拟环境 (推荐 Python 3.12)
uv venv .venv --python 3.12
source .venv/bin/activate

# 3. 安装依赖
uv pip install -r requirements.txt
```

---

### 第 3 步：配置模型与安全秘钥

#### 1. 生成配置文件
```bash
python -m aura init
```
该命令会在 `~/.aura/config.yaml` 生成基础配置文件。

#### 2. 修改配置 `~/.aura/config.yaml`
```yaml
onebot:
  http_url: http://127.0.0.1:3000
  ws_url: ws://127.0.0.1:3001
  http_token: ""
  ws_token: ""
  self_id: 0                     # 填 0 会在启动时自动获取机器人小号 QQ

identity:
  owner_qq: 123456789            # 必须填你自己的主号 QQ（否则主人鉴权和主动搭话失效）
  owner_name: 主人
  bot_name: 小鱼

llm:
  default: mimo                  # 默认模型：mimo / deepseek / openai / local
  temperature: 0.85
  providers:
    mimo:
      base_url: https://api.xiaomimimo.com/v1
      model: mimo-v2.6-flash
      api_key_env: XIAOMIMIMO_V25_API_KEY
    deepseek:
      base_url: https://api.deepseek.com/v1
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
    openai:
      base_url: https://api.openai.com/v1
      model: gpt-4o-mini
      api_key_env: OPENAI_API_KEY

behavior:
  enabled: true                  # 总开关
  dry_run: true                  # 初始设为 true（只在控制台输出回复，不实际发送消息）
  group_mode: mention_only       # 群聊仅在被 @ 或叫名字时回复
```

#### 3. 设置 API Key 环境变量（安全规范）
绝不在配置文件中明文填写 Key，建议将其加入你的终端环境变量中（如 `~/.zshrc` 或 `~/.bashrc`）：
```bash
export XIAOMIMIMO_V25_API_KEY="你的小米MiMo密钥"
# 或者
export DEEPSEEK_API_KEY="你的DeepSeek密钥"
```

---

### 第 4 步：健康体检与安全试运行（dry_run）

#### 1. 运行体检命令
```bash
python -m aura doctor
```
如果输出显示：
- 网关：通（`http://127.0.0.1:3000`）
- 机器人 QQ：已探测并显示小号昵称
- 模型与 API Key：已正确识别

说明环境与链路完全就绪！

#### 2. 启动前台常驻进行安全试调
```bash
python -m aura run
```
用你的主号 QQ 私聊小号发一条消息，例如：`在吗？`。
观察控制台日志，会输出类似：
```
[dry_run] 本来要发给 private:123456789：在呢，怎么啦？ / 刚才去喝了口水。
```
此时模型已正常完成推理、情绪打分与拟人分段，但受 `dry_run` 保护不会真正往 QQ 发出内容。

---

### 第 5 步：正式上线

确认回复风格与人设满意后，关闭 `dry_run`：

- **方式一（命令行一键切换）**：
  ```bash
  curl -X POST http://127.0.0.1:8799/toggle \
       -H "Content-Type: application/json" \
       -d '{"dry_run": false, "persist": true}'
  ```
- **方式二（直接修改配置文件）**：
  将 `~/.aura/config.yaml` 中的 `dry_run: true` 修改为 `dry_run: false`。

此时机器人已经正式进驻 QQ，成为你的专属随身伙伴！

---

### 第 6 步：接入 MCP 客户端（Hermes / Claude / Cursor）

`qq-aura` 内置 FastMCP 适配层，可以非常方便地作为 AI Agent 的外挂能力。

#### 1. 部署 MCP 运行环境
执行仓库根目录的部署脚本，自动同步至本地独立目录并配置虚拟环境：
```bash
./deploy.sh
```

#### 2. Hermes Agent 配置
在 `~/.hermes/config.yaml` 的 `mcp_servers` 节点下增加：
```yaml
mcp_servers:
  qq-aura:
    command: /Users/你的用户名/.hermes/mcp-servers/qq-aura/run_mcp.sh
    args: []
```

#### 3. Claude Desktop 配置
在 `claude_desktop_config.json` 中配置：
```json
{
  "mcpServers": {
    "qq-aura": {
      "command": "/Users/你的用户名/.hermes/mcp-servers/qq-aura/.venv/bin/python",
      "args": ["-m", "aura.mcp_server"],
      "cwd": "/Users/你的用户名/.hermes/mcp-servers/qq-aura"
    }
  }
}
```

配置后，你在对话中只需自然表达：“**看看小鱼今天的状态**”、“**让小鱼发一条空间说说**” 或 “**查查小鱼关于某事的记忆**”，Agent 即可自动调用对应工具！

---

## MCP 暴露工具列表

| 工具名称 | 参数 | 说明 |
|---|---|---|
| `aura_status` | 无 | 获取运行状态、网关连通性、当前模型、实时心情、记忆条数及统计 |
| `aura_start` | 无 | 后台拉起常驻守护进程（如果未启动） |
| `aura_send` | `chat_id`, `text`, `at` | 主动以小号身份发送消息（支持私聊 `private:xxx` 或群聊 `group:xxx`） |
| `aura_qzone_post` | `text`, `visibility` | 发布 QQ 空间说说（支持 `public` / `friends` / `self`） |
| `aura_avatar` | `source`, `force` | 更换 QQ 头像；支持 HTTPS URL 或 `~/.aura/avatars` 内文件名 |
| `aura_diary` | `force` | 汇总今日对话流水并创作发布一篇空间日记 |
| `aura_proactive` | `force` | 触发一次主动搭话检查 |
| `aura_remember` | `text`, `about`, `importance` | 手动写入一条长期事实记忆 |
| `aura_recall` | `query`, `about`, `limit` | 检索相关记忆（支持全文检索与重要度排序） |
| `aura_history` | `chat_id`, `limit` | 查看指定会话的最近聊天流水 |
| `aura_toggle` | `enabled`, `dry_run`, `group_mode` | 在线调整总开关、试运行状态或群聊门禁模式 |
| `aura_model` | `name`, `persist` | 查看或热切换推理模型（支持 mimo / deepseek / openai 等） |
| `aura_persona` | `name`, `bio`, `style` | 查看或动态调整当前人设及打字语言风格 |
| `aura_friends` | 无 | 列出当前小号的所有好友列表 |
| `aura_feeds` | `count` | 抓取好友最新的空间动态 |

---

## 记忆隔离与数据安全设计

1. **数据库本地化**：所有记忆与聊天流水均存储在本地 SQLite 数据库（`~/.aura/aura.db`）中，不经过任何第三方云端中转。
2. **物理会话隔离**：
   - 记忆根据会话维度（`chat_id`）做主键索引与物理隔离。
   - 群聊环境下模型永远无法检索到私聊关于主人的隐私记忆；
   - 跨会话只允许读取机器人自身的通用常识设定。
3. **敏感信息零落盘**：
   - 仓库内只提供 `config.example.yaml` 模版，代码中无任何默认硬编码密钥。

---

## 风控与安全建议（必读）

1. **必须使用 QQ 小号**：由于 OneBot 基于逆向协议，严禁使用重要个人生活主号做机器人，避免因腾讯风控策略受到连带影响。
2. **新号保护期**：新注册或长期未登录的 QQ 号，前 3 天内建议仅保持单聊测试，关闭群聊回复与空间批量点赞。
3. **主动搭话节制**：`proactive.max_per_day` 建议保持在 2~3 次以内，模拟真实人类习惯。
4. **验证码处置**：若在控制台发现空间发帖或登录提示验证码异常，请立刻暂停自动发帖功能，在手机 QQ 手动解除异常后再继续。

---

## 常见问题与排障手册

### Q1: 扫码后提示 `ErrType: 1 ErrCode: 5` 或“已登录无法重复登录”？
- **原因**：系统后台残留了未完全退出的原生 QQ 进程，占用了本地登录状态。
- **解决**：在终端执行 `killall QQ` 彻底杀掉所有 QQ 实例，然后重新启动 NapCat 即可顺畅扫码。

### Q2: NapCat 扫码控制台二维码无法识别怎么办？
- **解决**：NapCat 终端启动时会在第一屏打印 `二维码解码URL: https://txz.qq.com/...`，直接将该链接复制到手机 QQ 或微信中打开即可完成快捷授权。

### Q3: 运行 `python -m aura doctor` 报错 `HTTP 503` 或连不上网关？
- **解决**：请检查 NapCat 是否在后台正常运行，并确认 NapCat WebUI 中配置的 HTTP 服务端口（3000）与 WebSocket 端口（3001）与 `~/.aura/config.yaml` 严格保持一致。

---

## 运行测试套件

本项目自带高覆盖率的端到端自动化测试套件（使用内置假网关与 Mock 服务，**无需真实 QQ 账号即可测试全部功能**）：

```bash
# 运行 32 项端到端综合测试（涵盖收发、防抖、群聊门禁、记忆隔离、空间、头像与控制指令）
python tests/e2e.py
```

---

## 许可证 (License)

本项目采用 [MIT License](LICENSE) 开源协议。
