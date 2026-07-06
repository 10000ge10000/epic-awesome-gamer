# AGENTS.md

## 项目说明

这个项目是 Epic Kiosk 自动领取服务，运行在 Oracle-1 的 `/opt/epic-kiosk`，通过 Docker Compose 部署 `web`、`worker`、`redis` 和多组 WARP 出口容器。

## 技术栈

- Python FastAPI Web 后端，入口为 `app/main.py`。
- Python worker 后台任务，入口为 `worker.py`。
- Playwright / Camoufox 浏览器自动化，核心流程在 `app/deploy.py` 与 `app/services/`。
- Redis 用于任务队列、状态、锁和延迟重试。
- SQLite 数据库位于 `data/kiosk.db`。
- Docker Compose 本地构建镜像：`epic-kiosk-web:local`、`epic-kiosk-worker:local`。

## 常用命令

```bash
cd /opt/epic-kiosk

docker compose ps
docker compose logs --tail=200 worker
docker compose logs --tail=200 web
python3 -m py_compile app/services/epic_games_service.py worker.py

docker compose build web worker
docker compose up -d web worker
```

## 目录约定

- `app/`：后端、自动化和业务逻辑。
- `templates/`：Web 前端模板。
- `worker.py`：Redis 队列消费、状态写入、失败重试和游戏记录入库。
- `data/kiosk.db`：账号和领取记录数据库。
- `data/user_data/`：账号浏览器 profile，包含登录态，清理前必须确认影响。
- `data/runtime/`：测试、验证码、临时验证输出，可按保留策略清理。
- `data/logs/`：应用运行日志。
- `.codex-backups/`：Codex 修改前备份，不要随意删除最新备份。

## 修改规则

- 不要把 API Key、Token、Cookie、账号密码写入文档、提交或日志。
- 修改生产配置、重启容器、删除 profile、删除数据库或清理大量运行数据前，需要先说明范围并确认。
- PowerShell 到 Oracle-1 的复杂远端命令优先使用脚本上传或 heredoc，不写多层引号 SSH 单行命令。
- 修改 worker 或自动化流程后，至少运行 `python3 -m py_compile app/services/epic_games_service.py worker.py`。
- 生产生效需要重建并重启 `web` / `worker`：`docker compose build web worker && docker compose up -d web worker`。

## 验证方式

- `docker compose ps` 确认核心容器运行。
- `docker exec epic-redis redis-cli LLEN task_queue` 确认队列状态。
- `ps -eo pid,ppid,etimes,cmd | grep -Ei 'xvfb-run|app/deploy.py'` 检查是否有残留领取进程。
- `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/` 确认 Web 可用。

## 隐私与私有部署文档

- 禁止提交 Oracle-1 私有化部署、运维、清理、账号、生产路径、生产日志、生产截图、真实域名绑定细节、`.env`、数据库、浏览器 profile 或 WARP 运行数据说明。
- `OPERATIONS.md` 属于私有运维说明，不应进入 Git 跟踪文件；如需保留，只能放在仓库外目录，例如 `/opt/epic-kiosk-private/OPERATIONS.md`。
- 面向 GitHub 的文档只写通用部署、通用配置和脱敏示例；生产实例专属信息必须放在本机私有知识库或仓库外私有目录。
- 提交前必须检查 `git status --short` 和 `git diff --cached --name-only`，确认没有私有运维文档、运行数据、截图或密钥被暂存。
