import os
import json
import sqlite3
import redis
import shutil
import random
import httpx
import re
import secrets
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 1. 挂载与路径
IMAGES_DIR = "/app/data/images"
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
MANUAL_LOGIN_DIR = "/app/data/manual_login"
os.makedirs(MANUAL_LOGIN_DIR, exist_ok=True)
app.mount("/manual_login", StaticFiles(directory=MANUAL_LOGIN_DIR), name="manual_login")

DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "kiosk.db")

# 历史数据偏移量配置（通过环境变量设置，默认为 0）
# 用于补偿因入库 API 失效丢失的历史记录
# 其他用户部署时默认为 0，不影响其数据显示
CLAIM_HISTORY_OFFSET = int(os.getenv("CLAIM_HISTORY_OFFSET", "0"))
ACCOUNT_VERIFIED_OFFSET = int(os.getenv("ACCOUNT_VERIFIED_OFFSET", "0"))
ACCOUNT_TOTAL_OFFSET = int(os.getenv("ACCOUNT_TOTAL_OFFSET", "0"))
USER_DATA_DIR = os.path.join(DATA_DIR, "user_data")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

# 2. Redis
redis_host = os.getenv("REDIS_HOST", "localhost")
r = redis.Redis(host=redis_host, port=6379, decode_responses=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (email TEXT PRIMARY KEY, password TEXT)''')
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(accounts)").fetchall()}
    if "auth_method" not in existing_cols:
        c.execute("ALTER TABLE accounts ADD COLUMN auth_method TEXT DEFAULT 'password'")
    if "login_updated_at" not in existing_cols:
        c.execute("ALTER TABLE accounts ADD COLUMN login_updated_at TEXT")
    if "login_expires_hint" not in existing_cols:
        c.execute("ALTER TABLE accounts ADD COLUMN login_expires_hint TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS logs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  email TEXT, game_title TEXT, image_url TEXT, claim_time TEXT)''')
    conn.commit()
    conn.close()
init_db()

# Models
class Account(BaseModel):
    email: str
    password: str

class NukeRequest(BaseModel):
    email: str

class QueryAccount(BaseModel):
    email: str 

class GameLog(BaseModel):
    email: str
    game_title: str
    image_filename: str

class ManualLoginStart(BaseModel):
    email: str

class ManualLoginControl(BaseModel):
    session_id: str
    type: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    url: str | None = None

class ManualLoginComplete(BaseModel):
    email: str
    session_id: str

# --- 🛡️ 防滥用中间件 (多层防护) ---
@app.middleware("http")
async def anti_abuse_middleware(request: Request, call_next):
    # 仅针对"提交任务/启动引擎"接口进行限制
    if request.url.path == "/api/deposit" and request.method == "POST":
        client_ip = request.client.host

        # 1. 检查是否被永久封禁（恶意IP）
        perm_ban_key = f"perm_ban:{client_ip}"
        if r.exists(perm_ban_key):
            return JSONResponse(
                status_code=403,
                content={"status": "banned", "msg": "🚫 此 IP 因滥用已被永久封禁"}
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

# --- 🛠️ 内部工具函数：物理删除逻辑 ---
def _perform_physical_delete(email):
    """执行彻底删除操作：数据库 + 物理文件夹 + Redis缓存"""
    log_msgs = []
    
    # 1. 删数据库
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM accounts WHERE email=?", (email,))
    if c.rowcount > 0:
        log_msgs.append("数据库记录已删")
    conn.commit()
    conn.close()

    # 2. 删物理文件
    target_dir = os.path.join(USER_DATA_DIR, email)
    if os.path.exists(target_dir):
        try:
            shutil.rmtree(target_dir)
            log_msgs.append("物理文件夹已粉碎")
        except Exception as e:
            log_msgs.append(f"物理删除出错: {e}")
    
    # 3. 删 Redis
    r.delete(f"status:{email}")
    r.delete(f"result:{email}")
    r.delete(f"last_game:{email}")
    r.delete(f"pending_game:{email}")
    
    return "，".join(log_msgs)

def _login_expire_hint() -> str:
    return "Epic 登录态通常可维持数天到数周；系统检测到失效时会提示重新授权。"

def _save_cookie_only_account(email: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT OR REPLACE INTO accounts (email, password, auth_method, login_updated_at, login_expires_hint)
        VALUES (?, ?, ?, ?, ?)
        """,
        (email, "__COOKIE_ONLY__", "browser", now, _login_expire_hint()),
    )
    conn.commit()
    conn.close()

def _get_account_auth(email: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT password, COALESCE(auth_method, 'password'), login_updated_at, login_expires_hint FROM accounts WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return row

# --- API 接口 ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # Starlette 1.0.0 新签名：request 作为第一个参数
    return templates.TemplateResponse(request, "index.html")

@app.post("/api/deposit")
async def deposit(account: Account, request: Request):
    """
    提交任务接口

    安全机制：
    1. 检查是否有正在执行的任务
    2. 验证已存储账号的密码（防止恶意覆盖）
    3. 记录 IP 提交的账号数量（防止恶意刷量）
    """
    email = account.email
    password = account.password
    client_ip = request.client.host

    # 1. 检查是否正在处理中
    current_status = r.get(f"status:{email}")
    if current_status and not current_status.startswith("🎉") and not current_status.startswith("❌"):
        return {"status": "busy", "msg": "⏳ 该账号有任务正在执行中，请稍后再试"}

    # 2. 如果是已存储账号，验证密码
    row = _get_account_auth(email)

    if row and row[1] == "browser":
        password = row[0]
    elif row and row[0] != password:
        return {"status": "auth_failed", "msg": "❌ 密码错误，无法操作此账号"}

    # 3. 记录 IP 提交的账号（用于检测恶意刷量）
    ip_accounts_key = f"ip_accounts:{client_ip}"
    ip_accounts = r.smembers(ip_accounts_key)

    if email not in ip_accounts:
        # 新账号
        r.sadd(ip_accounts_key, email)
        r.expire(ip_accounts_key, 86400 * 7)  # 7天过期

        # 如果同一IP提交超过5个不同账号，永久封禁
        if len(ip_accounts) >= 5:
            r.set(f"perm_ban:{client_ip}", "1")  # 永久封禁
            return {"status": "banned", "msg": "🚫 检测到异常行为，此 IP 已被封禁"}

    # 3. 提交任务
    task = {"email": email, "password": password, "mode": "verify"}
    r.delete(f"status:{email}")
    r.delete(f"result:{email}")
    r.rpush("task_queue", json.dumps(task))
    return {"status": "queued", "msg": "✅ 任务已加入队列"}

@app.post("/api/delete_account")
async def delete_account(account: Account):
    """用户手动删除接口（需要验证密码）"""
    row = _get_account_auth(account.email)
    
    if row and row[1] != "browser" and row[0] != account.password:
        return {"status": "fail", "msg": "密码错误，无法删除"}
    
    msg = _perform_physical_delete(account.email)
    return {"status": "success", "msg": f"手动删除成功: {msg}"}

# Worker 专用的核弹接口（无需密码，直接销毁）
@app.post("/api/nuke_account")
async def nuke_account(req: NukeRequest):
    print(f"☢️ 接到 Worker 指令，正在销毁无效账号: {req.email}")
    msg = _perform_physical_delete(req.email)
    return {"status": "success", "msg": msg}

@app.get("/api/status/{email}")
async def get_status(email: str):
    status_msg = r.get(f"status:{email}")
    result = r.get(f"result:{email}")
    last_game = r.get(f"last_game:{email}")
    hint = r.get(f"hint:{email}")  # 🔥 获取错误提示
    if not status_msg: return {"status": "waiting", "msg": "Waiting..."}
    return {"status": "processing", "msg": status_msg, "result": result, "game_title": last_game, "hint": hint}

@app.post("/api/manual_login/start")
async def start_manual_login(req: ManualLoginStart, request: Request):
    email = req.email.strip()
    if not email or "@" not in email:
        return {"status": "fail", "msg": "请输入有效邮箱"}

    current_status = r.get(f"status:{email}")
    if current_status and not current_status.startswith(("🎉", "❌", "✅", "⚠️", "⚪")):
        return {"status": "busy", "msg": "该账号有任务正在执行中，请稍后再试"}

    session_id = secrets.token_urlsafe(24)
    task = {
        "email": email,
        "password": "__COOKIE_ONLY__",
        "mode": "manual_login",
        "session_id": session_id,
        "client_ip": request.client.host,
    }
    r.setex(f"manual_login:owner:{session_id}", 900, email)
    r.delete(f"status:{email}")
    r.delete(f"result:{email}")
    r.rpush("task_queue", json.dumps(task, ensure_ascii=False))
    return {
        "status": "queued",
        "msg": "远程登录会话已创建，请在画面中完成 Epic 登录",
        "session_id": session_id,
        "expires_in": 900,
    }

@app.get("/api/manual_login/status/{session_id}")
async def manual_login_status(session_id: str):
    raw = r.get(f"manual_login:status:{session_id}")
    if not raw:
        return {"status": "waiting", "msg": "等待远程浏览器启动..."}
    return json.loads(raw)

@app.post("/api/manual_login/control")
async def manual_login_control(cmd: ManualLoginControl):
    owner = r.get(f"manual_login:owner:{cmd.session_id}")
    if not owner:
        return {"status": "fail", "msg": "登录会话已过期"}
    payload = cmd.dict(exclude_none=True)
    r.rpush(f"manual_login:control:{cmd.session_id}", json.dumps(payload, ensure_ascii=False))
    r.expire(f"manual_login:control:{cmd.session_id}", 900)
    return {"status": "ok"}

@app.post("/api/manual_login/cancel")
async def manual_login_cancel(cmd: ManualLoginControl):
    owner = r.get(f"manual_login:owner:{cmd.session_id}")
    if not owner:
        return {"status": "ok", "msg": "登录会话已结束"}
    payload = {"type": "cancel", "session_id": cmd.session_id}
    r.rpush(f"manual_login:control:{cmd.session_id}", json.dumps(payload, ensure_ascii=False))
    r.expire(f"manual_login:control:{cmd.session_id}", 60)
    return {"status": "ok", "msg": "已取消浏览器授权会话"}

@app.post("/api/manual_login/complete")
async def manual_login_complete(req: ManualLoginComplete):
    owner = r.get(f"manual_login:owner:{req.session_id}")
    if owner != req.email:
        return {"status": "fail", "msg": "登录会话校验失败"}
    _save_cookie_only_account(req.email)
    r.setex(f"result:{req.email}", 3600, "success")
    r.setex(f"status:{req.email}", 3600, "✅ 浏览器授权登录成功")
    return {"status": "success", "msg": "浏览器登录态已保存", "hint": _login_expire_hint()}

@app.post("/api/check_login_state")
async def check_login_state(account: QueryAccount):
    row = _get_account_auth(account.email)
    if not row:
        return {"status": "fail", "msg": "账号不存在"}
    task = {"email": account.email, "password": row[0], "mode": "check_login"}
    r.delete(f"status:{account.email}")
    r.delete(f"result:{account.email}")
    r.rpush("task_queue", json.dumps(task, ensure_ascii=False))
    return {"status": "queued", "msg": "已加入登录态检测队列"}

@app.post("/api/confirm_success")
async def save_account(account: Account):
    row = _get_account_auth(account.email)
    if row and row[1] == "browser":
        return {"status": "saved", "auth_method": "browser", "hint": row[3] or _login_expire_hint()}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT OR REPLACE INTO accounts (email, password, auth_method, login_updated_at, login_expires_hint)
        VALUES (?, ?, ?, ?, ?)
        """,
        (account.email, account.password, "password", datetime.now().strftime("%Y-%m-%d %H:%M"), _login_expire_hint()),
    )
    conn.commit()
    conn.close()
    return {"status": "saved"}

@app.post("/api/query")
async def query_logs(account: QueryAccount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT game_title, claim_time, image_url FROM logs WHERE email=? ORDER BY id DESC", (account.email,))
    rows = c.fetchall()
    conn.close()
    logs = [{"game": r[0], "time": r[1], "image": f"/images/{r[2]}" if r[2] else "/images/default.jpg"} for r in rows]
    return {"status": "success", "data": logs}

@app.post("/api/report_game")
async def report_game(log: GameLog):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM logs WHERE email=? AND game_title=?", (log.email, log.game_title))
    if c.fetchone():
        conn.close()
        return {"status": "skipped", "msg": "Already recorded"}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    r.set(f"last_game:{log.email}", log.game_title, ex=600)
    c.execute("INSERT INTO logs (email, game_title, image_url, claim_time) VALUES (?, ?, ?, ?)",
              (log.email, log.game_title, log.image_filename, now))
    conn.commit()
    conn.close()
    return {"status": "recorded"}

# --- 🚦 错峰调度逻辑 (新增) ---

def push_task_to_redis(task_json):
    """这才是真正把任务推进队列的函数，由调度器触发"""
    task_data = json.loads(task_json)
    r.rpush("task_queue", task_json)
    print(f"🚦 [错峰执行] 任务已入队: {task_data['email']}")

def daily_job():
    print("⏰ 12点已到，正在为所有账号计算随机延迟...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT email, password FROM accounts")
    users = cursor.fetchall()
    conn.close()
    
    for email, password in users:
        task = {"email": email, "password": password, "mode": "claim"}
        task_json = json.dumps(task)
        
        # 🎲 生成 0 到 60 分钟 (3600秒) 的随机延迟
        jitter_seconds = random.randint(0, 3600)
        run_date = datetime.now() + timedelta(seconds=jitter_seconds)
        
        # 使用 APScheduler 的 'date' 触发器，在指定时间执行一次
        scheduler.add_job(push_task_to_redis, 'date', run_date=run_date, args=[task_json])
        
        print(f"📅 账号 {email} 将延迟 {jitter_seconds/60:.1f} 分钟，于 {run_date.strftime('%H:%M:%S')} 执行")

scheduler = AsyncIOScheduler()
scheduler.add_job(daily_job, 'cron', hour=12, minute=0)

@app.on_event("startup")
async def start_scheduler():
    if not scheduler.running:
        scheduler.start()

@app.on_event("shutdown")
async def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

# --- 📊 系统状态与免费游戏 API ---

@app.get("/api/system_stats")
async def get_system_stats():
    """
    获取系统统计数据：
    - 托管账号总数
    - 已验证账号数（有领取记录）
    - 今日领取数量
    - 累计领取数量
    - 系统运行时间
    """
    conn = sqlite3.connect(DB_PATH)
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

    conn.close()

    # 队列中等待的任务数
    queue_length = r.llen("task_queue")

    # 正在处理中的任务数（扫描 status:* keys，排除已完成的）
    # 已完成状态：🎉（成功）、❌（失败）、✅ 验证通过
    processing_count = 0
    for key in r.scan_iter("status:*"):
        status = r.get(key)
        if status and not status.startswith("🎉") and not status.startswith("❌") and not status.startswith("✅ 验证通过"):
            processing_count += 1

    return {
        "total_accounts": total_accounts,
        "verified_accounts": verified_accounts,
        "pending_accounts": pending_accounts,
        "today_claims": today_claims,
        "total_claims": total_claims,
        "queue_length": queue_length,
        "processing_count": processing_count,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

@app.get("/api/free_games")
async def get_free_games():
    """
    从 Epic Games API 获取当前免费游戏列表
    缓存 1 小时，避免频繁请求
    """
    cache_key = "cache:free_games"
    cached = r.get(cache_key)

    if cached:
        return {"status": "success", "data": json.loads(cached), "cached": True}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # 直接使用 Epic 官方 API
            api_url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            }
            resp = await client.get(api_url, headers=headers)
            data = resp.json()

            games = []
            elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])

            for item in elements:
                promotions = item.get("promotions")
                if not promotions:
                    continue

                current_promos = promotions.get("promotionalOffers", [])

                # 检查是否有当前免费促销
                is_free_now = False
                for promo_group in current_promos:
                    for promo in promo_group.get("promotionalOffers", []):
                        discount_setting = promo.get("discountSetting", {})
                        if discount_setting.get("discountType") == "PERCENTAGE" and discount_setting.get("discountPercentage") == 0:
                            is_free_now = True
                            break
                    if is_free_now:
                        break

                if is_free_now:
                    # 获取图片
                    image_url = ""
                    for img in item.get("keyImages", []):
                        if img.get("type") in ["OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall"]:
                            image_url = img.get("url", "")
                            break

                    # 获取游戏 slug
                    product_slug = item.get("productSlug", "") or item.get("urlSlug", "")
                    if item.get("offerMappings"):
                        for mapping in item["offerMappings"]:
                            if mapping.get("pageSlug"):
                                product_slug = mapping["pageSlug"]
                                break

                    games.append({
                        "title": item.get("title", "未知游戏"),
                        "slug": product_slug,
                        "image": image_url,
                        "url": f"https://store.epicgames.com/zh-CN/p/{product_slug}" if product_slug else "https://store.epicgames.com/zh-CN/free-games",
                        "original_price": item.get("price", {}).get("totalPrice", {}).get("fmtPrice", {}).get("originalPrice", "免费"),
                        "description": (item.get("description") or "")[:100]
                    })

            # 缓存 1 小时
            r.setex(cache_key, 3600, json.dumps(games))

            return {"status": "success", "data": games, "cached": False}

    except Exception as e:
        print(f"获取免费游戏失败: {e}")
        # 返回引导用户访问官网的数据
        fallback_games = [
            {
                "title": "查看本周免费游戏",
                "slug": "",
                "image": "",
                "url": "https://store.epicgames.com/zh-CN/free-games",
                "original_price": "免费",
                "description": "点击前往 Epic 官网查看本周免费游戏"
            }
        ]
        return {"status": "fallback", "data": fallback_games, "error": str(e)}
