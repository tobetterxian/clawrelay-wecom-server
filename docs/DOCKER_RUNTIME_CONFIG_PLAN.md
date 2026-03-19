# Docker 开箱即用配置方案

状态：方案阶段  
日期：2026-03-19

## 背景

当前仓库已经提供基础 Docker 运行方式：

- 镜像定义：`Dockerfile`
- 容器编排：`docker-compose.yml`
- 示例环境文件：`.env.example`
- Bot 配置示例：`config/bots.yaml.example`

目前的容器化方案可以运行，但距离“开箱即用”还有几个明显差距：

1. Docker 侧缺少统一的运行时配置设计
2. `bots.yaml` 中的 API key 主要还是示例硬编码风格
3. GitHub / Cloudflare / Claude / Codex 等凭证缺少统一注入方案
4. 容器内 CLI 的 `HOME`、工作目录、工作区目录、日志目录还没有形成标准约定
5. Windows / Linux / Docker 三种部署模式还没有统一的配置优先级

目标是把项目整理成：

- 镜像通用
- 配置外置
- 密钥运行时注入
- 挂载即用
- 可在 Docker 中稳定运行 Claude / Codex / OpenAI / Gemini / GitHub 推送等能力

## 目标

1. Docker 镜像本身不包含任何敏感凭证
2. 所有模型 API key、GitHub 凭证、CLI 登录态都通过运行时方式注入
3. 用户只需准备：
   - `docker-compose.yml`
   - `.env`
   - `config/bots.yaml`
4. 启动后即可：
   - 连企业微信
   - 调用 AI 模型
   - 访问挂载工作目录
   - 使用本地 CLI 类机器人
5. 为后续“机器人自动推 GitHub / 触发 CI / 发布 Cloudflare”打好基础

## 非目标

1. 不把生产密钥烘焙进 Docker 镜像
2. 不默认让机器人容器长期持有高权限 Cloudflare 生产部署凭证
3. 第一阶段不强制切换到 Docker secrets；允许先用 `.env` + 环境变量

## 核心原则

### 1. 镜像无状态

镜像只包含：

- 代码
- 依赖
- 默认运行命令

镜像不包含：

- `bots.yaml` 真实配置
- API key
- GitHub token / SSH 私钥
- Claude / Codex 登录态目录

### 2. 配置外置

以下目录和文件应通过 volume 挂载：

- `config/`
- `logs/`
- 工作目录（例如 `/workspace`）
- 工作区根目录（例如 `/data/workspaces`）
- CLI HOME 持久化目录（例如 `/data/codex-home`、`/data/claude-home`）

### 3. 密钥运行时注入

优先级建议：

1. Docker 环境变量
2. Docker secrets
3. `bots.yaml` 中的环境变量占位符
4. 默认值

### 4. 最小权限

容器中允许常驻的凭证：

- OpenAI / Anthropic / Gemini API key
- GitHub 最小权限 token 或单仓库 deploy key

不建议常驻在机器人容器中的高风险凭证：

- Cloudflare 生产部署 token
- 个人主账号 GitHub 全权限 token

## 推荐目录结构

推荐 Docker 运行目录结构如下：

```text
project-root/
├── docker-compose.yml
├── .env
├── config/
│   └── bots.yaml
├── logs/
├── workspace/
├── data/
│   ├── codex-home/
│   ├── claude-home/
│   ├── workspaces/
│   └── uploads/
└── secrets/
    ├── github_id_ed25519
    └── github_known_hosts
```

说明：

- `workspace/`：给本地代码机器人访问的项目根目录
- `data/workspaces/`：项目 / 工作区模式下的持久化目录
- `data/codex-home/`：Codex CLI 独立 `HOME`
- `data/claude-home/`：后续 Claude 本地直连时可复用
- `secrets/`：本地私钥或其他敏感文件挂载目录

## 推荐配置优先级

建议未来统一采用以下优先级：

1. 容器运行时环境变量
2. `bots.yaml` 中的 `${VAR_NAME}` 占位符
3. `bots.yaml` 中的普通字面值
4. 代码默认值

这样做的好处是：

- 本地调试时可直接写明文（不推荐，但方便）
- Docker / CI / 生产环境可以统一改成环境变量
- 不需要为每个 bot 单独写死敏感值

## 设计建议：`bots.yaml` 支持环境变量替换

### 建议语法

支持如下形式：

```yaml
env_vars:
  OPENAI_API_KEY: "${OPENAI_API_KEY}"

provider_config:
  api_key: "${ANTHROPIC_API_KEY}"
  base_url: "${OPENAI_BASE_URL}"
```

以及带默认值形式：

```yaml
provider_config:
  base_url: "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
```

### 建议实现位置

优先在：

- `config/bot_config.py`

中做 YAML 读取后的递归变量替换。

### 需要支持的行为

1. 递归处理 dict / list / str
2. 未找到环境变量时：
   - 有默认值则使用默认值
   - 无默认值则保留原值或给出告警
3. 日志中不要输出敏感值本身

## 凭证分类与注入建议

### A. 模型 API Key

适用：

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

推荐方案：

- 放进 `.env`
- 或生产环境用 Docker secrets / 宿主机环境变量

理由：

- 简单
- 生命周期清晰
- 与 bot 配置解耦

### B. GitHub 凭证

分两类。

#### 方案 1：GitHub Token

适合：

- 简单 `git push`
- GitHub API 操作
- 机器人创建仓库、提交 PR 等

推荐变量名：

- `GH_TOKEN`
- `GITHUB_TOKEN_CUSTOM`

注意：

- 使用 fine-grained token
- 仅授权目标仓库
- 不要用个人主账号全权限 token

#### 方案 2：SSH Deploy Key

适合：

- 单仓库 push
- 最小权限长期运行

推荐做法：

- 把私钥文件挂载到容器内只读目录
- 挂载 `known_hosts`
- 通过环境变量指定私钥路径

例如：

- `GIT_SSH_COMMAND=ssh -i /run/secrets/github_id_ed25519 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/secrets/github_known_hosts`

建议优先级：

- 单仓库自动推送：优先 SSH deploy key
- 多仓库 / API 操作：再考虑 fine-grained token

### C. Cloudflare 凭证

推荐分场景。

#### 不推荐常驻在机器人容器中的场景

- 生产直接部署 Cloudflare
- 自动改 DNS
- 自动改生产环境路由

更推荐：

- GitHub Actions Secrets
- 由 CI/CD 执行 Cloudflare 发布

#### 可以接受的场景

- 临时测试环境
- staging 环境
- 完全受控的内网自用场景

即便如此，也建议：

- 使用最小权限 token
- 区分 staging 和 production

### D. Claude / Codex CLI 登录态

这类不是单纯 key，而是 CLI 会在 `HOME` 下保存配置或登录态。

推荐做法：

- 为不同 provider 使用独立 `HOME`
- `HOME` 目录挂载为持久化卷
- 不直接复用容器默认 `/root`

例如：

- Codex：`/data/codex-home/<bot_key>`
- Claude：`/data/claude-home/<bot_key>`

## 容器卷挂载建议

### 推荐挂载项

```yaml
volumes:
  - ./config:/app/config
  - ./logs:/app/logs
  - ./workspace:/workspace
  - ./data/workspaces:/data/workspaces
  - ./data/codex-home:/data/codex-home
  - ./data/claude-home:/data/claude-home
```

### 说明

- `config` 不建议只读，除非你明确不需要配置向导写文件
- `workspace` 让本地 CLI 机器人能看到宿主机项目
- `data/workspaces` 用于项目 / 工作区模式
- `data/*-home` 用于保存 CLI 登录态和本地缓存

## 现有 `docker-compose.yml` 的问题

当前文件是：

- `config` 只读挂载
- 只有 `config` 和 `logs`
- 没有统一 `.env` 设计
- 没有工作目录和 CLI HOME 持久化

这意味着：

1. 首次向导在容器内可能无法写配置
2. 本地代码机器人无法稳定访问挂载项目
3. Codex / Claude CLI 登录态无法标准化持久化
4. GitHub / SSH 等凭证没有固定接入方式

## 推荐的 `docker-compose.yml` 方向

建议未来演进为如下结构：

```yaml
services:
  app:
    build: .
    container_name: clawrelay-wecom-server
    restart: unless-stopped
    env_file:
      - .env
    environment:
      BOT_CONFIG_PATH: /app/config/bots.yaml
      WORKSPACE_ROOT_BASE: /data/workspaces
      CODEX_HOME_BASE: /data/codex-home
      CLAUDE_HOME_BASE: /data/claude-home
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./workspace:/workspace
      - ./data/workspaces:/data/workspaces
      - ./data/codex-home:/data/codex-home
      - ./data/claude-home:/data/claude-home
      - ./secrets:/run/local-secrets:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

这个片段是设计方向，不是当前仓库已经实现的最终配置。

## 推荐的 `.env.example` 内容

建议新增或扩展以下变量：

```dotenv
# 基础
BOT_CONFIG_PATH=/app/config/bots.yaml

# 模型 API Keys
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=

# 可选 OpenAI 兼容端点
OPENAI_BASE_URL=https://api.openai.com/v1

# GitHub
GH_TOKEN=
GITHUB_REPO_SSH_URL=
GIT_SSH_COMMAND=

# Cloudflare（更推荐放 CI，不建议长期给容器）
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=

# 工作区和 CLI HOME
WORKSPACE_ROOT_BASE=/data/workspaces
CODEX_HOME_BASE=/data/codex-home
CLAUDE_HOME_BASE=/data/claude-home
```

## Bot 配置建议

### OpenAI / Codex API

```yaml
provider_config:
  api_key: "${OPENAI_API_KEY}"
  base_url: "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
```

### Codex CLI

```yaml
working_dir: "/workspace"
env_vars:
  OPENAI_API_KEY: "${OPENAI_API_KEY}"
provider_config:
  workspace_root: "/data/workspaces/codex"
  codex_home: "/data/codex-home/cx_bot"
```

### Claude Relay

如果仍使用 `clawrelay-api`：

```yaml
relay_url: "${CLAUDE_RELAY_URL:-http://host.docker.internal:50009}"
```

### 未来 Claude CLI

如果后续落地 `claude_cli`：

```yaml
working_dir: "/workspace"
env_vars:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
provider_config:
  claude_home: "/data/claude-home/claude_bot"
```

## GitHub 推送方案建议

### 推荐方案 1：SSH Deploy Key

优点：

- 权限最小
- 单仓库边界清晰
- 更适合长期运行在机器人容器中

做法：

1. 为目标仓库创建 deploy key
2. 私钥放宿主机 `./secrets/github_id_ed25519`
3. `known_hosts` 放 `./secrets/github_known_hosts`
4. 挂载到容器
5. 通过 `GIT_SSH_COMMAND` 注入

### 推荐方案 2：Fine-grained GitHub Token

适合：

- 除了 `git push`，还要调 GitHub API
- 创建仓库、PR、Issue

建议：

- 单独机器人账号
- 单仓库 / 最小权限
- 避免个人主账号长期凭证

## Cloudflare 部署方案建议

### 最推荐

机器人容器只负责：

- 改代码
- 提交代码
- 推送 GitHub

真正部署交给：

- GitHub Actions

理由：

1. 生产凭证不常驻容器
2. 有审计日志
3. 更容易做 staging / production 区分
4. 失败回滚链路更清晰

### 不推荐

让机器人容器长期持有生产 Cloudflare token 并直接部署。

## 建议分阶段实施

### Phase 1：文档与运行约定

交付项：

- 当前文档
- `.env.example` 扩展
- README Docker 章节补充

### Phase 2：配置替换能力

交付项：

- `bots.yaml` 环境变量替换
- 启动时日志提示缺失关键变量

### Phase 3：Docker Compose 强化

交付项：

- 工作区挂载
- CLI HOME 持久化
- secrets 挂载建议
- 可选 profile（例如 api / codex / claude）

### Phase 4：CLI Provider 容器体验收敛

交付项：

- Codex CLI / Claude CLI 的独立 HOME
- GitHub SSH / token 自动接入说明
- 更稳定的开箱即用体验

## 验收标准

做到以下几点，才能称为 Docker 版“开箱即用”：

1. 用户只需修改 `.env` 和 `config/bots.yaml`
2. `docker compose up -d` 后服务即可正常启动
3. 模型 API key 不出现在仓库文件中
4. 容器重启后 CLI 登录态和工作区仍然存在
5. GitHub 推送凭证通过运行时方式注入
6. 生产 Cloudflare 凭证不强制常驻机器人容器

## 开放问题

1. `bots.yaml` 环境变量替换是否支持默认值语法 `${VAR:-default}`
2. 是否要在 Docker Compose 中同时支持 `.env` 与 Docker secrets
3. GitHub 凭证默认走 SSH 还是 token
4. 是否为 Codex / Claude 提供独立 sidecar 容器，还是统一跑在主服务容器里

## 最终建议

从可维护性、安全性、用户体验三者平衡来看，推荐方向是：

1. **镜像通用**
2. **配置外置**
3. **密钥运行时注入**
4. **CLI HOME 持久化**
5. **GitHub 推送允许容器内完成**
6. **Cloudflare 生产部署尽量交给 GitHub Actions**

这样既能满足“开箱即用”，也不会把整个系统变成“镜像里塞满长期高权限凭证”的高风险模式。
