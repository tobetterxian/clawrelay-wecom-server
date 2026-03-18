# Codex 项目 / 工作区 / 会话三层改造设计

## 1. 背景与目标

当前 `codex_cli` 机器人把所有企业微信用户和群聊都落到同一个 `working_dir` 上执行。这个实现已经能跑通本地 Codex CLI、审批、图片/文件附件和流式回复，但在下面两类真实场景中会遇到明显问题：

- 单个用户希望同时开发多个独立项目
- 多个用户希望共同参与同一个项目，但又不想互相覆盖工作目录

因此，下一阶段需要把当前“固定 bot 工作目录”的实现，升级为“项目 / 工作区 / 会话”三层模型。

### 1.1 设计目标

- 支持**个人独立项目**与**多人共享项目**两种模式
- 支持**单聊**和**群聊**两种企业微信入口
- 默认避免多人直接共享同一执行目录，降低覆盖风险
- 尽量复用现有 `Codex thread`、审批、流式推送、上传附件等能力
- 第一阶段优先解决目录隔离与会话绑定，不强依赖 Git
- 为第二阶段的 `git worktree`、分支管理、合并审批预留扩展点

### 1.2 非目标（第一阶段不做）

- 不做完整权限中心或复杂成员角色体系
- 不做自动 merge / rebase / PR 流程
- 不做数据库依赖，优先使用 JSON 持久化
- 不做多机分布式状态同步

---

## 2. 现状分析

### 2.1 当前架构

当前 `codex_cli` 关键链路如下：

1. `MessageDispatcher` 接收企业微信消息
2. 按 `bot_key` 拿到固定的 `CodexCliOrchestrator`
3. `CodexCliOrchestrator` 使用固定 `working_dir`
4. 启动 `codex app-server --listen stdio://`
5. 通过 `thread/resume + turn/start` 继续同一 Codex thread

### 2.2 当前会话边界

- 单聊：`session_key = user_id`
- 群聊：`session_key = chatid`
- `SessionManager` 只存 `thread_id/relay_session_id`
- 进程重启后内存会话丢失
- 2 小时无活动时自动过期

### 2.3 当前问题

#### 问题 A：所有人共享同一个工作目录

例如当前 `cx_bot` 配置为：

- `working_dir: C:/next`

则所有用户、所有群聊都可能在同一个目录树下执行：

- 查看目录
- 创建文件
- 修改代码
- 执行命令

这会带来：

- 用户之间互相覆盖文件
- 群聊与单聊共享临时状态
- 不同项目的上下文污染
- 审批链路虽然按会话隔离，但文件系统没有隔离

#### 问题 B：缺少“项目”概念

当前系统只有 bot、会话，没有“项目”抽象，所以无法表达：

- 用户 ChenYue 有 3 个独立项目
- 群 A 在做项目 CRM
- 群 B 在做项目 ERP
- ChenYue 和 LiSi 都参与 CRM，但希望各自独立工作区

#### 问题 C：会话绑定不持久

当前 thread id 管理是纯内存的，无法在进程重启后恢复“当前会话正在操作哪个项目 / 哪个工作区”。

---

## 3. 核心模型

引入三个核心对象：

- **项目 `project`**：代码资产本身
- **工作区 `workspace`**：Codex 实际运行与修改代码的目录
- **会话 `session`**：企业微信当前对话上下文

### 3.1 关系定义

- 一个 `project` 可以拥有多个 `workspace`
- 一个 `workspace` 归属一个 `project`
- 一个企业微信 `session` 在某一时刻绑定一个 `workspace`
- 一个 `session` 还会绑定一个 `codex_thread_id`

### 3.2 设计原则

- **目录隔离单位 = workspace**
- **业务管理单位 = project**
- **上下文复用单位 = session**

这意味着：

- 多项目问题：通过 `project` 解决
- 多人协作问题：通过同一 `project` 下多个 `workspace` 解决
- 对话连续性问题：通过 `session -> workspace -> thread` 绑定解决

---

## 4. 推荐的业务模式

### 4.1 个人项目

适用场景：

- 用户自己开发独立项目
- 不打算与别人共享目录

推荐策略：

- 创建项目时自动创建一个个人 `workspace`
- 单聊默认绑定到这个个人 `workspace`

### 4.2 共享项目 + 个人工作区（默认推荐）

适用场景：

- 多个人共同参与一个项目
- 但每个人希望独立修改、独立审批、独立试验

推荐策略：

- 多人进入同一个 `project`
- 每个人在该 `project` 下拥有自己的个人 `workspace`
- Codex 默认在个人 `workspace` 中执行

优点：

- 最安全
- 最接近真实研发协作习惯
- 审批边界清晰
- 出问题容易回退

### 4.3 共享项目 + 共享工作区（可选增强）

适用场景：

- 群聊中多人现场结对编程
- 大家明确希望改同一个目录

推荐策略：

- 在群聊里显式切换到共享 `workspace`
- 非默认，必须显式启用

风险：

- 多人并发冲突高
- 误改风险大
- 审批责任边界模糊

结论：

- **默认使用共享项目 + 个人工作区**
- **共享工作区只作为群聊下的手动选项**

---

## 5. 目录结构设计

第一阶段建议引入统一状态与工作区根目录：

```text
C:\next\.codex_data\
  state\
    projects.json
    workspaces.json
    sessions.json
  projects\
    <project_id>\
      meta.json
      repo\
      workspaces\
        <workspace_id>\
  uploads\
    <bot_key>\
      <session_key>\
```

### 5.1 各目录含义

- `state/projects.json`
  - 项目元数据索引
- `state/workspaces.json`
  - 工作区元数据索引
- `state/sessions.json`
  - 当前会话绑定和 thread 信息
- `projects/<project_id>/repo`
  - 项目基线目录
  - 第一阶段可来自模板复制、本地目录初始化、或预留给未来 Git 仓库
- `projects/<project_id>/workspaces/<workspace_id>`
  - 实际给 Codex 执行的工作目录
- `uploads/...`
  - 企业微信附件落盘目录，不再放到共享项目根目录

### 5.2 第一阶段的 repo / workspace 策略

不强制上 `git worktree`，优先支持：

- `source_type = empty`：创建空项目目录
- `source_type = local_path`：从本地目录复制一份到 `repo`
- `workspace_strategy = copy`：从 `repo` 初始化一个 workspace

第二阶段再升级到：

- `workspace_strategy = git_worktree`

---

## 6. 数据模型设计

第一阶段可先用 JSON 文件持久化，后续再迁移 SQLite。

### 6.1 `projects.json`

```json
[
  {
    "project_id": "proj_chenyue_blog",
    "name": "blog",
    "kind": "personal",
    "owner_user_id": "ChenYue",
    "owner_chat_id": "",
    "source_type": "empty",
    "source_path": "",
    "repo_path": "C:/next/.codex_data/projects/proj_chenyue_blog/repo",
    "created_at": "2026-03-18T18:00:00Z",
    "updated_at": "2026-03-18T18:00:00Z"
  }
]
```

### 6.2 `workspaces.json`

```json
[
  {
    "workspace_id": "ws_proj_team_crm_ChenYue",
    "project_id": "proj_team_crm",
    "workspace_type": "personal",
    "owner_user_id": "ChenYue",
    "owner_chat_id": "",
    "path": "C:/next/.codex_data/projects/proj_team_crm/workspaces/ws_proj_team_crm_ChenYue",
    "branch_name": "chenyue",
    "created_at": "2026-03-18T18:00:00Z",
    "updated_at": "2026-03-18T18:00:00Z"
  }
]
```

### 6.3 `sessions.json`

```json
[
  {
    "bot_key": "cx_bot",
    "session_key": "ChenYue",
    "project_id": "proj_chenyue_blog",
    "workspace_id": "ws_proj_chenyue_blog_ChenYue",
    "codex_thread_id": "019d...",
    "mode": "personal_workspace",
    "last_active": "2026-03-18T18:00:00Z"
  }
]
```

---

## 7. 默认绑定规则

### 7.1 单聊默认规则

- 会话键：`session_key = user_id`
- 如果该用户已有“当前项目”，则绑定其个人 `workspace`
- 如果没有当前项目，则自动创建一个默认个人项目，并自动创建个人 `workspace`

### 7.2 群聊默认规则

- 会话键：`session_key = chatid`
- 群可以绑定一个“当前项目”
- 在该项目下，默认使用**发言人自己的个人 workspace**
- 若群未绑定项目，则提示先 `新建项目` 或 `进入项目`

### 7.3 群聊共享工作区规则

- 仅在显式切换后启用
- 例如执行：`使用共享工作区`
- 此后该群会话直接绑定共享 `workspace`

### 7.4 reset / clear 规则

发送：

- `reset`
- `new`
- `clear`
- `重置`
- `清空`

行为：

- 仅清空当前 `session -> codex_thread_id` 绑定
- 保留 `project` 和 `workspace`
- 不自动删除工作区文件

---

## 8. 命令设计

第一阶段先使用纯文本命令，不依赖卡片。

### 8.1 基础项目命令

- `项目列表`
- `新建项目 <名称>`
- `进入项目 <名称或ID>`
- `当前项目`
- `删除项目 <名称或ID>`（可后置）

### 8.2 工作区命令

- `当前工作区`
- `我的工作区`
- `使用个人工作区`
- `使用共享工作区`
- `工作区列表`

### 8.3 会话命令

- `重置会话`
- `reset`
- `new`
- `clear`

### 8.4 推荐的用户体验

#### 单聊第一次使用

用户发：

- `帮我做一个博客项目`

系统行为：

1. 自动创建默认个人项目
2. 自动创建个人 workspace
3. 将当前会话绑定到该 workspace
4. 告知当前项目和工作区路径

#### 群聊第一次使用

群里发：

- `我们做一个 CRM 项目`

系统回复：

- 建议先执行 `新建项目 crm`
- 再执行 `进入项目 crm`

之后群成员发消息时：

- 默认落在“该成员在 crm 下的个人 workspace”

---

## 9. 代码改造点

以下为与当前代码结构对齐后的详细改造方案。

### 9.1 新增模块

#### `src/core/project_registry.py`

职责：

- 管理 `projects.json`
- 提供项目 CRUD
- 支持按 user / chat 查询可见项目

建议接口：

- `list_projects(user_id, chat_id=None)`
- `create_project(name, kind, owner_user_id, owner_chat_id=None, source_type='empty', source_path='')`
- `get_project(project_id)`
- `resolve_project(name_or_id, user_id, chat_id=None)`

#### `src/core/workspace_manager.py`

职责：

- 管理 `workspaces.json`
- 负责创建和定位 workspace
- 封装目录初始化逻辑

建议接口：

- `get_or_create_personal_workspace(project_id, user_id)`
- `get_or_create_shared_workspace(project_id, chat_id)`
- `list_workspaces(project_id)`
- `resolve_workspace(...)`

#### `src/core/session_binding_manager.py`

职责：

- 管理 `sessions.json`
- 保存会话绑定：`session_key -> project_id/workspace_id/thread_id`
- 持久化当前会话模式（个人工作区 / 共享工作区）

建议接口：

- `get_binding(bot_key, session_key)`
- `bind_session(bot_key, session_key, project_id, workspace_id, mode)`
- `save_thread_id(bot_key, session_key, thread_id)`
- `clear_thread(bot_key, session_key)`
- `clear_binding(bot_key, session_key)`（仅管理员命令或高级用）

### 9.2 改造 `config/bot_config.py`

扩展 `BotConfig.provider_config` 约定，增加：

```yaml
provider_config:
  workspace_root: "C:/next/.codex_data"
  workspace_strategy: "copy"          # copy | git_worktree
  default_group_workspace_mode: "personal"  # personal | shared
  session_timeout_seconds: 7200
```

说明：

- `working_dir` 不再直接代表 Codex 执行目录
- `working_dir` 在新设计中更适合作为“默认项目源根”或兼容旧配置入口

### 9.3 改造 `src/core/orchestrator_factory.py`

现状：

- 启动时用固定 `working_dir` 创建一个 `CodexCliOrchestrator`

目标：

- `CodexCliOrchestrator` 可以动态根据 `session_key` 解析出 `workspace_path`

两种做法：

#### 做法 A（推荐）

- `CodexCliOrchestrator` 保持单例
- orchestrator 内部通过 `SessionBindingManager + WorkspaceManager` 在每次 turn 开始时解析 `workspace_path`
- `adapter.create_session()` 时传入当前 `workspace_path`

优点：

- 与现有工厂模式兼容最好
- 不需要为每个会话构建新的 orchestrator 实例

### 9.4 改造 `src/core/codex_cli_orchestrator.py`

这是本次改造的核心模块。

#### 当前问题

- `self.working_dir` 是固定值
- `upload_root` 也跟固定工作目录绑定

#### 目标改造

新增内部方法：

- `_resolve_project_and_workspace(session_key, user_id, log_context)`
- `_get_runtime_working_dir(session_key, user_id, log_context)`
- `_get_upload_root()`

调整 `_run_codex_turn(...)`：

1. 根据 `session_key + user_id` 解析当前绑定
2. 若没有绑定，则按默认规则自动创建 / 选择项目与 workspace
3. 使用 `workspace_path` 启动 `CodexAppServerSession`
4. 将 `codex_thread_id` 存回 `SessionBindingManager`

调整附件目录：

- 不再放在共享项目根目录下
- 改为统一放到：
  - `workspace_root/uploads/<bot_key>/<session_key>/...`

#### 预期效果

- 同一个 bot 下，不同用户可以落在不同 workspace
- 同一个项目下，多个用户可以各有自己的 workspace
- 切换项目后，新的消息会自动切换工作目录

### 9.5 改造 `src/adapters/codex_app_server_adapter.py`

当前 `CodexAppServerSession` 构造时要求固定 `working_dir`。

改造建议：

- 保持 `CodexAppServerSession` 不变
- 由 `CodexCliOrchestrator` 在每次创建 session 时传入当前 `workspace_path`

即：

- adapter 从“固定工作目录实例”转向“按 turn 创建运行时 session”

### 9.6 改造 `src/core/session_manager.py`

当前 `SessionManager` 过于单一，只存 thread id。

建议：

- 第一阶段保留 `SessionManager`，仅用于历史兼容
- 新逻辑转移到 `SessionBindingManager`
- 后续可考虑把 `SessionManager` 重命名为 `ThreadSessionManager`

### 9.7 改造 `src/transport/message_dispatcher.py`

需要新增两类职责：

#### 职责 A：项目/工作区命令路由

例如：

- `项目列表`
- `新建项目 xxx`
- `进入项目 xxx`
- `当前项目`
- `当前工作区`
- `使用共享工作区`

#### 职责 B：普通消息处理前补充上下文

对于非命令消息：

- 先根据 `session_key/user_id/chatid` 解析当前 workspace
- 如果缺少项目绑定，则按默认规则补建
- 然后把当前 workspace 信息传给 orchestrator

建议方式：

- 仍保持 dispatcher 轻量
- 项目与工作区解析委托给 `SessionBindingManager + WorkspaceManager`

### 9.8 命令处理实现位置

当前 `src/handlers/command_handlers.py` 偏 demo 性质。

建议：

- 新增模块：`src/handlers/project_commands.py`
- 通过现有 `custom_commands` 或直接在 dispatcher 中接入

建议不要把复杂项目逻辑继续堆进现有 demo handler 文件。

---

## 10. 状态迁移与兼容策略

### 10.1 向后兼容

现有 `working_dir` 配置仍然保留，但语义调整为：

- 旧模式：Codex 直接执行目录
- 新模式：默认项目源根 / 兼容入口目录

如果用户没有启用“项目工作区模式”，仍可回退使用当前固定目录逻辑。

### 10.2 建议的切换开关

在 `provider_config` 中增加：

```yaml
provider_config:
  enable_project_workspace_mode: true
```

这样可以分阶段上线：

- `false`：保留当前固定 `working_dir`
- `true`：启用项目 / workspace 模式

### 10.3 数据初始化

如果启用新模式但没有任何项目记录：

- 单聊首次消息时自动创建默认个人项目
- 群聊首次消息时引导用户先创建或进入项目

---

## 11. 实施阶段建议

### 阶段 1：基础隔离版（优先实施）

范围：

- 引入 `project/workspace/session_binding` 三个 JSON 状态文件
- 单聊默认个人项目 + 个人 workspace
- 群聊支持绑定项目 + 默认个人 workspace
- `CodexCliOrchestrator` 动态使用 `workspace_path`
- 附件目录切到统一 uploads 根目录

验收标准：

- 用户 A / B 在同一 bot 下互不共享执行目录
- 同一用户可以创建并切换多个项目
- 同一共享项目下，不同成员默认使用自己的 workspace
- 重启服务后，仍能恢复当前会话绑定的项目和 workspace

### 阶段 2：群聊共享工作区版

范围：

- 增加 `使用共享工作区`
- 允许群维度绑定共享 workspace
- 补充当前项目 / 当前工作区查看命令

### 阶段 3：Git 协作增强版

范围：

- `workspace_strategy = git_worktree`
- 每个 workspace 对应独立 branch / worktree
- 增加 diff / patch / merge 辅助命令
- 增加共享项目协作审计信息

---

## 12. 风险与应对

### 风险 1：目录数量快速增长

应对：

- 项目和 workspace 分级目录管理
- 增加“清理未使用 workspace”后台工具（后续）

### 风险 2：群聊共享工作区冲突

应对：

- 默认不启用共享工作区
- 首阶段只支持个人 workspace 默认模式

### 风险 3：进程重启导致绑定错乱

应对：

- session / workspace / project 状态必须持久化到 JSON
- thread 失效时仅重建 thread，不重建 workspace

### 风险 4：旧模式兼容问题

应对：

- 增加 `enable_project_workspace_mode` 开关
- 分阶段迁移

---

## 13. 建议的首批开发清单

### 必做

- 新增 `ProjectRegistry`
- 新增 `WorkspaceManager`
- 新增 `SessionBindingManager`
- 改造 `CodexCliOrchestrator` 支持动态 `workspace_path`
- 改造 `MessageDispatcher` 支持项目命令
- 增加 `项目列表 / 新建项目 / 进入项目 / 当前项目 / 当前工作区`
- 增加状态 JSON 持久化

### 建议一起做

- 将附件目录迁移到统一 uploads 根
- 为工作区模式增加配置开关
- 启动时打印项目工作区模式是否启用

### 可以后做

- Git worktree
- 共享工作区
- 成员邀请
- diff / merge 命令

---

## 14. 推荐结论

对于你当前的场景，最合适的路线不是“简单按 `user_id/chatid` 切换 `working_dir`”，而是：

1. 引入 `project`
2. 以 `workspace` 作为实际目录隔离单位
3. 用 `session` 绑定当前会话上下文
4. 默认采用“共享项目 + 个人工作区”模式
5. 群聊共享工作区作为增强功能后置

这是兼顾：

- 个人独立开发
- 多人协作开发
- 审批边界清晰
- 与当前 Codex 交互链路兼容
- 后续 Git 协作可扩展

的最稳妥方案。
