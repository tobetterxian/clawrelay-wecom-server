# GitHub + Cloudflare 自动部署方案

这份文档用于给**后续由机器人创建的新项目**准备自动部署方案。

当前仓库 `clawrelay-wecom-server` 是一个 Python WebSocket 服务，更适合部署在：

- Windows 服务 / Linux Systemd
- Docker / Docker Compose
- 云主机 / 容器平台

它**不适合直接部署到 Cloudflare Pages**，也**不建议直接迁移为 Cloudflare Worker**。

如果你后续在企业微信里让机器人新建一个 `hello world`、静态站点、前端项目或轻量边缘 API，这套方案可以直接复用。

## 选型建议

### 方案 A：Cloudflare Pages

适合：

- Vite / React / Vue / Svelte 静态站点
- 博客、Landing Page、文档站
- 构建结果是一个静态目录，例如 `dist/`、`build/`、`out/`

推荐模板：

- `docs/examples/github-actions/cloudflare-pages-deploy.yml:1`

### 方案 B：Cloudflare Workers

适合：

- 轻量 API
- Webhook / 边缘函数
- 小型全栈项目
- 需要低延迟边缘执行的服务

推荐模板：

- `docs/examples/github-actions/cloudflare-worker-deploy.yml:1`
- `docs/examples/wrangler/worker/wrangler.toml.example:1`

## 凭证怎么放

不要把下面这些值发到企业微信对话里，也不要提交到 Git 仓库：

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

推荐放法：

1. GitHub 仓库 `Settings -> Secrets and variables -> Actions`
2. 把敏感值放到 **Secrets**
3. 把非敏感配置放到 **Variables**

推荐配置如下：

### GitHub Secrets

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

### GitHub Variables

Pages 项目推荐：

- `CF_PAGES_PROJECT_NAME`
- `CF_PAGES_BUILD_DIR`

Workers 项目推荐：

- `CF_WORKER_DEPLOY_CMD`，例如 `deploy`

## Cloudflare Token 最小权限建议

### Pages

建议只给：

- Account -> Cloudflare Pages -> Edit

### Workers

建议从 Cloudflare 提供的 Workers 模板 token 开始，再按账号范围最小化：

- 仅授权目标 Account
- 只授予部署 Worker 所需权限

## 推荐落地方式

### 最推荐：GitHub Actions 部署 Cloudflare

优点：

- 机器人不直接持有生产部署凭证
- 所有部署动作都落在 GitHub Actions 审计日志里
- 团队成员容易接管
- 后续可加 PR Preview / staging / production

### 次推荐：Cloudflare 原生 Git 集成

如果项目就是普通前端站点，也可以直接在 Cloudflare 控制台连接 GitHub 仓库。

但如果你希望：

- 自定义构建流程
- 统一用 GitHub Actions 管理 CI/CD
- 同时跑 lint / test / build / deploy

那还是推荐 GitHub Actions + Wrangler。

## Pages 项目接入步骤

1. 在 Cloudflare Pages 创建项目，或先用 `wrangler pages project create`
2. 在 GitHub 仓库中创建：
   - Secret: `CLOUDFLARE_API_TOKEN`
   - Secret: `CLOUDFLARE_ACCOUNT_ID`
3. 把 `docs/examples/github-actions/cloudflare-pages-deploy.yml:1` 复制到目标项目：
   - `.github/workflows/deploy-cloudflare-pages.yml`
4. 根据项目调整安装和构建命令；如果通过机器人命令 `启用Pages部署 <Pages项目名> [构建目录]` 生成，则项目名和构建目录会直接写入工作流，不需要再配 GitHub Variables
   - 如果前端在子目录，例如 `xiaodaka/`，可直接把构建目录写成 `xiaodaka/dist`
   - 当前机器人也会在“仓库根没有 package.json、且只检测到一个前端子项目”时自动识别子目录并生成正确的工作流
5. 推送到 `main`，自动部署

## Workers 项目接入步骤

1. 在目标项目加入 `wrangler.toml`
2. 在 GitHub 仓库中创建：
   - Secret: `CLOUDFLARE_API_TOKEN`
   - Secret: `CLOUDFLARE_ACCOUNT_ID`
3. 把 `docs/examples/github-actions/cloudflare-worker-deploy.yml:1` 复制到目标项目：
   - `.github/workflows/deploy-cloudflare-worker.yml`
4. 需要时参考 `docs/examples/wrangler/worker/wrangler.toml.example:1`
5. 推送到 `main`，自动部署

## 企业微信对话命令

现在统一为两级体系：

- 一级：控制命令，输入完整命令或序号，立即执行
- 二级：普通对话，未命中一级命令的内容都交给 Codex 正常开发

一级控制命令支持直接写成 `序号 参数...`，例如：

- `6 hello-world`
- `12 react`
- `19 hello-world`
- `30`

推荐一级命令菜单：

- 帮助：`1 项目帮助`、`30 部署帮助`
- 项目与工作区：`2 项目列表`、`3 当前项目`、`4 当前工作区`、`5 工作区列表`、`6 新建项目 <名称>`、`7 新建仓库项目 <名称> <Git地址>`、`8 从仓库派生项目 <名称> <源Git地址>`、`9 进入项目 <名称或ID>`、`28 使用个人工作区`、`29 使用共享工作区`
- Git 身份：`10 Git身份状态`、`11 设置Git身份 <name> <email>`
- GitHub 仓库：`12 GitHub仓库列表 [关键词]`、`13 当前选中仓库`、`14 选择仓库 <序号>`、`15 从选中仓库派生项目 <名称>`、`16 创建GitHub仓库 <仓库名>`、`17 创建GitHub公开仓库 <仓库名>`、`18 创建GitHub仓库并发布 <仓库名>`、`19 推送到GitHub [仓库名]`、`20 推送到GitHub公开 [仓库名]`
- 发布部署：`21 远程状态`、`22 部署状态`、`23 准备GitHub仓库 <Git地址>`、`24 发布到新仓库 <新Git地址>`、`25 同步上游 [Git地址]`、`26 启用Pages部署 <Pages项目名> [构建目录]`、`27 启用Worker部署 <Worker名称> [入口文件]`、`31 一键发布Pages <仓库名> <Pages项目名> [构建目录]`、`32 一键发布Worker <仓库名> <Worker名称> [入口文件]`、`33 发布流水线状态`、`34 Cloudflare项目状态`

兼容保留少量旧别名，如：

- `GitHub组织仓库 <org> [关键词]`
- `创建GitHub私有仓库 <仓库名>`
- `创建GitHub组织仓库 <org> <仓库名>`

如果希望企业微信里的 GitHub 列仓、建仓、推送统一走同一个账号，可在 `bots.yaml` 中配置：

- `provider_config.default_github_owner`

## 二次开发并发布到新仓库

适合“从现有 GitHub 仓库选一个做二次开发，完成后再推到你自己的新仓库”：

1. `从仓库派生项目 my-app <源Git地址>`
2. 在当前项目里继续开发
3. `发布到新仓库 <新Git地址>`
4. 需要时执行 `同步上游`
5. 再执行 `启用Pages部署 ...` 或 `启用Worker部署 ...`

说明：

- 发布到新仓库时，系统会尽量保留原来的源仓库为 `upstream`
- 新仓库会作为新的 `origin`
- `同步上游` 当前只执行 `git fetch`，不会自动 merge / rebase

## 不安装 gh 也能创建新仓库

如果已经配置了 `GITHUB_TOKEN` 或 `GH_TOKEN`，现在可以直接通过企业微信命令：

1. `创建GitHub仓库 my-new-project`
2. `创建GitHub仓库并发布 my-new-project`

这样会直接调用 GitHub API 创建仓库，不依赖本机 `gh` 命令。

### `resource not accessible by personal access token` 怎么处理

如果企业微信里能列仓，但执行“创建 GitHub 仓库”或“推送到 GitHub（自动建仓）”时报这个错，通常说明当前用的是 **fine-grained PAT**，但权限不够。

建议检查：

- `Resource owner` 是否就是你配置给机器人的 GitHub 账号
- `Repository access` 是否允许访问目标仓库；如果要让机器人自动创建新仓库，更稳妥的是选 `All repositories`
- `Repository permissions` 是否至少包含：
  - 列出仓库：仓库元数据读取权限
  - 自动创建仓库：仓库管理写权限
- 如果改用 classic PAT，通常至少需要 `repo` scope

说明：

- 机器人现在会把 GitHub API 的原始 403 错误翻译成更具体的中文提示
- 如果暂时不想重配 Token，也可以先在 GitHub 手动创建空仓库，再让机器人通过 SSH 执行推送

## 以后让机器人自动生成项目时，建议这样提需求

### 静态站点

> 新建一个 hello world Vite 项目，初始化 GitHub 仓库，加入 Cloudflare Pages GitHub Actions 部署工作流，构建目录用 `dist`

### Worker API

> 新建一个 hello world Worker 项目，初始化 GitHub 仓库，加入 `wrangler.toml` 和 Cloudflare Workers GitHub Actions 部署工作流

## 注意事项

- `Pages` 更适合静态前端
- `Workers` 更适合 API / 边缘逻辑
- 不要把 Cloudflare Token 发到聊天里
- GitHub Actions 的生产凭证建议放到 `production` environment 下，而不是裸仓库 Secrets
- 如果后续你需要，我可以继续补：
  - PR Preview 工作流
  - staging / production 双环境
  - 自动绑定自定义域名
  - 自动回滚策略

## 官方参考

- Cloudflare Pages CI/CD：`https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/`
- Cloudflare Workers GitHub Actions：`https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/`
- GitHub Actions Secrets：`https://docs.github.com/actions/security-guides/using-secrets-in-github-actions`
