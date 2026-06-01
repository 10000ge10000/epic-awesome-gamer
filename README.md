# Epic Kiosk - 自动驾驶领取系统

![Docker](https://img.shields.io/badge/Docker-Enabled-blue?logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-yellow?logo=python)
![Status](https://img.shields.io/badge/Status-Stable-green)
![License](https://img.shields.io/badge/License-GPL--3.0-blue)

基于 Docker 的 Epic Games 免费游戏自动领取工具，支持多账号托管、智能验证码识别、错峰调度。

> 公益站点：[https://epic.910501.xyz/](https://epic.910501.xyz/)

<p align="center">
  <img src="assets/image_2.png" alt="Epic Kiosk Dashboard" width="100%" style="max-width: 800px;">
</p>

---

## 核心功能

| 功能 | 说明 |
|------|------|
| 自动驾驶 | 一键启动，自动完成登录、验证码识别、免费游戏领取 |
| Cookie 托管 | 首次登录后保存 Cookie，后续尽量复用登录态 |
| AI 验证码 | 使用 SiliconFlow 视觉模型识别 hCaptcha |
| 错峰调度 | 随机延迟执行，降低多账号同时触发风控概率 |
| 防滥用保护 | IP 限流 + 恶意账号检测 |
| 一键部署 | Docker Compose 本地编译，支持 x86_64 / ARM64 |

---

## 快速开始

### 方式一：Linux 一键部署

适用于云服务器、VPS、Linux 主机：

```bash
curl -fsSL https://raw.githubusercontent.com/10000ge10000/epic-kiosk/main/install.sh | bash
```

脚本功能：

- 自动检测系统架构（x86_64 / ARM64）
- 自动安装 Docker 和 Docker Compose
- 交互式引导配置 SiliconFlow API Key
- 自动克隆项目并启动服务

首次部署约需 5-10 分钟。

### 方式二：手动部署

适用于已有 Docker 环境的 Linux / macOS / Windows 主机。

**1. 克隆项目**

```bash
git clone https://github.com/10000ge10000/epic-kiosk.git
cd epic-kiosk
```

**2. 配置 SiliconFlow API Key**

推荐使用 `.env`，不要把真实 key 写进 `docker-compose.yml`：

```bash
cp .env.example .env
nano .env
```

`.env` 示例：

```env
API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
API_BASE_URL=https://api.siliconflow.cn/v1
```

SiliconFlow key 获取地址：

- 推荐使用分享链接：[https://cloud.siliconflow.cn/i/OVI2n57p](https://cloud.siliconflow.cn/i/OVI2n57p)

通过该分享链接注册并完成认证后，邀请双方可获得 16 元代金券。登录 SiliconFlow 后进入 API Key 管理页，创建并复制 `sk-` 开头的 key。

**3. 启动**

```bash
docker compose up -d --build
```

首次启动约需 5-10 分钟。

---

## 部署注意事项

1. 默认 Web 端口是 `18000`，访问地址为 `http://服务器IP:18000`。
2. 默认 AI 提供商是 SiliconFlow，接口地址为 `https://api.siliconflow.cn/v1`。
3. `API_KEY` 必须放在 `.env`，`.env` 已被 `.gitignore` 忽略。
4. 旧版 `API_KEY` / `API_BASE_URL` 仍可兼容读取，但新部署不要再使用旧变量名。
5. 默认 `USE_WARP=true`，WARP 容器负责访问 Epic Games；如果要使用服务器自身 IP，设置 `USE_WARP=false` 后执行 `docker compose up -d --build`。
6. 如果 Epic 风控严重，可能需要更稳定的住宅代理或更换出口。

---

## 使用说明

### 添加账号

1. 输入 Epic 邮箱和密码。
2. 点击「启动引擎」。
3. 系统自动处理登录、验证码和免费游戏领取。

### 查看资产

- 点击「资产清单」Tab 查看已领取游戏。
- 点击游戏封面跳转 Epic 商店。

### 删除账号

- 输入密码后点击红色删除按钮。
- 系统将清除数据库记录和本地 Cookie 数据。

---

## 配置说明

### AI 模型配置

当前默认配置基于 SiliconFlow 实测结果：

| 类型 | 主模型 | 备用模型 | 用途 |
|------|--------|----------|------|
| 主力文本 | `deepseek-ai/DeepSeek-V4-Flash` | `deepseek-ai/DeepSeek-V4-Pro` | 页面判断、流程决策、结构化文本输出 |
| 验证码视觉 | `Qwen/Qwen3-VL-32B-Instruct` | `Qwen/Qwen3-VL-30B-A3B-Instruct` | hCaptcha 图像识别 |

相关环境变量：

```env
API_PROVIDER=siliconflow
API_BASE_URL=https://api.siliconflow.cn/v1
USE_WARP=true
PRIMARY_MODEL=deepseek-ai/DeepSeek-V4-Flash
PRIMARY_MODEL_FALLBACK=deepseek-ai/DeepSeek-V4-Pro
CAPTCHA_MODEL=Qwen/Qwen3-VL-32B-Instruct
CAPTCHA_MODEL_FALLBACK=Qwen/Qwen3-VL-30B-A3B-Instruct
```

### WARP 代理配置

项目默认通过 WARP 代理访问 Epic Games，保持原部署行为不变：

```env
USE_WARP=true
```

如需关闭 WARP 并使用服务器自身 IP 直连，在 `.env` 中设置：

```env
USE_WARP=false
```

修改后重新构建并启动：

```bash
docker compose up -d --build
```

智能切换机制：

- 验证码连续调用超过阈值后自动切换备用视觉模型。
- API 调用异常时自动使用对应备用模型重试一次。
- 视觉请求和文本请求会按是否包含图片自动选择不同模型。
- `USE_WARP=true` 时，验证码失败会触发 WARP 换 IP，并将账号放入延迟队列，默认 15 分钟后重试，最多 2 次。
- `USE_WARP=false` 时，验证码失败只会进入延迟重试，不会重启 WARP。
- Docker 容器日志和应用文件日志均按 1 MB 自动轮转，避免长期运行撑满磁盘。

### 费用与额度

SiliconFlow hosted API 的额度和价格以 SiliconFlow 当前账号页面为准。不同账号、地区、模型和活动政策可能不同，部署前应在 SiliconFlow 控制台确认可用额度。

---

## 项目结构

```text
epic-kiosk/
├── app/                    # 核心代码
│   ├── main.py             # FastAPI 后端
│   ├── deploy.py           # 浏览器自动化
│   ├── settings.py         # 模型/API 配置与 OpenAI 兼容补丁
│   └── services/           # 业务逻辑
├── templates/              # 前端页面
├── data/                   # 持久化数据
│   ├── images/             # 游戏海报
│   ├── user_data/          # 用户 Cookie / 浏览器 profile
│   └── logs/               # 日志文件
├── worker.py               # Redis 队列 Worker
├── docker-compose.yml      # 容器编排
├── install.sh              # 一键部署脚本
├── Dockerfile              # Web 镜像
└── Dockerfile.worker       # Worker 镜像
```

---

## 安全机制

### IP 保护

- 1 分钟内最多 3 次请求。
- 超限后临时封禁 1 小时。
- 同一 IP 提交超过 5 个不同账号将永久封禁。

### 账号保护

- 同一邮箱任务互斥。
- 已存储账号需验证密码。
- 自动清理浏览器缓存。

### Key 安全

- 不要把 `API_KEY` 写进 Git 跟踪文件。
- 不要把 `sk-` key 发到公开聊天、Issue、日志或截图里。
- 如果 key 泄露，应立即在 SiliconFlow 后台删除并重新生成。

---

## 版本升级

已部署用户升级到最新版本：

```bash
cd /epic-kiosk
git pull
docker compose up -d --build
```

仅升级 Worker：

```bash
docker compose build worker && docker compose up -d worker
```

---

## 故障排查

### 常见问题

**Q: 日志提示 `未配置 API_KEY`？**

A: 检查 `.env` 是否存在，且包含 `API_KEY=sk-...`。修改后执行 `docker compose up -d worker`。

**Q: SiliconFlow API 返回 401 / 403？**

A: key 无效、过期、权限不足或账号额度不可用。通过 [SiliconFlow 分享链接](https://cloud.siliconflow.cn/i/OVI2n57p) 登录后进入 API Key 管理页重新生成 key。

**Q: SiliconFlow API 返回 404？**

A: 通常是模型 ID 不存在或该账号无权使用该模型。先通过 [SiliconFlow 分享链接](https://cloud.siliconflow.cn/i/OVI2n57p) 登录控制台确认模型可用。

**Q: 验证码一直失败？**

A: 先检查 worker 日志中是否出现 `调用 OpenAI 兼容 API`、模型名和 API 错误码；如果没有 API 错误，重点排查 WARP 出口 IP、Epic 风控、验证码类型是否变化。

**Q: 按钮显示 `Requires Base Game`？**

A: 该游戏需要先拥有基础游戏，属于 DLC，跳过即可。

**Q: 日志显示「游戏已在库中」？**

A: 该账号已领取过此游戏，正常现象。

### 查看日志

```bash
# Worker 日志（实时）
docker logs epic-worker --tail 50 -f

# 日志文件（按日期分类）
ls data/logs/

# 查看当天运行时日志
cat data/logs/runtime-$(date +%Y-%m-%d).log | tail -50

# 查看当天错误日志
cat data/logs/error-$(date +%Y-%m-%d).log
```

### 重新构建

```bash
# 仅重新构建 Worker
docker compose build worker && docker compose up -d worker

# 重新构建所有服务
docker compose build --no-cache && docker compose up -d
```

---

## 相关文档

- [快速开始指南](docs/QUICKSTART.md)
- [模型配置说明](docs/MODEL_CONFIG.md)

> 注意：部分旧文档可能仍保留历史 SiliconFlow 配置说明，当前 README 和 `.env.example` 以 SiliconFlow 配置为准。

---

## 致谢

- 原项目：[QIN2DIM/epic-awesome-gamer](https://github.com/QIN2DIM/epic-awesome-gamer)
- AI 服务：[SiliconFlow 分享链接](https://cloud.siliconflow.cn/i/OVI2n57p)

---

## 免责声明

本项目仅供学习和技术研究使用。请合理使用，遵守 Epic Games 服务条款。开发者不对因使用本项目导致的任何损失承担责任。

---

*Created by [一万](https://github.com/10000ge10000) | 公益站点：[epic.910501.xyz](https://epic.910501.xyz/)*
