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
   - Variable: `CF_PAGES_PROJECT_NAME`
   - Variable: `CF_PAGES_BUILD_DIR`
3. 把 `docs/examples/github-actions/cloudflare-pages-deploy.yml:1` 复制到目标项目：
   - `.github/workflows/deploy-cloudflare-pages.yml`
4. 根据项目调整安装和构建命令
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
