# Codex CLI 容器化方案

状态：方案 + 第一版落地骨架  
日期：2026-03-19

## 结论先说

如果希望在 Docker 容器内部直接使用 `bot_type: codex_cli`，那么：

1. **最好把 Codex CLI 一并纳入镜像或镜像变体**
2. 不要依赖宿主机上的 `codex` 二进制
3. 不要把登录态和凭证烘焙进镜像
4. `codex_home`、工作目录、工作区目录都应通过 volume 持久化

本轮代码已经先把 Docker 基础镜像补齐为更适合代码机器人的形态：

- 已内置 `git`
- 已内置 `openssh-client`
- 已预留 `/workspace`、`/data/workspaces`、`/data/codex-home` 等目录

这样容器已经能支持：

- `git_remote` 工作区初始化
- 容器内 `git clone`
- 配合 SSH key 的 `git push`

但**默认镜像仍然没有直接安装 `codex`**。本轮已经补上第一版增强骨架，采用“通用镜像 + Codex 增强镜像”双轨方式推进。

## 本轮已落地

仓库现在新增了以下文件：

- `Dockerfile.codex`
- `docker-compose.codex.yml`
- `config/bots.codex-cli.docker.yaml.example`

用途分别是：

1. `Dockerfile.codex`
   - 基于当前 Python 服务镜像扩展
   - 在构建阶段安装 `@openai/codex`
   - 构建时执行 `codex --version` 做基础自检

2. `docker-compose.codex.yml`
   - 作为 overlay 覆盖基础 `docker-compose.yml`
   - 将 `app` 服务切换为使用 `Dockerfile.codex`
   - 支持通过 `CODEX_NPM_PACKAGE` 调整安装包版本

3. `config/bots.codex-cli.docker.yaml.example`
   - 提供容器内 `codex_cli` 的最小可用 bot 模板
   - 默认工作区模式为 `empty`
   - 默认 `codex_path` 为 `codex`

4. 启动自检 + healthcheck
   - 服务启动前会对所有 `codex_cli` bot 执行运行时自检
   - Docker `HEALTHCHECK` 会复用同一套检查逻辑
   - 重点检查：
     - 配置是否可加载
     - `codex` 是否可执行且 `--version` 成功
     - `workspace_root` / `codex_home` 是否存在且可写
     - `working_dir` 是否有效

## 为什么不建议依赖宿主机 `codex`

若容器内的 `codex_cli` 机器人依赖宿主机二进制，会有几个问题：

1. 路径不可移植，Windows / Linux / WSL 差异大
2. 容器重建后行为不稳定，不符合“开箱即用”
3. 企业微信消息触发的是容器内进程，容器内找不到宿主机 PATH
4. 登录态、模型配置、审批配置难以统一

因此更合理的方向是：

- 容器负责运行 `codex`
- 宿主机只负责挂载代码目录、工作区目录、凭证目录

## 官方安装信息

根据 OpenAI 官方 Codex CLI 文档，Linux / macOS 下的基础安装方式是：

- `npm i -g @openai/codex`

文档同时说明：

- `codex` 首次运行会要求登录
- CLI 可在 Linux 本地运行
- Windows 支持仍偏实验性，官方更推荐 WSL 工作区

这意味着容器内启用 `codex_cli` 的核心前提是：

1. 镜像里有 Node.js / npm
2. 镜像里已经安装好 `@openai/codex`
3. 运行时可提供 `OPENAI_API_KEY` 或持久化的 Codex 登录态目录

## 推荐镜像分层

建议后续拆成两层：

### 方案 A：保持当前通用镜像

用途：

- `claude_code`
- `gemini`
- `openai`
- `codex`
- 不需要本地 `codex` 二进制的场景

特点：

- 体积更小
- 构建更稳定
- 已满足 Git / SSH / 工作区能力

### 方案 B：提供 Codex 增强镜像

用途：

- `codex_cli`
- 容器内直接跑 `codex app-server`

推荐做法：

1. 基于当前镜像继续扩展
2. 安装 Node.js / npm
3. 安装 `@openai/codex`
4. 启动前做一次 `codex --version` 自检

可以采用两种落地方式：

- 单独 `Dockerfile.codex`
- 或在现有 `Dockerfile` 中增加构建参数控制

为了降低默认镜像体积，**更推荐单独的 Codex 增强镜像**。

## 运行时目录约定

容器内建议固定以下目录：

- 工作目录根：`/workspace`
- 项目工作区根：`/data/workspaces`
- Codex HOME 根：`/data/codex-home`
- 本地敏感文件：`/run/local-secrets`

一个 `codex_cli` bot 的推荐配置形态：

```yaml
bots:
  codex_cli_bot:
    bot_id: "${WECOM_BOT_ID}"
    secret: "${WECOM_BOT_SECRET}"
    bot_type: "codex_cli"
    working_dir: "/workspace"
    env_vars:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
    provider_config:
      codex_path: "codex"
      workspace_root: "${WORKSPACE_ROOT_BASE:-/data/workspaces}/codex"
      codex_home: "${CODEX_HOME_BASE:-/data/codex-home}/codex_cli_bot"
      default_workspace_init_mode: "empty"
      sandbox_mode: "workspace-write"
      approval_policy: "on-request"
```

## 凭证建议

### 1. OpenAI / Codex 访问凭证

优先级建议：

1. 容器环境变量中的 `OPENAI_API_KEY`
2. 挂载并持久化的 `codex_home`

不建议：

- 把 API key 写进镜像
- 把 `~/.codex` 登录态直接提交到仓库

### 2. GitHub 推送凭证

推荐：

- SSH Deploy Key
- 挂载到 `/run/local-secrets`
- 通过 `GIT_SSH_COMMAND` 指定

### 3. Cloudflare 部署凭证

推荐尽量放到 GitHub Actions 或 CI 平台，而不是长期留在机器人容器里。

## 与当前工作区方案的关系

当前项目已经支持三种工作区初始化模式：

- `empty`
- `git_remote`
- `legacy_copy`

如果容器内使用 `codex_cli`，推荐默认策略仍然是：

1. `empty`
2. `git_remote`
3. `legacy_copy` 仅兼容旧逻辑

这样可以避免：

- 容器启动时复制大目录
- 扫到宿主机异常路径
- 把宿主机整个项目树无差别复制进工作区

## 推荐实施顺序

### Phase 1（已完成 / 已具备基础）

- `bots.yaml` 支持环境变量占位符
- Docker 运行目录约定已补齐
- 镜像已具备 `git` / `ssh` 基础能力
- 工作区默认模式已优先 `empty` / `git_remote`

### Phase 2（本轮已完成基础骨架）

1. 增加 `Dockerfile.codex`
2. 在该镜像中安装 Node.js + Codex CLI
3. 新增 `docker-compose.codex.yml`
4. 启动时增加 `codex --version` 构建期自检

剩余可继续增强的点：

5. 增加更细粒度的版本锁定策略
6. 根据实际运行反馈决定是否拆分更轻量的 runtime 层

### Phase 3（可选增强）

1. 为 Claude 本地 CLI 也提供镜像变体
2. 增加 secrets 文件自动注入
3. 增加首次启动自检命令
4. 增加更多示例 `codex_cli` Docker 配置模板

## 验收标准

做到以下几点，就算容器内 `codex_cli` 基本可用了：

1. `docker compose up -d --build` 后服务可正常启动
2. 容器内执行 `codex --version` 成功
3. 企业微信发送消息可触发 `codex_cli`
4. 可以创建 `empty` 工作区
5. 可以创建 `git_remote` 工作区
6. 可通过 SSH deploy key 执行 `git push`

## 快速启动

```bash
cp .env.example .env
cp config/bots.codex-cli.docker.yaml.example config/bots.yaml

# 编辑 .env，至少填入：
# WECOM_BOT_ID
# WECOM_BOT_SECRET
# OPENAI_API_KEY

docker compose -f docker-compose.yml -f docker-compose.codex.yml up -d --build
```

如需锁定 Codex CLI 版本，可在 `.env` 中设置：

```bash
CODEX_NPM_PACKAGE=@openai/codex@latest
```

## 相关文档

- `docs/DOCKER_RUNTIME_CONFIG_PLAN.md`
- `docs/GITHUB_CLOUDFLARE_DEPLOY.md`
- `docs/CLAUDE_CLI_LOCAL_PLAN.md`
