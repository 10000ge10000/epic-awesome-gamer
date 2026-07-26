import os
import asyncio
import json
import sqlite3
import redis
import shutil
import httpx
import re
import secrets
import ipaddress
import uuid
from pathlib import Path
from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.utils import cleanup_debug_artifacts, cleanup_old_logs
from app.secure_store import (
    CredentialCipher,
    CredentialError,
    account_for_token,
    account_ref,
    cancel_obsolete_scheduled_runs,
    canonicalize_promotion_games,
    confirm_pending_account,
    connect,
    create_cycle_assignments,
    create_task_run,
    delete_account_record,
    ensure_schema,
    get_account,
    get_active_promotion_cycle,
    issue_account_token,
    normalize_email,
    read_secret,
    record_game_result,
    safe_profile_path,
    scheduled_cycle_runs,
    set_active_promotion_cycle,
    task_for_token,
    update_task,
    verify_account_password,
)

# 站点经反代对公网开放，交互式文档会把包含 /api/nuke_account、/api/admin/* 在内的
# 完整内部 API 契约发布出去（这些端点虽有 token 保护，但没有公布 schema 的必要）。
# 本地调试可用 EXPOSE_DOCS=1 临时打开。
_EXPOSE_DOCS = os.getenv("EXPOSE_DOCS", "0") == "1"
app = FastAPI(
    docs_url="/docs" if _EXPOSE_DOCS else None,
    redoc_url="/redoc" if _EXPOSE_DOCS else None,
    openapi_url="/openapi.json" if _EXPOSE_DOCS else None,
)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """剥掉校验错误里的 input 字段。

    Pydantic v2 + FastAPI 默认会把用户提交的原值原样放进 422 响应体。
    Account.password 有 len>512 的校验，一旦触发，用户的明文密码就会随响应
    回到 nginx access log、Cloudflare 日志和浏览器 devtools 里。
    这里只保留定位问题所需的 loc/msg/type。
    """
    safe = [
        {k: v for k, v in error.items() if k in {"loc", "msg", "type"}}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe})
templates = Jinja2Templates(directory="templates")

# 1. 挂载与路径
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

DB_PATH = os.path.join(DATA_DIR, "kiosk.db")
INTERNAL_API_TOKEN = read_secret("INTERNAL_API_TOKEN", "INTERNAL_API_TOKEN_FILE")
TASK_LOCK_SECONDS = int(os.getenv("TASK_LOCK_SECONDS", "9000"))
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "https://epic.910501.xyz").rstrip("/")
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "false").lower() in {"1", "true", "yes", "on"}

# 历史数据偏移量配置（通过环境变量设置，默认为 0）
# 用于补偿因入库 API 失效丢失的历史记录
# 其他用户部署时默认为 0，不影响其数据显示
CLAIM_HISTORY_OFFSET = int(os.getenv("CLAIM_HISTORY_OFFSET", "0"))
ACCOUNT_VERIFIED_OFFSET = int(os.getenv("ACCOUNT_VERIFIED_OFFSET", "0"))
ACCOUNT_TOTAL_OFFSET = int(os.getenv("ACCOUNT_TOTAL_OFFSET", "0"))
USER_DATA_DIR = os.path.join(DATA_DIR, "user_data")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)
ENABLE_APSCHEDULER = os.getenv("ENABLE_APSCHEDULER", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SCHEDULER_INSTANCE_ID = os.getenv("HOSTNAME", "web")
CLAIM_BATCH_SIZE = max(1, int(os.getenv("CLAIM_BATCH_SIZE", "5")))
CLAIM_BATCH_INTERVAL_SECONDS = max(
    0, int(os.getenv("CLAIM_BATCH_INTERVAL_SECONDS", "600"))
)
PROMOTION_REFRESH_INTERVAL_SECONDS = max(
    300, int(os.getenv("PROMOTION_REFRESH_INTERVAL_SECONDS", "3600"))
)
SCHEDULED_TASK_QUEUE = "task_scheduled_queue"
# 读接口每 IP 每分钟配额。前端正常节奏是 system_stats 每 30 秒一次 +
# 任务进行中 tasks/{id} 每 1.5 秒一次，一分钟约 42 次，留足余量。
API_READ_RATE_PER_MINUTE = max(1, int(os.getenv("API_READ_RATE_PER_MINUTE", "120")))

# 2. Redis
redis_host = os.getenv("REDIS_HOST", "localhost")
r = redis.Redis(host=redis_host, port=6379, decode_responses=True)

def init_db():
    ensure_schema(DB_PATH)


init_db()

# Models
class Account(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value or len(value) > 512:
            raise ValueError("Invalid password")
        return value


class TaskRequest(BaseModel):
    task_id: str

class NukeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class UnbanRequest(BaseModel):
    ip: str

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        return str(ipaddress.ip_address(value.strip()))

class GameLog(BaseModel):
    email: str
    game_title: str
    image_filename: str
    run_id: str | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


def get_cipher() -> CredentialCipher:
    try:
        return CredentialCipher.from_environment()
    except (CredentialError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Credential storage is unavailable") from exc


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token is required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token is required")
    return token


def _require_internal_token(authorization: str | None) -> None:
    """Worker-only APIs fail closed unless a shared token is configured."""
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="Internal API token is not configured")
    expected = f"Bearer {INTERNAL_API_TOKEN}"
    # compare_digest 对含非 ASCII 字符的 str 会抛 TypeError，而 Authorization 头
    # 完全由调用方控制 —— 线上日志里已出现过因此返回 500 的 traceback。
    # 先编码成 bytes 再比较，让这种请求正常落到 401。
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        supplied = authorization.encode("utf-8")
    except (UnicodeError, AttributeError):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(supplied, expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _enqueue_task(run_id: str, email: str) -> bool:
    """Atomically deduplicate queued/running work for one account."""
    ref = account_ref(email)
    lock_key = f"task_lock:{ref}"
    if not r.set(lock_key, "queued", nx=True, ex=TASK_LOCK_SECONDS):
        return False
    try:
        r.rpush("task_queue", json.dumps({"run_id": run_id}, ensure_ascii=True))
        return True
    except Exception:
        r.delete(lock_key)
        raise

# --- 🛡️ 防滥用中间件 (多层防护) ---
@app.middleware("http")
async def anti_abuse_middleware(request: Request, call_next):
    path = request.url.path.lower()
    sensitive_prefixes = ("/.env", "/.git", "/data", "/app/volumes")
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in sensitive_prefixes):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    # Some crawlers incorrectly request CSS url(...) fragments as paths.
    # Return an empty success response to reduce noisy 404 logs without affecting the UI.
    if path.startswith("/url(") or path.startswith("/url%28"):
        return Response(status_code=204)

    if MAINTENANCE_MODE and path.startswith("/api/") and path not in {
        "/api/system_stats",
        "/api/free_games",
        "/api/admin/metrics",
        "/api/admin/unban",
    }:
        return JSONResponse(
            status_code=503,
            content={"status": "maintenance", "msg": "系统维护中，请稍后重试"},
        )

    # 未鉴权的读接口也要有节流。/api/system_stats 会跑 5 条 SQL，
    # 线上单 IP 已经打进去 7554 次，而此前限流只覆盖下面两个写入口。
    # 读接口用宽松得多的配额，只挡住明显的滥用。
    if path.startswith("/api/") and request.url.path not in {"/api/deposit", "/api/session"}:
        read_key = f"rate_read:{request.client.host}"
        read_count = r.incr(read_key)
        if read_count == 1:
            r.expire(read_key, 60)
        if read_count > API_READ_RATE_PER_MINUTE:
            return JSONResponse(
                status_code=429,
                content={"status": "rate_limited", "msg": "⏳ 请求过于频繁，请稍后重试"},
            )

    # 对提交任务和密码换取会话令牌的入口统一限流。
    if request.url.path in {"/api/deposit", "/api/session"} and request.method == "POST":
        client_ip = request.client.host

        # 1. 检查是否被长期封禁（最长 7 天）
        ban_key = f"ban:{client_ip}"
        legacy_ban_key = f"perm_ban:{client_ip}"
        if r.exists(ban_key) or r.exists(legacy_ban_key):
            return JSONResponse(
                status_code=403,
                content={"status": "banned", "msg": "🚫 此 IP 因滥用已被限期封禁"}
            )

        # 2. 检查是否在临时封禁中（1小时）
        temp_ban_key = f"temp_ban:{client_ip}"
        ban_ttl = r.ttl(temp_ban_key)
        if ban_ttl > 0:
            mins = ban_ttl // 60
            return JSONResponse(
                status_code=429,
                content={"status": "rate_limited", "msg": f"⏳ 操作过于频繁，请 {mins} 分钟后重试"}
            )

        # 3. 限流：1分钟内最多3次
        rate_key = f"rate:{client_ip}"
        current_count = r.incr(rate_key)
        if current_count == 1:
            r.expire(rate_key, 60)

        if current_count > 3:
            r.setex(temp_ban_key, 3600, "1")  # 1小时临时封禁
            return JSONResponse(
                status_code=429,
                content={"status": "rate_limited", "msg": "⏳ 操作过于频繁，请 1 小时后重试"}
            )

    response = await call_next(request)
    return response


# --- Public crawler metadata endpoints ---
def _public_url(path: str = "") -> str:
    if not path:
        return PUBLIC_SITE_URL
    return f"{PUBLIC_SITE_URL}/{path.lstrip('/')}"


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /data/",
        "Disallow: /.env",
        "Disallow: /.git",
        "Disallow: /app/volumes/",
        f"Sitemap: {_public_url('sitemap.xml')}",
        "",
    ])
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml")
async def sitemap_xml():
    today = datetime.utcnow().date().isoformat()
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{_public_url('/')}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.7</priority>
  </url>
</urlset>
'''
    return Response(content=content, media_type="application/xml; charset=utf-8")


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    content = f'''# Epic Kiosk

Epic Kiosk is a public web console for an open-source Epic Games weekly free-game automation project.

Public links:
- Site: {_public_url('/')}
- GitHub: https://github.com/10000ge10000/epic-kiosk
- Blog: https://blog.910501.xyz/

Crawler notes:
- Public page: /
- API, runtime data, private configuration, and repository internals are not intended for crawling.
'''
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@app.get("/security.txt", response_class=PlainTextResponse)
@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
async def security_txt():
    expires = (datetime.utcnow() + timedelta(days=365)).date().isoformat()
    content = "\n".join([
        "Contact: https://github.com/10000ge10000/epic-kiosk/issues",
        "Preferred-Languages: zh, en",
        f"Canonical: {_public_url('.well-known/security.txt')}",
        f"Expires: {expires}T00:00:00Z",
        "",
    ])
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")

# --- 🛠️ 内部工具函数：物理删除逻辑 ---
def _perform_physical_delete(email: str) -> str:
    """只删除数据库已绑定的随机 profile_id，绝不使用用户输入拼接路径。"""
    email = normalize_email(email)
    account = get_account(DB_PATH, email)
    if not account:
        raise KeyError("Account does not exist")

    profile_id = account["profile_id"] or ""
    if profile_id:
        target_dir = safe_profile_path(USER_DATA_DIR, profile_id)
        if target_dir.exists():
            if target_dir.is_symlink():
                raise ValueError("Profile path must not be a symlink")
            shutil.rmtree(target_dir)

    delete_account_record(DB_PATH, email)
    ref = account_ref(email)
    r.delete(f"task_lock:{ref}", f"retry_pending:{ref}")
    return "账号记录和浏览器登录态已删除"

# --- API 接口 ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # Starlette 1.0.0 新签名：request 作为第一个参数
    return templates.TemplateResponse(request, "index.html")


@app.get("/preview/v4", response_class=HTMLResponse)
async def read_v4_preview(request: Request):
    """Serve the current V4 design at its explicit preview route."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/health/live")
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    try:
        r.ping()
        with connect(DB_PATH) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Dependency check failed") from exc
    return {"status": "ready"}

@app.post("/api/deposit")
def deposit(account: Account, request: Request):
    """
    提交任务接口

    安全机制：
    1. 检查是否有正在执行的任务
    2. 验证已存储账号的密码（防止恶意覆盖）
    3. 记录 IP 提交的账号数量（防止恶意刷量）
    """
    email = normalize_email(account.email)
    password = account.password
    client_ip = request.client.host
    cipher = get_cipher()

    # 1. 检查是否正在处理中
    if r.exists(f"task_lock:{account_ref(email)}"):
        return {"status": "busy", "msg": "⏳ 该账号有任务正在执行中，请稍后再试"}

    # 2. 如果是已存储账号，验证密码
    row = get_account(DB_PATH, email)
    if row and not verify_account_password(DB_PATH, cipher, email, password):
        return {"status": "auth_failed", "msg": "❌ 密码错误，无法操作此账号"}

    # 3. 记录 IP 提交的账号（用于检测恶意刷量）
    ip_accounts_key = f"ip_accounts:{client_ip}"
    ip_accounts = r.smembers(ip_accounts_key)
    ref = account_ref(email)

    if ref not in ip_accounts:
        # 新账号
        r.sadd(ip_accounts_key, ref)
        r.expire(ip_accounts_key, 86400 * 7)  # 7天过期

        # 如果同一IP提交超过5个不同账号，永久封禁
        if len(ip_accounts) >= 5:
            r.setex(f"ban:{client_ip}", 86400 * 7, "1")
            return {"status": "banned", "msg": "🚫 检测到异常行为，此 IP 已被封禁"}

    # 3. 提交任务
    run_id, access_token = create_task_run(
        DB_PATH, cipher, email, "verify", password=password
    )
    if not _enqueue_task(run_id, email):
        update_task(DB_PATH, run_id, state="failed", error_type="duplicate_task")
        return {"status": "busy", "msg": "⏳ 该账号有任务正在执行中，请稍后再试"}
    return {
        "status": "queued",
        "msg": "✅ 任务已加入队列",
        "task_id": run_id,
        "access_token": access_token,
    }

@app.post("/api/delete_account")
def delete_account(authorization: str | None = Header(default=None)):
    token = _bearer_token(authorization)
    account = account_for_token(DB_PATH, token)
    if not account:
        raise HTTPException(status_code=401, detail="Invalid account token")
    try:
        msg = _perform_physical_delete(account["email"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Account does not exist") from exc
    return {"status": "success", "msg": msg}

# Worker 专用的核弹接口（无需密码，直接销毁）
@app.post("/api/nuke_account")
def nuke_account(req: NukeRequest, authorization: str | None = Header(default=None)):
    _require_internal_token(authorization)
    print(f"Worker requested invalid-account deletion: account={account_ref(req.email)}")
    try:
        msg = _perform_physical_delete(req.email)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Account does not exist") from exc
    return {"status": "success", "msg": msg}


@app.post("/api/admin/unban")
def unban_ip(req: UnbanRequest, authorization: str | None = Header(default=None)):
    _require_internal_token(authorization)
    removed = r.delete(
        f"ban:{req.ip}",
        f"perm_ban:{req.ip}",
        f"temp_ban:{req.ip}",
        f"rate:{req.ip}",
        f"ip_accounts:{req.ip}",
    )
    return {"status": "success", "removed_keys": removed}


@app.get("/api/admin/metrics")
def admin_metrics(authorization: str | None = Header(default=None)):
    _require_internal_token(authorization)
    with connect(DB_PATH) as conn:
        task_states = {
            row["state"]: row["count"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS count FROM task_runs GROUP BY state"
            )
        }
        task_errors = {
            row["error_type"]: row["count"]
            for row in conn.execute(
                """
                SELECT error_type, COUNT(*) AS count
                FROM task_runs
                WHERE error_type IS NOT NULL
                GROUP BY error_type
                """
            )
        }
        game_results = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM task_game_results GROUP BY status"
            )
        }
        oldest = conn.execute(
            """
            SELECT CAST((julianday('now') - julianday(MIN(created_at))) * 86400 AS INTEGER)
            FROM task_runs
            WHERE state IN ('queued', 'running', 'deferred')
            """
        ).fetchone()[0]

    provider_metrics = {}
    for key in r.scan_iter(match="metrics:provider:*"):
        provider = key.rsplit(":", 1)[-1]
        provider_metrics[provider] = r.hgetall(key)
        provider_metrics[provider]["circuit_open"] = bool(
            r.exists(f"metrics:provider_circuit:{provider}")
        )
    # warp:health:* 由 probe_warp_egresses 每轮写入且不设 TTL，任务空窗期也有数据；
    # metrics:warp:* 只在 worker 跑任务时写、TTL 仅 300 秒，作为补充信息合并进来。
    warp_metrics = {}
    for index in range(WARP_PROXY_COUNT):
        entry = {}
        raw_health = r.get(f"warp:health:{index}")
        if raw_health:
            with suppress(ValueError, TypeError):
                entry.update(json.loads(raw_health))
        raw_task = r.get(f"metrics:warp:{index}")
        if raw_task:
            with suppress(ValueError, TypeError):
                entry["last_task"] = json.loads(raw_task)
        if entry:
            warp_metrics[str(index)] = entry
    warp_summary = {}
    raw_summary = r.get("warp:health:summary")
    if raw_summary:
        with suppress(ValueError, TypeError):
            warp_summary = json.loads(raw_summary)

    heartbeat = r.get("worker:heartbeat")
    try:
        heartbeat_age = (
            max(0, int(datetime.now().timestamp() - float(heartbeat))) if heartbeat else None
        )
    except (TypeError, ValueError):
        heartbeat_age = None
    disk = shutil.disk_usage(DATA_DIR)
    disk_percent = round(disk.used * 100 / disk.total, 2) if disk.total else 0
    return {
        "queue": {
            "ready": r.llen("task_queue"),
            "retry": r.zcard("task_retry_queue"),
            "oldest_active_seconds": oldest,
        },
        "tasks": {"states": task_states, "errors": task_errors},
        "games": game_results,
        "providers": provider_metrics,
        "warp": warp_metrics,
        "warp_summary": warp_summary,
        "worker_heartbeat_age_seconds": heartbeat_age,
        "disk_used_percent": disk_percent,
    }

@app.get("/api/status/{email}")
async def get_status(email: str):
    raise HTTPException(status_code=410, detail="Use /api/tasks/{task_id}")


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, authorization: str | None = Header(default=None)):
    token = _bearer_token(authorization)
    row = task_for_token(DB_PATH, task_id, token)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid task token")
    with connect(DB_PATH) as conn:
        games = [
            {
                "title": game["game_title"],
                "status": game["status"],
                "updated_at": game["updated_at"],
            }
            for game in conn.execute(
                """
                SELECT game_title, status, updated_at
                FROM task_game_results
                WHERE run_id=?
                ORDER BY game_title
                """,
                (task_id,),
            )
        ]
    return {
        "status": row["state"],
        "msg": row["status_message"] or "Waiting...",
        "result": row["state"],
        "error_type": row["error_type"],
        "hint": row["hint"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "games": games,
    }

@app.post("/api/confirm_success")
def save_account(
    request: TaskRequest, authorization: str | None = Header(default=None)
):
    token = _bearer_token(authorization)
    try:
        email = confirm_pending_account(DB_PATH, request.task_id, token)
    except (PermissionError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired task token") from exc
    return {
        "status": "saved",
        "access_token": issue_account_token(DB_PATH, email),
    }


@app.post("/api/session")
def create_session(account: Account):
    cipher = get_cipher()
    if not verify_account_password(DB_PATH, cipher, account.email, account.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"status": "success", "access_token": issue_account_token(DB_PATH, account.email)}


@app.post("/api/query")
def query_logs(authorization: str | None = Header(default=None)):
    token = _bearer_token(authorization)
    account = account_for_token(DB_PATH, token)
    if not account:
        raise HTTPException(status_code=401, detail="Invalid account token")
    # 此前是裸 sqlite3.connect（busy_timeout 只有默认 5 秒），且 fetchall 抛异常时
    # conn.close() 不会执行、连接泄漏。改用统一的 connect()（busy_timeout 30 秒 + WAL），
    # 并用 closing 保证异常路径也关闭。
    with closing(connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT game_title, claim_time, image_url FROM logs WHERE email=? ORDER BY id DESC",
            (account["email"],),
        ).fetchall()
    logs = [{"game": r[0], "time": r[1], "image": f"/images/{r[2]}" if r[2] else "/images/default.jpg"} for r in rows]
    return {"status": "success", "data": logs}

@app.post("/api/report_game")
def report_game(log: GameLog, authorization: str | None = Header(default=None)):
    _require_internal_token(authorization)
    with connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM logs WHERE email=? AND game_title=?",
            (log.email, log.game_title),
        ).fetchone()
        if not existing:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn.execute(
                "INSERT INTO logs (email, game_title, image_url, claim_time) VALUES (?, ?, ?, ?)",
                (log.email, log.game_title, log.image_filename, now),
            )
    if existing:
        if log.run_id:
            record_game_result(
                DB_PATH, log.run_id, log.game_title, "owned", overwrite=False
            )
        return {"status": "skipped", "msg": "Already recorded"}
    r.set(f"last_game:{account_ref(log.email)}", log.game_title, ex=600)
    if log.run_id:
        record_game_result(
            DB_PATH, log.run_id, log.game_title, "claimed", overwrite=False
        )
    return {"status": "recorded"}

# --- 周免周期调度 ---

def _scheduled_payload(run_id: str) -> str:
    return json.dumps({"run_id": run_id}, ensure_ascii=True, separators=(",", ":"))


def _persist_scheduled_runs(runs: list[tuple[str, int]]) -> None:
    if runs:
        r.zadd(SCHEDULED_TASK_QUEUE, {_scheduled_payload(run_id): run_at for run_id, run_at in runs})


async def refresh_promotion_cycle() -> None:
    lock_key = "promotion_cycle_refresh_lock"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{secrets.token_hex(8)}"
    if not r.set(
        lock_key,
        lock_value,
        nx=True,
        ex=max(PROMOTION_REFRESH_INTERVAL_SECONDS, 600),
    ):
        return
    try:
        samples = await _fetch_current_free_game_samples()
        if not samples:
            print("Promotion refresh skipped: no reliable current promotion response")
            return
        candidate = max(samples, key=len)
        active = get_active_promotion_cycle(DB_PATH)
        if active and len(candidate) < len(active[1]):
            sample_ids = {
                json.dumps(canonicalize_promotion_games(sample), sort_keys=True)
                for sample in samples
            }
            if len(samples) < 3 or len(sample_ids) != 1:
                print("Promotion refresh deferred: smaller promotion set is not stable")
                return

        cycle_id, changed = set_active_promotion_cycle(DB_PATH, candidate)
        obsolete = cancel_obsolete_scheduled_runs(DB_PATH, cycle_id)
        for run_id in obsolete:
            r.zrem(SCHEDULED_TASK_QUEUE, _scheduled_payload(run_id))
        # 每次刷新都补齐当前周期尚未分配的账号。create_cycle_assignments
        # 会排除已有 assignment/completion，因此在周期未变化时不会重复建任务，
        # 但可以覆盖“周期开始后新托管账号加入”的情况。
        created = create_cycle_assignments(
            DB_PATH,
            cycle_id,
            candidate,
            start_at=int(datetime.now(timezone.utc).timestamp()),
            batch_size=CLAIM_BATCH_SIZE,
            batch_interval_seconds=CLAIM_BATCH_INTERVAL_SECONDS,
        )
        if changed:
            print(
                f"Promotion cycle changed: cycle={cycle_id[:12]} "
                f"games={len(candidate)} scheduled={len(created)}"
            )
        elif created:
            print(
                f"Promotion cycle assignments backfilled: cycle={cycle_id[:12]} "
                f"scheduled={len(created)}"
            )
        _persist_scheduled_runs(scheduled_cycle_runs(DB_PATH, cycle_id))
    except Exception as exc:
        print(f"Promotion refresh failed: type={type(exc).__name__}")
    finally:
        if r.get(lock_key) == lock_value:
            r.delete(lock_key)

# --- WARP 出口健康探测与自愈 ---
#
# 背景：2026-07-24 出口 4 的 warp-svc 被 OOM 杀掉后死了 65 小时无人发现。
# supervisor 的 /health 探测的是外层 wrapper 进程（已 exec 成 gost）而非 warp-svc
# 本身，poll() 恒为 None，所以 process_running 永远是 true —— compose healthcheck、
# systemd 看门狗、worker 的 WARP 探测三处都依赖这个接口，于是全部被瞒过。
# 而账号是按邮箱哈希固定绑定到出口的（worker.py get_warp_index_for_email），
# 一个出口静默死亡就意味着绑在它上面的账号静默漏领，且不会自动切换。
#
# 这里不问 /health，直接走每个代理端口请求 Cloudflare 的 trace 接口，
# 并断言 warp=on —— 仅仅"能通"是不够的：warp-svc 死后流量有可能直接从容器出去，
# 那样 IP 会泄漏成宿主 IP，trace 会返回 warp=off，这种情况同样必须判为故障。
WARP_PROXY_HOST = os.getenv("WARP_PROXY_HOST", "epic-warp")
WARP_PROXY_START_PORT = int(os.getenv("WARP_PROXY_START_PORT", "19000"))
WARP_PROXY_COUNT = max(1, int(os.getenv("WARP_PROXY_COUNT", "10")))
WARP_CONTROL_URL_TEMPLATE = os.getenv(
    "WARP_CONTROL_URL_TEMPLATE", "http://epic-warp:18080/restart/{idx}"
)
WARP_PROBE_INTERVAL_SECONDS = max(60, int(os.getenv("WARP_PROBE_INTERVAL_SECONDS", "600")))
WARP_PROBE_URL = os.getenv("WARP_PROBE_URL", "https://www.cloudflare.com/cdn-cgi/trace")
WARP_PROBE_TIMEOUT_SECONDS = float(os.getenv("WARP_PROBE_TIMEOUT_SECONDS", "15"))
# 连续失败达到该次数才重启，避免单次网络抖动触发重启
WARP_PROBE_FAILURES_BEFORE_RESTART = max(
    1, int(os.getenv("WARP_PROBE_FAILURES_BEFORE_RESTART", "2"))
)
WARP_PROBE_RESTART_COOLDOWN_SECONDS = max(
    60, int(os.getenv("WARP_PROBE_RESTART_COOLDOWN_SECONDS", "900"))
)
# 单轮最多重启几个出口，防止 WARP 整体故障时把 10 个实例全部反复重启
WARP_PROBE_MAX_RESTARTS_PER_ROUND = max(
    1, int(os.getenv("WARP_PROBE_MAX_RESTARTS_PER_ROUND", "2"))
)


async def _probe_warp_egress(index: int) -> dict:
    """走指定出口请求 trace 接口，确认既连得通、又确实在 WARP 隧道内。"""
    port = WARP_PROXY_START_PORT + index
    started = datetime.now().timestamp()
    record: dict = {"index": index, "port": port, "checked_at": int(started)}
    try:
        async with httpx.AsyncClient(
            proxy=f"http://{WARP_PROXY_HOST}:{port}",
            timeout=WARP_PROBE_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.get(WARP_PROBE_URL)
        fields = dict(
            line.split("=", 1)
            for line in response.text.splitlines()
            if "=" in line
        )
        record["status"] = response.status_code
        record["warp"] = fields.get("warp", "")
        record["exit_ip"] = fields.get("ip", "")
        record["colo"] = fields.get("loc", "")
        record["ok"] = response.status_code == 200 and fields.get("warp") == "on"
        if not record["ok"] and response.status_code == 200:
            # 能出网但没走隧道 —— 出口 IP 已经泄漏成宿主 IP
            record["error"] = "warp_tunnel_down"
    except Exception as exc:
        record["ok"] = False
        record["error"] = type(exc).__name__
    record["elapsed"] = round(datetime.now().timestamp() - started, 2)
    return record


async def _restart_warp_egress(index: int) -> bool:
    url = WARP_CONTROL_URL_TEMPLATE.format(idx=index)
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(url)
        return response.status_code == 200
    except Exception as exc:
        print(f"WARP restart failed: index={index} type={type(exc).__name__}")
        return False


async def probe_warp_egresses() -> None:
    lock_key = "warp_probe_lock"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{secrets.token_hex(8)}"
    if not r.set(lock_key, lock_value, nx=True, ex=max(WARP_PROBE_INTERVAL_SECONDS - 5, 60)):
        return
    try:
        results = await asyncio.gather(
            *(_probe_warp_egress(i) for i in range(WARP_PROXY_COUNT)),
            return_exceptions=True,
        )
        unhealthy: list[int] = []
        healthy_count = 0
        for index, record in enumerate(results):
            if isinstance(record, BaseException):
                record = {
                    "index": index,
                    "ok": False,
                    "error": type(record).__name__,
                    "checked_at": int(datetime.now().timestamp()),
                }
            fail_key = f"warp:probe_failures:{index}"
            if record.get("ok"):
                healthy_count += 1
                r.delete(fail_key)
                record["consecutive_failures"] = 0
            else:
                streak = r.incr(fail_key)
                r.expire(fail_key, 86400)
                record["consecutive_failures"] = streak
                if streak >= WARP_PROBE_FAILURES_BEFORE_RESTART:
                    unhealthy.append(index)
            # 不设 TTL：任务空窗期也要能看到最后一次探测结果。
            # （旧的 metrics:warp:* 只在跑任务时写且 TTL 300 秒，所以面板长期是空的。）
            with suppress(Exception):
                r.set(f"warp:health:{index}", json.dumps(record, ensure_ascii=True))

        r.set(
            "warp:health:summary",
            json.dumps(
                {
                    "healthy": healthy_count,
                    "total": WARP_PROXY_COUNT,
                    "checked_at": int(datetime.now().timestamp()),
                },
                ensure_ascii=True,
            ),
        )

        if healthy_count < WARP_PROXY_COUNT:
            print(
                f"WARP probe: {healthy_count}/{WARP_PROXY_COUNT} healthy, "
                f"needs_restart={unhealthy}"
            )

        restarted = 0
        for index in unhealthy:
            if restarted >= WARP_PROBE_MAX_RESTARTS_PER_ROUND:
                print(f"WARP probe: restart budget exhausted, index={index} deferred")
                break
            cooldown_key = f"warp:probe_restart_cooldown:{index}"
            if not r.set(cooldown_key, "1", nx=True, ex=WARP_PROBE_RESTART_COOLDOWN_SECONDS):
                continue
            restarted += 1
            print(f"WARP probe: restarting index={index}")
            if await _restart_warp_egress(index):
                r.delete(f"warp:probe_failures:{index}")
                print(f"WARP probe: restart accepted index={index}")
    except Exception as exc:
        print(f"WARP probe failed: type={type(exc).__name__}")
    finally:
        if r.get(lock_key) == lock_value:
            r.delete(lock_key)


# --- 孤儿浏览器 profile 与调试产物清理 ---
#
# verify 任务失败时，secure_store 会删掉 pending_credentials，
# 而 profile_id 这条唯一线索也随之消失，目录就永远留在 data/user_data 下。
# 线上实测 28 个孤儿目录、64MB，且每个都含 cookies.sqlite / key4.db（登录态）。
# 数量精确对应 26 次 failed + 2 次 manual_required。
#
# 清理采取两阶段：先移到 data/trash/<日期>/ 保留一段时间再删，
# 避免把正在跑的任务的 profile 误删掉。
PROFILE_SWEEP_INTERVAL_SECONDS = max(
    3600, int(os.getenv("PROFILE_SWEEP_INTERVAL_SECONDS", "86400"))
)
PROFILE_TRASH_RETENTION_DAYS = max(1, int(os.getenv("PROFILE_TRASH_RETENTION_DAYS", "7")))
RUNTIME_DEBUG_RETENTION_DAYS = max(1, int(os.getenv("RUNTIME_DEBUG_RETENTION_DAYS", "7")))
APP_LOG_RETENTION_DAYS = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))


def _known_profile_ids() -> set[str]:
    """当前仍被引用的 profile_id：账号表 + 待确认凭据 + 在途任务。"""
    known: set[str] = set()
    with connect(DB_PATH) as conn:
        for table, column in (
            ("accounts", "profile_id"),
            ("pending_credentials", "profile_id"),
        ):
            with suppress(sqlite3.Error):
                for row in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
                    if row[0]:
                        known.add(str(row[0]))
    return known


def sweep_orphan_profiles() -> None:
    lock_key = "profile_sweep_lock"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{secrets.token_hex(8)}"
    if not r.set(lock_key, lock_value, nx=True, ex=3600):
        return
    try:
        # 有任务在跑就整轮跳过：此刻正在使用的 profile 不在任何"已知"集合的
        # 保证之内（run 期间 pending_credentials 可能已被清），宁可下次再扫。
        if r.llen("task_queue") or r.keys("task_lock:*"):
            print("Profile sweep skipped: tasks in flight")
            return

        user_data = Path(USER_DATA_DIR)
        if not user_data.is_dir():
            return
        known = _known_profile_ids()
        trash_root = Path(DATA_DIR) / "trash"
        moved = 0
        for entry in user_data.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in known:
                continue
            try:
                uuid.UUID(entry.name)
            except ValueError:
                # 不是 profile 目录，不碰
                continue
            target_dir = trash_root / datetime.now().strftime("%Y%m%d")
            target_dir.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                shutil.move(str(entry), str(target_dir / entry.name))
                moved += 1
        if moved:
            print(f"Profile sweep: {moved} orphan profiles moved to trash")

        # 回收超期的 trash
        cutoff = datetime.now().timestamp() - PROFILE_TRASH_RETENTION_DAYS * 86400
        purged = 0
        if trash_root.is_dir():
            for day_dir in trash_root.iterdir():
                if day_dir.is_dir() and day_dir.stat().st_mtime < cutoff:
                    with suppress(OSError):
                        shutil.rmtree(day_dir)
                        purged += 1
        if purged:
            print(f"Profile sweep: {purged} expired trash buckets removed")

        # 顺带清理调试产物与应用日志（worker 侧只在子进程启动时清一次，
        # 空窗期没人清；web 这边每天兜一次底）
        with suppress(Exception):
            removed = cleanup_debug_artifacts(
                Path(DATA_DIR) / "runtime", retention_days=RUNTIME_DEBUG_RETENTION_DAYS
            )
            if removed:
                print(f"Profile sweep: {removed} stale debug artifacts removed")
        with suppress(Exception):
            removed = cleanup_old_logs(
                Path(DATA_DIR) / "logs", retention_days=APP_LOG_RETENTION_DAYS
            )
            if removed:
                print(f"Profile sweep: {removed} expired log files removed")
    except Exception as exc:
        print(f"Profile sweep failed: type={type(exc).__name__}")
    finally:
        if r.get(lock_key) == lock_value:
            r.delete(lock_key)


# --- WARP 实例定期轮转回收 ---
#
# warp-svc 存在稳定的内存泄漏：2026-07-27 重建前实测 10 个实例共 4.79 GB
# （平均 545 MB/个，而全新启动约 30-50 MB），容器 cgroup 触顶 19144 次、
# OOM 7 次杀掉 3 个进程。调大 mem_limit 只是把 OOM 推迟几天 —— 主机 23 GiB
# 里已用掉 12 GiB 且 swap 吃了 2.5/4 GiB，没有调大的余量。
#
# 这里按固定节奏轮流重启实例，把内存曲线压平：每次只动一个，
# 10 个实例即每个约 WARP_RECYCLE_INTERVAL × 10 被回收一次。
WARP_RECYCLE_INTERVAL_SECONDS = max(
    600, int(os.getenv("WARP_RECYCLE_INTERVAL_SECONDS", "21600"))
)
WARP_RECYCLE_ENABLED = os.getenv("WARP_RECYCLE_ENABLED", "1") == "1"


async def recycle_warp_egress() -> None:
    if not WARP_RECYCLE_ENABLED:
        return
    lock_key = "warp_recycle_lock"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{secrets.token_hex(8)}"
    if not r.set(lock_key, lock_value, nx=True, ex=max(WARP_RECYCLE_INTERVAL_SECONDS - 60, 300)):
        return
    try:
        # 有任务在跑就跳过这一轮。回收是预防性的，没必要跟正在领取的任务抢出口；
        # 下一轮（默认 6 小时后）再做。
        if r.llen("task_queue") or r.keys("task_lock:*"):
            print("WARP recycle skipped: tasks in flight")
            return

        # 轮转指针存在 Redis 里，web 重启也不会从头再来
        index = int(r.incr("warp:recycle_cursor")) % WARP_PROXY_COUNT

        # 刚被探测重启过的实例跳过，避免短时间内连续重启同一个
        if r.exists(f"warp:probe_restart_cooldown:{index}"):
            print(f"WARP recycle skipped index={index}: restarted recently")
            return

        print(f"WARP recycle: restarting index={index}")
        if await _restart_warp_egress(index):
            # 打上与探测共用的冷却标记，两条路径互不打架
            r.set(
                f"warp:probe_restart_cooldown:{index}",
                "1",
                ex=WARP_PROBE_RESTART_COOLDOWN_SECONDS,
            )
            r.delete(f"warp:probe_failures:{index}")
            print(f"WARP recycle: index={index} restarted")
        else:
            print(f"WARP recycle: index={index} restart failed")
    except Exception as exc:
        print(f"WARP recycle failed: type={type(exc).__name__}")
    finally:
        if r.get(lock_key) == lock_value:
            r.delete(lock_key)


scheduler = AsyncIOScheduler()
scheduler.add_job(
    refresh_promotion_cycle,
    "interval",
    seconds=PROMOTION_REFRESH_INTERVAL_SECONDS,
    next_run_time=datetime.now() + timedelta(seconds=10),
    id="promotion-cycle-refresh",
    replace_existing=True,
)

scheduler.add_job(
    probe_warp_egresses,
    "interval",
    seconds=WARP_PROBE_INTERVAL_SECONDS,
    next_run_time=datetime.now() + timedelta(seconds=30),
    id="warp-egress-probe",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)

scheduler.add_job(
    sweep_orphan_profiles,
    "interval",
    seconds=PROFILE_SWEEP_INTERVAL_SECONDS,
    next_run_time=datetime.now() + timedelta(minutes=5),
    id="orphan-profile-sweep",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)

scheduler.add_job(
    recycle_warp_egress,
    "interval",
    seconds=WARP_RECYCLE_INTERVAL_SECONDS,
    next_run_time=datetime.now() + timedelta(minutes=30),
    id="warp-egress-recycle",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)

@app.on_event("startup")
async def start_scheduler():
    if not ENABLE_APSCHEDULER:
        print("⏸️ APScheduler 未启用，跳过自动调度")
        return
    if not scheduler.running:
        scheduler.start()

@app.on_event("shutdown")
async def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

# --- 📊 系统状态与免费游戏 API ---

@app.get("/api/system_stats")
def get_system_stats():
    """
    获取系统统计数据：
    - 托管账号总数
    - 已验证账号数（有领取记录）
    - 今日领取数量
    - 累计领取数量
    - 系统运行时间
    """
    conn = connect(DB_PATH)
    c = conn.cursor()

    # 托管账号总数（数据库记录 + 偏移量）
    c.execute("SELECT COUNT(*) FROM accounts")
    total_accounts = c.fetchone()[0] + ACCOUNT_TOTAL_OFFSET

    # 已验证账号数（数据库记录 + 偏移量）
    c.execute("""
        SELECT COUNT(DISTINCT l.email)
        FROM logs l
        INNER JOIN accounts a ON l.email = a.email
    """)
    verified_accounts = c.fetchone()[0] + ACCOUNT_VERIFIED_OFFSET

    # 待验证账号数（数据库记录 + 偏移量）
    pending_accounts = total_accounts - verified_accounts

    # 今日领取数量
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM logs WHERE claim_time LIKE ?", (f"{today}%",))
    today_claims = c.fetchone()[0]

    # 累计领取数量（数据库记录 + 历史偏移量补偿）
    c.execute("SELECT COUNT(*) FROM logs")
    total_claims = c.fetchone()[0] + CLAIM_HISTORY_OFFSET

    processing_count = c.execute(
        "SELECT COUNT(*) FROM task_runs WHERE state IN ('queued', 'running', 'deferred')"
    ).fetchone()[0]
    oldest_active = c.execute(
        """
        SELECT CAST((julianday('now') - julianday(MIN(created_at))) * 86400 AS INTEGER)
        FROM task_runs
        WHERE state IN ('queued', 'running', 'deferred')
        """
    ).fetchone()[0]
    conn.close()

    queue_length = r.llen("task_queue")

    # worker 是否还活着。此前前端只要这个接口返回 200 就点亮"服务状态正常"绿灯，
    # worker 挂掉时页面依然全绿、用户照常提交、任务永远躺在队列里。
    heartbeat = r.get("worker:heartbeat")
    try:
        heartbeat_age = (
            max(0, int(datetime.now().timestamp() - float(heartbeat))) if heartbeat else None
        )
    except (TypeError, ValueError):
        heartbeat_age = None

    warp_healthy = warp_total = None
    raw_summary = r.get("warp:health:summary")
    if raw_summary:
        with suppress(ValueError, TypeError):
            summary = json.loads(raw_summary)
            warp_healthy, warp_total = summary.get("healthy"), summary.get("total")

    return {
        "total_accounts": total_accounts,
        "verified_accounts": verified_accounts,
        "pending_accounts": pending_accounts,
        "today_claims": today_claims,
        "total_claims": total_claims,
        "queue_length": queue_length,
        "processing_count": processing_count,
        "oldest_active_seconds": oldest_active,
        "worker_heartbeat_age_seconds": heartbeat_age,
        "warp_healthy": warp_healthy,
        "warp_total": warp_total,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def _parse_current_free_games(data: dict) -> list[dict[str, str]]:
    games: list[dict[str, str]] = []
    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    for item in elements:
        promotions = item.get("promotions") or {}
        active_offer = next(
            (
                promo
                for group in promotions.get("promotionalOffers", [])
                for promo in group.get("promotionalOffers", [])
                if promo.get("discountSetting", {}).get("discountType") == "PERCENTAGE"
                and promo.get("discountSetting", {}).get("discountPercentage") == 0
            ),
            None,
        )
        if not active_offer:
            continue

        image_url = next(
            (
                image.get("url", "")
                for image in item.get("keyImages", [])
                if image.get("type")
                in {"OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall"}
            ),
            "",
        )
        product_slug = item.get("productSlug", "") or item.get("urlSlug", "")
        for mapping in item.get("offerMappings") or []:
            if mapping.get("pageSlug"):
                product_slug = mapping["pageSlug"]
                break
        namespace = str(item.get("namespace", "")).strip()
        stable_id = namespace or str(product_slug).strip() or str(item.get("title", "")).strip()
        if not stable_id:
            continue
        games.append(
            {
                "id": stable_id,
                "namespace": namespace,
                "title": item.get("title", "未知游戏"),
                "slug": product_slug,
                "image": image_url,
                "url": (
                    f"https://store.epicgames.com/zh-CN/p/{product_slug}"
                    if product_slug
                    else "https://store.epicgames.com/zh-CN/free-games"
                ),
                "original_price": item.get("price", {})
                .get("totalPrice", {})
                .get("fmtPrice", {})
                .get("originalPrice", "免费"),
                "description": (item.get("description") or "")[:100],
                "promotion_end": active_offer.get("endDate", ""),
            }
        )
    unique = {game["id"].casefold(): game for game in games}
    return [unique[key] for key in sorted(unique)]


async def _fetch_current_free_games_once() -> list[dict[str, str]]:
    api_url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    headers = {
        "User-Agent": "Mozilla/5.0 EpicKiosk/1.0",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=False) as client:
        response = await client.get(api_url, headers=headers)
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            raise ValueError("Epic promotion response is not JSON")
        games = _parse_current_free_games(response.json())
    if not games:
        raise ValueError("Epic promotion response contains no current free games")
    return games


async def _fetch_current_free_game_samples() -> list[list[dict[str, str]]]:
    samples: list[list[dict[str, str]]] = []
    for attempt in range(3):
        try:
            samples.append(await _fetch_current_free_games_once())
        except (httpx.HTTPError, ValueError):
            pass
        if attempt < 2:
            await asyncio.sleep(0.5)
    return samples


@app.get("/api/free_games")
async def get_free_games():
    cache_key = "cache:free_games"
    cached = r.get(cache_key)
    if cached:
        return {"status": "success", "data": json.loads(cached), "cached": True}
    try:
        samples = await _fetch_current_free_game_samples()
        if not samples:
            raise ValueError("No reliable current promotion response")
        games = max(samples, key=len)
        r.setex(cache_key, 3600, json.dumps(games, ensure_ascii=True))
        return {"status": "success", "data": games, "cached": False}
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Free game fetch failed: type={type(exc).__name__}")
        return {
            "status": "fallback",
            "data": [
                {
                    "title": "查看本周免费游戏",
                    "slug": "",
                    "image": "",
                    "url": "https://store.epicgames.com/zh-CN/free-games",
                    "original_price": "免费",
                    "description": "点击前往 Epic 官网查看本周免费游戏",
                }
            ],
        }
