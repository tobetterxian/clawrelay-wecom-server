# 多 GitHub 账号并存的 SSH 配置方案

适用场景：

- 一台机器同时使用多个 GitHub 账号
- 不同仓库要推到不同账号 / 组织
- 既希望 `git push` 稳定，又不想频繁切换全局 SSH key
- 需要兼容当前项目这种“机器人可自动推 GitHub”的工作流

---

## 目标

推荐把 GitHub 配置拆成 3 层：

1. **仓库远程地址**
   - 决定推送到哪个 GitHub 仓库
2. **Git 提交身份**
   - 决定 commit 里的 `user.name` / `user.email`
3. **SSH 私钥**
   - 决定这次连接 GitHub 时实际以哪个账号认证

这三层是相互独立的，不要混在一起。

---

## 推荐方案

最稳的做法是：

- 每个 GitHub 账号单独生成一把 SSH key
- 在 `~/.ssh/config` 中给每个账号定义一个独立别名
- 仓库远程地址统一写成这个别名，而不是直接写 `github.com`

例如：

- `github-main` → 个人主账号
- `github-work` → 公司 / 工作账号
- `github-alt` → 备用账号

这样你在不同仓库里只需要切换 remote URL，不需要来回覆盖默认 SSH key。

---

## 目录建议

建议把 SSH 文件整理成这样：

```sshconfig
~/.ssh/
├── config
├── id_ed25519_github_main
├── id_ed25519_github_main.pub
├── id_ed25519_github_work
├── id_ed25519_github_work.pub
├── id_ed25519_github_alt
└── id_ed25519_github_alt.pub
```

不要再让多个账号共用一把 `~/.ssh/id_ed25519`。

---

## 第一步：为每个账号生成独立 SSH key

示例：

```bash
ssh-keygen -t ed25519 -C "main-account@users.noreply.github.com" -f ~/.ssh/id_ed25519_github_main
ssh-keygen -t ed25519 -C "work-account@company.com" -f ~/.ssh/id_ed25519_github_work
ssh-keygen -t ed25519 -C "alt-account@example.com" -f ~/.ssh/id_ed25519_github_alt
```

然后把对应的 `.pub` 公钥分别加到不同 GitHub 账号：

- GitHub -> `Settings`
- `SSH and GPG keys`
- `New SSH key`

建议每把 key 的标题写清楚机器和用途，例如：

- `wsl-dev-main`
- `wsl-dev-work`
- `windows-laptop-main`

---

## 第二步：配置 `~/.ssh/config`

推荐配置：

```sshconfig
Host github-main
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_github_main
  IdentitiesOnly yes

Host github-work
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_github_work
  IdentitiesOnly yes

Host github-alt
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_github_alt
  IdentitiesOnly yes
```

如果你已经启用了 `ssh-agent`，这个配置仍然建议保留，因为：

- 它让每个 alias 明确绑定到一把 key
- 能避免 agent 里加载了多把 key 时 GitHub 误选
- 远程地址一眼就能看出仓库绑定了哪个账号

---

## 第三步：分别验证每个账号

验证主账号：

```bash
ssh -T git@github-main
```

验证工作账号：

```bash
ssh -T git@github-work
```

验证备用账号：

```bash
ssh -T git@github-alt
```

理想返回类似：

```text
Hi your-account! You've successfully authenticated, but GitHub does not provide shell access.
```

如果显示的是错误账号，优先检查：

- `~/.ssh/config` 中 `IdentityFile` 是否写对
- 这把公钥是否真的加到了目标 GitHub 账号
- 是否遗漏了 `IdentitiesOnly yes`

---

## 第四步：仓库 remote 要改成 alias

不要继续用这种 remote：

```bash
git@github.com:owner/repo.git
```

而要改成：

```bash
git@github-main:owner/repo.git
git@github-work:org/repo.git
git@github-alt:owner/repo.git
```

示例：

```bash
git remote set-url origin git@github-main:tobetterxian/clawrelay-wecom-server.git
```

如果另一个仓库属于工作账号：

```bash
git remote set-url origin git@github-work:company/project.git
```

这样 `git push` 时就会自动按 remote 里写的 alias 选对账号。

---

## 第五步：每个仓库单独配置提交身份

SSH key 只决定“你是谁在连 GitHub”，不决定 commit 作者。

每个仓库建议单独设置：

```bash
git config user.name "tobetterxian"
git config user.email "tobetterxian@users.noreply.github.com"
```

工作仓库可以设置成：

```bash
git config user.name "your-work-name"
git config user.email "your-work-email@example.com"
```

检查方式：

```bash
git config --get user.name
git config --get user.email
git config --show-origin --get user.name
git config --show-origin --get user.email
```

---

## 当前这个项目怎么迁移

你当前仓库的状态大致是：

- remote：`git@github.com:tobetterxian/clawrelay-wecom-server.git`
- 提交身份：`tobetterxian` / `tobetterxian@users.noreply.github.com`
- 当前默认私钥：`~/.ssh/id_ed25519`

如果你要把它迁移到多账号方案，建议步骤：

### 方案 A：保留现有 key，先只做 alias

如果当前 `~/.ssh/id_ed25519` 已经是主账号可用 key，可以先这样：

```sshconfig
Host github-main
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

然后把当前仓库 remote 改成：

```bash
git remote set-url origin git@github-main:tobetterxian/clawrelay-wecom-server.git
```

这是**改动最小**的做法。

### 方案 B：彻底整理成每账号独立 key

更推荐长期这么做：

1. 把主账号 key 独立成 `~/.ssh/id_ed25519_github_main`
2. 在 `~/.ssh/config` 中定义 `github-main`
3. 把当前仓库 remote 改成 `git@github-main:...`
4. 后续新账号统一继续加 `github-work`、`github-alt`

---

## 新仓库的推荐初始化方式

如果你新建了一个仓库，建议按这个顺序：

1. 先确定这个仓库属于哪个账号
2. 直接把 remote 写成对应 alias
3. 再设置这个仓库自己的 `user.name` / `user.email`

例如：

```bash
git init
git remote add origin git@github-work:company/new-project.git
git config user.name "your-work-name"
git config user.email "your-work-email@example.com"
```

---

## 如果不想改 remote，也可以按仓库指定 SSH 命令

有时你不想改 remote，可以在单仓库里这样做：

```bash
git config core.sshCommand "ssh -i ~/.ssh/id_ed25519_github_work -o IdentitiesOnly=yes"
```

这会让当前仓库强制使用指定 key。

但这个方案的缺点是：

- 不如 remote alias 直观
- 你从 `git remote -v` 看不出这个仓库到底绑定哪个账号

所以仍然更推荐 **remote alias + `~/.ssh/config`**。

---

## WSL 和 Windows 同时用 Git 时要注意

如果你在：

- Windows 里用 Git
- WSL 里也用 Git

那它们通常是两套独立环境：

- Windows Git 看的是 Windows 用户目录下的 SSH 配置
- WSL Git 看的是 WSL 用户目录下的 `~/.ssh/config`

也就是说：

- 你在 WSL 里配好的 `~/.ssh/config`
- 不一定会自动给 Windows 里的 Git 生效

如果你主要是在 WSL 里跑本项目，就优先把配置做在 **WSL 的 `~/.ssh/`** 里。

---

## Docker / 机器人场景建议

如果后续要让机器人自动推 GitHub：

- 优先让容器 / 进程使用**单独的部署 key**
- 不要直接复用你个人主账号的日常 SSH 私钥
- 对单仓库推送，最推荐：
  - 仓库 deploy key
  - 或专门的机器人账号 key

如果机器人需要“列仓库 / 选仓库 / 创建仓库”，再考虑：

- `GITHUB_TOKEN`
- `GH_TOKEN`

也就是说：

- **Git push**：优先 SSH key
- **GitHub API**：优先 token

---

## 推荐的最终规范

建议长期固定成下面这套：

- 多账号：每账号一把独立 SSH key
- `~/.ssh/config`：每账号一个 alias
- 仓库 remote：统一写 alias，不直接写 `github.com`
- commit 身份：每仓库单独设置
- 机器人 / Docker：优先专用 deploy key，不复用个人日常 key

---

## 快速示例

主账号仓库：

```bash
git remote set-url origin git@github-main:tobetterxian/clawrelay-wecom-server.git
git config user.name "tobetterxian"
git config user.email "tobetterxian@users.noreply.github.com"
```

工作账号仓库：

```bash
git remote set-url origin git@github-work:company/internal-project.git
git config user.name "your-work-name"
git config user.email "your-work-email@example.com"
```

验证：

```bash
ssh -T git@github-main
ssh -T git@github-work
git remote -v
git config --get user.name
git config --get user.email
```

---

## 你可以下一步怎么做

如果你要落地到这台机器，建议按下面顺序操作：

1. 先给当前主账号定义 `github-main`
2. 把当前仓库 remote 改成 `git@github-main:...`
3. 再新增 `github-work`
4. 新仓库统一按 alias 方式配置

如果你愿意，我下一步可以直接帮你：

- 生成一份适合你当前机器的 `~/.ssh/config` 模板
- 或直接把当前仓库的 `origin` 改造成 `github-main` 风格
