# Epic Kiosk / Epic 自动驾驶设计系统

> **V4：Horizon Mission Control（2026-07-20）**。这是当前线上首页的设计基线：明亮、精确的蓝青科技界面，借鉴成熟工具产品的层级和节奏，不复制任何具体网站。禁止以黑色模块、霓虹、粒子、玻璃拟态或蓝紫渐变作为主视觉。

## 1. 产品定位

- **产品类型**：自动领取 Epic 周免、展示任务状态、查询游戏资产的账号托管工具。
- **目标用户**：希望稳定完成周免领取、能看懂任务状态并随时查阅资产的 Epic 玩家。
- **核心任务**：提交托管账号、跟踪三阶段领取任务、处理人工验证、查询已入库与当周游戏。
- **体验原则**：游戏封面提供情绪，运行状态提供信任，账户操作保持克制；信息密度优先于装饰。

## 2. 视觉方向与信息架构

- 顶栏、系统状态轨、任务地平线、账号通行证和任务信号舱构成控制台主路径。
- 首屏使用深蓝“任务地平线”叙事层；浮起的白色操作面板承载真实输入和任务状态。
- 资产与周免采用统一的封面书架和工具条，不使用两套风格的游戏卡片。
- 页脚是深蓝“系统终点站”，而不是黑色模块：渠道区固定为桌面与移动端均可用的 2×2 网格，链接由图标、文字、平台标签和箭头组成。

## 3. 设计令牌

```css
:root {
  --ek-canvas: #F7FAFF;
  --ek-surface: #FFFFFF;
  --ek-surface-soft: #EDF5FF;
  --ek-surface-tint: #F0F7FF;
  --ek-horizon: #154A9A;
  --ek-footer: #133F87;
  --ek-text: #15375F;
  --ek-text-muted: #607B9D;
  --ek-text-subtle: #8BA6C5;
  --ek-border: #D4E1F1;
  --ek-border-light: #E2ECF7;
  --ek-primary: #1359E8;
  --ek-primary-hover: #0F48BD;
  --ek-signal: #45D5F0;
  --ek-success: #14B88E;
  --ek-warning: #EC9B2D;
  --ek-danger: #DE5B73;
  --ek-focus: #276DFF;
  --ek-radius-sm: 10px;
  --ek-radius-md: 14px;
  --ek-radius-lg: 20px;
  --ek-radius-xl: 28px;
  --ek-shadow-card: 0 18px 40px rgb(32 84 152 / 0.13);
  --ek-shadow-float: 0 22px 48px rgb(18 66 137 / 0.20);
}
```

状态不可只依赖色彩：必须同时使用文字、状态点、图标或步骤位置。

## 4. 字体与排版

- 界面：`Space Grotesk, PingFang SC, Microsoft YaHei, system-ui, sans-serif`。
- 状态、日期、日志与技术标识：`DM Mono, JetBrains Mono, ui-monospace, monospace`。
- 页面主标题：`clamp(38px, 5vw, 66px) / 1.03`；面板标题 22–26px；分区标题 16px。
- 正文 14–15px；辅助文字 11–13px；输入与按钮文本不低于 16px 的可读规格。
- 数值使用等宽数字；游戏标题最多两行截断，日志与任务信息优先换行或滚动。

## 5. 布局、间距与圆角

- 4px 基础间距：4、8、12、16、18、22、24、32、40、48、56、64。
- 页面左右内边距：桌面 32px，平板 24px，手机 16px；V4 首屏桌面内边距 56px、手机 22px。
- 控制台桌面为约 2:1 双栏；在 1050px 以下变为纵向；任务面板和表单保持清晰阅读顺序。
- 普通输入和按钮使用 10px 圆角；游戏卡使用 14px；浮起工作台使用 20px；地平线与页脚用 28px 顶角。
- 交互目标高度不低于 44px；关键按钮和输入在实现中为 48px 或以上。

## 6. 组件规范

### 导航和系统状态

- 顶栏包含产品身份、三个功能 Tab 和服务状态；激活 Tab 使用浅蓝底与蓝青状态点，不做强发光。
- Tab 保持 `role=tab`、`aria-selected` 与 `aria-controls`，支持键盘方向键与 Enter/Space。
- 公告为可关闭的低高度提示，不占据主操作区域。

### 按钮和输入

- 主按钮为实心 `--ek-primary`，悬浮使用 `--ek-primary-hover`；危险操作为次级危险样式并保留二次确认。
- 次级按钮使用白色或透明底加细边框；不使用无意义的渐变文字或大面积发光。
- 输入框采用白底、细蓝灰边框、可见 label、11px 以上辅助说明；密码显示按钮保留独立的 44px 以上点击区且不遮挡内容。
- 运行中禁用重复提交，并保留原按钮语义与明确的进行中反馈。

### 卡片、任务和日志

- 账户通行证为白色实体面板；任务信号舱使用浅蓝背景和顶部信号线；二者共享边框、圆角和蓝色阴影语言。
- 游戏资产和周免统一封面比例、边框、标题和 hover 位移；图片加载前保留固定比例以避免布局抖动。
- 日志终端在 V4 中为白色可滚动容器、等宽字体和明确行高，不再使用黑色“伪终端”模块；仅任务摘要使用 `aria-live=polite`，避免逐条日志打断读屏。

### 弹窗和 Toast

- 弹窗最大高度不超过 `calc(100dvh - 40px)`，支持 ESC；非关键流程可以点遮罩关闭，关键与危险流程不可误关。
- 弹窗使用白底、蓝色边框与柔和阴影；焦点进入时可见，关闭后回到触发按钮。
- Toast 桌面位于右下、手机位于安全区域；成功、警告、错误和信息均有状态点、文字与关闭按钮。

### 页脚渠道

- 四个渠道固定为 2 列 × 2 行，链接包含语义 SVG 图标与中文标题；桌面显示平台标签和外链箭头。
- 手机上保留图标与中文标题，隐藏非必要的平台缩写及箭头，以保证两列都拥有 44px 以上点击空间。

## 7. 响应式规范

| 宽度 | 行为 |
| --- | --- |
| >= 1280px | 宽容器、四项系统指标、双栏工作台、资产 5 列以上 |
| 1024–1279px | 双栏工作台、四项指标、资产 4 列 |
| 768–1023px | 工作台纵向、资产 3–4 列，导航完整保留 |
| 430–767px | 16px 页面边距、两列指标、两列游戏卡、查询栏堆叠 |
| < 430px | 一列输入表单、极窄游戏卡可降一列；仅日志等技术内容允许内部滚动 |

至少检查 1440×900、1280×800、1024×768、768×1024、430×932、390×844、360×800，并满足 `document.documentElement.scrollWidth <= innerWidth`。

## 8. 动效规范

- 任务地平线轨道仅做 18 秒小角度旋转；信号点 2.8 秒呼吸；系统轨道为低频扫描线。
- Tab 内容进入 420ms，模态进入 280ms，卡片 hover 仅上移 5px；只使用 `transform` 与 `opacity`。
- 禁止滚动劫持、自动播放视频、持续大范围背景动画、夸张缩放和无意义的悬浮。
- `prefers-reduced-motion: reduce` 下关闭非必要动画和扫描效果。

## 9. 无障碍和安全文案

- 普通文字与背景至少满足 4.5:1 对比度；焦点环为 3px 蓝色，不得移除。
- 具备跳至主内容链接、可见 label、键盘导航、禁用态、加载态、空态与错误态。
- 安全说明只陈述可由代码与业务证明的事实：建议使用专门账号、关闭两步验证会降低安全性、公共设备不要记住邮箱、删除托管数据不会删除 Epic 账号。
- 不宣称密码绝不保存、数据绝不上传或服务百分之百安全。

## 10. 不变功能契约

页面视觉重构不改变以下契约：

- 接口与认证：`GET /api/system_stats`、`GET /api/free_games`、`POST /api/deposit`、`POST /api/session`、`POST /api/delete_account`、`GET /api/tasks/{task_id}`、`POST /api/confirm_success`、`POST /api/query`，以及 Bearer Token 和既有响应处理。
- 核心函数：`closeAnnouncement`、`showToast`、`escapeHtml`、`safeHttpUrl`、`loadRememberedEmail`、`saveRememberedEmail`、`fetchSystemStats`、`switchTab`、`fetchFreeGames`、`togglePwd`、`startVerify`、`deleteAccount`、`checkStatus`、`resetUI`、`showModal`、`closeModal`、`showErrorModal`、`closeErrorModal`、`doQuery`。
- DOM ID、Jinja2 raw 边界、Axios、任务轮询、成功确认、LocalStorage 记住邮箱、URL 白名单、IP 封禁提示、删除二次确认和初始化行为。
