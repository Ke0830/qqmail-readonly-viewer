---
name: QQ 邮箱只读查看器
description: 面向快速扫读与可靠定位的本地只读收件箱界面。
colors:
  primary: "#282828"
  dark-accent: "#d9d9d9"
  canvas: "#ffffff"
  surface: "#ffffff"
  surface-raised: "#f5f5f5"
  ink: "#242424"
  muted: "#6b6b6b"
  line: "#e6e6e6"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "clamp(26px, 4vw, 34px)"
    fontWeight: 700
    lineHeight: 1.12
    letterSpacing: "-0.025em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 650
  control:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 650
rounded:
  control: "8px"
  group: "11px"
spacing:
  compact: "8px"
  control: "11px"
  row: "13px 20px"
  page: "48px 26px 76px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#ffffff"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "35px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "35px"
---

# Design System: QQ 邮箱只读查看器

## Overview

**Creative North Star: "清晰的收件箱工作台"**

这是一个用于个人收件箱扫读的操作界面，而不是营销页面或拟物邮箱。页面优先让用户看清“现在看的是什么、共有多少、自己在哪一页、下一步能做什么”，再以克制的层次和留白降低大量邮件带来的压力。

界面跟随系统浅色或深色模式：浅色采用纯白画布、浅灰控件与石墨黑主操作；深色采用石墨黑、深灰表面与灰白高光。两种模式都依靠中性明暗层级，而不是彩色强调，来区分当前筛选与主要操作。邮件行本身就是内容，不叠加无意义的卡片。

**Key Characteristics:**

- 高密度三列邮件列表，主题始终是主视觉。
- 筛选、页码和范围在首屏即可理解。
- 浅深色拥有各自完整的表面、边界和文字层级。
- 仅通过状态、边线和有限阴影建立层级。

## Colors

冷静的蓝灰中性底色服务长时间阅读，单一蓝色强调服务于定位而非装饰。

### Primary

- **石墨主操作**：浅色模式下以深灰用于主操作与当前筛选，避免在白底中引入多余色彩。
- **石墨高光**：深色模式下以灰白取代彩色强调，依靠明暗层级而非饱和色来突出操作焦点。

### Neutral

- **工作台底色**：画布、内容表面、抬升表面、正文、次级文字和分隔线构成可扫读的层级。

**The One Accent Rule.** 只有中性高光表示已选中或最主要的下一步；不要把它扩展成大面积装饰色。

## Typography

**Headline Font:** 系统无衬线字体栈。
**Body Font:** 系统无衬线字体栈。

**Character:** 标题紧凑有力，正文保持普通系统文本的可读性；邮件主题通过字重而不是颜色抢占注意力。

### Hierarchy

- **Headline**：用于页面标题与邮件标题，承担页面识别和阅读焦点。
- **Body**：用于账户、邮件正文和普通信息。
- **Label**：用于状态、表头与控件说明，保持紧凑且清楚。
- **Control**：用于按钮、筛选分段与分页操作，保证小尺寸下仍有清晰的点击目标。

## Layout

桌面内容最大宽度为 1160px。标题区先交代账户和当前状态，控制区紧随其后，分页同时出现在列表前后。桌面列表按发件人、主题、日期三列扫读；窄屏取消表头并把每封邮件改为主题、发件人、日期的纵向顺序，避免日期被挤压或横向溢出。

## Elevation & Depth

页面主体依靠画布与内容表面的明度差工作。邮件列表和邮件阅读页只使用一层柔和、向下的环境阴影，避免多重悬浮卡片带来的噪声。

### Shadow Vocabulary

- **内容表面**：`0 18px 42px rgba(31,49,77,.09)`；仅用于完整列表或阅读页与画布分离。

## Shapes

控件采用轻微圆角：常规按钮、输入框和分页按钮为 8px；筛选分段组为 11px。列表本身以直线分隔维持信息密度，不使用圆角包裹每一封邮件。

## Components

### Buttons

- **Shape:** 紧凑圆角（8px），高度 35px。
- **Primary:** 仅用于刷新和恢复操作。
- **Secondary:** 用于分页、跳转和确认每页数量；悬停时边线转为主色。
- **Focus:** 使用明显的主色轮廓，保证键盘操作可见。

### Chips

- **Style:** 当前筛选以低饱和蓝色底搭配主色文字显示。
- **State:** 筛选分段中只有当前视图拥有实心表面与轻微阴影。

### Inputs / Fields

- **Style:** 白色或深色表面、清晰边线、8px 圆角。
- **Focus:** 保持原生可访问性并与按钮边线的主色反馈一致。

### Navigation

- **Style:** 页码状态放在分页器左侧，首页、上一页、跳页、下一页、末页按常见浏览顺序排列。
- **Disabled:** 无可用目标时使用低对比但仍可辨认的静态控件，不伪装成可点击链接。

### 邮件行

- **Style:** 桌面三列、窄屏纵向；主题为最重文字，发件人名称与地址分两层显示。
- **State:** 整行可点击，悬停为轻微主色洗色，键盘聚焦有明确轮廓。

## Do's and Don'ts

### Do:

- **Do** 在列表首屏同时显示筛选、总数、显示范围和当前页。
- **Do** 让所有详情页返回到用户打开前的筛选与分页位置。
- **Do** 在浅色和深色模式中分别验证次级文字与边界的可读性。

### Don't:

- **Don't** 用“每页 100 封”这类静态按钮代替真实的每页数量选择。
- **Don't** 在无下一页或无上一页时保留假装可点击的链接。
- **Don't** 以卡片、图标或装饰性渐变取代邮件内容本身的信息层级。
