# Epic Kiosk Oracle-1 运维说明

## 当前生产模型配置

Oracle-1 当前生产实例使用 NVIDIA OpenAI-compatible API 处理 hCaptcha 视觉识别。

`.env` 中需要保持以下关键配置，禁止把真实 `API_KEY` 写入文档或提交：

```env
API_PROVIDER=nvidia
API_BASE_URL=https://integrate.api.nvidia.com/v1
API_KEY=<从生产 .env 读取，不写入文档>
CAPTCHA_MODEL=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_MODEL_FALLBACK=meta/llama-4-maverick-17b-128e-instruct
COOKIE_INVALID_MAX_RETRIES=1
HCAPTCHA_EXECUTION_TIMEOUT=240
HCAPTCHA_RESPONSE_TIMEOUT=120
HCAPTCHA_PAYLOAD_TIMEOUT=90
CAPTCHA_API_TIMEOUT=60
```

`docker-compose.yml` 已从 `.env` 读取这些变量。切换模型、重建容器或迁移服务时，优先确认 `.env` 是否仍是 NVIDIA 配置。

## 当前 WARP 配置

当前 epic-kiosk 使用独立的单容器多 WARP 出口：

```text
epic-warp:19000-19009
控制接口：http://epic-warp:18080/restart/{idx}
实例数量：10
```

worker 会按账号邮箱稳定选择一个 WARP index，并把 `HTTP_PROXY` / `HTTPS_PROXY` 注入给本次 `app/deploy.py` 子进程。网络超时或驱动断连时优先调用控制接口重启对应 index，而不是重启整个 WARP 容器。

旧的 `epic-warp-2` / `epic-warp-3` 容器已移除；旧数据目录如需保留，应放在 `data/archive/`。

## 当前关键行为

- `/api/deposit` 负责提交账号任务并创建确认 token。
- 前端成功后调用 `/api/confirm_success`，账号才会重新写回 `accounts` 表。
- worker 消费 Redis `task_queue`，执行 `xvfb-run -a python3 app/deploy.py`。
- `cookie_invalid` 会清理该账号浏览器 profile 并立即重试，默认 1 次。
- `network_timeout` / `driver_crash` 会重启 WARP 并延迟重试。
- 单个周免游戏 checkout / hCaptcha 失败时，当前逻辑会记录该游戏失败并继续处理后续游戏。
- 如果一轮中部分游戏成功、部分失败，成功游戏先入库，失败游戏会自动延迟补跑 1 次。

## 重要修复记录

### 登录态确认修复

`app/services/epic_authorization_service.py` 已增加登录成功兜底判断：如果 Epic 登录 API 回调缺失，但页面已进入账号页或导航状态显示已登录，则视为登录成功。

### 账号校验超时修复

账号校验页面从强制 `networkidle` 改为 `domcontentloaded` + 尽力等待，避免 Epic 长连接导致已登录账号被误判为 `unknown`。

### 多周免游戏处理修复

`app/services/epic_games_service.py`：单个游戏结账失败不再中断整轮任务。

`worker.py`：部分成功时先记录成功游戏；失败游戏安排一次验证码类延迟补跑。

## 清理建议

以下路径包含临时测试和调试数据，可以在确认无复盘需求后清理：

```text
data/runtime/manual_login/
data/runtime/login_debug/
data/runtime/checkout_debug/
data/runtime/random_multi_game_validation_20260707/
app/volumes/runtime/login_debug/
```

以下是大体积 profile 备份，删除前必须确认不需要回滚登录态：

```text
data/user_data_backups/
data/user_data/kouyisu8888@gmail.com.before_new_password_20260706-222843/
data/user_data/kouyisu8888@gmail.com.cookie_invalid_backup_20260706-221028/
```

建议保留：

```text
.codex-backups/partial-game-retry-20260707-005010/
.codex-backups/formal-flow-fixes-20260706-233202/
.codex-backups/formal-flow-20260706-231629/kiosk.db
```

## 安全清理命令模板

执行前先确认路径，不要直接复制执行未知路径。生产清理建议先移动到隔离目录，观察服务正常后再删除。

```bash
cd /opt/epic-kiosk
mkdir -p data/archive/cleanup-$(date +%Y%m%d-%H%M%S)
# 示例：mv data/runtime/login_debug data/archive/cleanup-YYYYMMDD-HHMMSS/
```

彻底删除前再次确认：

```bash
docker compose ps
docker exec epic-redis redis-cli LLEN task_queue
ps -eo pid,ppid,etimes,cmd | grep -Ei 'xvfb-run|app/deploy.py' | grep -v grep || true
```
