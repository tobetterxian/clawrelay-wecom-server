# ClawRelay WeCom Server

Claude Code 企业微信中转服务 —— 三步启动，开箱即用。

> A WeCom (Enterprise WeChat) bot relay for Claude Code. Open-source alternative to [Openclaw](https://github.com/nicepkg/openclaw).

![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)
![License MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

将 [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) 接入企业微信的开源中转方案。支持流式回复、多模态消息、多机器人管理，**无需公网 IP**。

```
企业微信用户发消息 → 本服务 WebSocket 接收 → clawrelay-api → Claude Code 处理 → 流式回复推送
```

无需回调 URL，无需数据库。通过 WebSocket 长连接直连企业微信，YAML 配置即用。

---

## 30 秒了解

你需要准备：

1. **企业微信智能机器人**的 `bot_id` 和 `secret`（从企业微信管理后台 → 应用管理 → 智能机器人 获取）
2. **[clawrelay-api](https://github.com/roodkcab/clawrelay-api)** 运行在本机（默认端口 50009）

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

```yaml
bots:
  my_bot:
    # === 必填 ===
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"
    relay_url: "http://localhost:50009"    # Docker 中改为 http://host.docker.internal:50009

    # === 可选 ===
    name: "My Bot"                         # 机器人名称（群聊中过滤 @提及）
    description: "My AI assistant"
    working_dir: "/path/to/project"        # Claude 工作目录
    model: "claude-sonnet-4-6"             # 模型名称
    system_prompt: "You are a helpful assistant."

    allowed_users:                          # 用户白名单（不设 = 不限制）
      - "user_id_1"

    env_vars:                               # 注入 Claude 子进程的环境变量
      MY_API_KEY: "xxx"

    custom_commands:                        # 自定义命令模块
      - "src.handlers.custom.demo_commands"
```

添加多个机器人只需在 `bots:` 下增加新的配置块，重启生效。

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
│   ├── bots.yaml.example               # 机器人配置模板（复制为 bots.yaml 使用）
│   └── bot_config.py                   # 配置加载 & 首次向导
├── src/
│   ├── adapters/
│   │   └── claude_relay_adapter.py     # clawrelay-api SSE 客户端
│   ├── transport/
│   │   ├── ws_client.py                # WebSocket 连接、心跳、重连
│   │   └── message_dispatcher.py       # 消息路由、节流推送
│   ├── core/
│   │   ├── claude_relay_orchestrator.py # AI 调用编排
│   │   ├── session_manager.py          # 会话管理（内存，2h 过期）
│   │   ├── chat_logger.py             # 聊天日志（JSONL）
│   │   └── task_registry.py           # 异步任务注册表
│   ├── handlers/
│   │   ├── command_handlers.py         # 内置命令（help, reset 等）
│   │   └── custom/
│   │       └── demo_commands.py        # 自定义命令示例
│   └── utils/
│       ├── weixin_utils.py             # 消息构建 & 文件解密
│       ├── text_utils.py               # 文本处理
│       └── logging_config.py           # 日志配置
├── logs/                                # 聊天日志（chat.jsonl）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

</details>

<details>
<summary>消息处理流程</summary>

```
用户发送消息
    │
    v
企业微信 WebSocket 推送
    │
    v
消息路由 ─── text ────> 命令检查 ─── 匹配 ──> 执行命令（reset, help, 自定义...）
    │                        │
    │                     不匹配
    │                        v
    │              ClaudeRelayOrchestrator
    │                        │
    │                        ├── 获取/创建会话
    │                        ├── SSE 流式调用 clawrelay-api
    │                        ├── 300ms 节流推送回复
    │                        └── 记录聊天日志
    │
    ├── voice ──> 语音转文字 → 同 text
    ├── image ──> 解密图片 → 多模态分析
    ├── file  ──> 解密文件 → 内容分析
    ├── mixed ──> 图文分离 → 多模态分析
    └── event ──> 欢迎语 / 卡片事件
```

</details>

---

## 相关项目

| 项目 | 说明 |
|------|------|
| [clawrelay-api](https://github.com/roodkcab/clawrelay-api) | Go 编写的 Claude Code 中转 API（本项目的后端依赖） |

---

## License

[MIT](LICENSE)
