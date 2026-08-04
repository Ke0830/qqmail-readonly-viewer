# 本地只读邮箱查看器

一个适用于 macOS 和 Windows 的本地、多账户 IMAP 邮箱查看器。它通过加密连接读取收件箱：可在浏览器中扫读邮件，也可输出 JSON 给 Agent 或定时任务使用。

**让 Agent 以只读方式筛选和汇总邮件，并干练的汇报给你，避免重要信息淹没在广告与垃圾邮件中。**

> 本项目不是任何邮箱服务商的官方产品；仓库名和 `qqmail-viewer` 命令为兼容已有安装而保留。

- [关于如何配置给 Agent 使用](#配置给-agent-使用)

## 支持范围

- QQ / Foxmail、网易 163、126、yeah、iCloud Mail、Gmail，以及手动填写的加密 IMAPS 邮箱。
- 每个账户独立保存在 macOS 登录钥匙串或 Windows 凭据管理器中；密码、授权码和应用专用密码不会写入项目文件。
- 网页可查看单个账户或汇总全部已配置账户；汇总时其中一个账户失败不会隐藏其他账户的邮件。
- 不发送、回复、删除、移动、下载附件或修改已读状态。

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

[安装](#安装)完成后来到这一步

先在邮箱服务商的设置中开启 IMAP ([如何开启](#各服务商准备方式))，并生成授权码或应用专用密码。运行配置命令后按提示输入该密码；输入不会显示，且只会在测试到只读 IMAPS 连接成功后保存。

QQ / Foxmail、163、126、yeah、iCloud Mail 和 Gmail 都会按邮箱地址自动识别。建议为每个账户指定一个简短名称，方便以后在网页、`list` 和 `show` 中切换：

```bash
qqmail-viewer configure --email 你的号码@qq.com --name qq
qqmail-viewer configure --email name@gmail.com --name personal-gmail
qqmail-viewer configure --provider icloud --email name@icloud.com --name icloud
qqmail-viewer configure --provider 163 --email name@163.com --name netease

其他邮箱格式以此类推
```

不写 `--provider` 时会自动识别；若需要手动指定，也可以使用 `--provider qq`、`163`、`126`、`yeah`、`icloud` 或 `gmail`。第一个配置的账户会成为默认账户；之后添加账户时，可用 `--default` 更换命令行默认账户。

自建或其他服务商仅允许加密 IMAPS：

```bash
qqmail-viewer configure --provider custom --email name@example.com --name work --imap-host imap.example.com --port 993
```

查看已配置账户（不显示任何密码或授权码）：

```bash
qqmail-viewer accounts
```

### 各服务商准备方式

- **QQ / Foxmail**：登录 QQ 邮箱，进入“设置 → 账号与安全 → 安全设置”，开启 IMAP/SMTP 服务并生成授权码。
- **163 / 126 / yeah**：登录对应网易邮箱，在客户端或 POP3/SMTP/IMAP 设置中开启 IMAP，生成“客户端授权密码”。
- **iCloud Mail**：在 Apple Account 的“登录与安全”中生成应用专用密码，然后用该密码配置；服务器为 `imap.mail.me.com:993`。详见 [Apple 官方说明](https://support.apple.com/en-us/102525)。
- **Gmail**：先开启两步验证，再创建 Google 应用专用密码并用于配置。详见 [Google 应用专用密码说明](https://support.google.com/mail/answer/185833) 和 [Gmail IMAP 文档](https://developers.google.com/workspace/gmail/imap/imap-smtp)。

## 使用

启动网页：

```bash
qqmail-viewer serve
```

打开 <http://127.0.0.1:8765>。一个账户时显示该账户；两个或更多账户时默认显示“全部账户”。账户选择、筛选、每页数量、页码和从详情返回的位置都会保留。

命令行读取默认账户的未读邮件：

```bash
qqmail-viewer list --unread --limit 20
```

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

## 配置给 Agent 使用

先完成[配置](#添加账户)，并确认下面命令能返回邮件：

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

现有 QQ 每日任务可继续使用默认命令，不需要改动。确认新增账户都能读取后，再将任务改为：

> 每天运行 `/查看器的绝对路径/qqmail-viewer list --all-accounts --unread --since-hours 24 --all-pages --include-text`，读取最近 24 小时内各账户的未读邮件并生成中文简报。
>
> 将邮件分为“需要处理”“重要通知”和“普通信息”，列出账户、发件人、主题、核心内容、截止时间和建议下一步；忽略明显广告和重复邮件。若某个账户读取失败，说明该账户与错误，不要阻断其余账户的摘要。
>
> 只允许读取和分析邮件。不得发送、回复、删除、移动邮件，不得修改已读状态，也不得执行 `configure` 或索取密码、授权码。验证码、授权码和登录链接中的令牌必须脱敏。

Agent 必须运行在完成授权的同一台电脑、同一个系统用户下。建议只授权它执行 `list` 和 `show`。

## 安全设计与限制

- 所有账户只使用 TLS 加密的 IMAPS，并验证服务器证书与主机名；自定义账户不支持明文 IMAP。
- 只读打开 `INBOX`，使用 `BODY.PEEK` 读取邮件，不会标为已读。
- 网页只监听 `127.0.0.1`，不会暴露到局域网或互联网。
- HTML 邮件仅转换为纯文本，不执行脚本或加载远程图片；附件只列出名称，不下载或解析。
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
