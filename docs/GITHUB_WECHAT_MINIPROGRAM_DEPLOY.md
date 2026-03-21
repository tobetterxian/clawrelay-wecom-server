# GitHub + 微信小程序上传 / 提审 / 发布

本文档说明如何在本项目里，通过企业微信对话为当前工作区接入 **微信小程序上传、提审与发布** 能力。

当前分两段：

- 第一阶段：通过 GitHub Actions 上传 **体验版**
- 第二阶段：由机器人直接调用微信 OpenAPI 完成 **提审 / 查审核 / 发布 / 撤回审核**

## 一、前置条件

需要同时满足以下条件：

- 机器人运行环境已配置 `GITHUB_TOKEN`
- 如需一键上传，还需配置 `WECHAT_MINIPROGRAM_PRIVATE_KEY`
- 如需提审 / 发布，还需配置 `WECHAT_MINIPROGRAM_APPSECRET`
- 小程序目录内存在 `project.config.json`
- 微信公众平台已为该小程序开启 CI，并生成上传密钥

可选环境变量：

- `WECHAT_MINIPROGRAM_APPID`
- `WECHAT_MINIPROGRAM_PRIVATE_KEY`
- `WECHAT_MINIPROGRAM_APPSECRET`
- `WECHAT_MINIPROGRAM_ROBOT`

## 二、企业微信命令

最常用的是两条：

- `35 启用小程序上传 [AppID] [项目路径]`
- `36 一键上传小程序 [仓库名] [AppID] [项目路径]`

第二阶段常用命令：

- `37 启用小程序提审 [配置文件]`
- `38 提交小程序审核 [配置文件]`
- `39 小程序审核状态 [审核单号]`
- `40 发布小程序`
- `41 撤回小程序审核`

也兼容这些说法：

- `启用微信小程序上传`
- `一键发布小程序`
- `一键上传微信小程序`

### 1）只写上传脚手架

适合仓库已经存在、只想接入上传流程：

```text
35
35 wx1234567890ab
35 wx1234567890ab miniprogram
35 miniprogram
```

默认行为：

- `AppID` 留空时，优先读取项目里已保存的配置，其次读取 `WECHAT_MINIPROGRAM_APPID`
- `项目路径` 留空时，会自动探测这些目录：
  - `.`
  - `miniprogram`
  - `dist`
  - `dist/wechat`
  - `dist/mp-weixin`
  - `unpackage/dist/dev/mp-weixin`
  - `unpackage/dist/build/mp-weixin`

### 2）一键推 GitHub 并开启体验版上传

适合本地代码已完成、想直接接通 GitHub Actions：

```text
36
36 hello-mini
36 hello-mini wx1234567890ab
36 hello-mini wx1234567890ab miniprogram
36 wx1234567890ab miniprogram
```

默认行为：

- `仓库名` 留空时，默认使用当前项目名
- `AppID` 留空时，优先使用项目配置或 `WECHAT_MINIPROGRAM_APPID`
- `项目路径` 留空时自动探测

执行完成后，机器人会：

- 推送代码到 GitHub
- 写入 `WECHAT_MINIPROGRAM_PRIVATE_KEY` 到该仓库的 GitHub Actions Secrets
- 写入 `.github/workflows/upload-wechat-miniprogram.yml`
- 写入 `.github/scripts/upload-wechat-miniprogram.js`
- 再次推送触发体验版上传

### 3）启用提审模板

适合已经有体验版，准备正式提审：

```text
37
37 .github/wechat-miniprogram-audit.json
```

默认会生成：

- `.github/wechat-miniprogram-audit.json`

这个文件是提审 JSON 模板，建议先让机器人帮你补充真实分类、页面地址、标题和版本说明，再提交审核。

### 4）提交审核 / 查询审核 / 发布

```text
38
38 .github/wechat-miniprogram-audit.json
39
39 123456789
40
41
```

默认行为：

- `38` 不带参数时，优先使用项目里已保存的审核配置路径
- `39` 不带审核单号时，默认查询最近一次 `38` 返回的审核单号
- `40` 为正式发布
- `41` 为撤回当前审核

## 三、生成的文件

脚手架会生成：

- `.github/workflows/upload-wechat-miniprogram.yml`
- `.github/scripts/upload-wechat-miniprogram.js`

工作流默认在以下场景触发：

- push 到 `main`
- 手动 `workflow_dispatch`

## 四、GitHub Secret

当前最少需要这个 Secret：

- `WECHAT_MINIPROGRAM_PRIVATE_KEY`

这个值来自微信公众平台生成的 CI 上传私钥内容。

第二阶段额外需要：

- `WECHAT_MINIPROGRAM_APPSECRET`

这个值来自小程序后台的应用密钥，用于机器人直接调用微信 OpenAPI。

## 五、适用项目类型

可直接支持：

- 原生小程序项目
- 已经产出小程序构建目录的 Taro / uni-app / 自定义前端项目

关键要求只有一个：

- 目标目录里必须有 `project.config.json`

如果你的仓库根目录不是小程序目录，执行时传入项目路径即可，例如：

```text
35 wx1234567890ab miniprogram
36 hello-mini wx1234567890ab dist/mp-weixin
```

## 六、排障建议

如果上传失败，优先检查：

- GitHub Actions 是否已触发
- 仓库 Secret `WECHAT_MINIPROGRAM_PRIVATE_KEY` 是否已写入
- `AppID` 是否正确
- `项目路径/project.config.json` 是否存在
- 微信公众平台是否已启用 CI 上传能力

如果提审 / 发布失败，优先检查：

- 运行环境是否已配置 `WECHAT_MINIPROGRAM_APPSECRET`
- `37` 生成的提审 JSON 是否已改成真实业务内容
- 小程序是否已经先上传过体验版
- 微信开放平台 / 公众平台当前是否允许该小程序提审或发布

企业微信里可继续使用：

- `33 发布流水线状态`
- `21 远程状态`
- `22 部署状态`
