# ClawRelay WeCom Server

企业微信 AI 机器人中转服务 —— 支持 Claude Code、本地 Codex CLI、Codex API、Gemini、OpenAI 等多种 AI 模型。

> A WeCom (Enterprise WeChat) bot relay for multiple AI models. Open-source alternative to [Openclaw](https://github.com/nicepkg/openclaw).

![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)
![License MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

将 AI 模型接入企业微信的开源中转方案。支持流式回复、多模态消息、多机器人管理，**无需公网 IP**。

**支持的 AI 模型：**
- **Claude Code** - 通过 [clawrelay-api](https://github.com/roodkcab/clawrelay-api) 连接，支持代码操作和工具调用
- **OpenAI Codex API** - 通过 OpenAI Responses API 调用 Codex 模型，适合代码分析与实现
- **本地 Codex CLI** - 直接调用本机 `codex app-server` 原生交互协议，更适合本地代码修改、命令执行与排障
- **Google Gemini** - 直接调用 Gemini API，支持 Google Search 联网功能
- **OpenAI GPT** - 直接调用 OpenAI API，支持自定义 base_url（兼容第三方 API）
- **自动模型选择** - 留空 model 字段，自动选择最佳可用模型

```
企业微信用户发消息 → 本服务 WebSocket 接收 → AI 模型处理 → 流式回复推送
```

无需回调 URL，无需数据库。通过 WebSocket 长连接直连企业微信，YAML 配置即用。

---

## 30 秒了解

你需要准备：

1. **企业微信智能机器人**的 `bot_id` 和 `secret`（从企业微信管理后台 → 应用管理 → 智能机器人 获取）
2. 根据需要选择：
   - **Claude Code**: 需要 [clawrelay-api](https://github.com/roodkcab/clawrelay-api) 运行在本机（默认端口 50009）
   - **Gemini**: 需要 Gemini API Key
   - **OpenAI / Codex API**: 需要 OpenAI API Key 或第三方兼容 API
   - **本地 Codex CLI**: 需要本机已安装并登录 `codex`，且服务进程能访问目标工作目录

然后：

```bash
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom-server
pip install -r requirements.txt
python main.py
```

首次启动会自动进入**配置向导**，按提示填入 `bot_id` 和 `secret` 即可：

```
============================================================
  ClawRelay WeCom Server — 首次配置向导
============================================================

  bot_id（企业微信机器人 ID）: __________
  secret（企业微信机器人密钥）: __________
  relay_url（clawrelay-api 地址）[http://localhost:50009]: __________

  配置已保存到 config/bots.yaml
```

配置完成，服务自动启动 WebSocket 连接。去企业微信给机器人发条消息试试吧。

---

## Windows 后台运行

在 Windows 下可以将服务注册为系统服务，支持开机自启和崩溃重启。

### 使用 NSSM（推荐）

1. **安装 NSSM**：
   ```powershell
   choco install nssm
   # 或从 https://nssm.cc/download 下载
   ```

2. **运行安装脚本**（以管理员身份）：
   ```powershell
   cd C:\next\clawrelay-wecom-server
   .\install-service.bat
   ```

3. **管理服务**：
   ```powershell
   # 使用管理脚本（推荐）
   .\manage-service.bat

   # 或使用命令行
   nssm status clawrelay-wecom
   nssm restart clawrelay-wecom
   nssm stop clawrelay-wecom
   ```

详细说明请查看 [Windows 服务配置指南](docs/WINDOWS_SERVICE.md)

如果你想先排障或临时运行，不走服务方式，也可以参考 [Windows 手动运行指南](docs/WINDOWS_MANUAL_RUN.md)

---

## Docker 部署

```bash
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom-server

# 编辑配置（Docker 中不支持交互式向导，需提前填写）
cp config/bots.yaml.example config/bots.yaml
vim config/bots.yaml

docker compose up -d
```

> Docker 模式下 `relay_url` 需使用 `http://host.docker.internal:50009`（而非 `localhost`）连接宿主机的 clawrelay-api。

```bash
docker compose logs -f app   # 查看日志
docker compose down           # 停止
```

---

## 功能一览

| 特性 | 说明 |
|------|------|
| **多 AI 模型** | 支持 Claude Code、本地 Codex CLI、Codex API、Gemini、OpenAI，可同时运行多个不同模型的机器人 |
| **自动模型选择** | Gemini 和 OpenAI 支持自动选择最佳可用模型 |
| **Google Search** | Gemini 支持联网搜索功能 |
| **第三方 API** | OpenAI 兼容格式，支持任何第三方 API 服务 |
| **WebSocket 长连接** | 无需公网 IP、回调 URL，WSS 直连企业微信 |
| **零外部依赖** | 无数据库，YAML 配置 + 内存会话 + JSONL 日志 |
| **首次配置向导** | 启动即引导，无需手动编辑配置文件 |
| **多机器人** | 一个服务托管多个机器人，YAML 中加一段配置即可 |
| **流式回复** | 300ms 节流推送，实时展示 AI 回复和思考过程 |
| **多模态** | 文本 / 图片 / 语音 / 文件 / 图文混排 |
| **会话管理** | 2h 自动过期，发送 `reset` 或 `new` 手动重置 |
| **自定义命令** | 模块化扩展，动态加载 |
| **用户白名单** | 按机器人维度的访问控制 |

---

## 配置说明

配置文件：`config/bots.yaml`

### Claude Code 机器人

```yaml
bots:
  claude_bot:
    # === 必填 ===
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"
    bot_type: "claude_code"                # 机器人类型
    relay_url: "http://localhost:50009"    # clawrelay-api 地址

    # === 可选 ===
    name: "Claude Assistant"
    description: "Claude Code AI assistant"
    working_dir: "/path/to/project"        # Claude 工作目录
    model: "claude-sonnet-4-6"
    system_prompt: "You are a helpful assistant."

    allowed_users:                          # 用户白名单（不设 = 不限制）
      - "user_id_1"

    env_vars:                               # 注入 Claude 子进程的环境变量
      MY_API_KEY: "xxx"
```

### Gemini 机器人

```yaml
bots:
  gemini_bot:
    # === 必填 ===
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"
    bot_type: "gemini"                     # 机器人类型

    # === 可选 ===
    name: "Gemini Assistant"
    description: "Gemini chat bot"
    model: ""                               # 留空自动选择，或指定如 gemini-2.5-flash
    system_prompt: "你是一个友好的AI助手。"

    # === Provider 配置 ===
    provider_config:
      api_key: "YOUR_GEMINI_API_KEY"       # Gemini API Key
      enable_search: false                  # 启用 Google Search 联网功能
```

**自动模型选择：**
- 将 `model` 留空，系统会自动查询可用模型并选择最佳的（优先级：gemini-3.1-pro > gemini-2.5-pro > gemini-2.5-flash）
- 免费 API Key 建议使用 `gemini-2.5-flash`（配额更高）

**Google Search：**
- 设置 `enable_search: true` 启用联网搜索
- 需要使用 v1beta API（自动切换）

### OpenAI 机器人

```yaml
bots:
  openai_bot:
    # === 必填 ===
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"
    bot_type: "openai"                     # 机器人类型

    # === 可选 ===
    name: "GPT Assistant"
    description: "OpenAI GPT chat bot"
    model: ""                               # 留空自动选择，或指定如 gpt-4o
    system_prompt: "You are a helpful AI assistant."

    # === Provider 配置 ===
    provider_config:
      api_key: "YOUR_OPENAI_API_KEY"       # OpenAI API Key
      base_url: "https://api.openai.com/v1"  # 可选，自定义 API 端点
```

**第三方 API 支持：**

OpenAI 类型支持任何兼容 OpenAI 格式的 API 服务：

```yaml
bots:
  claude_api_bot:
    bot_type: "openai"                     # 使用 openai 类型
    model: ""                               # 留空自动选择最佳模型
    provider_config:
      api_key: "YOUR_API_KEY"
      base_url: "https://your-api-service.com/v1"
```

**查询可用模型：**

```bash
# 查询指定 API 支持的模型
python list_openai_models.py --base-url https://api.openai.com/v1 --api-key YOUR_KEY

# 或从配置文件读取
python list_openai_models.py
```

### Codex API 机器人

```yaml
bots:
  codex_bot:
    # === 必填 ===
    bot_id: "YOUR_CODEX_BOT_ID"
    secret: "YOUR_CODEX_BOT_SECRET"
    bot_type: "codex"                      # 机器人类型

    # === 可选 ===
    name: "Codex Assistant"
    description: "OpenAI Codex coding bot"
    model: ""                               # 留空默认使用 gpt-5.3-codex
    system_prompt: "你是一个擅长代码分析和实现的 AI 助手。"

    # === Provider 配置 ===
    provider_config:
      api_key: "YOUR_OPENAI_API_KEY"       # OpenAI API Key
      base_url: "https://api.openai.com/v1"  # 可选，自定义 API 端点
      reasoning_effort: "medium"            # 可选: low | medium | high
```

**说明：**
- `codex` 类型使用 OpenAI Responses API，而不是 `clawrelay-api`
- `model` 留空时默认使用 `gpt-5.3-codex`
- 支持文本、图片、文件输入，以及 `reset` / `new` 会话重置命令
- 如需普通对话模型，仍建议使用 `openai` 类型


### Codex CLI 机器人

```yaml
bots:
  codex_cli_bot:
    # === 必填 ===
    bot_id: "YOUR_CODEX_CLI_BOT_ID"
    secret: "YOUR_CODEX_CLI_BOT_SECRET"
    bot_type: "codex_cli"                  # 机器人类型
    working_dir: "/path/to/project"        # 必填，默认项目源目录 / 兼容旧配置入口

    # === 可选 ===
    name: "Codex CLI"
    description: "Local Codex CLI coding bot"
    model: ""                               # 留空默认使用 gpt-5.3-codex
    system_prompt: "你是一个擅长本地代码修改与排障的 AI 助手。"

    env_vars:                               # 可选，传给 codex 子进程
      OPENAI_API_KEY: "YOUR_OPENAI_API_KEY"

    provider_config:
      codex_path: "/home/youruser/.nvm/versions/node/v20.20.1/bin/codex"
      sandbox_mode: "workspace-write"      # 可选: read-only | workspace-write
      skip_git_repo_check: false
      approval_policy: "on-request"     # 可选: untrusted | on-request | on-failure | never
      dangerously_bypass_approvals_and_sandbox: false  # 仅可信环境建议开启
      workspace_root: "/path/to/project/.codex_data"  # 可选，默认 <working_dir>/.codex_data
      default_workspace_init_mode: "empty" # 可选: empty | git_remote | legacy_copy
      workspace_strategy: "copy"           # 兼容旧配置，等价于 legacy_copy
      default_group_workspace_mode: "personal"  # 可选: personal | shared
      session_timeout_seconds: 7200
      enable_project_workspace_mode: true
      # add_dirs:
      #   - "/another/writable/path"
      # profile: "default"
```

**说明：**
- `codex_cli` 类型直接启动本机 `codex app-server --listen stdio://`，**不经过** `clawrelay-api`
- 如果服务进程拿不到 `PATH` 中的 `codex`，可显式配置 `provider_config.codex_path`
- 会按企业微信运行时会话保存 `thread_id`，后续消息自动走 `thread/resume + turn/start`
- 默认启用 `项目 / 工作区 / 会话` 三层模型：单聊自动进入个人项目，群聊先绑定项目，再按个人/共享工作区路由
- 默认工作区初始化优先使用 `empty` / `git_remote`，不再默认复制本地大目录；`workspace_strategy: copy` 仅作兼容，实际等价 `legacy_copy`
- 图片和文件统一落到 `workspace_root/uploads/<bot_key>/<runtime_session_key>/...`，不再写进共享项目根目录
- 支持文字命令：`项目列表`、`新建项目 <名称>`、`新建仓库项目 <名称> <Git地址>`、`新建复制项目 <名称> [本地目录]`、`进入项目 <名称或ID>`、`当前项目`、`当前工作区`、`工作区列表`、`使用个人工作区`、`使用共享工作区`
- 若当前环境的 Codex 沙箱不可用（例如 `bwrap` / user namespace 异常），可在**可信环境**里启用 `dangerously_bypass_approvals_and_sandbox: true`
- 审批、文件改动确认、补充提问会通过企业微信文字回复回传，体验更接近原生交互式 Codex
- 这个类型更适合本地读写代码、执行命令、排障，以及多人协作场景下的目录隔离

### 多机器人配置示例

可以在同一个服务中运行多个不同类型的机器人：

```yaml
bots:
  # Claude Code 机器人 - 用于代码操作
  code_assistant:
    bot_id: "BOT_ID_1"
    secret: "SECRET_1"
    bot_type: "claude_code"
    relay_url: "http://localhost:50009"
    working_dir: "/workspace"

  # Gemini 机器人 - 用于日常对话（自动选择模型）
  chat_assistant:
    bot_id: "BOT_ID_2"
    secret: "SECRET_2"
    bot_type: "gemini"
    model: ""                               # 自动选择
    provider_config:
      api_key: "GEMINI_KEY"
      enable_search: true                   # 启用联网

  # 第三方 API 机器人 - 使用中转服务
  api_assistant:
    bot_id: "BOT_ID_3"
    secret: "SECRET_3"
    bot_type: "openai"
    model: ""                               # 自动选择
    provider_config:
      api_key: "YOUR_KEY"
      base_url: "https://api.example.com/v1"

  # Codex API 机器人 - 用于代码分析
  codex_assistant:
    bot_id: "BOT_ID_4"
    secret: "SECRET_4"
    bot_type: "codex"
    model: ""
    provider_config:
      api_key: "OPENAI_KEY"
      base_url: "https://api.openai.com/v1"
      reasoning_effort: "medium"

  # 本地 Codex CLI 机器人 - 用于直接改代码和执行命令
  codex_cli_assistant:
    bot_id: "BOT_ID_5"
    secret: "SECRET_5"
    bot_type: "codex_cli"
    working_dir: "/workspace"
    env_vars:
      OPENAI_API_KEY: "OPENAI_KEY"
    provider_config:
      sandbox_mode: "workspace-write"
      approval_policy: "on-request"
      dangerously_bypass_approvals_and_sandbox: false

    custom_commands:                        # 自定义命令模块
      - "src.handlers.custom.demo_commands"
```

添加多个机器人只需在 `bots:` 下增加新的配置块，重启生效。

---

## 工具脚本

### 列出可用模型

查询 Gemini 或 OpenAI 兼容 API 支持的模型：

```bash
# Gemini 模型
python list_gemini_models.py

# OpenAI 兼容 API 模型（自动分类显示）
python list_openai_models.py --base-url https://api.openai.com/v1 --api-key YOUR_KEY
```

### 测试 API 连接

```bash
# 测试 Gemini API
python test_gemini_api.py

# 测试 Gemini Google Search
python test_gemini_grounding.py
```

---

## 自定义命令

在 `src/handlers/custom/` 下创建 Python 文件：

```python
from src.handlers.command_handlers import CommandHandler

class PingCommandHandler(CommandHandler):
    command = "ping"
    description = "Check if the bot is alive"

    def handle(self, cmd, stream_id, user_id):
        return "Pong!", None

def register_commands(command_router):
    command_router.register(PingCommandHandler())
```

在 `config/bots.yaml` 中添加模块路径后重启即可。参考示例：[`src/handlers/custom/demo_commands.py`](src/handlers/custom/demo_commands.py)

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_CONFIG_PATH` | 配置文件路径 | `config/bots.yaml` |
| `CHAT_LOG_DIR` | 聊天日志目录 | `logs` |
| `WEIXIN_AGENT_TIMEOUT_SECONDS` | 任务超时（秒） | `30` |
| `WEIXIN_MAX_FILE_SIZE` | 文件大小限制（字节） | `20971520` (20MB) |

---

## 架构

```
┌──────────┐    WSS     ┌─────────────────────────┐   SSE    ┌───────────────┐
│ 企业微信  │ <───────> │  ClawRelay WeCom Server  │ ──────> │ clawrelay-api │
│          │  长连接     │  (Python asyncio)        │ <────── │ (Go :50009)   │
└──────────┘             └─────────────────────────┘  流式响应 └───────┬───────┘
                                                                       │
                                                                       v
                                                              ┌───────────────┐
                                                              │  Claude Code  │
                                                              └───────────────┘
```

- **WebSocket 长连接**：通过 `wss://openws.work.weixin.qq.com` 连接企业微信，30s 心跳保活，断线自动重连
- **会话管理**：每个用户-机器人对独立会话，内存存储，2h 过期
- **多机器人隔离**：每个机器人独立的 WebSocket 连接、命令路由器和会话管理

<details>
<summary>项目结构</summary>

```
clawrelay-wecom-server/
├── main.py                              # 入口（asyncio，per-bot WebSocket）
├── config/
│   ├── bots.yaml.example               # 机器人配置模板
│   └── bot_config.py                   # 配置加载 & 首次向导
├── src/
│   ├── adapters/
│   │   └── claude_relay_adapter.py     # clawrelay-api SSE 客户端
│   ├── transport/
│   │   ├── ws_client.py                # WebSocket 连接、心跳、重连
│   │   └── message_dispatcher.py       # 消息路由、节流推送
│   ├── core/
│   │   ├── base_orchestrator.py        # 编排器基类
│   │   ├── claude_relay_orchestrator.py # Claude Code 编排器
│   │   ├── gemini_orchestrator.py      # Gemini 编排器
│   │   ├── openai_orchestrator.py      # OpenAI 编排器
│   │   ├── orchestrator_factory.py     # 编排器工厂
│   │   ├── gemini_model_selector.py    # Gemini 模型选择器
│   │   ├── openai_model_selector.py    # OpenAI 模型选择器
│   │   ├── session_manager.py          # 会话管理
│   │   ├── chat_logger.py             # 聊天日志
│   │   └── task_registry.py           # 异步任务注册表
│   ├── handlers/
│   │   ├── command_handlers.py         # 内置命令
│   │   └── custom/
│   │       └── demo_commands.py        # 自定义命令示例
│   └── utils/
│       ├── weixin_utils.py             # 消息构建 & 文件解密
│       ├── text_utils.py               # 文本处理
│       └── logging_config.py           # 日志配置
├── docs/
│   └── WINDOWS_SERVICE.md              # Windows 服务配置指南
├── logs/                                # 聊天日志
├── install-service.bat                  # Windows 服务安装脚本
├── manage-service.bat                   # Windows 服务管理脚本
├── uninstall-service.bat                # Windows 服务卸载脚本
├── list_gemini_models.py                # Gemini 模型列表工具
├── list_openai_models.py                # OpenAI 模型列表工具
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

</details>

---

## 常见问题

### Gemini API 配额超限

**错误：** `HTTP 429 - You exceeded your current quota`

**原因：** 自动选择了 `gemini-2.5-pro`，免费配额较低

**解决：** 在配置中指定 `model: "gemini-2.5-flash"`（免费配额更高）

### 服务显示 running 但不响应

检查错误日志：
```bash
# Windows
Get-Content C:\next\clawrelay-wecom-server\logs\service-error.log -Tail 50

# Linux/macOS
tail -f logs/service-error.log
```

### 第三方 API 模型不可用

使用工具查询可用模型：
```bash
python list_openai_models.py --base-url YOUR_BASE_URL --api-key YOUR_KEY
```

---

## 相关项目

| 项目 | 说明 |
|------|------|
| [clawrelay-api](https://github.com/roodkcab/clawrelay-api) | Go 编写的 Claude Code 中转 API（本项目的后端依赖） |

---

## License

[MIT](LICENSE)
