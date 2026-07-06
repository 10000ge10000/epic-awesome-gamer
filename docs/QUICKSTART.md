# 快速开始

本文面向首次部署用户。Epic Kiosk 通过 Docker Compose 运行 `web`、`worker`、`redis` 和 `warp` 服务。

## 前置条件

- Linux 服务器、VPS 或 NAS 主机。
- 已安装 Docker 和 Docker Compose。
- 准备一个 OpenAI-compatible API Key，用于验证码视觉识别。

## 方式一：手动部署（推荐）

### 1. 克隆项目

```bash
git clone https://github.com/10000ge10000/epic-kiosk.git
cd epic-kiosk
```

### 2. 创建 `.env`

```bash
cp .env.example .env
nano .env
```

推荐生产配置使用 NVIDIA：

```env
API_PROVIDER=nvidia
API_BASE_URL=https://integrate.api.nvidia.com/v1
API_KEY=<your-nvidia-api-key>
CAPTCHA_MODEL=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_MODEL_FALLBACK=meta/llama-4-maverick-17b-128e-instruct
```

如果使用 SiliconFlow：

```env
API_PROVIDER=siliconflow
API_BASE_URL=https://api.siliconflow.cn/v1
API_KEY=<your-siliconflow-api-key>
CAPTCHA_MODEL=Qwen/Qwen3-VL-32B-Instruct
CAPTCHA_MODEL_FALLBACK=Qwen/Qwen3-VL-30B-A3B-Instruct
```

生成内部接口密钥，并写入 `.env`：

```bash
openssl rand -hex 32
```

```env
INTERNAL_API_TOKEN=<random-64-character-hex>
```

### 3. 构建并启动

```bash
docker compose up -d --build
```

首次构建会下载基础镜像和浏览器依赖，通常需要数分钟。

### 4. 访问控制台

```text
http://服务器IP:18000
```

在 Web 页面提交 Epic 邮箱和密码，系统会验证登录、处理验证码，并将账号加入后续定时领取流程。

## 方式二：Linux 一键部署

一键脚本会自动安装依赖、克隆仓库、创建 `.env` 并启动服务。当前交互式流程默认按 SiliconFlow 兼容配置引导。

```bash
curl -fsSL https://raw.githubusercontent.com/10000ge10000/epic-kiosk/main/install.sh | bash
```

如果你要使用 NVIDIA 或自定义 Provider，建议使用手动部署，直接编辑 `.env`。

## 验证部署

```bash
docker compose ps
docker exec epic-redis redis-cli LLEN task_queue
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/
```

预期结果：

- `epic-web`、`epic-worker`、`epic-redis`、`epic-warp` 均处于运行状态。
- Web HTTP 状态码为 `200`。
- 新部署时队列长度通常为 `0`。

## 查看日志

```bash
docker compose logs --tail=200 worker
docker compose logs --tail=200 web
```

日志文件位于：

```text
data/logs/
```

## 更新项目

```bash
cd /opt/epic-kiosk   # 如果不是一键脚本部署，请进入你的实际项目目录
git pull
docker compose up -d --build
```

仅更新 Worker：

```bash
docker compose build worker && docker compose up -d worker
```

## 停止服务

```bash
docker compose down
```

该命令不会删除 `data/`。不要在不了解影响时执行 `docker compose down -v`。
