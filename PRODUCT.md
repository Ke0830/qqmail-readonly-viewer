# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

拥有 QQ / Foxmail、网易、iCloud、Gmail 或自定义 IMAPS 邮箱的个人用户，在本机浏览器中快速浏览一个或多个收件箱并打开邮件阅读。

## Product Purpose

以本地只读方式展示多个 IMAP 邮箱邮件，帮助用户在不改变邮件已读状态的前提下筛选、浏览和阅读收件箱内容。

## Positioning

每个账户的凭据保存在本机系统凭据存储中；查看器通过加密 IMAPS 和 `BODY.PEEK` 读取邮件，可单独查看或汇总排序，不提供删除、发送或修改邮件状态的能力。

## Operating Context

用户从命令行启动本机网页服务，在 `127.0.0.1` 的浏览器页面中选择一个账户或全部账户，查看未读或全部收件箱邮件，并按日期倒序翻页。

## Capabilities and Constraints

- 支持 macOS 与 Windows，使用本机钥匙串或凭据管理器保存每个账户的授权码或应用专用密码。
- 支持 QQ / Foxmail、163、126、yeah、iCloud、Gmail 与手动配置的加密 IMAPS；Gmail OAuth 与 Outlook / Microsoft 365 不在首期范围。
- 只读取 `INBOX`；附件仅列出文件名，不下载或解析。
- 网页与 JSON 接口均不应要求前端依赖或外部网络资源。
- 界面需同时适配桌面与窄屏，并跟随系统浅色或深色模式。

## Evidence on Hand

- `qqmail_viewer.py` 包含单文件网页服务、账户索引、单账户与汇总邮件列表、邮件详情路由。
- `test_qqmail_viewer.py` 覆盖邮件解析、分页命令行参数、TLS 和系统凭据边界。

## Product Principles

- 只读边界必须清晰、可验证且不为便利牺牲。
- 邮件列表优先服务于快速扫读与可靠定位。
- 用户始终应看懂当前筛选、所处位置和可执行操作。
- 真实邮件内容优先，界面不添加虚构状态或营销性信息。
