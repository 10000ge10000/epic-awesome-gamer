# Epic Kiosk

[![Blog](https://img.shields.io/badge/Blog-910501.xyz-orange)](https://blog.910501.xyz/)
[![Bilibili](https://img.shields.io/badge/B%E7%AB%99-59438380-00a1d6?logo=bilibili)](https://space.bilibili.com/59438380)
[![YouTube](https://img.shields.io/badge/YouTube-10000%20AI%20Share-ff0000?logo=youtube&logoColor=white)](https://www.youtube.com/channel/UCqgvZnCN9-9pZcL4SWxmnDw)
![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)
![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-yellow?logo=python)

Epic Kiosk 是一个基于 Docker 的 Epic Games 每周免费游戏自动领取服务，支持多账号托管、验证码自动处理、队列调度和多 WARP 出口。

> 公益站点：[https://epic.910501.xyz/](https://epic.910501.xyz/)

<p align="center">
  <img src="assets/dashboard.png" alt="Epic Kiosk Dashboard" width="100%" style="max-width: 920px;">
</p>

## 特性

- **多账号托管**：在 Web 控制台提交 Epic 邮箱和密码，后续由 Worker 自动处理登录与领取。
- **验证码处理**：通过 OpenAI-compatible API 调用视觉模型处理 hCaptcha，并支持外部验证码服务商兜底。
- **多游戏流程**：本周免费游戏有多个时，单个游戏失败不会中断整轮任务，失败项会延迟补跑。
- **队列调度**：Redis 管理任务队列、任务锁、延迟重试和每日自动调度。
- **多 WARP 出口**：单个 `epic-warp` 容器提供 10 个内部 WARP 实例，Worker 按账号稳定分配代理出口。
- **Docker 部署**：`web`、`worker`、`redis`、`warp` 四个服务通过 Docker Compose 本地构建和运行。

## 快速开始

### 手动部署（推荐）

适合 NVIDIA、SiliconFlow 或其他 OpenAI-compatible API 提供商。

```bash
git clone https://github.com/10000ge10000/epic-kiosk.git
cd epic-kiosk
cp .env.example .env
nano .env
```

推荐生产配置使用 NVIDIA OpenAI-compatible API：

```env
API_PROVIDER=nvidia
API_BASE_URL=https://integrate.api.nvidia.com/v1
API_KEY=<your-nvidia-api-key>
CAPTCHA_MODEL=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_MODEL_FALLBACK=meta/llama-4-maverick-17b-128e-instruct
INTERNAL_API_TOKEN=<run-openssl-rand-hex-32>
```

生成内部接口密钥：

```bash
openssl rand -hex 32
```

启动服务：

```bash
docker compose up -d --build
```

访问控制台：

```text
http://服务器IP:18000
```

### Linux 一键部署

一键脚本适合快速部署，并会交互式写入 `.env`。当前脚本默认按 SiliconFlow 兼容配置引导；如果使用 NVIDIA 或自定义 OpenAI-compatible 服务，建议使用上面的手动部署流程。

```bash
curl -fsSL https://raw.githubusercontent.com/10000ge10000/epic-kiosk/main/install.sh | bash
```

## 配置说明

### API Provider

项目通过 OpenAI-compatible `/v1/chat/completions` 接口调用模型。当前生产实测推荐 NVIDIA，SiliconFlow 和自建兼容网关也可以通过 `.env` 接入。

| Provider | API_BASE_URL | 说明 |
| --- | --- | --- |
| `nvidia` | `https://integrate.api.nvidia.com/v1` | 当前生产推荐，用于 hCaptcha 视觉识别。 |
| `siliconflow` | `https://api.siliconflow.cn/v1` | 兼容部署路径，一键脚本默认引导。 |
| `custom` | 自定义 `/v1` 地址 | 适合自建 OpenAI-compatible 网关。 |

核心环境变量：

```env
API_PROVIDER=nvidia
API_BASE_URL=https://integrate.api.nvidia.com/v1
API_KEY=<your-provider-api-key>
INTERNAL_API_TOKEN=<random-64-character-hex>
CAPTCHA_MODEL=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_MODEL_FALLBACK=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_PROVIDER=none
```

`INTERNAL_API_TOKEN` 只用于 `web` 与 `worker` 的内部接口认证，必须是独立随机值，不能与模型 API Key 共用。

### WARP 出口

当前 Compose 使用单容器多 WARP 架构：

```text
epic-warp:19000-19009
控制接口：http://epic-warp:18080/restart/{idx}
实例数量：10
```

Worker 会根据账号邮箱稳定选择一个 WARP index，并把 `HTTP_PROXY` / `HTTPS_PROXY` 注入本次浏览器任务。网络超时或浏览器驱动异常时，系统优先重启对应 index，而不是重启整个 WARP 容器。

可调恢复参数：

```env
WARP_CONTROL_RESTART_RETRIES=3
WARP_CONTROL_RESTART_BACKOFF_SECONDS=5
WARP_CONTAINER_FALLBACK_RESTARTS=1
```

WARP 控制接口请求会显式绕过 `HTTP_PROXY` / `HTTPS_PROXY`，避免 Worker 调用 `epic-warp:18080` 时被自己的 WARP 代理劫持。当控制接口短暂返回 `503` 时，Worker 会先退避，并同时检查 `/health` 与代理端口连通性；只有单出口恢复失败时，才最多执行一次整容器兜底重启。

### 任务恢复

- 验证码失败：默认延迟 15 分钟重试，最多 2 次。
- 网络超时：默认延迟 10 分钟重试，最多 2 次。
- Cookie 失效：清理该账号浏览器 profile 后立即重试，默认 1 次。
- 多游戏领取：成功游戏先入库，失败游戏进入一次延迟补跑。

## 使用方法

1. 打开 Web 控制台。
2. 输入 Epic 邮箱和密码。
3. 点击「启动引擎」。
4. 系统验证登录、处理验证码，并把账号加入后续定时任务。
5. 在「资产」和「本周免费」页面查看领取记录和当前免费游戏。

删除托管账号时需要重新输入密码确认。删除后会清除数据库记录和对应浏览器 profile。

## 目录结构

```text
epic-kiosk/
├── app/                    # FastAPI 后端、自动化和业务服务
├── templates/              # Web 页面模板
├── data/                   # SQLite、浏览器 profile、图片和日志
├── docs/                   # 部署与模型配置文档
├── worker.py               # Redis 队列 Worker
├── docker-compose.yml      # Docker Compose 编排
├── Dockerfile              # Web 镜像
├── Dockerfile.worker       # Worker 镜像
└── install.sh              # Linux 一键部署脚本
```

## 安全说明

- 为了定时自动登录，Epic 密码会保存在本机 `data/kiosk.db`，不要把 `data/` 同步到公开仓库、公共网盘或不可信备份。
- `.env` 不应提交到 Git；其中包含模型 API Key 和 `INTERNAL_API_TOKEN`。
- 日志、截图、Issue、PR 中不要公开 API Key、Cookie、Token、Epic 密码或完整生产配置。
- 对外开放 Web 控制台前，应额外配置反向代理访问控制、面板鉴权或防火墙白名单。

## 升级

进入你的项目目录后执行：

```bash
git pull
docker compose up -d --build
```

仅重建 Worker：

```bash
docker compose build worker && docker compose up -d worker
```

## 故障排查

查看服务状态：

```bash
docker compose ps
docker exec epic-redis redis-cli LLEN task_queue
```

查看日志：

```bash
docker compose logs --tail=200 worker
docker compose logs --tail=200 web
ls data/logs/
tail -50 data/logs/runtime-$(date +%Y-%m-%d).log
```

常见问题：

- `未配置 API_KEY`：检查 `.env` 是否存在，且 `API_KEY` 不是空值或示例占位符。
- API 返回 `401` / `403`：Key 无效、额度不可用或 Provider 权限不足。
- API 返回 `404`：模型 ID 不存在，或当前账号无权调用该模型。
- 验证码一直失败：先看 Worker 日志中的模型名、API 错误码和 WARP 重启记录，再判断是模型能力、Provider 权限还是 Epic 风控。
- `Requires Base Game`：该免费项是 DLC，需要先拥有基础游戏，系统会跳过或记录失败原因。
- WARP 控制接口短暂 `503`：Worker 调用控制接口时会绕过环境代理，并按 `WARP_CONTROL_RESTART_RETRIES` 和 `WARP_CONTROL_RESTART_BACKOFF_SECONDS` 退避重试，同时检查 `/health` 与代理端口连通性，避免连续重启整个 `epic-warp` 容器。
- 爬虫访问 `robots.txt`、`sitemap.xml`、`llms.txt`、`security.txt`：这些公开元文件会返回 200；明显错误的 CSS `url(...)` 路径会返回 204，以减少无意义 404 日志。

## 相关文档

- [快速开始](docs/QUICKSTART.md)
- [模型配置](docs/MODEL_CONFIG.md)

## 许可证

`pyproject.toml` 声明本项目使用 `GPL-3.0-or-later`。当前仓库尚未包含独立 `LICENSE` 文件，建议后续补充完整许可证文本。

## 致谢

- 原项目：[QIN2DIM/epic-awesome-gamer](https://github.com/QIN2DIM/epic-awesome-gamer)
- OpenAI-compatible Provider：NVIDIA、SiliconFlow 及其他兼容服务

## 免责声明

本项目仅供学习和技术研究使用。请合理使用，遵守 Epic Games 服务条款。开发者不对因使用本项目导致的任何损失承担责任。

*Created by [一万](https://github.com/10000ge10000) | 公益站点：[epic.910501.xyz](https://epic.910501.xyz/)*
