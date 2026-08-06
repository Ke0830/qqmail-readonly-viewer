# 本地邮箱查看器

**把所有邮箱整合到一个本地收件箱，再把它变成 Agent 持续更新的邮件插件。** QQ、163、126、Gmail 等邮箱原本彼此分散，普通 Agent 很难直接汇总查阅；本项目只需为每个邮箱授权一次，就会持续在你的电脑上同步最新邮件。你可以在一个网页里查看所有账户，也可以 **让 Agent 随时跨邮箱查找邮件、筛选重点、汇总内容和生成摘要。** 与普通邮箱客户端相比，它不仅方便人汇总查看，还专门为 Agent 提供统一的只读入口；

> 本项目不是任何邮箱服务商的官方产品

- [关于如何使用网页查看器查看邮件](#使用网页查看器查看邮件)（与命令行相比更为推荐 精心设计 界面直观）
- [关于如何使用命令行查看邮件](#使用命令行查看邮件)
- [关于如何配置给 Agent 使用](#配置给-agent-使用)（超好用）

## 支持范围

- QQ / Foxmail、网易 163、126、yeah、iCloud Mail、Gmail，以及手动填写的加密 IMAPS 邮箱。
- 每个账户独立保存在 macOS 登录钥匙串或 Windows 凭据管理器中；密码、授权码和应用专用密码不会写入项目文件。
- 网页可查看单个账户或汇总全部已配置账户；汇总时其中一个账户失败不会隐藏其他账户的邮件。
- 不发送、回复、删除、移动或修改已读状态；不提供附件打开或保存功能，也不会传输附件内容。
- 网页详情默认显示经过严格清洗和隔离的安全富文本，也可切换到纯文本；CLI 和 JSON 输出始终保持纯文本结构。
- 网页进入后会按日期从新到旧、跨账户并行预取全部已读与未读邮件的安全正文和正文图片；用户打开邮件时仍拥有更高读取优先级。图片请求先经过本机受限代理，附件图片仍只显示名称。远程图片可能让发件方看到公网 IP、预取时间和跟踪标识；网页链接必须确认完整地址后才会打开。
- 邮件列表优先读取本机 SQLite 缓存；默认使用 AES-GCM 加密持久化后台预取的纯文本、清洗后 HTML 和通过安全校验的正文图片，原始 HTML 不会落盘。
- 邮件详情可按需翻译为简体中文，支持 DeepL Free、DeepL Pro、OpenAI 兼容接口和本机 Ollama。翻译使用用户自己的 API，API Key 只保存在系统凭据库中；不会随正文预取自动调用。

Gmail 首版使用 IMAP + Google 应用专用密码；没有应用专用密码选项、Google Workspace 组织限制或 Advanced Protection 的账号需要 OAuth，本版本暂不支持。Microsoft 365 / Outlook OAuth 也不在本期范围。

## 安装

macOS：

```bash
git clone https://github.com/Ke0830/qqmail-readonly-viewer.git
cd qqmail-readonly-viewer
python3 -m pip install .
```

Windows PowerShell：

```powershell
git clone https://github.com/Ke0830/qqmail-readonly-viewer.git
cd qqmail-readonly-viewer
py -m pip install .
```

需要 Python 3.10 或更高版本。Linux 暂不支持。


## 添加账户

完成[安装](#安装)后，先在邮箱服务商的设置中开启 IMAP（参见[各服务商准备方式](#各服务商准备方式)），并生成授权码或应用专用密码。运行配置命令后按提示输入该密码；输入不会显示，且只会在测试到只读 IMAPS 连接成功后保存。

QQ / Foxmail、163、126、yeah、iCloud Mail 和 Gmail 都会按邮箱地址自动识别。建议为每个账户指定一个简短名称，方便以后在网页、`list` 和 `show` 中切换：

名字可以自定义 也就是--name后的内容可以按自己喜好决定 如`工作` `生活` `家庭`
```bash
qqmail-viewer configure --email 你的号码@qq.com --name qq
qqmail-viewer configure --email name@163.com --name 163
qqmail-viewer configure --email name@126.com --name 126
qqmail-viewer configure --email name@yeah.net --name yeah
qqmail-viewer configure --email name@gmail.com --name personal-gmail
qqmail-viewer configure --provider icloud --email name@icloud.com --name icloud
```

其他邮箱格式以此类推。

不写 `--provider` 时会自动识别；若需要手动指定，也可以使用 `--provider qq`、`163`、`126`、`yeah`、`icloud` 或 `gmail`。第一个配置的账户会成为默认账户；之后添加账户时，可用 `--default` 更换命令行默认账户。

自建或其他服务商仅允许加密 IMAPS：

```bash
qqmail-viewer configure --provider custom --email name@example.com --name work --imap-host imap.example.com --port 993
```

查看已配置账户（不显示任何密码或授权码）：

```bash
qqmail-viewer accounts
```

- [跳转到 使用网页查看器查看邮件](#使用网页查看器查看邮件)
- [跳转到 使用命令行查看邮件](#使用命令行查看邮件)
- [跳转到 配置给 Agent 使用](#配置给-agent-使用)

## 各服务商准备方式

- **QQ / Foxmail**：登录 QQ 邮箱，进入“设置 → 账号与安全 → 安全设置”，开启 IMAP/SMTP 服务并生成授权码。
- **163 / 126 / yeah**：登录对应的网易邮箱网页版，进入“设置 → POP3/SMTP/IMAP”或相应的客户端设置，开启 IMAP/SMTP 服务并生成客户端授权密码。查看器会在登录后自动发送网易要求的 IMAP 客户端标识，无需额外配置。
- **iCloud Mail**：在 Apple Account 的“登录与安全”中生成应用专用密码，然后用该密码配置；服务器为 `imap.mail.me.com:993`。详见 [Apple 官方说明](https://support.apple.com/en-us/102525)。
- **Gmail**：先开启两步验证，再创建 Google 应用专用密码并用于配置。详见 [Google 应用专用密码说明](https://support.google.com/mail/answer/185833) 和 [Gmail IMAP 文档](https://developers.google.com/workspace/gmail/imap/imap-smtp)。

[获得授权码后返回](#添加账户)

## 使用网页查看器查看邮件

先完成[配置](#添加账户)

启动网页：

```bash
qqmail-viewer serve
```

打开 <http://127.0.0.1:8765>。

## 使用命令行查看邮件：

先完成[配置](#添加账户)

开始查询：
```bash
qqmail-viewer list --unread --limit 20
```
**命令行默认只读取未读邮件**

读取指定账户：

```bash
qqmail-viewer list --account personal-gmail --all --limit 20
qqmail-viewer show 邮件UID --account personal-gmail
```

汇总读取全部账户：

```bash
qqmail-viewer list --all-accounts --unread --since-hours 24 --all-pages --include-text
```

单账户 JSON 保持原有数组格式；`--all-accounts` 返回 `messages` 与 `errors`，每封邮件带有账户归属。若其中一个账户暂时不可读，其错误会出现在 `errors`，其他账户仍会返回。

## 缓存与同步设置

网页右上角的“设置”或以下命令可以查看当前缓存与同步设置：

```bash
qqmail-viewer settings
```

缓存模式：

- `memory`：邮件元数据、后台预取的正文与正文图片只保留在当前进程内，退出后清除。
- `metadata`：持久化主题、发件人、日期、大小、未读状态和附件名称，正文不落盘。
- `body`：默认模式；进入网页后按日期从新到旧后台预取全部邮件正文与正文图片，并使用 AES-GCM 加密持久化。随机加密密钥只保存在 macOS 钥匙串或 Windows 凭据管理器；原始 HTML 不会写入缓存。

默认每 3 分钟自动同步一次。可设置为 `0`（只在网页手动刷新或 CLI 读取时同步）或 `1–1440` 分钟：

```bash
qqmail-viewer settings --cache-mode body --refresh-minutes 3
qqmail-viewer settings --refresh-minutes 0
```

降低缓存模式时必须明确处理旧缓存：

```bash
qqmail-viewer settings --cache-mode metadata --existing-cache purge
qqmail-viewer settings --cache-mode memory --existing-cache keep
```

清理缓存：

```bash
qqmail-viewer cache clear --bodies
qqmail-viewer cache clear --all
```

macOS 缓存位置为 `~/Library/Caches/local-readonly-mail-viewer/mail-cache.sqlite3`；Windows 为 `%LOCALAPPDATA%\local-readonly-mail-viewer\mail-cache.sqlite3`。`--all-pages` 在缓存尚未补齐时会等待完整索引，保证仍返回全部匹配邮件。

## 网页中文翻译

打开一封邮件后点击“翻译为中文”。首次使用会要求绑定自己的翻译 API，并明确提示当前邮件的主题和可见正文将发送给所选服务。支持：

- **DeepL Free / DeepL Pro**：使用固定的 DeepL 官方端点，只需填写 API Key。
- **OpenAI 兼容接口**：填写服务的 `Base URL`、模型名称和 API Key，可用于 OpenAI、DeepSeek、通义等兼容服务。
- **本机 Ollama**：可填写 `http://127.0.0.1:11434/v1` 或 `http://localhost:11434/v1`，API Key 可以留空。

远程接口必须使用 HTTPS。本机 HTTP 只允许 `127.0.0.1`、`localhost` 或 `::1`；查看器不会跟随重定向，也不会把密钥发送给其他主机。API Key 与公开配置分别保存在 macOS 钥匙串或 Windows 凭据管理器，网页只能显示服务商、地址和模型，无法读取密钥。

翻译只由用户点击触发，不会批量翻译缓存中的邮件。单封邮件最多翻译 100,000 个字符；主题、段落、表格单元格和按钮文字会翻译，发件人、收件人、附件名、图片、邮箱地址和链接地址保持原样。生成译文后，详情页默认显示中文，可随时切回“原文排版”或“原文纯文本”，标题也会同步切换。

译文与正文一样按缓存模式处理：`body` 模式使用 AES-GCM 加密后持久化，`memory` 和 `metadata` 模式只保留在当前进程内。更换或解绑 API 不会删除已有译文；可以在设置页重新翻译、只清除译文缓存，或随正文缓存一起清除。CLI 和现有 JSON 输出仍然只返回原始纯文本，不包含译文。

## 配置给 Agent 使用

先完成[配置](#添加账户)，并确认下面的命令能返回邮件：

```bash
qqmail-viewer list --unread --limit 5
```

获取命令绝对路径：

```bash
command -v qqmail-viewer
```

Windows PowerShell：

```powershell
(Get-Command qqmail-viewer -ErrorAction Stop).Path
```

建议提供给 Agent 的每日任务说明：

> 每天运行 `/查看器的绝对路径/qqmail-viewer list --all-accounts --unread --since-hours 24 --all-pages --include-text`，读取最近 24 小时内各账户的未读邮件并生成中文简报。
>
> 将邮件分为“需要处理”“重要通知”和“普通信息”，列出账户、发件人、主题、核心内容、截止时间和建议下一步；忽略明显广告和重复邮件。若某个账户读取失败，说明该账户与错误，不要阻断其余账户的摘要。
>
> 只允许读取和分析邮件。不得发送、回复、删除、移动邮件，不得修改已读状态，也不得执行 `configure` 或索取密码、授权码。验证码、授权码和登录链接中的令牌必须脱敏。

Agent 必须运行在完成授权的同一台电脑、同一个系统用户下。建议只授权它执行 `list` 和 `show`。

## 安全设计与限制

- 所有账户只使用 TLS 加密的 IMAPS，并验证服务器证书与主机名；自定义账户不支持明文 IMAP。
- 只读打开 `INBOX`，使用 `BODY.PEEK` 读取邮件头和选定的文本 MIME section，不会标为已读。
- 网页只监听 `127.0.0.1`，不会暴露到局域网或互联网。
- 后台预取或详情页读取时，先获取邮件头和 `BODYSTRUCTURE`，再按 MIME 树选择一个明确的非附件正文 section。网页优先读取 HTML 正文，必要时降级为纯文本；CLI 和现有 JSON 接口始终输出纯文本。正文读取不会请求附件 payload 或完整邮件，图片只通过独立的安全资源路径获取。
- 原始 HTML 只在内存中短暂存在，不会直接渲染或写入缓存。网页仅显示经过严格清洗的安全 HTML：脚本、表单、事件属性、自动跳转、危险链接以及可发起网络请求的样式都会被移除，并在受限 iframe 中通过独立 CSP 隔离。
- 正文中的 CID、Content-Location、data 图片和安全校验通过的 HTTP(S) 图片会在排版版正文显示后自动加载；浏览器只请求本机 opaque 图片地址，不会直连远程 URL。远程图片仍可能暴露公网 IP、打开时间和跟踪标识，因此页面会显示加载状态。明显的 1×1 或隐藏跟踪图、CSS 背景图、远程字体、SVG 和主动内容继续阻止；邮件中的 `http` / `https` 链接必须先在应用内确认完整地址，才会以新标签页打开。
- 附件只从 MIME 结构读取名称，不会下载、打开或保存附件内容；无法安全识别正文结构时，查看器会拒绝读取正文，而不会回退下载整封原始邮件。
- SQLite 中的邮件元数据（包括主题、发件人和附件名称）不是正文加密的一部分；默认 `body` 模式下，后台预取的纯文本、清洗后安全 HTML 和正文图片使用 AES-GCM 加密。缓存密钥与邮箱凭据分开保存在系统凭据库。
- 翻译 API 的公开配置和 API Key 使用两个独立的系统凭据记录。只有用户点击翻译时，当前邮件的主题和可见正文才会发送给所选服务；不发送账户凭据、发件人、收件人、附件、图片或链接地址。模型返回值只作为纯文本嵌回已清洗结构，不能新增 HTML、链接、图片或主动内容。
- 图片只读取正文实际引用的资源，绝不会回退下载附件或完整 RFC 邮件。body 缓存模式会使用 AES-GCM 持久化图片，memory/metadata 模式只保留进程内图片；单图 8 MiB、单封 30 MiB 并受像素和动画帧数限制。即使加载图片，CSS 背景图、远程字体和主动内容仍被阻止，网页无法与原邮箱做到像素级一致。
- 不读取垃圾箱、自定义文件夹或已发送邮件。

你可随时在服务商设置中撤销授权码、应用专用密码或关闭 IMAP，查看器会立即失去该账户的访问能力。

## 开发与安全问题

运行测试：

```bash
python3 -m unittest -v
```

请勿在公开 Issue 中提交真实邮件、邮箱地址、密码或授权码。安全问题报告方式见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT License](LICENSE)
