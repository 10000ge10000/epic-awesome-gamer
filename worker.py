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

from app.secure_store import (
    CredentialCipher,
    account_ref,
    connect,
    cycle_run_dispatch_status,
    load_task_context,
    mark_cycle_complete_if_ready,
    read_secret,
    record_game_result,
    safe_profile_path,
    task_cycle_is_active,
    update_task,
)

# Redis
redis_host = os.getenv("REDIS_HOST", "localhost")
r = redis.Redis(host=redis_host, port=6379, decode_responses=True)
WEB_BASE_URL = "http://web:8000"
WEB_API_URL = f"{WEB_BASE_URL}/api/report_game"
NUKE_API_URL = f"{WEB_BASE_URL}/api/nuke_account" # 核弹接口
INTERNAL_API_TOKEN = read_secret("INTERNAL_API_TOKEN", "INTERNAL_API_TOKEN_FILE")
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "kiosk.db"))
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "900"))
TASK_SOFT_TIMEOUT_SECONDS = int(os.getenv("TASK_SOFT_TIMEOUT_SECONDS", "480"))
# 锁的存活时间只需覆盖「一次任务执行 + 最长一次延迟重试等待」，
# 而不是一整天。线上实测最长单次任务 2790 秒、绝大多数在 116 秒左右，
# 最长的延迟重试是 captcha 的 7200 秒。取 9000 秒留有余量。
# 此前的 86400 会把任何一次锁泄漏都放大成整整一天的账号不可用，
# 而系统里并没有任何解锁入口。
TASK_LOCK_SECONDS = int(os.getenv("TASK_LOCK_SECONDS", "9000"))
TASK_MIN_GAME_BUDGET_SECONDS = int(os.getenv("TASK_MIN_GAME_BUDGET_SECONDS", "180"))

IMAGES_DIR = os.path.join(DATA_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

# 定义清理路径
PATHS_TO_CHECK = [
    os.path.join(DATA_DIR, "user_data"),
    "/app/app/volumes/user_data"
]

# ============================================================
# 🌐 WARP 代理配置
# ============================================================
WARP_PROXY_HOST = os.getenv("WARP_PROXY_HOST", "epic-warp")
WARP_PROXY_START_PORT = int(os.getenv("WARP_PROXY_START_PORT", os.getenv("WARP_PROXY_PORT", "19000")))
WARP_PROXY_COUNT = max(1, int(os.getenv("WARP_PROXY_COUNT", "1")))
WARP_CONTROL_URL_TEMPLATE = os.getenv("WARP_CONTROL_URL_TEMPLATE", "").strip()
WARP_CONTROL_RESTART_RETRIES = int(os.getenv("WARP_CONTROL_RESTART_RETRIES", "3"))
WARP_CONTROL_RESTART_BACKOFF_SECONDS = int(os.getenv("WARP_CONTROL_RESTART_BACKOFF_SECONDS", "5"))
WARP_CONTAINER_FALLBACK_RESTARTS = int(os.getenv("WARP_CONTAINER_FALLBACK_RESTARTS", "1"))
WARP_MAX_RETRIES = 5  # 最大重启次数
EPIC_TEST_URL = "https://store.epicgames.com/en-US/"
EPIC_TEST_TIMEOUT = 10  # 秒

# ============================================================
# 验证码失败恢复策略
# ============================================================
RETRY_QUEUE = "task_retry_queue"
SCHEDULED_TASK_QUEUE = "task_scheduled_queue"
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


def task_redis_key(run_id: str, field: str) -> str:
    return f"task:{run_id}:{field}"


def set_task_feedback(
    task_data: dict,
    *,
    status: str | None = None,
    result: str | None = None,
    hint: str | None = None,
    state: str | None = None,
    error_type: str | None = None,
) -> None:
    run_id = task_data["run_id"]
    if status is not None:
        r.set(task_redis_key(run_id, "status"), status, ex=TASK_LOCK_SECONDS)
    if result is not None:
        r.set(task_redis_key(run_id, "result"), result, ex=TASK_LOCK_SECONDS)
    if hint is not None:
        r.set(task_redis_key(run_id, "hint"), hint, ex=TASK_LOCK_SECONDS)
    update_task(
        DB_PATH,
        run_id,
        state=state,
        error_type=error_type,
        status_message=status,
        hint=hint,
        retry_data=task_data.get("retry_data"),
    )


def redact_log_line(line: str, email: str, password: str) -> str:
    safe = line.replace(email, f"acct-{account_ref(email)}")
    if password:
        safe = safe.replace(password, "<redacted>")
    safe = re.sub(
        r"(?i)(authorization|api[_-]?key|cookie|token)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        safe,
    )
    safe = re.sub(r"\b[A-Za-z0-9_-]{80,}\b", "<token>", safe)
    return safe


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


# 主循环存活标记。心跳线程只是"转发"这个值，绝不自己凭空刷新 ——
# 此前心跳是独立 daemon 线程无条件每 30 秒写一次，与主循环零耦合：
# 主循环卡死时心跳照常刷新，Docker healthcheck 一直绿灯，队列却再也不消费。
_MAIN_LOOP_TICK = time.monotonic()
_CURRENT_TASK_STARTED_AT: float | None = None
# 主循环空转一圈最多 10 秒（blpop timeout），给足余量
MAIN_LOOP_STALL_SECONDS = int(os.getenv("MAIN_LOOP_STALL_SECONDS", "120"))
# 单个任务的硬上限之上再留 10 分钟；超过即认为 run_task 自身卡死
TASK_STALL_SECONDS = int(os.getenv("TASK_STALL_SECONDS", str(TASK_TIMEOUT_SECONDS + 600)))


def _mark_main_loop_alive(task_started_at: float | None = ...) -> None:
    global _MAIN_LOOP_TICK, _CURRENT_TASK_STARTED_AT
    _MAIN_LOOP_TICK = time.monotonic()
    if task_started_at is not ...:
        _CURRENT_TASK_STARTED_AT = task_started_at


def _main_loop_stall_seconds() -> float:
    """返回主循环卡住的秒数；0 表示健康。"""
    now = time.monotonic()
    started = _CURRENT_TASK_STARTED_AT
    if started is not None:
        overrun = now - started - TASK_STALL_SECONDS
        return overrun if overrun > 0 else 0.0
    overrun = now - _MAIN_LOOP_TICK - MAIN_LOOP_STALL_SECONDS
    return overrun if overrun > 0 else 0.0


def worker_heartbeat_loop() -> None:
    warned = False
    while True:
        stalled = _main_loop_stall_seconds()
        if stalled > 0:
            # 不再刷新心跳，让它按 90 秒 TTL 自然过期，healthcheck 随之转红。
            # 与其谎报健康，不如让故障可见。
            if not warned:
                print(
                    f"Worker main loop appears stalled for {int(stalled)}s beyond budget; "
                    f"heartbeat withheld so healthcheck can turn red"
                )
                warned = True
        else:
            warned = False
            with suppress(Exception):
                r.set("worker:heartbeat", str(time.time()), ex=90)
        time.sleep(30)


def get_warp_index_for_email(email: str | None) -> int:
    if WARP_PROXY_COUNT <= 1:
        return 0
    seed = (email or "").strip().lower().encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:4], "big") % WARP_PROXY_COUNT


def get_task_warp_index(task_data: dict) -> int:
    default_index = get_warp_index_for_email(task_data.get("email"))
    try:
        override = int(task_data.get("retry_data", {}).get("warp_index"))
    except (TypeError, ValueError):
        return default_index
    if WARP_PROXY_COUNT <= 0:
        return 0
    return override % WARP_PROXY_COUNT


def next_retry_warp_index(error_type: str, current_index: int) -> int:
    if error_type not in {"network_timeout", "driver_crash"} or WARP_PROXY_COUNT <= 1:
        return current_index
    return (current_index + 1) % WARP_PROXY_COUNT


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


def get_warp_health_url() -> str:
    """Return the WARP control health URL derived from the restart endpoint."""
    if not WARP_CONTROL_URL_TEMPLATE:
        return ""
    if "/restart/" in WARP_CONTROL_URL_TEMPLATE:
        return WARP_CONTROL_URL_TEMPLATE.split("/restart/", 1)[0].rstrip("/") + "/health"
    return WARP_CONTROL_URL_TEMPLATE.rstrip("/") + "/health"


def request_warp_control(method: str, url: str, **kwargs):
    """Call the WARP control API without inheriting HTTP_PROXY/HTTPS_PROXY."""
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, **kwargs)


def _control_health_reports_ready(idx: int) -> tuple[bool, str]:
    health_url = get_warp_health_url()
    if not health_url:
        return False, "control health URL is not configured"
    try:
        resp = request_warp_control("GET", health_url, timeout=10)
        if resp.status_code != 200:
            return False, f"health status={resp.status_code}"
        payload = resp.json()
        if not payload.get("ok"):
            return False, f"health not ok: {str(payload)[:120]}"
        for inst in payload.get("instances", []):
            if int(inst.get("index", -1)) == idx:
                if inst.get("process_running") and inst.get("forwarder_running"):
                    return True, "control health ready"
                return False, f"instance not ready: {inst}"
        return False, f"index {idx} not found in health payload"
    except Exception as exc:
        return False, f"health error={exc}"


def wait_for_warp_recovery(idx: int, timeout_seconds: int = 60) -> bool:
    """Poll control health and proxy connectivity without triggering another restart."""
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_info = "not checked"
    while True:
        health_ok, health_info = _control_health_reports_ready(idx)
        proxy_ok, proxy_info = check_warp_proxy(idx)
        if proxy_ok:
            if health_ok:
                print(f"✅ WARP recovered: index={idx} info={proxy_info}")
            else:
                print(
                    f"✅ WARP proxy reachable while control health is not ready: "
                    f"index={idx} health={health_info} proxy={proxy_info}"
                )
            return True

        last_info = f"{health_info}; proxy={proxy_info}"
        if time.monotonic() >= deadline:
            print(f"⚠️ WARP recovery wait timed out: index={idx} last={last_info}")
            return False
        time.sleep(min(2, max(1, int(deadline - time.monotonic()))))


def restart_warp_instance_via_control(idx: int = 0) -> bool:
    """Restart one WARP instance through the control API with bounded backoff."""
    if not WARP_CONTROL_URL_TEMPLATE:
        print("⚠️ WARP control restart URL is not configured; skip single-exit restart")
        return False

    url = WARP_CONTROL_URL_TEMPLATE.format(idx=idx, index=idx, port=get_warp_proxy_port(idx))
    for attempt in range(1, WARP_CONTROL_RESTART_RETRIES + 1):
        try:
            resp = request_warp_control("POST", url, timeout=120)
            if resp.status_code == 200:
                print(
                    f"🔄 WARP control restart accepted: index={idx} port={get_warp_proxy_port(idx)} "
                    f"[{attempt}/{WARP_CONTROL_RESTART_RETRIES}]"
                )
                return wait_for_warp_recovery(
                    idx,
                    timeout_seconds=max(30, WARP_CONTROL_RESTART_BACKOFF_SECONDS * 3),
                )
            print(
                f"⚠️ WARP control restart failed: index={idx} status={resp.status_code} "
                f"body={resp.text[:200]} [{attempt}/{WARP_CONTROL_RESTART_RETRIES}]"
            )
        except Exception as e:
            print(
                f"⚠️ WARP control restart error: index={idx} error={e} "
                f"[{attempt}/{WARP_CONTROL_RESTART_RETRIES}]"
            )

        # The supervisor may still be restarting the instance after a 5xx response.
        if wait_for_warp_recovery(idx, timeout_seconds=WARP_CONTROL_RESTART_BACKOFF_SECONDS):
            print(f"✅ WARP recovered after control failure: index={idx}")
            return True
        if attempt < WARP_CONTROL_RESTART_RETRIES:
            time.sleep(WARP_CONTROL_RESTART_BACKOFF_SECONDS * attempt)

    print(f"❌ WARP single-exit recovery exhausted: index={idx}")
    return False


def restart_whole_warp_container() -> bool:
    """Restart the whole WARP container as a last-resort fallback."""
    try:
        result = subprocess.run(["docker", "restart", "epic-warp"], capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            print(f"🔄 WARP container fallback restart done: {result.stdout.strip()}")
            time.sleep(15)
            return True
        print(f"❌ WARP container fallback restart failed: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("❌ WARP container fallback restart timed out")
        return False
    except FileNotFoundError:
        print("⚠️ docker command is unavailable; cannot fallback restart WARP container")
        return False
    except Exception as e:
        print(f"❌ WARP container fallback restart error: {e}")
        return False


def restart_warp_container(idx: int = 0) -> bool:
    """Recover one WARP exit first; restart the whole container only as a bounded fallback."""
    if restart_warp_instance_via_control(idx):
        return True

    if WARP_CONTAINER_FALLBACK_RESTARTS <= 0:
        print(f"⚠️ WARP container fallback disabled: index={idx}")
        return False

    for attempt in range(1, WARP_CONTAINER_FALLBACK_RESTARTS + 1):
        print(f"🔄 WARP container fallback restart [{attempt}/{WARP_CONTAINER_FALLBACK_RESTARTS}]")
        if restart_whole_warp_container() and wait_for_warp_recovery(idx, timeout_seconds=90):
            return True

    print(f"❌ WARP recovery failed: index={idx}")
    return False


def ensure_warp_ready(warp_index: int = 0) -> bool:
    """Check WARP readiness and run at most one recovery flow per check cycle."""
    if not os.getenv("HTTP_PROXY") and not os.getenv("HTTPS_PROXY"):
        print("ℹ️ WARP proxy is not configured; skip readiness check")
        with suppress(Exception):
            r.setex(
                f"metrics:warp:{warp_index}",
                300,
                json.dumps({"status": "not_configured", "updated_at": int(time.time())}),
            )
        return True

    print(f"🔍 Checking WARP proxy: {WARP_PROXY_HOST}:{get_warp_proxy_port(warp_index)} [index={warp_index}]")

    recovery_attempted = False
    for attempt in range(1, WARP_MAX_RETRIES + 1):
        success, info = check_warp_proxy(warp_index)

        if success:
            print(f"✅ WARP ready - exit IP: {info}")
            with suppress(Exception):
                r.setex(
                    f"metrics:warp:{warp_index}",
                    300,
                    json.dumps({"status": "healthy", "updated_at": int(time.time())}),
                )
            return True

        print(f"⚠️ WARP check failed [{attempt}/{WARP_MAX_RETRIES}]: {info}")

        if attempt < WARP_MAX_RETRIES:
            if not recovery_attempted:
                print("🔄 Starting one WARP recovery flow...")
                if restart_warp_container(warp_index):
                    print("✅ WARP recovery flow completed; rechecking...")
                else:
                    print("⚠️ WARP recovery flow failed; continue health polling...")
                recovery_attempted = True
            else:
                print("⏳ WARP recovery already attempted in this cycle; wait and recheck...")
                time.sleep(WARP_CONTROL_RESTART_BACKOFF_SECONDS)

    print("❌ WARP readiness check failed after bounded recovery")
    with suppress(Exception):
        r.setex(
            f"metrics:warp:{warp_index}",
            300,
            json.dumps({"status": "unhealthy", "updated_at": int(time.time())}),
        )
    return False


def restart_warp_for_retry(email: str, reason: str, warp_index: int = 0) -> bool:
    """可恢复失败后按冷却时间重启 WARP，避免连续抖动代理。"""
    ref = account_ref(email)
    if not os.getenv("HTTP_PROXY") and not os.getenv("HTTPS_PROXY"):
        print(f"Account {ref}: no WARP proxy; skip recovery for {reason}")
        return False

    now = time.time()
    restart_key = f"warp:last_restart_at:{warp_index}"
    last_restart = r.get(restart_key)
    if last_restart:
        try:
            elapsed = now - float(last_restart)
            if elapsed < WARP_RESTART_COOLDOWN_SECONDS:
                wait_left = int(WARP_RESTART_COOLDOWN_SECONDS - elapsed)
                print(f"Account {ref}: WARP cooldown remaining={wait_left}s")
                return False
        except ValueError:
            pass

    print(f"Account {ref}: restart WARP reason={reason}")
    ok = restart_warp_container(warp_index)
    if ok:
        r.set(restart_key, str(time.time()), ex=max(WARP_RESTART_COOLDOWN_SECONDS * 2, 3600))
    else:
        print(f"Account {ref}: WARP recovery failed")
    return ok

def reset_profile_for_retry(profile_id: str, ref: str) -> int:
    """删除该账号的本地浏览器 profile，清理失效 Cookie/CSRF 状态。"""
    removed = 0
    for base_dir in PATHS_TO_CHECK:
        profile_path = safe_profile_path(base_dir, profile_id)
        if not os.path.exists(profile_path):
            continue
        if profile_path.is_symlink():
            print(f"Account {ref}: profile cleanup refused symlink")
            continue
        try:
            shutil.rmtree(profile_path)
            removed += 1
            print(f"Account {ref}: invalid profile removed")
        except Exception as exc:
            print(f"Account {ref}: profile cleanup failed error={type(exc).__name__}")
    return removed


def schedule_cookie_invalid_retry(task_data: dict) -> bool:
    email = task_data.get("email", "")
    ref = account_ref(email)
    retry_data = task_data.setdefault("retry_data", {})
    retry_count = int(retry_data.get("cookie_invalid", 0))
    if retry_count >= COOKIE_INVALID_MAX_RETRIES:
        set_task_feedback(
            task_data,
            status="❌ 登录状态失效，重试后仍失败",
            result="error_cookie_invalid",
            hint="本地登录态已清理但 Epic 仍拒绝登录，请稍后重新提交或联系管理员",
            state="failed",
            error_type="session_invalid",
        )
        print(f"Account {ref}: cookie retry limit reached")
        return False

    reset_profile_for_retry(task_data["profile_id"], ref)
    retry_data["cookie_invalid"] = retry_count + 1
    payload = json.dumps({"run_id": task_data["run_id"]}, ensure_ascii=True)
    r.rpush("task_queue", payload)
    r.setex(f"retry_pending:{task_data['run_id']}", TASK_TIMEOUT_SECONDS + 300, "cookie_invalid")
    set_task_feedback(
        task_data,
        status="🧹 登录状态失效，已清理本地 Cookie 并立即重试",
        result="retry_scheduled",
        hint="系统已清理该账号本地浏览器 profile，正在重新登录",
        state="queued",
        error_type="session_invalid",
    )
    print(f"Account {ref}: cookie retry queued attempt={retry_count + 1}")
    return True


def schedule_failure_retry(task_data: dict, error_type: str, warp_index: int | None = None) -> bool:
    """把可恢复失败放入 Redis 延迟队列，并限制重试次数和节奏。"""
    email = task_data.get("email", "")
    # 注意：这张表是硬编码的，不读环境变量。
    # 此前 compose、.env.example 和 README 都在维护
    # CAPTCHA_FAILURE_MAX_RETRIES / *_RETRY_DELAY_SECONDS /
    # NETWORK_FAILURE_MAX_RETRIES / *_RETRY_DELAY_SECONDS 这四个变量，
    # 但 worker.py 把它们读进常量后从不使用 —— 运维照着文档改配置、
    # 重建 2.84GB 的镜像，行为却纹丝不动。这四个变量已一并删除。
    # 如需调整重试策略，直接改这里的元组：(最大重试次数, 延迟秒数, 中文标签)。
    policies = {
        "captcha_failed": (1, 7200, "验证码失败"),
        "captcha_unsolved": (1, 7200, "验证码未能识别"),
        "provider_timeout": (2, (900, 3600), "验证码服务暂时不可用"),
        "network_timeout": (2, (600, 1800), "网络连接超时"),
        "driver_crash": (2, (600, 1800), "浏览器驱动断连"),
        "task_deadline": (1, 900, "任务软期限已到"),
    }
    if error_type not in policies:
        return False

    max_retries, delays, label = policies[error_type]
    retry_data = task_data.setdefault("retry_data", {})
    retry_count = int(retry_data.get(error_type, 0))

    if retry_count >= max_retries:
        set_task_feedback(
            task_data,
            status=f"❌ {label}，已达重试上限",
            result="fail",
            hint=f"{label}多次发生，请稍后手动重试",
            state="manual_required" if "captcha" in error_type else "failed",
            error_type=error_type,
        )
        return False

    if warp_index is None:
        warp_index = get_warp_index_for_email(email)
    if error_type in {"network_timeout", "driver_crash"}:
        restart_warp_for_retry(email, label, warp_index)

    retry_data[error_type] = retry_count + 1
    retry_data["warp_index"] = next_retry_warp_index(error_type, warp_index)
    retry_delay = delays[min(retry_count, len(delays) - 1)] if isinstance(delays, tuple) else delays
    run_at = int(time.time() + retry_delay)
    payload = json.dumps({"run_id": task_data["run_id"]}, ensure_ascii=True)
    r.zadd(RETRY_QUEUE, {payload: run_at})
    r.setex(f"retry_pending:{task_data['run_id']}", retry_delay + TASK_TIMEOUT_SECONDS + 300, error_type)
    set_task_feedback(
        task_data,
        status=f"⏳ {label}，{max(1, retry_delay // 60)} 分钟后重试 [{retry_count + 1}/{max_retries}]",
        result="retry_scheduled",
        hint="系统已安排延迟重试",
        state="deferred",
        error_type=error_type,
    )
    print(f"Account {account_ref(email)}: retry scheduled type={error_type} delay={retry_delay}s")
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
                run_id = task_data["run_id"]
                r.delete(f"retry_pending:{run_id}")
                update_task(DB_PATH, run_id, state="queued")
                print(f"Delayed retry queued: run_id={run_id}")
            except Exception:
                print("🚦 [延迟重试] 任务已重新入队")
    return moved


def move_due_scheduled_tasks(limit: int = 10) -> int:
    now = int(time.time())
    due_tasks = r.zrangebyscore(SCHEDULED_TASK_QUEUE, 0, now, start=0, num=limit)
    moved = 0
    for payload in due_tasks:
        if not r.zrem(SCHEDULED_TASK_QUEUE, payload):
            continue
        run_id = ""
        lock_key = ""
        try:
            run_id = str(json.loads(payload)["run_id"])
            email, _cycle_id = cycle_run_dispatch_status(DB_PATH, run_id)
            lock_key = f"task_lock:{account_ref(email)}"
            if not r.set(lock_key, "queued", nx=True, ex=TASK_LOCK_SECONDS):
                r.zadd(SCHEDULED_TASK_QUEUE, {payload: now + 60})
                continue
            update_task(DB_PATH, run_id, state="queued")
            r.rpush("task_queue", json.dumps({"run_id": run_id}, ensure_ascii=True))
            moved += 1
        except RuntimeError:
            if run_id:
                with suppress(Exception):
                    update_task(
                        DB_PATH,
                        run_id,
                        state="failed",
                        error_type="cycle_obsolete",
                        status_message="周免周期已变化，旧任务已取消",
                    )
        except Exception as exc:
            if lock_key:
                r.delete(lock_key)
            if run_id:
                with suppress(Exception):
                    update_task(DB_PATH, run_id, state="scheduled")
            r.zadd(SCHEDULED_TASK_QUEUE, {payload: now + 60})
            print(f"Scheduled task dispatch deferred: type={type(exc).__name__}")
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
    except Exception as exc: print(f"Steam cover lookup failed: {type(exc).__name__}")
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
    except Exception as exc: print(f"Cover download failed: {type(exc).__name__}")
    return None

def report_success(email, game_title, run_id=None):
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
                "image_filename": filename or "default.png",
                "run_id": run_id,
            }, headers=headers, timeout=5, proxies=no_proxy)
            resp.raise_for_status()

            result = resp.json()
            status = result.get("status", "unknown")

            if status == "recorded":
                print(f"Game recorded: account={account_ref(email)} title={game_title}")
                return True
            elif status == "skipped":
                print(f"Game already recorded: account={account_ref(email)} title={game_title}")
                return True
            else:
                print(f"⚠️ 入库返回异常: {status} (尝试 {attempt+1}/3)")

        except requests.exceptions.RequestException as e:
            print(f"❌ 入库请求失败: {e} (尝试 {attempt+1}/3)")

        # 重试前等待
        if attempt < 2:
            time.sleep(1)

    print(f"Game report abandoned: account={account_ref(email)} title={game_title}")
    return False

def clean_user_profile(profile_id):
    """普通瘦身优化"""
    for base_dir in PATHS_TO_CHECK:
        profile_path = safe_profile_path(base_dir, profile_id)
        if not os.path.exists(profile_path): continue
        if profile_path.is_symlink():
            continue
        
        folders_to_nuke = ["cache2", "startupCache", "thumbnails", "datareporting", "shader-cache", "crashes", "minidumps", "saved-telemetry-pings", "storage/default"]
        files_to_nuke = ["favicon*", "places.sqlite*", "formhistory.sqlite*", "webappsstore.sqlite*", "content-prefs.sqlite*", "*.log", "SiteSecurityServiceState.txt"]
        
        for folder in folders_to_nuke:
            try: shutil.rmtree(os.path.join(profile_path, folder))
            except Exception: pass  # 单个目录删不掉不影响整体瘦身
        for pattern in files_to_nuke:
            for f in glob.glob(os.path.join(profile_path, pattern)):
                try: os.remove(f)
                except Exception: pass  # 单个文件删不掉不影响整体瘦身

def nuke_account_immediately(email: str, profile_id: str) -> None:
    """
    等待浏览器退出后，请求 Web 删除账号，并安全清理当前 UUID profile。

    内部 API 调用必须禁用代理，否则会被 WARP 拦截导致 503 错误。
    """
    ref = account_ref(email)
    print(f"Invalid account cleanup requested: account={ref}")

    # 浏览器进程可能仍持有 profile 文件，先给进程退出留出时间。
    print("Waiting for browser process exit (5s)")
    time.sleep(5)

    # 显式禁用代理，确保内部服务请求不被 WARP 拦截
    no_proxy = {"http": None, "https": None}
    headers = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}

    # 已存在账号由 Web 根据数据库保存的 profile_id 删除；响应正文可能含敏感信息，不记录。
    try:
        res = requests.post(
            NUKE_API_URL,
            json={"email": email},
            headers=headers,
            timeout=5,
            proxies=no_proxy,
        )
        print(f"Account {ref}: backend cleanup status={res.status_code}")
    except requests.RequestException as exc:
        print(f"Account {ref}: backend cleanup failed error={type(exc).__name__}")

    # 新账号尚未写入 accounts 时 Web 会拒绝删除，只清理本次任务绑定的 UUID profile。
    for base_dir in PATHS_TO_CHECK:
        target_dir = safe_profile_path(base_dir, profile_id)
        if not target_dir.exists():
            continue
        if target_dir.is_symlink():
            print(f"Account {ref}: local cleanup refused symlink")
            continue
        try:
            shutil.rmtree(target_dir)
            print(f"Account {ref}: local profile removed")
        except OSError as exc:
            print(f"Account {ref}: local cleanup failed error={type(exc).__name__}")

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
    "two_factor_authentication.required": "该账号需要两步验证",
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
    "provider_timeout": {
        "status": "⚠️ 验证码服务暂时不可用",
        "hint": "系统将切换供应商并延迟重试，无需重复提交",
        "nuke": False,
    },
    "captcha_unsolved": {
        "status": "⚠️ 验证码未能自动完成",
        "hint": "系统仅会低频补跑一次，仍失败时需要人工处理",
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
    # 账号开启了两步验证
    "two_factor_required": {
        "status": "❌ 该账号开启了两步验证",
        "hint": "自动化无法完成邮箱验证码环节。请在 Epic 账户设置 → 密码与安全中关闭两步验证后重新托管",
        # 不设 nuke：两步验证是用户可以自行关闭的，不该因此删掉账号。
        # 该类型也不在任何重试策略集合里，所以会直接终止，不会每个周期都撞一次。
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
    if not title or status not in {"claimed", "owned", "failed", "deferred", "unconfirmed"}:
        raise ValueError("invalid game result payload")
    return title, status


def summarize_game_results(game_results: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    successful = [
        title for title, status in game_results.items() if status in {"claimed", "owned"}
    ]
    claimed = [title for title, status in game_results.items() if status == "claimed"]
    failed = [
        title
        for title, status in game_results.items()
        if status in {"failed", "deferred", "unconfirmed"}
    ]
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
                # 子进程已退出，但管道里可能还有缓冲数据。这里必须受 deadline 约束：
                # 若 camoufox/xvfb 留下了仍持有管道写端的孙进程，
                # 无界的 `for line in process.stdout` 会永远阻塞，
                # 而外层的 deadline 检查在 while 循环里，根本轮不到执行。
                while time.monotonic() < deadline:
                    if not selector.select(timeout=1):
                        break
                    line = process.stdout.readline()
                    if not line:
                        break
                    yield line
                else:
                    print("Worker drain timed out; residual pipe holder suspected")
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
    run_id = task_data["run_id"]
    email = task_data.get("email")
    password = task_data.get("password")
    profile_id = task_data["profile_id"]
    mode = task_data.get("mode")
    ref = account_ref(email)
    started_at = time.monotonic()

    warp_index = get_task_warp_index(task_data)
    warp_port = get_warp_proxy_port(warp_index)
    print(f"Task started: run_id={run_id} mode={mode} account={ref} warp={warp_index}:{warp_port}")
    set_task_feedback(task_data, status="🚀 初始化环境...", state="running")

    # ============================================================
    # 🌐 WARP 代理检测
    # 领取前先检测 WARP 是否可以访问 Epic Games
    # 如果不通则重启 WARP 容器换 IP，最多尝试 5 次
    # ============================================================
    if not ensure_warp_ready(warp_index):
        set_task_feedback(
            task_data,
            status="❌ 网络代理不可用",
            result="warp_unavailable",
            hint="WARP 代理无法连接 Epic Games，请联系管理员",
            state="failed",
            error_type="warp_unavailable",
        )
        print(f"Task failed: run_id={run_id} error=warp_unavailable")
        return

    env = os.environ.copy()
    env["EPIC_EMAIL"] = email
    env["EPIC_PASSWORD"] = password
    env["EPIC_PROFILE_ID"] = profile_id
    if mode == "verify":
        env["EPIC_VERIFY_ONLY"] = "1"
    target_games = task_data.get("retry_data", {}).get("target_games")
    if isinstance(target_games, list) and target_games:
        env["EPIC_TARGET_GAMES_JSON"] = json.dumps(target_games, ensure_ascii=False)
    env["TASK_SOFT_DEADLINE_EPOCH"] = str(time.time() + TASK_SOFT_TIMEOUT_SECONDS)
    disk = shutil.disk_usage(DATA_DIR)
    disk_percent = disk.used * 100 / disk.total if disk.total else 0
    if disk_percent >= 80:
        print(f"Disk usage warning: percent={disk_percent:.1f}")
    if disk_percent >= 85:
        env["EPIC_DISABLE_DEBUG_ARTIFACTS"] = "1"
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

            print(f"[acct-{ref}] {redact_log_line(line, email, password)}")

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
                        set_task_feedback(
                            task_data,
                            status=error_info["status"],
                            result=f"error_{error_type}",
                            hint=error_info.get("hint"),
                            error_type=error_type,
                        )

                        # 如果需要删除账号
                        if error_info.get("nuke"):
                            is_fatal_failure = True

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
                        set_task_feedback(
                            task_data,
                            status=error_info["status"],
                            result=f"game_error_{game_error}",
                            hint=error_info.get("hint"),
                            error_type=game_error,
                        )

                        # 如果需要删除账号
                        if error_info.get("nuke"):
                            is_fatal_failure = True

                    continue

            if "GAME_RESULT:" in line:
                try:
                    title, status = parse_game_result_line(line)
                    game_results[title] = status
                    record_game_result(DB_PATH, run_id, title, status)
                    print(f"🎮 游戏结果: {title} -> {status}")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    print(f"⚠️ 无法解析游戏结果: {exc}")
                continue

            # 🛑 致命错误 A: 无法获取 Cookie
            if "context cookies is not available" in line:
                set_task_feedback(
                    task_data,
                    status="❌ 登录失败：无效账号",
                    result="fail",
                    state="failed",
                    error_type="invalid_account",
                )
                is_fatal_failure = True
                terminate_process_group(process)
                nuke_account_immediately(email, profile_id)
                return

            # 🛑 致命错误 B: 密码错误（兼容旧日志格式）
            if "invalid_account_credentials" in line or "账号或密码错误" in line:
                set_task_feedback(
                    task_data,
                    status="❌ 密码错误",
                    result="fail",
                    state="failed",
                    error_type="invalid_credentials",
                )
                terminate_process_group(process)
                nuke_account_immediately(email, profile_id)
                return

            if "Could not find Place Order button" in line:
                set_task_feedback(task_data, status="⚠️ 找不到下单按钮")
                has_critical_error = True

            if "Timeout 30000ms exceeded" in line:
                set_task_feedback(task_data, status="⚠️ 操作超时，重试中...")
                has_critical_error = True

            if "Connection closed while reading from the driver" in line or "playwright/driver" in line:
                final_error_type = "driver_crash"
                set_task_feedback(task_data, status="⚠️ 浏览器驱动断连，准备延迟重试")

            # 验证码超时
            if "captcha response timeout" in line.lower() or "验证码响应超时" in line:
                set_task_feedback(task_data, status="⚠️ 验证码超时，重试中...")

            # 验证码成功
            if "Challenge success" in line or "验证码通过" in line:
                set_task_feedback(task_data, status="✅ 验证码通过")

            if "Already in the library" in line or "游戏已在库中" in line:
                is_already_owned = True
                has_critical_error = False  # 游戏已在库中，清除错误标记
                set_task_feedback(task_data, status="ℹ️ 游戏已在库中")

            # 游戏领取成功，清除错误标记
            if "任务完成" in line or "领取成功" in line:
                has_critical_error = False
                collection_completed = True

            if "所有周免游戏已在库中" in line:
                is_already_owned = True
                collection_completed = True

            # 登录成功识别（匹配多种日志格式）
            if "Authentication completed" in line or "already logged in" in line or "Epic Games 已登录" in line or "✅ 登录成功" in line:
                set_task_feedback(task_data, status="✅ 登录成功")
                is_login_success = True

            if '"title":' in line:
                try:
                    match = re.search(r'"title":\s*"([^"]+)"', line)
                    if match:
                        game_name = match.group(1)
                        set_task_feedback(task_data, status=f"🎁 发现: {game_name}")
                        if game_name not in discovered_games:
                            discovered_games.append(game_name)
                        scrape_and_download_image(game_name)
                except Exception as exc:
                    print(f"⚠️ 解析游戏标题失败: {exc}")

        return_code = process.wait(timeout=10)

        # 正常结束，执行常规瘦身
        clean_user_profile(profile_id)

        # 先把本轮已经领到的游戏入库，再判断是否要早退重试。
        # 此前下面三个早退分支都排在 report_success 之前就 return，于是走重试路径时
        # 本轮明明已经领到手的游戏不会写进 logs 表 —— docker-compose.yml 里那个
        # CLAIM_HISTORY_OFFSET（注释写着"用于补偿因入库 API 失效丢失的历史记录"）
        # 就是在给这个缺口打补丁。入库本身是幂等的（/api/report_game 先查后插、
        # record_game_result 用 ON CONFLICT），提前执行不会产生重复记录。
        successful_games, claimed_games, failed_games = summarize_game_results(game_results)
        report_failures = []
        for game_title in successful_games:
            if not report_success(email, game_title, run_id):
                report_failures.append(game_title)

        if final_error_type == "cookie_invalid" and not is_fatal_failure:
            schedule_cookie_invalid_retry(task_data)
            return

        if final_error_type in {"network_timeout", "driver_crash"} and not is_fatal_failure:
            schedule_failure_retry(task_data, final_error_type, warp_index)
            return

        if final_error_type in {"provider_timeout", "captcha_failed", "captcha_unsolved"} and not is_fatal_failure:
            schedule_failure_retry(task_data, final_error_type, warp_index)
            return

        if return_code != 0 and not final_error_type:
            final_error_type = "unknown"

        if successful_games:
            if failed_games or report_failures or (
                final_error_type and final_error_type not in {"success", "all_owned"}
            ):
                failure_parts = failed_games + report_failures
                failure_detail = ", ".join(failure_parts) or final_error_type
                partial_retry_count = int(task_data.setdefault("retry_data", {}).get("partial", 0))
                if failed_games and not report_failures and partial_retry_count < 1:
                    retry_task = dict(task_data)
                    retry_task["retry_data"] = dict(task_data["retry_data"])
                    retry_task["retry_data"]["partial"] = partial_retry_count + 1
                    retry_task["retry_data"]["target_games"] = failed_games
                    if schedule_failure_retry(retry_task, "captcha_failed"):
                        set_task_feedback(
                            retry_task,
                            status="⚠️ 部分游戏领取失败，已安排自动补跑",
                            result="retry_scheduled",
                            hint=f"已成功记录本轮已领取游戏，失败游戏稍后自动补跑: {failure_detail}",
                            state="deferred",
                            error_type="captcha_failed",
                        )
                        return
                set_task_feedback(
                    task_data,
                    status="⚠️ 部分游戏领取失败",
                    result="error_unknown_error",
                    hint=f"失败详情: {failure_detail}",
                    state="failed",
                    error_type=final_error_type or "checkout_failed",
                )
            elif failed_games:
                deferred_only = all(
                    game_results.get(title) == "deferred" for title in failed_games
                )
                if deferred_only:
                    retry_task = dict(task_data)
                    retry_task["retry_data"] = dict(task_data.get("retry_data", {}))
                    retry_task["retry_data"]["target_games"] = failed_games
                    if schedule_failure_retry(retry_task, "task_deadline", warp_index):
                        return
                set_task_feedback(
                    task_data,
                    status="⚠️ 领取结果无法确认",
                    result="unconfirmed",
                    hint="未确认的游戏已保留明细，请稍后人工复核",
                    state="manual_required",
                    error_type="unconfirmed",
                )
            elif not mark_cycle_complete_if_ready(DB_PATH, run_id):
                set_task_feedback(
                    task_data,
                    status="⚠️ 本周期游戏尚未全部确认",
                    result="unconfirmed",
                    hint="只有本周期全部周免均为已领取或已拥有才会标记完成",
                    state="manual_required",
                    error_type="cycle_incomplete",
                )
            elif claimed_games:
                set_task_feedback(
                    task_data,
                    status=f"🎉 已领取 {len(claimed_games)} 个游戏",
                    result="success_new",
                    state="succeeded",
                )
            else:
                set_task_feedback(
                    task_data,
                    status="✅ 任务完成（已在库中）",
                    result="success_owned",
                    state="succeeded",
                )
            return

        if final_error_type and final_error_type not in {"success", "all_owned"}:
            error_info = ERROR_TYPE_MESSAGES.get(final_error_type, ERROR_TYPE_MESSAGES["unknown"])
            set_task_feedback(
                task_data,
                status=error_info["status"],
                result=f"error_{final_error_type}",
                hint=error_info.get("hint"),
                state="failed",
                error_type=final_error_type,
            )
            return

        # Backward-compatible fallback for older deploy output.
        if collection_completed and discovered_games and not has_critical_error:
            for game_title in discovered_games:
                report_success(email, game_title, run_id)
            if not mark_cycle_complete_if_ready(DB_PATH, run_id):
                set_task_feedback(
                    task_data,
                    status="⚠️ 本周期游戏尚未全部确认",
                    result="unconfirmed",
                    state="manual_required",
                    error_type="cycle_incomplete",
                )
                return
            set_task_feedback(task_data, status="🎉 领取成功！", result="success_new", state="succeeded")
            return

        if is_already_owned and not has_critical_error:
            if not mark_cycle_complete_if_ready(DB_PATH, run_id):
                set_task_feedback(
                    task_data,
                    status="⚠️ 本周期游戏尚未全部确认",
                    result="unconfirmed",
                    state="manual_required",
                    error_type="cycle_incomplete",
                )
                return
            set_task_feedback(
                task_data,
                status="✅ 任务完成（已在库中）",
                result="success_owned",
                state="succeeded",
            )
            return

        if mode == 'verify':
            if is_login_success and not is_fatal_failure and not has_critical_error:
                set_task_feedback(
                    task_data,
                    status="✅ 验证通过",
                    result="success",
                    state="succeeded",
                )
            else:
                set_task_feedback(
                    task_data,
                    status="❌ 验证失败",
                    result="fail",
                    state="failed",
                    error_type=final_error_type or "verification_failed",
                )
        else:
            set_task_feedback(
                task_data,
                status="❌ 未能确认领取结果",
                result="fail",
                state="failed",
                error_type=final_error_type or "unconfirmed",
            )

    except subprocess.TimeoutExpired:
        print(f"Task hard timeout: run_id={run_id} limit={TASK_TIMEOUT_SECONDS}s")
        schedule_failure_retry(task_data, "task_deadline", warp_index)
    except Exception as e:
        print(f"Task error: run_id={run_id} type={type(e).__name__}")
        traceback.print_exc()
        set_task_feedback(
            task_data,
            status="❌ 系统错误",
            result="fail",
            state="failed",
            error_type="system_error",
        )
    finally:
        if process and process.poll() is None:
            terminate_process_group(process)
        cleanup_residual_browser_processes()
        reap_child_processes()
        log_worker_runtime_health(f"after_task:{ref}")

def main_loop():
    log_worker_boot_info()
    cipher = CredentialCipher.from_environment()
    threading.Thread(target=worker_heartbeat_loop, name="worker-heartbeat", daemon=True).start()
    while True:
        _mark_main_loop_alive()
        if _sigchld_seen:
            reap_child_processes()
        move_due_scheduled_tasks()
        move_due_retry_tasks()
        task = r.blpop("task_queue", timeout=10)
        if task:
            _, data_json = task
            task_data = None
            run_id = None
            try:
                payload = json.loads(data_json)
                run_id = payload["run_id"]
                context = load_task_context(DB_PATH, cipher, run_id)
                task_data = {
                    "run_id": context.run_id,
                    "email": context.email,
                    "password": context.password,
                    "profile_id": context.profile_id,
                    "mode": context.mode,
                    "attempt": context.attempt,
                    "retry_data": context.retry_data,
                }
                if not task_cycle_is_active(DB_PATH, context.retry_data):
                    update_task(
                        DB_PATH,
                        run_id,
                        state="failed",
                        error_type="cycle_obsolete",
                        status_message="周免周期已变化，旧任务已取消",
                    )
                    r.delete(f"task_lock:{context.account_ref}")
                    task_data = None
                    continue
                # retry_pending 的语义统一为「重试已排期但尚未开始执行」。
                # 此前只有 move_due_retry_tasks 会删它，而 schedule_cookie_invalid_retry
                # 是直接 rpush 到 task_queue、不经过延迟队列的，于是该键永远留着，
                # 下面 finally 里的判定就永远不删 task_lock —— 账号被锁死 TASK_LOCK_SECONDS。
                r.delete(f"retry_pending:{run_id}")
                r.setex(f"task_lock:{context.account_ref}", TASK_LOCK_SECONDS, "running")
                _mark_main_loop_alive(time.monotonic())
                try:
                    run_task(task_data)
                finally:
                    _mark_main_loop_alive(None)
            except Exception as exc:
                print(f"Task context load failed: type={type(exc).__name__}")
                if run_id:
                    with suppress(Exception):
                        update_task(
                            DB_PATH,
                            run_id,
                            state="failed",
                            error_type="task_context_unavailable",
                            status_message="任务上下文不可用",
                        )
                    with suppress(Exception), connect(DB_PATH) as conn:
                        row = conn.execute(
                            "SELECT email FROM task_runs WHERE run_id=?", (run_id,)
                        ).fetchone()
                        if row:
                            r.delete(f"task_lock:{account_ref(row['email'])}")
            finally:
                if task_data:
                    run_id = task_data["run_id"]
                    if not r.exists(f"retry_pending:{run_id}"):
                        r.delete(f"task_lock:{account_ref(task_data['email'])}")
            if TASK_SPACING_SECONDS > 0:
                time.sleep(TASK_SPACING_SECONDS)
            reap_child_processes()
        else:
            reap_child_processes()
            time.sleep(0.1)

if __name__ == "__main__":
    main_loop()
