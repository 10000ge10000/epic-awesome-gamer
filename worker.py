import os
import time
import json
import redis
import subprocess
import requests
import re
import shutil
import glob
import socket
import selectors
import signal
import traceback
import queue
import threading
import hashlib
from contextlib import suppress
from bs4 import BeautifulSoup

# Redis
redis_host = os.getenv("REDIS_HOST", "localhost")
r = redis.Redis(host=redis_host, port=6379, decode_responses=True)
WEB_BASE_URL = "http://web:8000"
WEB_API_URL = f"{WEB_BASE_URL}/api/report_game"
NUKE_API_URL = f"{WEB_BASE_URL}/api/nuke_account" # 核弹接口
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "900"))
TASK_LOCK_SECONDS = int(os.getenv("TASK_LOCK_SECONDS", "86400"))

IMAGES_DIR = "/app/data/images"
os.makedirs(IMAGES_DIR, exist_ok=True)

# 定义清理路径
PATHS_TO_CHECK = [
    "/app/data/user_data",
    "/app/app/volumes/user_data"
]

# ============================================================
# 🌐 WARP 代理配置
# ============================================================
WARP_PROXY_HOST = os.getenv("WARP_PROXY_HOST", "epic-warp")
WARP_PROXY_START_PORT = int(os.getenv("WARP_PROXY_START_PORT", os.getenv("WARP_PROXY_PORT", "19000")))
WARP_PROXY_COUNT = max(1, int(os.getenv("WARP_PROXY_COUNT", "1")))
WARP_CONTROL_URL_TEMPLATE = os.getenv("WARP_CONTROL_URL_TEMPLATE", "").strip()
WARP_MAX_RETRIES = 5  # 最大重启次数
EPIC_TEST_URL = "https://store.epicgames.com/en-US/"
EPIC_TEST_TIMEOUT = 10  # 秒

# ============================================================
# 验证码失败恢复策略
# ============================================================
RETRY_QUEUE = "task_retry_queue"
CAPTCHA_FAILURE_MAX_RETRIES = int(os.getenv("CAPTCHA_FAILURE_MAX_RETRIES", "2"))
CAPTCHA_FAILURE_RETRY_DELAY_SECONDS = int(os.getenv("CAPTCHA_FAILURE_RETRY_DELAY_SECONDS", "900"))
NETWORK_FAILURE_MAX_RETRIES = int(os.getenv("NETWORK_FAILURE_MAX_RETRIES", "2"))
NETWORK_FAILURE_RETRY_DELAY_SECONDS = int(os.getenv("NETWORK_FAILURE_RETRY_DELAY_SECONDS", "600"))
COOKIE_INVALID_MAX_RETRIES = int(os.getenv("COOKIE_INVALID_MAX_RETRIES", "1"))
WARP_RESTART_COOLDOWN_SECONDS = int(os.getenv("WARP_RESTART_COOLDOWN_SECONDS", "300"))
TASK_SPACING_SECONDS = int(os.getenv("TASK_SPACING_SECONDS", "5"))
PID_WARN_THRESHOLD = int(os.getenv("PID_WARN_THRESHOLD", "250"))
ZOMBIE_WARN_THRESHOLD = int(os.getenv("ZOMBIE_WARN_THRESHOLD", "1"))
RESIDUAL_PROCESS_PATTERNS = (
    "app/deploy.py",
    "xvfb-run",
    "Xvfb",
    "firefox",
    "camoufox",
    "playwright",
)
_sigchld_seen = False


def _mark_sigchld(signum, frame):
    global _sigchld_seen
    _sigchld_seen = True


with suppress(Exception):
    signal.signal(signal.SIGCHLD, _mark_sigchld)


def _read_text(path: str, default: str = "unknown") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or default
    except OSError:
        return default


def collect_process_metrics() -> dict[str, int]:
    """Return lightweight process counts for worker health logs."""
    process_count = 0
    zombie_count = 0
    with suppress(OSError):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            process_count += 1
            status = _read_text(os.path.join(entry.path, "stat"), "")
            parts = status.split()
            if len(parts) > 2 and parts[2] == "Z":
                zombie_count += 1
    return {"process_count": process_count, "zombie_count": zombie_count}


def log_worker_runtime_health(prefix: str = "worker") -> dict[str, int]:
    metrics = collect_process_metrics()
    print(
        f"📊 [{prefix}] process_count={metrics['process_count']} "
        f"zombie_count={metrics['zombie_count']}"
    )
    if metrics["process_count"] >= PID_WARN_THRESHOLD:
        print(f"⚠️ [{prefix}] PID 数接近上限: {metrics['process_count']}/{PID_WARN_THRESHOLD}")
    if metrics["zombie_count"] >= ZOMBIE_WARN_THRESHOLD:
        print(f"⚠️ [{prefix}] 检测到 zombie 进程: {metrics['zombie_count']}")
    return metrics


def log_worker_boot_info() -> None:
    pid1_cmdline = _read_text("/proc/1/cmdline", "").replace("\x00", " ").strip()
    print(
        "🧭 Worker runtime: "
        f"pid={os.getpid()} ppid={os.getppid()} "
        f"pid1={pid1_cmdline or 'unknown'} "
        f"pids.max={_read_text('/sys/fs/cgroup/pids.max')} "
        f"cpu.max={_read_text('/sys/fs/cgroup/cpu.max')}"
    )
    log_worker_runtime_health("startup")


def get_warp_index_for_email(email: str | None) -> int:
    if WARP_PROXY_COUNT <= 1:
        return 0
    seed = (email or "").strip().lower().encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:4], "big") % WARP_PROXY_COUNT


def get_warp_proxy_port(idx: int) -> int:
    return WARP_PROXY_START_PORT + idx


def get_warp_proxy_url(idx: int) -> str:
    return f"http://{WARP_PROXY_HOST}:{get_warp_proxy_port(idx)}"


def check_warp_proxy(idx: int = 0) -> tuple[bool, str]:
    """
    检测指定 WARP 出口是否可用。

    只检测代理连通性和出口 IP，不检测 Epic Games；Epic 有 Cloudflare 挑战，需浏览器验证。
    """
    proxy_port = get_warp_proxy_port(idx)
    proxy_url = get_warp_proxy_url(idx)
    proxies = {"http": proxy_url, "https": proxy_url}

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((WARP_PROXY_HOST, proxy_port))
        sock.close()

        if result != 0:
            return False, f"WARP 代理端口不可达: {WARP_PROXY_HOST}:{proxy_port}"

        try:
            ip_resp = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
            if ip_resp.status_code == 200:
                ip = ip_resp.text.strip()
                return True, ip
            return False, f"IP 查询失败: {ip_resp.status_code}"
        except requests.exceptions.ProxyError:
            return False, "代理连接失败"
        except requests.exceptions.Timeout:
            return False, "代理超时"

    except socket.timeout:
        return False, "TCP 连接超时"
    except Exception as e:
        return False, str(e)[:50]


def restart_warp_container(idx: int = 0) -> bool:
    """重启指定 WARP 出口；多实例优先调用控制接口，失败时回退整容器重启。"""
    if WARP_CONTROL_URL_TEMPLATE:
        url = WARP_CONTROL_URL_TEMPLATE.format(idx=idx, index=idx, port=get_warp_proxy_port(idx))
        try:
            resp = requests.post(url, timeout=120)
            if resp.status_code == 200:
                print(f"🔄 WARP 出口已重启: index={idx} port={get_warp_proxy_port(idx)}")
                return True
            print(f"❌ WARP 出口重启失败: index={idx} status={resp.status_code} body={resp.text[:200]}")
        except Exception as e:
            print(f"❌ WARP 出口重启异常: index={idx} error={e}")

    try:
        result = subprocess.run(["docker", "restart", "epic-warp"], capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            print(f"🔄 WARP 容器已重启: {result.stdout.strip()}")
            time.sleep(15)
            return True
        print(f"❌ WARP 容器重启失败: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("❌ WARP 容器重启超时")
        return False
    except FileNotFoundError:
        print("⚠️ docker 命令不可用，跳过重启")
        return False
    except Exception as e:
        print(f"❌ WARP 容器重启异常: {e}")
        return False


def ensure_warp_ready(warp_index: int = 0) -> bool:
    """
    确保 WARP 代理可用，必要时重启换 IP

    Returns:
        bool: WARP 是否可用
    """
    # 如果没有配置 WARP 代理，直接返回成功（不使用代理）
    if not os.getenv("HTTP_PROXY") and not os.getenv("HTTPS_PROXY"):
        print("ℹ️ 未配置 WARP 代理，跳过检测")
        return True

    print(f"🔍 检测 WARP 代理: {WARP_PROXY_HOST}:{get_warp_proxy_port(warp_index)} [index={warp_index}]")

    for attempt in range(1, WARP_MAX_RETRIES + 1):
        success, info = check_warp_proxy(warp_index)

        if success:
            print(f"✅ WARP 代理可用 - 出口 IP: {info}")
            return True

        print(f"⚠️ WARP 检测失败 [{attempt}/{WARP_MAX_RETRIES}]: {info}")

        if attempt < WARP_MAX_RETRIES:
            print(f"🔄 正在重启 WARP 容器换 IP...")
            if restart_warp_container(warp_index):
                print(f"✅ WARP 已重启，等待恢复...")
            else:
                print(f"❌ WARP 重启失败，继续尝试...")

    print(f"❌ WARP 代理不可用，已达最大重试次数")
    return False


def restart_warp_for_retry(email: str, reason: str, warp_index: int = 0) -> bool:
    """可恢复失败后按冷却时间重启 WARP，避免连续抖动代理。"""
    if not os.getenv("HTTP_PROXY") and not os.getenv("HTTPS_PROXY"):
        print(f"ℹ️ [{email}] 未配置 WARP 代理，跳过{reason}换 IP")
        return False

    now = time.time()
    restart_key = f"warp:last_restart_at:{warp_index}"
    last_restart = r.get(restart_key)
    if last_restart:
        try:
            elapsed = now - float(last_restart)
            if elapsed < WARP_RESTART_COOLDOWN_SECONDS:
                wait_left = int(WARP_RESTART_COOLDOWN_SECONDS - elapsed)
                print(f"⏳ [{email}] WARP 刚重启过，冷却剩余 {wait_left}s，跳过本次重启")
                return False
        except ValueError:
            pass

    print(f"🔄 [{email}] {reason}，重启 WARP 换 IP")
    ok = restart_warp_container(warp_index)
    if ok:
        r.set(restart_key, str(time.time()), ex=max(WARP_RESTART_COOLDOWN_SECONDS * 2, 3600))
        ensure_warp_ready(warp_index)
    return ok


def reset_profile_for_retry(email: str) -> int:
    """删除该账号的本地浏览器 profile，清理失效 Cookie/CSRF 状态。"""
    removed = 0
    for base_dir in PATHS_TO_CHECK:
        profile_path = os.path.join(base_dir, email)
        if not os.path.exists(profile_path):
            continue
        try:
            shutil.rmtree(profile_path)
            removed += 1
            print(f"🧹 [{email}] 已清理失效浏览器 profile: {profile_path}")
        except Exception as exc:
            print(f"⚠️ [{email}] 清理浏览器 profile 失败: {profile_path} - {exc}")
    return removed


def schedule_cookie_invalid_retry(task_data: dict) -> bool:
    email = task_data.get("email", "")
    retry_count = int(task_data.get("cookie_invalid_retry_count", 0))
    if retry_count >= COOKIE_INVALID_MAX_RETRIES:
        print(f"🛑 [{email}] Cookie/CSRF 重试已达上限: {retry_count}/{COOKIE_INVALID_MAX_RETRIES}")
        r.set(f"status:{email}", "❌ 登录状态失效，重试后仍失败", ex=3600)
        r.set(f"result:{email}", "error_cookie_invalid", ex=3600)
        r.set(f"hint:{email}", "本地登录态已清理但 Epic 仍拒绝登录，请稍后重新提交或联系管理员", ex=3600)
        return False

    reset_profile_for_retry(email)
    next_task = dict(task_data)
    next_task["cookie_invalid_retry_count"] = retry_count + 1
    r.rpush("task_queue", json.dumps(next_task, ensure_ascii=False))
    r.setex(f"task_lock:{email}", TASK_TIMEOUT_SECONDS + 300, "queued_cookie_reset")
    r.set(f"status:{email}", "🧹 登录状态失效，已清理本地 Cookie 并立即重试", ex=3600)
    r.set(f"result:{email}", "retry_scheduled", ex=3600)
    r.set(f"hint:{email}", "系统已清理该账号本地浏览器 profile，正在重新登录", ex=3600)
    print(f"🔁 [{email}] Cookie/CSRF 失效，已清理 profile 并重新入队 [{retry_count + 1}/{COOKIE_INVALID_MAX_RETRIES}]")
    return True


def schedule_failure_retry(task_data: dict, error_type: str, warp_index: int | None = None) -> bool:
    """把可恢复失败放入 Redis 延迟队列，并限制重试次数和节奏。"""
    email = task_data.get("email", "")
    policies = {
        "captcha_failed": (
            CAPTCHA_FAILURE_MAX_RETRIES,
            CAPTCHA_FAILURE_RETRY_DELAY_SECONDS,
            "验证码失败",
        ),
        "network_timeout": (
            NETWORK_FAILURE_MAX_RETRIES,
            NETWORK_FAILURE_RETRY_DELAY_SECONDS,
            "网络连接超时",
        ),
        "driver_crash": (
            NETWORK_FAILURE_MAX_RETRIES,
            NETWORK_FAILURE_RETRY_DELAY_SECONDS,
            "浏览器驱动断连",
        ),
    }
    if error_type not in policies:
        return False

    max_retries, retry_delay, label = policies[error_type]
    retry_key = f"{error_type}_retry_count"
    retry_count = int(task_data.get(retry_key, 0))

    if retry_count >= max_retries:
        print(f"🛑 [{email}] {label}重试已达上限: {retry_count}/{max_retries}")
        r.set(f"status:{email}", f"❌ {label}，已达重试上限", ex=3600)
        r.set(f"result:{email}", "fail", ex=3600)
        r.set(f"hint:{email}", f"{label}多次发生，请稍后手动重试", ex=3600)
        return False

    if warp_index is None:
        warp_index = get_warp_index_for_email(email)
    restart_warp_for_retry(email, label, warp_index)

    next_task = dict(task_data)
    next_task[retry_key] = retry_count + 1
    run_at = int(time.time() + retry_delay)
    payload = json.dumps(next_task, ensure_ascii=False)
    r.zadd(RETRY_QUEUE, {payload: run_at})
    r.setex(f"retry_pending:{email}", retry_delay + TASK_TIMEOUT_SECONDS + 300, error_type)
    r.setex(f"task_lock:{email}", retry_delay + TASK_TIMEOUT_SECONDS + 300, "retry_scheduled")
    r.set(
        f"status:{email}",
        f"⏳ {label}，已换 IP，{max(1, retry_delay // 60)} 分钟后重试 "
        f"[{retry_count + 1}/{max_retries}]",
        ex=3600,
    )
    r.set(f"result:{email}", "retry_scheduled", ex=3600)
    r.set(f"hint:{email}", "系统已自动更换 WARP 出口并安排延迟重试", ex=3600)
    print(
        f"⏳ [{email}] 已安排{label}延迟重试: {retry_delay}s 后执行 "
        f"[{retry_count + 1}/{max_retries}]"
    )
    return True


def move_due_retry_tasks(limit: int = 10) -> int:
    """把到期的延迟任务移动到主队列。"""
    now = int(time.time())
    due_tasks = r.zrangebyscore(RETRY_QUEUE, 0, now, start=0, num=limit)
    moved = 0
    for payload in due_tasks:
        if r.zrem(RETRY_QUEUE, payload):
            r.rpush("task_queue", payload)
            moved += 1
            try:
                task_data = json.loads(payload)
                r.delete(f"retry_pending:{task_data.get('email')}")
                r.setex(f"task_lock:{task_data.get('email')}", TASK_LOCK_SECONDS, "queued_retry")
                print(f"🚦 [延迟重试] 任务已重新入队: {task_data.get('email')}")
            except Exception:
                print("🚦 [延迟重试] 任务已重新入队")
    return moved


print("👷 Worker V28 (WARP Retry Guard) 启动！")

def clean_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_").lower()

def clean_game_title_for_search(title):
    title = re.sub(r"(?i)\s+(goty|edition|director's cut|remastered|digital deluxe).*", "", title)
    return title.strip()

def fetch_steam_cover(game_title):
    search_title = clean_game_title_for_search(game_title)
    try:
        url = f"https://store.steampowered.com/api/storesearch/?term={search_title}&l=english&cc=US"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get('total') > 0 and data.get('items'):
            app_id = data['items'][0]['id']
            return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/library_600x900.jpg"
    except: pass
    return None

def scrape_and_download_image(game_title):
    print(f"🖼️ 刮削海报: 《{game_title}》")
    filename = f"{clean_filename(game_title)}.jpg"
    save_path = os.path.join(IMAGES_DIR, filename)
    if os.path.exists(save_path): return filename
    img_url = fetch_steam_cover(game_title)
    if not img_url:
        safe_name = game_title.replace(" ", "+")
        img_url = f"https://ui-avatars.com/api/?name={safe_name}&background=1e293b&color=3b82f6&size=512&length=2&font-size=0.33&bold=true"
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        img_data = requests.get(img_url, headers=headers, timeout=10).content
        if len(img_data) > 1000:
            with open(save_path, 'wb') as f:
                f.write(img_data)
            return filename
    except: pass
    return None

def report_success(email, game_title):
    """
    向 Web 后端上报游戏领取成功记录

    包含重试机制（最多3次），避免因网络波动导致记录丢失

    ⚠️ 重要：内部 API 调用必须禁用代理，否则会被 WARP 拦截导致 503 错误
    """
    filename = scrape_and_download_image(game_title)

    # 显式禁用代理，确保内部服务请求不被 WARP 拦截
    no_proxy = {"http": None, "https": None}
    headers = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}

    for attempt in range(3):
        try:
            resp = requests.post(WEB_API_URL, json={
                "email": email,
                "game_title": game_title,
                "image_filename": filename or "default.png"
            }, headers=headers, timeout=5, proxies=no_proxy)
            resp.raise_for_status()

            result = resp.json()
            status = result.get("status", "unknown")

            if status == "recorded":
                print(f"✅ 入库成功: {email} → {game_title}")
                return True
            elif status == "skipped":
                print(f"ℹ️ 已存在记录: {email} → {game_title}")
                return True
            else:
                print(f"⚠️ 入库返回异常: {status} (尝试 {attempt+1}/3)")

        except requests.exceptions.RequestException as e:
            print(f"❌ 入库请求失败: {e} (尝试 {attempt+1}/3)")

        # 重试前等待
        if attempt < 2:
            time.sleep(1)

    print(f"❌ 入库失败（已放弃）: {email} → {game_title}")
    return False

def clean_user_profile(email):
    """普通瘦身优化"""
    for base_dir in PATHS_TO_CHECK:
        profile_path = os.path.join(base_dir, email)
        if not os.path.exists(profile_path): continue
        
        folders_to_nuke = ["cache2", "startupCache", "thumbnails", "datareporting", "shader-cache", "crashes", "minidumps", "saved-telemetry-pings", "storage/default"]
        files_to_nuke = ["favicon*", "places.sqlite*", "formhistory.sqlite*", "webappsstore.sqlite*", "content-prefs.sqlite*", "*.log", "SiteSecurityServiceState.txt"]
        
        for folder in folders_to_nuke:
            try: shutil.rmtree(os.path.join(profile_path, folder))
            except: pass
        for pattern in files_to_nuke:
            for f in glob.glob(os.path.join(profile_path, pattern)):
                try: os.remove(f)
                except: pass

def nuke_account_immediately(email):
    """
    ☢️ 核弹模式：等待进程死亡后，执行双重删除

    ⚠️ 重要：内部 API 调用必须禁用代理，否则会被 WARP 拦截导致 503 错误
    """
    print(f"💀 [致命错误] 正在执行销毁程序: {email}")

    # ⚠️ 关键步骤：先睡 5 秒，让浏览器进程死透，防止它诈尸写回文件
    print("⏳ 等待浏览器进程完全退出 (5s)...")
    time.sleep(5)

    # 显式禁用代理，确保内部服务请求不被 WARP 拦截
    no_proxy = {"http": None, "https": None}
    headers = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}

    # 1. 呼叫后端删除 (后端权限通常更高)
    try:
        print(f"📞 呼叫后端 API: {NUKE_API_URL}")
        res = requests.post(
            NUKE_API_URL,
            json={"email": email},
            headers=headers,
            timeout=5,
            proxies=no_proxy,
        )
        print(f"📞 后端响应: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ 后端 API 连接失败: {e}")
    
    # 2. Worker 再次执行本地物理删除 (补刀)
    print("🗑️ 执行本地物理补刀...")
    for base_dir in PATHS_TO_CHECK:
        target_dir = os.path.join(base_dir, email)
        if os.path.exists(target_dir):
            try: 
                shutil.rmtree(target_dir)
                print(f"✅ [补刀成功] 已粉碎文件夹: {target_dir}")
            except Exception as e:
                print(f"❌ 删除失败 {target_dir}: {e}")
        else:
            print(f"ℹ️ 路径不存在(无需补刀): {target_dir}")

def is_verbose_traceback(line):
    """
    过滤掉冗长的 Python 堆栈跟踪行和 Playwright 调试信息
    """
    verbose_patterns = [
        # rich 格式输出
        line.startswith("│"),
        line.startswith("└"),
        line.startswith("├"),
        # Python 追踪
        line.startswith("File \""),
        line.startswith("Traceback "),
        line.startswith("asyncio.run"),
        line.startswith("return await"),
        line.startswith("return runner.run"),
        line.startswith("return self."),
        line.startswith("return call"),
        line.startswith("raise "),
        line.startswith("self._loop"),
        line.startswith("self.run_forever"),
        line.startswith("self._run_once"),
        line.startswith("do = await"),
        line.startswith("result = await"),
        line.startswith("has_cart_items"),
        line.startswith("await execute_browser_tasks"),
        line.startswith("await agent.collect_epic_games"),
        line.startswith("await self.epic_games"),
        line.startswith("> File"),
        # 对象表示
        "<function " in line,
        "<" in line and ">" in line and "object at" in line,
        "AsyncRetrying" in line,
        "RetryCallState" in line,
        "RetryError" in line,
        "Future at" in line,
        "self._context.run" in line,
        "handle._run()" in line,
        # Playwright 调试信息
        "locator resolved to" in line,
        "attempting click action" in line,
        "waiting for element" in line,
        "element is not enabled" in line,
        "retrying click action" in line,
        line.startswith("- waiting"),
        line.startswith("- element"),
        line.startswith("- retrying"),
        line.startswith("- locator"),
        "waiting 20ms" in line,
        "waiting 100ms" in line,
        "waiting 500ms" in line,
        "× waiting" in line,
        line.startswith("Call log:"),
        # hsw 脚本注入详细错误
        "@debugger eval code" in line,
        "eval code line" in line,
        "evaluate@debugger" in line,
    ]
    return any(verbose_patterns)

# 日志汉化映射
LOG_TRANSLATIONS = {
    "Wait for captcha response timeout": "验证码响应超时",
    "Challenge success": "验证码通过",
    "An error occurred while injecting hsw script": "脚本注入错误（可忽略）",
    "is read-only": "（只读错误，已忽略）",
    "invalid_account_credentials": "账号或密码错误",
    "errors.com.epicgames.account.invalid_account_credentials": "账号或密码错误",
    "errorCode": "错误码",
    "errorMessage": "错误信息",
}

# ============================================================
# 🔥 错误类型映射
# 将 ErrorType 映射为用户友好的中文提示和操作建议
# ============================================================
ERROR_TYPE_MESSAGES = {
    # 成功
    "success": {
        "status": "✅ 操作成功",
        "hint": None,  # 无需额外提示
    },
    # 账号或密码错误
    "invalid_credentials": {
        "status": "❌ 密码错误",
        "hint": "请检查密码后重新托管",
        "nuke": True,  # 需要删除账号
    },
    # 账号被锁定
    "account_locked": {
        "status": "❌ 账号被锁定",
        "hint": "请登录 Epic 官网解锁账号",
        "nuke": True,
    },
    # EULA 协议处理失败
    "eula_failed": {
        "status": "⚠️ 需要手动接受协议",
        "hint": "请登录 Epic 官网同意服务条款后重新托管",
        "nuke": False,  # 不删除账号，保留 Cookie
    },
    # 验证码识别失败
    "captcha_failed": {
        "status": "⚠️ 验证码识别困难",
        "hint": "系统已停止本次高风险验证码会话，请稍后重试或联系管理员人工处理",
        "nuke": False,
    },
    # 验证码需要人工处理
    "captcha_manual_required": {
        "status": "⚠️ 需要人工完成验证码",
        "hint": "Epic 触发了 hCaptcha 动物拖拽题，系统已停止自动重试以避免账号风控，请联系管理员人工完成一次登录",
        "nuke": False,
    },
    # 验证码已通过，但 Epic 结账结果无法可靠确认
    "checkout_failed": {
        "status": "❌ 无法确认游戏已入库",
        "hint": "Epic 结账页面可能已更新，请稍后重试并检查游戏库",
        "nuke": False,
    },
    # 登录超时
    "login_timeout": {
        "status": "⚠️ 登录超时",
        "hint": "网络波动，请稍后重试",
        "nuke": False,
    },
    # 网络超时
    "network_timeout": {
        "status": "⚠️ 网络连接超时",
        "hint": "Epic 服务可能不可用，请稍后重试",
        "nuke": False,
    },
    # 浏览器驱动断连，通常是 Playwright/Camoufox 与 Epic 页面或代理状态不稳定
    "driver_crash": {
        "status": "⚠️ 浏览器驱动断连",
        "hint": "系统会稍后自动重试；若频繁出现，请联系管理员查看 Worker 日志",
        "nuke": False,
    },
    # Cookie 无效（下次执行时会自动重新登录，无需删除）
    "cookie_invalid": {
        "status": "⚠️ 登录已过期，请重新提交任务",
        "hint": "系统会自动用存储的密码重新登录",
        "nuke": False,  # 不删除账号，下次执行会自动重新登录
    },
    # 未知错误
    "unknown": {
        "status": "❌ 未知错误",
        "hint": "请联系管理员查看日志",
        "nuke": False,
    },
    # ===== 游戏收集相关错误 =====
    # 所有游戏已在库中（这是成功状态）
    "all_owned": {
        "status": "✅ 所有游戏已在库中",
        "hint": None,
    },
    # 未知错误（游戏收集阶段）
    "unknown_error": {
        "status": "❌ 游戏领取失败",
        "hint": "请稍后重试或联系管理员",
        "nuke": False,
    },
}

def translate_log(line):
    """汉化关键日志消息"""
    for en, zh in LOG_TRANSLATIONS.items():
        if en in line:
            # 对于特定错误，只保留汉化后的简短消息
            if "is read-only" in line:
                return "⚠️ 脚本注入警告（已忽略）"
            if "@debugger" in line:
                return None  # 完全过滤掉
            if "errorCode" in line:
                # 提取错误码
                import re
                match = re.search(r'"errorCode":\s*"([^"]+)"', line)
                if match:
                    code = match.group(1)
                    if "invalid_account_credentials" in code:
                        return "❌ 登录失败：账号或密码错误"
                return line
    return line


def parse_game_result_line(line: str) -> tuple[str, str] | None:
    if "GAME_RESULT:" not in line:
        return None
    payload = line.split("GAME_RESULT:", 1)[1].strip()
    game_result = json.loads(payload)
    title = str(game_result["title"]).strip()
    status = str(game_result["status"]).strip()
    if not title or status not in {"claimed", "owned", "failed"}:
        raise ValueError("invalid game result payload")
    return title, status


def summarize_game_results(game_results: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    successful = [
        title for title, status in game_results.items() if status in {"claimed", "owned"}
    ]
    claimed = [title for title, status in game_results.items() if status == "claimed"]
    failed = [title for title, status in game_results.items() if status == "failed"]
    return successful, claimed, failed


def iter_process_output(process: subprocess.Popen, timeout_seconds: int):
    """Yield output without allowing a silent child process to run forever."""
    if os.name == "nt":
        yield from _iter_process_output_windows(process, timeout_seconds)
        return

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds

    try:
        while True:
            if time.monotonic() >= deadline:
                terminate_process_group(process)
                raise subprocess.TimeoutExpired(process.args, timeout_seconds)

            events = selector.select(timeout=1)
            if events:
                line = process.stdout.readline()
                if line:
                    yield line
                    continue

            if process.poll() is not None:
                for line in process.stdout:
                    yield line
                break
    finally:
        selector.close()


def _iter_process_output_windows(process: subprocess.Popen, timeout_seconds: int):
    """Windows pipes are not selectable, so read stdout from a small helper thread."""
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    stdout_closed = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_process_group(process)
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)

        try:
            line = output_queue.get(timeout=min(0.2, remaining))
        except queue.Empty:
            if process.poll() is not None and stdout_closed:
                break
            continue

        if line is None:
            stdout_closed = True
            if process.poll() is not None:
                break
            continue

        yield line


def terminate_process_group(process: subprocess.Popen, grace_seconds: int = 5) -> None:
    """Terminate the browser task and every child process it spawned."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        with suppress(Exception):
            process.wait(timeout=grace_seconds)
            return
        with suppress(Exception):
            process.kill()
            process.wait(timeout=grace_seconds)
        return

    try:
        process_group_id = os.getpgid(process.pid)
        if process_group_id != os.getpgrp():
            os.killpg(process_group_id, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return

    with suppress(Exception):
        process.wait(timeout=grace_seconds)
        return

    with suppress(ProcessLookupError):
        process_group_id = os.getpgid(process.pid)
        if process_group_id != os.getpgrp():
            os.killpg(process_group_id, signal.SIGKILL)
        else:
            process.kill()

    with suppress(Exception):
        process.wait(timeout=grace_seconds)


def reap_child_processes() -> int:
    """Reap orphaned children adopted by worker after browser shutdown."""
    global _sigchld_seen
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if pid == 0:
            break
        reaped += 1
    if reaped:
        print(f"🧹 已回收孤儿子进程: {reaped}")
    _sigchld_seen = False
    return reaped


def _iter_process_rows() -> list[dict[str, str | int]]:
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,stat=,comm=,args="],
            text=True,
            timeout=5,
        )
    except Exception as exc:
        print(f"⚠️ 无法读取进程列表: {exc}")
        return []

    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, stat, comm, args = parts
        with suppress(ValueError):
            rows.append(
                {
                    "pid": int(pid),
                    "ppid": int(ppid),
                    "stat": stat,
                    "comm": comm,
                    "args": args,
                }
            )
    return rows


def _residual_browser_pids() -> list[int]:
    current_pid = os.getpid()
    pids = []
    for row in _iter_process_rows():
        pid = int(row["pid"])
        if pid in {0, 1, current_pid}:
            continue
        if "Z" in str(row["stat"]):
            continue
        command_text = f"{row['comm']} {row['args']}"
        if any(pattern in command_text for pattern in RESIDUAL_PROCESS_PATTERNS):
            pids.append(pid)
    return sorted(set(pids), reverse=True)


def cleanup_residual_browser_processes(grace_seconds: int = 3) -> int:
    """Terminate leftover browser/Xvfb processes after a task finishes."""
    pids = _residual_browser_pids()
    if not pids:
        return 0

    print(f"🧹 检测到浏览器残留进程: {pids}")
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _residual_browser_pids():
            break
        time.sleep(0.2)

    survivors = _residual_browser_pids()
    for pid in survivors:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)

    reaped = reap_child_processes()
    killed_count = len(pids)
    print(f"🧹 浏览器残留清理完成: signaled={killed_count} reaped={reaped}")
    return killed_count


def run_task(task_data):
    email = task_data.get("email")
    password = task_data.get("password")
    mode = task_data.get("mode")

    warp_index = get_warp_index_for_email(email)
    warp_port = get_warp_proxy_port(warp_index)
    print(f"🚀 接到任务: {mode} - {email} | WARP index={warp_index} port={warp_port}")
    r.set(f"status:{email}", "🚀 初始化环境...", ex=3600)

    # ============================================================
    # 🌐 WARP 代理检测
    # 领取前先检测 WARP 是否可以访问 Epic Games
    # 如果不通则重启 WARP 容器换 IP，最多尝试 5 次
    # ============================================================
    if not ensure_warp_ready(warp_index):
        r.set(f"status:{email}", "❌ 网络代理不可用", ex=3600)
        r.set(f"result:{email}", "warp_unavailable", ex=3600)
        r.set(f"hint:{email}", "WARP 代理无法连接 Epic Games，请联系管理员", ex=3600)
        print(f"❌ [{email}] WARP 代理不可用，任务终止")
        return

    env = os.environ.copy()
    env["EPIC_EMAIL"] = email
    env["EPIC_PASSWORD"] = password
    env["ENABLE_APSCHEDULER"] = "false"
    env["HTTP_PROXY"] = get_warp_proxy_url(warp_index)
    env["HTTPS_PROXY"] = get_warp_proxy_url(warp_index)

    cmd = ["xvfb-run", "-a", "python3", "app/deploy.py"]

    is_login_success = False
    has_critical_error = False
    is_fatal_failure = False
    is_already_owned = False
    collection_completed = False
    game_results: dict[str, str] = {}
    discovered_games: list[str] = []

    # 🔥 新增：记录最终的错误类型
    final_error_type = None

    process = None
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=True
        )

        for line in iter_process_output(process, TASK_TIMEOUT_SECONDS):
            line = line.strip()
            if not line: continue

            # 过滤掉冗长的堆栈跟踪
            if is_verbose_traceback(line):
                continue

            # 汉化关键日志
            translated = translate_log(line)
            if translated is None:
                continue  # 完全过滤
            if translated:
                line = translated

            print(f"[{email}] {line}")

            # ============================================================
            # 🔥 新增：解析错误类型（格式: ❌ ERROR_TYPE:xxx）
            # ============================================================
            if "ERROR_TYPE:" in line:
                match = re.search(r"ERROR_TYPE:(\w+)", line)
                if match:
                    error_type = match.group(1)
                    final_error_type = error_type
                    print(f"🔍 检测到错误类型: {error_type}")

                    # 根据错误类型设置状态
                    if error_type in ERROR_TYPE_MESSAGES:
                        error_info = ERROR_TYPE_MESSAGES[error_type]
                        r.set(f"status:{email}", error_info["status"], ex=3600)

                        # 设置错误提示，供前端弹窗使用
                        if error_info.get("hint"):
                            r.set(f"hint:{email}", error_info["hint"], ex=3600)

                        # 如果需要删除账号
                        if error_info.get("nuke"):
                            is_fatal_failure = True

                        # 对于 EULA 失败等非致命错误，设置特殊结果
                        r.set(f"result:{email}", f"error_{error_type}", ex=3600)
                    continue

            # 解析最终错误类型（格式: ❌ FINAL_ERROR:xxx）
            if "FINAL_ERROR:" in line:
                match = re.search(r"FINAL_ERROR:(\w+)", line)
                if match:
                    final_error_type = match.group(1)
                    print(f"🔍 最终错误类型: {final_error_type}")
                continue

            # ============================================================
            # 🔥 新增：解析游戏收集错误（格式: ❌ GAME_ERROR:xxx）
            # ============================================================
            if "GAME_ERROR:" in line:
                match = re.search(r"GAME_ERROR:(\w+)", line)
                if match:
                    game_error = match.group(1)
                    if game_error == "unknown_error" and final_error_type == "driver_crash":
                        game_error = "driver_crash"
                    final_error_type = game_error
                    print(f"🎮 检测到游戏收集错误: {game_error}")

                    # 根据错误类型设置状态
                    if game_error in ERROR_TYPE_MESSAGES:
                        error_info = ERROR_TYPE_MESSAGES[game_error]
                        r.set(f"status:{email}", error_info["status"], ex=3600)

                        # 设置错误提示，供前端弹窗使用
                        if error_info.get("hint"):
                            r.set(f"hint:{email}", error_info["hint"], ex=3600)

                        # 如果需要删除账号
                        if error_info.get("nuke"):
                            is_fatal_failure = True

                        # 设置结果
                        r.set(f"result:{email}", f"game_error_{game_error}", ex=3600)
                    continue

            if "GAME_RESULT:" in line:
                try:
                    title, status = parse_game_result_line(line)
                    game_results[title] = status
                    print(f"🎮 游戏结果: {title} -> {status}")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    print(f"⚠️ 无法解析游戏结果: {exc}")
                continue

            # 🛑 致命错误 A: 无法获取 Cookie
            if "context cookies is not available" in line:
                r.set(f"status:{email}", "❌ 登录失败：无效账号", ex=300)
                r.set(f"result:{email}", "fail", ex=3600)
                is_fatal_failure = True
                terminate_process_group(process)
                nuke_account_immediately(email)
                return

            # 🛑 致命错误 B: 密码错误（兼容旧日志格式）
            if "invalid_account_credentials" in line or "账号或密码错误" in line:
                r.set(f"status:{email}", "❌ 密码错误", ex=300)
                r.set(f"result:{email}", "fail", ex=3600)
                terminate_process_group(process)
                nuke_account_immediately(email)
                return

            if "Could not find Place Order button" in line:
                r.set(f"status:{email}", "⚠️ 找不到下单按钮", ex=3600)
                has_critical_error = True

            if "Timeout 30000ms exceeded" in line:
                r.set(f"status:{email}", "⚠️ 操作超时，重试中...", ex=3600)
                has_critical_error = True

            if "Connection closed while reading from the driver" in line or "playwright/driver" in line:
                final_error_type = "driver_crash"
                r.set(f"status:{email}", "⚠️ 浏览器驱动断连，准备延迟重试", ex=3600)

            # 验证码超时
            if "captcha response timeout" in line.lower() or "验证码响应超时" in line:
                r.set(f"status:{email}", "⚠️ 验证码超时，重试中...", ex=3600)

            # 验证码成功
            if "Challenge success" in line or "验证码通过" in line:
                r.set(f"status:{email}", "✅ 验证码通过", ex=3600)

            if "Already in the library" in line or "游戏已在库中" in line:
                is_already_owned = True
                has_critical_error = False  # 游戏已在库中，清除错误标记
                r.set(f"status:{email}", "ℹ️ 游戏已在库中", ex=3600)

            # 游戏领取成功，清除错误标记
            if "任务完成" in line or "领取成功" in line:
                has_critical_error = False
                collection_completed = True

            if "所有周免游戏已在库中" in line:
                is_already_owned = True
                collection_completed = True

            # 登录成功识别（匹配多种日志格式）
            if "Authentication completed" in line or "already logged in" in line or "Epic Games 已登录" in line or "✅ 登录成功" in line:
                r.set(f"status:{email}", "✅ 登录成功", ex=3600)
                is_login_success = True

            if '"title":' in line:
                try:
                    match = re.search(r'"title":\s*"([^"]+)"', line)
                    if match:
                        game_name = match.group(1)
                        r.set(f"status:{email}", f"🎁 发现: {game_name}", ex=3600)
                        if game_name not in discovered_games:
                            discovered_games.append(game_name)
                        scrape_and_download_image(game_name)
                except Exception as exc:
                    print(f"⚠️ 解析游戏标题失败: {exc}")

        return_code = process.wait(timeout=10)

        # 正常结束，执行常规瘦身
        clean_user_profile(email)

        if final_error_type == "cookie_invalid" and not is_fatal_failure:
            schedule_cookie_invalid_retry(task_data)
            return

        if final_error_type in {"network_timeout", "driver_crash"} and not is_fatal_failure:
            schedule_failure_retry(task_data, final_error_type, warp_index)
            return

        if return_code != 0 and not final_error_type:
            final_error_type = "unknown"

        successful_games, claimed_games, failed_games = summarize_game_results(game_results)

        if successful_games:
            report_failures = []
            for game_title in successful_games:
                if not report_success(email, game_title):
                    report_failures.append(game_title)
            if failed_games or report_failures or (
                final_error_type and final_error_type not in {"success", "all_owned"}
            ):
                failure_parts = failed_games + report_failures
                failure_detail = ", ".join(failure_parts) or final_error_type
                partial_retry_count = int(task_data.get("partial_game_retry_count", 0))
                if failed_games and not report_failures and partial_retry_count < 1:
                    retry_task = dict(task_data)
                    retry_task["partial_game_retry_count"] = partial_retry_count + 1
                    if schedule_failure_retry(retry_task, "captcha_failed"):
                        r.set(f"status:{email}", "⚠️ 部分游戏领取失败，已安排自动补跑", ex=3600)
                        r.set(
                            f"hint:{email}",
                            f"已成功记录本轮已领取游戏，失败游戏稍后自动补跑: {failure_detail}",
                            ex=3600,
                        )
                        return
                r.set(f"status:{email}", "⚠️ 部分游戏领取失败", ex=3600)
                r.set(f"result:{email}", "error_unknown_error", ex=3600)
                r.set(
                    f"hint:{email}",
                    f"失败详情: {failure_detail}",
                    ex=3600,
                )
            elif claimed_games:
                r.set(f"status:{email}", f"🎉 已领取 {len(claimed_games)} 个游戏", ex=3600)
                r.set(f"result:{email}", "success_new", ex=3600)
            else:
                r.set(f"status:{email}", "✅ 任务完成（已在库中）", ex=3600)
                r.set(f"result:{email}", "success_owned", ex=3600)
            return

        if final_error_type and final_error_type not in {"success", "all_owned"}:
            error_info = ERROR_TYPE_MESSAGES.get(final_error_type, ERROR_TYPE_MESSAGES["unknown"])
            r.set(f"status:{email}", error_info["status"], ex=3600)
            if error_info.get("hint"):
                r.set(f"hint:{email}", error_info["hint"], ex=3600)
            r.set(f"result:{email}", f"error_{final_error_type}", ex=3600)
            return

        # Backward-compatible fallback for older deploy output.
        if collection_completed and discovered_games and not has_critical_error:
            for game_title in discovered_games:
                report_success(email, game_title)
            r.set(f"status:{email}", "🎉 领取成功！", ex=3600)
            r.set(f"result:{email}", "success_new", ex=3600)
            return

        if is_already_owned and not has_critical_error:
            r.set(f"status:{email}", "✅ 任务完成（已在库中）", ex=3600)
            r.set(f"result:{email}", "success_owned", ex=3600)
            return

        if mode == 'verify':
            if is_login_success and not is_fatal_failure and not has_critical_error:
                r.set(f"result:{email}", "success", ex=3600)
                r.set(f"status:{email}", "✅ 验证通过", ex=3600)
            else:
                if not r.get(f"result:{email}"):
                    r.set(f"result:{email}", "fail", ex=3600)
                    if not r.get(f"status:{email}"):
                        r.set(f"status:{email}", "❌ 验证失败", ex=3600)
        elif not r.get(f"result:{email}"):
            r.set(f"status:{email}", "❌ 未能确认领取结果", ex=3600)
            r.set(f"result:{email}", "fail", ex=3600)

    except subprocess.TimeoutExpired:
        print(f"❌ [{email}] 任务超过硬超时 {TASK_TIMEOUT_SECONDS}s，已终止子进程")
        r.set(f"status:{email}", "❌ 任务执行超时", ex=3600)
        r.set(f"hint:{email}", "浏览器任务长时间无响应，请稍后重试", ex=3600)
        r.set(f"result:{email}", "fail", ex=3600)
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        r.set(f"status:{email}", "❌ 系统错误", ex=3600)
        r.set(f"result:{email}", "fail", ex=3600)
    finally:
        if process and process.poll() is None:
            terminate_process_group(process)
        cleanup_residual_browser_processes()
        reap_child_processes()
        log_worker_runtime_health(f"after_task:{email}")

def main_loop():
    log_worker_boot_info()
    while True:
        if _sigchld_seen:
            reap_child_processes()
        move_due_retry_tasks()
        task = r.blpop("task_queue", timeout=10)
        if task:
            _, data_json = task
            email = None
            try:
                task_data = json.loads(data_json)
                email = task_data.get("email")
                if email:
                    r.setex(f"task_lock:{email}", TASK_LOCK_SECONDS, "running")
                run_task(task_data)
            except Exception:
                traceback.print_exc()
            finally:
                if email and not r.exists(f"retry_pending:{email}"):
                    r.delete(f"task_lock:{email}")
            if TASK_SPACING_SECONDS > 0:
                time.sleep(TASK_SPACING_SECONDS)
            reap_child_processes()
        else:
            reap_child_processes()
            time.sleep(0.1)

if __name__ == "__main__":
    main_loop()
