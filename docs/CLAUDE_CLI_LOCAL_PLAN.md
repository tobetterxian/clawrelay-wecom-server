# Claude 本地直连方案（替代 `clawrelay-api`）

状态：方案阶段  
日期：2026-03-19

## 背景

当前仓库中的 Claude 机器人实现依赖 `clawrelay-api`：

- 编排器：`src/core/claude_relay_orchestrator.py`
- 适配器：`src/adapters/claude_relay_adapter.py`
- 工厂入口：`src/core/orchestrator_factory.py`

而 `codex_cli` 机器人已经实现了本地直连模式：

- 编排器：`src/core/codex_cli_orchestrator.py`
- 适配器：`src/adapters/codex_app_server_adapter.py`

目标是为 Claude 也提供一种**不依赖 `clawrelay-api`** 的本地运行方式，让企业微信侧体验尽量接近当前 `codex_cli` 机器人。

## 目标

1. 新增 Claude 的本地直连实现，不依赖 `clawrelay-api`
2. 保留现有 `claude_code` 类型，避免破坏线上兼容性
3. 新实现尽量复用现有 `message_dispatcher`、会话管理、工作区与上传区能力
4. 企业微信侧继续支持：
   - 流式回复
   - 会话恢复
   - `reset` / `new` / `stop`
   - 后续接入人工确认与补充输入

## 非目标

1. 第一阶段不要求完全复刻 `codex app-server` 那套协议级体验
2. 第一阶段不强制接入完整的“项目 / 工作区 / 群共享工作区”体系
3. 第一阶段不替换现有 `claude_code` 实现

## 官方能力调研结论

基于 2026-03-19 查阅的 Claude 官方文档：

- Claude CLI 官方支持：
  - `-p` / `--print`
  - `--output-format stream-json`
  - `--continue`
  - `--resume`
  - `--session-id`
  - `--permission-prompt-tool`
- Claude Agent SDK 官方说明：
  - 现已从 “Claude Code SDK” 更名为 “Claude Agent SDK”
  - 提供与 Claude Code 一致的 tools、agent loop、context management
  - 支持 Python 和 TypeScript
- Claude Code GitHub Actions 官方文档说明其构建在 Claude Agent SDK 之上

### 关键判断

基于已查阅的官方公开文档，**没有看到一个像 Codex `app-server --listen stdio://` 那样公开的 Claude 本地 JSON-RPC app-server 协议**。

这意味着：

- 可以实现“像 `codex_cli` 一样的本地直连机器人”
- 但不应假设 Claude 也存在一个可直接复用的公开 `app-server` 协议
- 设计上应优先考虑：
  - **Claude CLI headless / print 模式桥接**
  - **Claude Agent SDK 直连**

上面这条“没有公开 app-server 协议”的结论，是基于官方文档内容做出的工程推断，不排除未来官方新增相关能力。

## 方案选型

### 方案 A：Claude CLI Bridge

思路：

- 服务本地直接启动 `claude` 子进程
- 通过 `claude -p --output-format stream-json` 获取结构化流式输出
- 通过 `--resume` / `--session-id` 恢复会话
- 后续结合 `--permission-prompt-tool` 桥接人工授权

优点：

- 改造成本较低
- 与当前 `codex_cli` 的“本地子进程 + 流式事件”模型最接近
- 能较快摆脱 `clawrelay-api`

缺点：

- 权限确认/补充输入的桥接复杂度高于 Codex app-server
- 事件结构可能不像 Codex 那样稳定、清晰
- 需要自己维护 CLI 事件解析兼容层

### 方案 B：Claude Agent SDK

思路：

- 直接使用 Claude Agent SDK 实现本地编排器
- 用 Python 原生方式管理 agent loop、工具、上下文和流式事件

优点：

- 官方支持的程序化集成能力更强
- 长期维护性更好
- 更适合作为未来的“Claude 本地机器人正式实现”

缺点：

- 初次接入成本高于 CLI bridge
- 和当前 `claude_relay` 路线差异更大
- 需要重写更多 orchestration 逻辑

### 方案 C：继续使用 `clawrelay-api`

优点：

- 已有实现稳定
- 不需要新增复杂本地交互桥接

缺点：

- 多一层外部依赖
- 维护和部署链路更长
- 与 `codex_cli` 的本地直连形态不统一

## 推荐方案

推荐采用：

### 第一阶段：CLI Bridge MVP

新增一个新的 bot type，例如：

- `claude_cli`

先实现最小本地直连版本，目标是尽快摆脱 `clawrelay-api`。

### 第二阶段：按能力演进到 Agent SDK

在不改变企业微信侧接口的前提下，把底层从 CLI bridge 平滑替换为 Agent SDK，或者并行保留两种实现。

### 为什么这样选

这是一个兼顾短期落地和长期维护的方案：

- 短期：CLI bridge 更快落地
- 长期：Agent SDK 更稳、更官方

## 总体架构

建议新增：

- `src/adapters/claude_cli_adapter.py`
- `src/core/claude_cli_orchestrator.py`

并在 `src/core/orchestrator_factory.py` 中注册：

- `bot_type: "claude_cli"`

### 数据流

企业微信消息  
→ `src/transport/message_dispatcher.py`  
→ `ClaudeCliOrchestrator`  
→ `ClaudeCliAdapter`  
→ 本地 `claude` 子进程  
→ `stream-json` 事件  
→ 统一映射为仓库内部事件  
→ 企业微信流式回复

## 第一阶段设计

### 1. Bot 类型

新增：

```yaml
bots:
  claude_local_bot:
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"
    bot_type: "claude_cli"
    name: "Claude CLI"
    description: "Local Claude CLI bot"
    working_dir: "/path/to/project"
    model: "claude-sonnet-4-6"
    system_prompt: "你是一个擅长本地代码分析和实现的 AI 助手。"
    env_vars:
      ANTHROPIC_API_KEY: "YOUR_ANTHROPIC_API_KEY"
    provider_config:
      claude_path: "claude"
      approval_policy: "on-request"
      session_timeout_seconds: 7200
      workspace_root: "/path/to/project/.claude_data"
      enable_project_workspace_mode: false
```

### 2. Adapter 职责

`ClaudeCliAdapter` 负责：

1. 启动本地 `claude` 子进程
2. 注入运行环境变量
3. 拼装 CLI 参数
4. 解析 `stream-json` 输出
5. 将输出转换为统一事件

建议事件模型参考 `codex_cli`，但不必完全相同，例如：

- `ClaudeTextDelta`
- `ClaudeThinkingDelta`
- `ClaudeToolUseStart`
- `ClaudePermissionRequest`
- `ClaudeUserInputRequest`
- `ClaudeTurnComplete`

### 3. Orchestrator 职责

`ClaudeCliOrchestrator` 负责：

1. 处理企业微信文本/文件/图片消息
2. 复用现有日志与流式推送机制
3. 维护会话 ID
4. 处理停止、重置、继续会话
5. 后续承接人工确认与补充输入

第一阶段建议先复用：

- `src/core/chat_logger.py`
- `src/core/session_manager.py`
- `src/transport/message_dispatcher.py`

而不是一开始就强耦合到 `codex_cli` 的完整项目工作区体系。

## 会话设计

### MVP 会话模型

先延续 `SessionManager` 的思路：

- `session_key = chatid`（群聊）或 `user_id`（单聊）
- 持久化保存 `claude_session_id`

建议在 `SessionManager` 中为 Claude 增加独立字段，避免和 relay 的旧 `relay_session_id` 混淆，例如：

- `claude_session_id`

### 恢复策略

优先级：

1. 显式绑定的 `claude_session_id`
2. `--continue`
3. `--resume <session>`

不建议第一版直接依赖“最近会话自动发现”作为唯一机制，最好持久化明确 session id。

## 权限与交互设计

### 第一阶段

第一阶段可以先支持两种策略：

1. **安全默认**
   - 保持权限提示
   - 先将 CLI 返回的阻塞场景转成“请管理员稍后处理”或最小文字确认
2. **可信环境快速模式**
   - 提供配置允许跳过部分权限确认

### 第二阶段

重点打通：

- `--permission-prompt-tool`
- 企业微信文字确认
- 企业微信模板卡片确认

这部分建议复用 `codex_cli` 已有的人机交互经验，但不要直接复制协议结构。

## 工作区与文件访问设计

### 第一阶段

先用单一 `working_dir`，不启用复杂工作区模型。

### 第二阶段

接入现有工作区体系：

- `src/core/project_registry.py`
- `src/core/workspace_manager.py`
- `src/core/session_binding_manager.py`

目标是最终做到：

- 单聊：个人工作区
- 群聊：共享 / 个人工作区切换
- 上传文件进入独立 upload 目录

## 建议的实现顺序

### Phase 0：设计文档

- 当前文档

### Phase 1：本地 CLI MVP

交付项：

- `claude_cli` bot type
- 文本流式回复
- 基本会话恢复
- `reset` / `new` / `stop`
- 本地 `working_dir`

验收标准：

- 不需要 `clawrelay-api`
- 企业微信可以正常完成多轮文本对话
- Claude 能持续访问当前目录并执行本地代码相关任务

### Phase 2：企业微信交互增强

交付项：

- 权限确认桥接
- Ask user / 补充输入桥接
- 审批结果回传继续执行

验收标准：

- 遇到权限确认不再卡死
- 企业微信用户可通过文字或卡片继续 Claude 执行

### Phase 3：工作区体系接入

交付项：

- 项目 / 工作区 / 会话三层模型
- 上传目录隔离
- 群聊共享/个人工作区模式

验收标准：

- 体验与 `codex_cli` 基本一致

### Phase 4：抽象收敛

交付项：

- 抽取 Claude / Codex 共用的本地 Agent 编排公共层
- 减少重复代码

注意：不建议在 MVP 前就做这一步。

## 代码改造建议

### 新增文件

- `src/adapters/claude_cli_adapter.py`
- `src/core/claude_cli_orchestrator.py`

### 修改文件

- `src/core/orchestrator_factory.py`
- `src/transport/message_dispatcher.py`
- `config/bots.yaml.example`
- `README.md`
- 视实现情况调整 `src/core/session_manager.py`

### 保持不变

- `src/core/claude_relay_orchestrator.py`
- `src/adapters/claude_relay_adapter.py`

这样可以让 `claude_code` 与 `claude_cli` 并存，降低切换风险。

## 风险与缓解

### 风险 1：CLI 流式事件格式变化

缓解：

- 使用宽松解析
- 对未知事件类型容错
- 记录原始事件日志以便排障

### 风险 2：权限确认难桥接

缓解：

- Phase 1 先跑通无交互或弱交互流程
- Phase 2 单独实现权限桥接

### 风险 3：本地登录态/凭证污染

缓解：

- 为 Claude 配置独立 `HOME`
- 使用服务进程环境变量注入 key
- 不把凭证暴露给聊天内容

### 风险 4：和 `codex_cli` 一次性做太像导致过度设计

缓解：

- 优先追求本地可用
- 后续逐步对齐，不强求第一版完全统一

## 验收标准

最低可接受版本需要满足：

1. 配置 `bot_type=claude_cli` 后可直接本地运行
2. 不依赖 `clawrelay-api`
3. 支持企业微信文本流式回复
4. 支持基本会话续接
5. 支持 `reset` / `new` / `stop`
6. 至少支持单聊场景下的本地代码分析和修改

增强版再继续验收：

1. 权限确认桥接
2. 用户补充输入桥接
3. 项目 / 工作区模式
4. 文件与图片多模态输入

## 开放问题

1. 第一版是否直接接入现有工作区系统，还是先只用单一 `working_dir`
2. `SessionManager` 是扩展 Claude 专用字段，还是抽象成 provider-agnostic 会话存储
3. 权限确认是否优先走文字回复，还是直接上模板卡片
4. 第二阶段是继续保留 CLI bridge，还是切到 Agent SDK

## 最终建议

如果目标是：

- **尽快摆脱 `clawrelay-api`**：先做 CLI bridge
- **长期稳定可维护**：后续演进到 Agent SDK

因此推荐落地顺序是：

1. `claude_cli` MVP
2. 权限/补充输入桥接
3. 工作区体系对齐
4. 评估是否切换到底层 Agent SDK

## 官方参考

以下信息基于 2026-03-19 查阅的官方文档：

- Claude CLI reference  
  https://code.claude.com/docs/en/cli-reference
- Claude Agent SDK overview  
  https://platform.claude.com/docs/en/agent-sdk/overview
- Claude Code GitHub Actions  
  https://code.claude.com/docs/en/github-actions

关键依据：

- CLI reference 中公开列出了 `--output-format stream-json`、`--resume`、`--session-id`、`--permission-prompt-tool`
- Agent SDK overview 中说明 Agent SDK 提供与 Claude Code 相同的 tools、agent loop、context management，并支持 Python / TypeScript
- GitHub Actions 文档说明 Claude Code GitHub Actions 建立在 Claude Agent SDK 之上
