# QQ 邮箱只读查看器

一个运行在 macOS 上的本地 QQ 邮箱查看器。它通过加密 IMAP 读取邮件，可以在浏览器中查看，也可以输出结构化 JSON，供 Codex 等自动化工具生成邮件摘要。

本项目不是腾讯或 QQ 邮箱的官方产品，也不隶属于腾讯。

**你可以把它部署到你常用agent的每日待办上 让agent只读你的邮箱 帮你检索出真正重要的信息并简要发送给你 让你无需在大量广告与垃圾信息中查找真正重要的邮件**
- [在agent上的快速配置方法](#快速配置方法)

## 功能

- 按邮件日期倒序列出最近邮件，支持未读筛选和分页。
- 解码 UTF-8、GBK、GB18030、Big5 等常见邮件编码。
- 显示纯文本正文和附件名称，不下载附件或加载远程图片。
- 提供命令行 JSON 输出，适合只读自动化和每日简报。
- 凭据仅保存在 macOS 登录钥匙串。

## 安全设计

- 只连接 `imap.qq.com:993`（IMAPS/TLS）。
- 以只读方式打开 `INBOX`，并使用 `BODY.PEEK`，查看不会把邮件标为已读。
- 不包含发送、删除、移动、下载附件等写操作。
- QQ 邮箱授权码不写入代码、配置文件或命令历史。
- 网页服务只监听 `127.0.0.1`，不会直接暴露给局域网或互联网。
- HTML 邮件转换为纯文本，不执行脚本，不加载远程图片。

“持续授权”不代表不可撤销的永久权限。你可以随时在 QQ 邮箱设置中关闭 IMAP 或撤销授权码，查看器会立即失去访问能力。

## 环境要求

- macOS 12 或更高版本
- Python 3.10 或更高版本
- 已开启 QQ 邮箱的 IMAP 服务

目前凭据存储依赖 macOS `security` 命令，因此暂不支持 Windows 和 Linux。

## 安装

克隆仓库后，可以直接运行单文件脚本：

```bash
git clone https://github.com/Ke0830/qqmail-readonly-viewer.git
cd qqmail-readonly-viewer
python3 qqmail_viewer.py --help
```

也可以安装为本机命令：

```bash
python3 -m pip install .
qqmail-viewer --help
```

## 首次配置

1. 登录 [QQ 邮箱](https://mail.qq.com)。
2. 打开“设置 → 账号与安全 → 安全设置 → POP3/IMAP/SMTP/Exchange/CardDAV 服务”。
3. 开启 IMAP/SMTP，按页面提示生成授权码。
4. 在终端运行：

```bash
qqmail-viewer configure --email 你的号码@qq.com
```

如果没有安装为命令，将上述 `qqmail-viewer` 替换为 `python3 qqmail_viewer.py`。

终端会隐式读取授权码（输入时不显示字符）。程序先验证只读连接，成功后才把邮箱地址和授权码保存到 macOS 登录钥匙串。授权码不是 QQ 登录密码；不要把密码或授权码粘贴到聊天、Issue 或代码中。

## 使用

启动本地网页查看器：

```bash
qqmail-viewer serve
```

然后打开 <http://127.0.0.1:8765>。按 `Control-C` 停止服务。

列出最近 20 封未读邮件：

```bash
qqmail-viewer list --unread --limit 20
```

附带每封邮件前 1200 字正文，适合生成摘要：

```bash
qqmail-viewer list --unread --limit 20 --include-text
```

读取指定 UID 邮件的纯文本正文：

```bash
qqmail-viewer show 邮件UID
```

## 用于定时任务

定时任务应调用安装后的稳定路径或 `qqmail-viewer` 命令，不要依赖临时工作目录。任务指令示例：

> 每个工作日早上 8:00，运行 `qqmail-viewer list --unread --limit 20 --include-text`。仅汇总需要我处理的邮件，列出发件人、主题、核心内容、截止时间和建议下一步。不要发送、删除、移动或标记任何邮件。如果钥匙串、网络或登录失败，明确通知我重新授权。

macOS 钥匙串锁定时，后台任务可能无法读取凭据。建议先在同一登录用户下手动运行一次命令并确认成功。

## 测试

```bash
python3 -m unittest -v
```

测试使用构造的邮件数据，不连接真实邮箱，也不读取授权码。

## 快速配置方法
**这里是快速配置到agent上的方法 按这个方法配置就不用再看上面的几条使用步骤**

### 0. 在qq邮箱中打开相关配置

1. 登录 [QQ 邮箱](https://mail.qq.com)。
2. 打开“设置 → 账号与安全 → 安全设置 → POP3/IMAP/SMTP/Exchange/CardDAV 服务”。
3. 开启 IMAP/SMTP，按页面提示生成授权码。

### 1. 由用户手动完成首次授权

请在终端中运行：

```bash
qqmail-viewer configure --email 你的号码@qq.com
```

按照提示输入 QQ 邮箱授权码。输入内容不会显示，验证成功后凭据将保存在当前 macOS 用户的登录钥匙串中。

不要让 Agent 自动执行 `configure`，也不要把 QQ 密码或授权码写入提示词、配置文件、环境变量或聊天内容。

### 2. 验证只读访问

```bash
qqmail-viewer list --unread --limit 5
```

如果终端返回 JSON 格式的邮件列表，说明配置成功。该命令只读取邮件，不会将邮件标记为已读。

如果没有安装命令行入口，也可以使用脚本的绝对路径：

```bash
python3 /项目的绝对路径/qqmail_viewer.py list --unread --limit 5
```

### 3. 确定 Agent 使用的命令路径

运行：

```bash
command -v qqmail-viewer
```

将返回的绝对路径提供给 Agent。使用绝对路径可以避免 Agent 或定时任务找不到命令。

例如：

```text
/Users/你的用户名/.local/bin/qqmail-viewer
```

### 4. 给 Agent 的推荐指令

可以把下面的内容加入 Agent 的任务说明：

> 使用 QQ 邮箱只读查看器运行：
>
> `/查看器的绝对路径/qqmail-viewer list --unread --limit 20 --include-text`
>
> 根据返回的 JSON 邮件数据生成摘要，并将邮件分为“需要处理”“重要通知”和“普通信息”。列出发件人、主题、核心内容、截止时间和建议下一步。
>
> 只允许读取和分析邮件。不得发送、回复、删除、移动邮件，不得修改已读状态，也不得执行 `configure` 或索取邮箱密码、授权码。
>
> 如果命令执行失败，请原样报告错误，不要自行重新配置账号或修改钥匙串。

### 5. 定时任务示例

> 每个工作日早上 8:00，运行 QQ 邮箱只读查看器，读取最近 20 封未读邮件并生成中文简报。只报告需要处理的事项、重要通知和明确的截止时间。如果没有新邮件，直接说明“最近没有需要处理的新邮件”。如果钥匙串、网络或登录失败，通知我手动检查，不要自行重新授权。

### 注意事项

- Agent 必须运行在完成授权的同一台 Mac、同一个 macOS 用户下。
- Mac 未登录或登录钥匙串被锁定时，后台任务可能无法读取凭据。
- 建议只授权 Agent 执行 `list` 和 `show` 命令。
- Agent 能否自动执行命令，取决于所使用的软件及其本地命令权限设置。
- 本项目只提供邮件读取能力；通知最终显示在 Agent 软件、系统通知还是其他渠道，由自动化平台的通知方式决定。


## 已知限制

- 只读取收件箱 `INBOX`，不遍历垃圾箱、自定义文件夹或已发送邮件。
- 附件只显示文件名，不下载或解析内容。


## 安全问题与贡献

发现安全问题时，请不要公开提交包含真实邮件、邮箱地址或授权码的 Issue。处理方式见 [SECURITY.md](SECURITY.md)。普通缺陷和改进建议可以通过 GitHub Issue 提交。

## 许可证

[MIT License](LICENSE)
