# AGENTS.md

## 项目说明

这个项目是 Epic Kiosk 自动领取服务，通过 Docker Compose 部署 `web`、`worker`、`redis` 和多组 WARP 出口容器。

## 技术栈

- Python FastAPI Web 后端，入口为仓库根的 `main.py`（`uvicorn main:app`）。
- Python worker 后台任务，入口为 `worker.py`。
- Playwright / Camoufox 浏览器自动化，核心流程在 `app/deploy.py` 与 `app/services/`。
- Redis 用于任务队列、状态、锁和延迟重试。
- SQLite 数据库位于 `data/kiosk.db`。
- Docker Compose 本地构建镜像：`epic-kiosk-web:local`、`epic-kiosk-worker:local`。

## 常用命令

```bash
cd /path/to/epic-kiosk

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
- `data/trash/`：孤儿浏览器 profile 的回收站，由 web 的 sweep 任务写入，保留 7 天后自动清除。

## 修改规则

- 不要把 API Key、Token、Cookie、账号密码写入文档、提交或日志。
- 修改生产配置、重启容器、删除 profile、删除数据库或清理大量运行数据前，需要先说明范围并确认。
- 从 Windows PowerShell 执行复杂远端命令时，优先使用脚本上传或标准输入，不写多层引号 SSH 单行命令。
- 修改 worker 或自动化流程后，至少运行 `python3 -m py_compile app/services/epic_games_service.py worker.py`。
- 提交前跑一遍单元测试。注意不要用 `docker compose --profile test`：compose 顶层的
  secrets 用了 `${VAR:?}` 必填语法，而 docker compose 会在按 profile 过滤**之前**
  插值整个文件，干净环境下直接报错。改用 worker 镜像跑：

  ```bash
  docker run --rm     -v "$PWD/app:/app/app:ro" -v "$PWD/tests:/app/tests:ro"     --tmpfs /app/data:uid=1002,gid=1002,size=64m     --tmpfs /usr/local/lib/python3.12/site-packages/hcaptcha_challenger/logs:uid=1002,gid=1002,size=16m     --network none -w /app -e PYTHONPATH=/app:/app/app -e DATA_DIR=/app/data     -e EPIC_CREDENTIAL_KEYS=9QKUvZF-uPSzD4suKpprhwUTUyoj5nrR9BgeJeAs5mM=     -e INTERNAL_API_TOKEN=test -e GEMINI_API_KEY=not_used     --entrypoint python3 epic-kiosk-worker:local -m unittest discover -s tests
  ```

  两个坑：测试会 import `app.settings`，它需要 `hcaptcha_challenger`（只在 worker
  镜像里有）；而该包在 import 时就要建日志目录，所以两个 tmpfs 都得给。

  `tests/test_security_queue.py` 是例外 —— 它 import `main`，需要 fastapi，
  而 worker 镜像里没有。这一个用 web 镜像跑：

  ```bash
  docker run --rm     -v "$PWD/app:/app/app:ro" -v "$PWD/tests:/app/tests:ro" -v "$PWD/main.py:/app/main.py:ro"     --tmpfs /app/data:uid=1002,gid=1002,size=64m --network none     -w /app -e PYTHONPATH=/app:/app/app -e DATA_DIR=/app/data     -e EPIC_CREDENTIAL_KEYS=9QKUvZF-uPSzD4suKpprhwUTUyoj5nrR9BgeJeAs5mM=     -e INTERNAL_API_TOKEN=test -e ENABLE_APSCHEDULER=false     --entrypoint python3 epic-kiosk-web:local -m unittest tests.test_security_queue
  ```

  2026-07-27 基线：worker 镜像 75 个用例（除上面那个模块外全绿），
  web 镜像 16 个用例全绿。
- 生产生效需要重建并重启 `web` / `worker`：`docker compose build web worker && docker compose up -d web worker`。

## 验证方式

- `docker compose ps` 确认核心容器运行。
- `docker exec epic-redis redis-cli LLEN task_queue` 确认队列状态。
- `ps -eo pid,ppid,etimes,cmd | grep -Ei 'xvfb-run|app/deploy.py'` 检查是否有残留领取进程。
- `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/` 确认 Web 可用。

## 隐私与私有部署文档

- 禁止提交具体实例的私有化部署、运维、清理、账号、生产路径、生产日志、生产截图、真实域名绑定细节、`.env`、数据库、浏览器 profile 或 WARP 运行数据说明。
- `OPERATIONS.md` 属于私有运维说明，不应进入 Git 跟踪文件；如需保留，只能放在仓库外的私有目录。
- 面向 GitHub 的文档只写通用部署、通用配置和脱敏示例；生产实例专属信息必须放在本机私有知识库或仓库外私有目录。
- 提交前必须检查 `git status --short` 和 `git diff --cached --name-only`，确认没有私有运维文档、运行数据、截图或密钥被暂存。
