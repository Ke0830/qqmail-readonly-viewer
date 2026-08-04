# QQ 邮箱只读查看器

一个适用于 macOS 和 Windows 的本地 QQ 邮箱查看器。它通过加密 IMAP 读取邮件，支持浏览器查看和 JSON 输出，也可以配置给 Agent 生成每日邮件简报。

**让 Agent 以只读方式筛选和汇总邮件，避免重要信息淹没在广告与垃圾邮件中。**

- [安装与配置](#安装与配置)
- [配置给 Agent 使用](#配置给-agent-使用)

> 本项目不是腾讯或 QQ 邮箱的官方产品，也不隶属于腾讯。

## 功能

- 按日期倒序查看邮件，支持未读筛选、最近 N 小时筛选和分页。
- 解码 UTF-8、GBK、GB18030、Big5 等常见邮件编码。
- 在浏览器中查看纯文本正文和附件名称。
- 输出 JSON，供 Agent、脚本和定时任务读取。
- 不发送、删除、移动、下载邮件，也不会把邮件标记为已读。

## 环境要求

- macOS 12 或更高版本，或 Windows 10/11
- Python 3.10 或更高版本
- 已开启 QQ 邮箱的 IMAP 服务

Linux 暂不支持。

## 安装与配置

先登录 [QQ 邮箱](https://mail.qq.com)，进入“设置 → 账号与安全 → 安全设置”，开启 IMAP/SMTP 服务并生成授权码。

### macOS

```bash
git clone https://github.com/Ke0830/qqmail-readonly-viewer.git
cd qqmail-readonly-viewer
python3 -m pip install .
qqmail-viewer configure --email 你的号码@qq.com
```

### Windows

在 PowerShell 中运行：

```powershell
git clone https://github.com/Ke0830/qqmail-readonly-viewer.git
cd qqmail-readonly-viewer
py -m pip install .
qqmail-viewer configure --email 你的号码@qq.com
```

终端会提示输入授权码，输入内容不会显示。授权码将保存在 macOS 登录钥匙串或 Windows 凭据管理器中，不会写入项目文件。

## 使用

### 浏览器查看

```bash
qqmail-viewer serve
```

打开 <http://127.0.0.1:8765>。按 `Control-C` 停止服务。

### 命令行读取

最近 20 封未读邮件：

```bash
qqmail-viewer list --unread --limit 20
```

附带正文预览：

```bash
qqmail-viewer list --unread --limit 20 --include-text
```

最近 24 小时内的全部未读邮件：

```bash
qqmail-viewer list --unread --since-hours 24 --all-pages --include-text
```

查看已读和未读邮件：

```bash
qqmail-viewer list --all --limit 20
```

读取指定 UID 的邮件正文：

```bash
qqmail-viewer show 邮件UID
```

需要手动分页时，使用 `--limit` 设置每页数量，使用 `--offset` 跳过前面的邮件。

## 配置给 Agent 使用

先完成[安装与配置](#安装与配置)，并确认下面的命令可以正常返回邮件列表：

```bash
qqmail-viewer list --unread --limit 5
```

### 获取命令路径

macOS：

```bash
command -v qqmail-viewer
```

Windows PowerShell：

```powershell
(Get-Command qqmail-viewer -ErrorAction Stop).Path
```

将返回的绝对路径填入 Agent 的任务说明。如果命令不在 `PATH` 中，也可以让 Agent 使用 Python 和 `qqmail_viewer.py` 的绝对路径运行。

### Agent 任务示例

> 每天运行 `/查看器的绝对路径/qqmail-viewer list --unread --since-hours 24 --all-pages --include-text`，读取最近 24 小时内的未读邮件并生成中文简报。
>
> 将邮件分为“需要处理”“重要通知”和“普通信息”，列出发件人、主题、核心内容、截止时间和建议下一步；忽略明显广告和重复邮件。如果没有需要关注的新邮件，说明“QQ 邮箱暂无需要关注的新邮件”。
>
> 只允许读取和分析邮件。不得发送、回复、删除、移动邮件，不得修改已读状态，也不得执行 `configure` 或索取密码、授权码。验证码、授权码和登录链接中的令牌必须脱敏。
>
> 如果读取失败，报告错误并通知我手动检查，不要自行重新授权或修改系统凭据。

Agent 必须在完成邮箱授权的同一台电脑、同一个系统用户下运行。建议只授权 Agent 执行 `list` 和 `show` 命令。

## 安全设计

- 使用 TLS 连接 `imap.qq.com:993`，并验证服务器证书和主机名。
- 以只读方式打开 `INBOX`，使用 `BODY.PEEK` 读取邮件。
- 授权码仅保存在系统凭据库中。
- 网页服务只监听 `127.0.0.1`，不会直接暴露给局域网或互联网。
- HTML 邮件转换为纯文本，不执行脚本，也不加载远程图片。

你可以随时在 QQ 邮箱设置中关闭 IMAP 或撤销授权码，查看器会立即失去访问能力。

## 已知限制

- 只读取收件箱 `INBOX`，不遍历垃圾箱、自定义文件夹或已发送邮件。
- 附件只显示文件名，不下载或解析内容。
- macOS 登录钥匙串锁定，或 Windows 后台任务使用其他用户时，可能无法读取凭据。

## 开发与测试

macOS：

```bash
python3 -m unittest -v
```

Windows PowerShell：

```powershell
py -m unittest -v
```

## 安全问题与贡献

请勿在公开 Issue 中提交真实邮件、邮箱地址、密码或授权码。安全问题的报告方式见 [SECURITY.md](SECURITY.md)。普通缺陷和改进建议可以通过 GitHub Issue 提交。

## 许可证

[MIT License](LICENSE)
