# QQ 邮箱只读查看器

一个运行在 macOS 上的本地 QQ 邮箱查看器。它通过加密 IMAP 读取邮件，可以在浏览器中查看，也可以输出结构化 JSON，供 Codex 等自动化工具生成邮件摘要。

本项目不是腾讯或 QQ 邮箱的官方产品，也不隶属于腾讯。

**你可以把它部署到你常用agent的每日待办上 让agent只读你的邮箱 帮你检索出真正重要的信息并简要发送给你 让你无需在大量广告与垃圾信息中查找真正重要的邮件**

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
2. 打开“设置 → 账户”。
3. 找到 POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务。
4. 开启 IMAP/SMTP，按页面提示生成授权码。
5. 在终端运行：

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

## 已知限制

- 只读取收件箱 `INBOX`，不遍历垃圾箱、自定义文件夹或已发送邮件。
- 附件只显示文件名，不下载或解析内容。
- QQ 邮箱可能因安全策略、密码变更或用户撤销授权而使授权码失效。
- 实时提醒需要额外的后台守护任务；本项目更适合按计划执行的只读摘要。

## 安全问题与贡献

发现安全问题时，请不要公开提交包含真实邮件、邮箱地址或授权码的 Issue。处理方式见 [SECURITY.md](SECURITY.md)。普通缺陷和改进建议可以通过 GitHub Issue 提交。

## 许可证

[MIT License](LICENSE)
