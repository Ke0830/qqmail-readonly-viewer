# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

拥有 QQ / Foxmail、网易、iCloud、Gmail 或自定义 IMAPS 邮箱的个人用户，在本机浏览器中快速浏览一个或多个收件箱并打开邮件阅读。

## Product Purpose

以本地缓存优先、后台增量同步的只读方式展示多个 IMAP 邮箱，帮助用户快速筛选、浏览和阅读收件箱内容，同时不改变邮件已读状态。

## Positioning

每个账户的凭据和正文缓存密钥保存在本机系统凭据存储中；查看器通过加密 IMAPS 增量同步元数据，从本机 SQLite 提供单账户或汇总列表，并按需读取安全的文本 MIME section，不提供删除、发送或修改邮件状态的能力。

## Operating Context

用户从命令行启动本机网页服务，在 `127.0.0.1` 的浏览器页面中选择一个账户或全部账户，查看未读或全部收件箱邮件，并按日期倒序翻页。首次无缓存时先显示并行取得的未读候选，随后后台补齐索引；暖缓存交互不等待 IMAP。

## Capabilities and Constraints

- 支持 macOS 与 Windows，使用本机钥匙串或凭据管理器保存每个账户的授权码或应用专用密码。
- 支持 QQ / Foxmail、163、126、yeah、iCloud、Gmail 与手动配置的加密 IMAPS；Gmail OAuth 与 Outlook / Microsoft 365 不在首期范围。
- 只读取 `INBOX`；附件仅列出文件名，不下载或解析。
- 列表、筛选和分页只查询本机缓存；每账户后台 worker 独占并复用一条 IMAP 连接，账户之间并行。
- 支持仅内存、持久化元数据、加密按需正文三种缓存模式，以及 0–1440 分钟同步间隔。
- 默认正文缓存使用 AES-GCM；只缓存用户打开过的文本正文，不预取全部正文。
- 网页与 JSON 接口均不应要求前端依赖或外部网络资源。
- 界面需同时适配桌面与窄屏，并跟随系统浅色或深色模式。

## Evidence on Hand

- `qqmail_viewer.py` 保留兼容入口、账户与凭据管理、网页和 CLI；缓存、同步与 MIME section 读取分别位于内部模块。
- 测试覆盖缓存恢复、并行同步、增量状态、UIDVALIDITY、安全 MIME 读取、分页、TLS、CSRF 和系统凭据边界。

## Product Principles

- 只读边界必须清晰、可验证且不为便利牺牲。
- 邮件列表优先服务于快速扫读与可靠定位。
- 暖缓存交互不应被 IMAP 网络延迟阻塞，后台完成也不应打断当前页面。
- 用户始终应看懂当前筛选、所处位置和可执行操作。
- 真实邮件内容优先，界面不添加虚构状态或营销性信息。
