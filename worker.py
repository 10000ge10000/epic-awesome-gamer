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
from bs4 import BeautifulSoup

# Redis
redis_host = os.getenv("REDIS_HOST", "localhost")
r = redis.Redis(host=redis_host, port=6379, decode_responses=True)
WEB_BASE_URL = "http://web:8000"
WEB_API_URL = f"{WEB_BASE_URL}/api/report_game"
NUKE_API_URL = f"{WEB_BASE_URL}/api/nuke_account" # 核弹接口

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
WARP_PROXY_PORT = int(os.getenv("WARP_PROXY_PORT", "19000"))
WARP_MAX_RETRIES = 5  # 最大重启次数
EPIC_TEST_URL = "https://store.epicgames.com/en-US/"
EPIC_TEST_TIMEOUT = 10  # 秒


def check_warp_proxy() -> tuple[bool, str]:
    """
    检测 WARP 代理是否可用

    只检测代理连通性和出口 IP，不检测 Epic Games
    因为 Epic 有 Cloudflare 挑战，需要浏览器才能通过

    Returns:
        tuple[bool, str]: (是否成功, 错误信息或IP地址)
    """
    proxy_url = f"http://{WARP_PROXY_HOST}:{WARP_PROXY_PORT}"
    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }

    try:
        # 1. 先检测代理是否可达（TCP 连接）
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((WARP_PROXY_HOST, WARP_PROXY_PORT))
        sock.close()

        if result != 0:
            return False, f"WARP 代理端口不可达: {WARP_PROXY_HOST}:{WARP_PROXY_PORT}"

        # 2. 检测是否可以获取出口 IP（简单测试代理是否工作）
        try:
            ip_resp = requests.get(
                "https://api.ipify.org",
                proxies=proxies,
                timeout=10
            )
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


def restart_warp_container() -> bool:
    """
    重启 WARP 容器以获取新 IP

    Returns:
        bool: 是否成功重启
    """
    try:
        # 使用 docker 命令重启容器
        result = subprocess.run(
            ["docker", "restart", "epic-warp"],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            print(f"🔄 WARP 容器已重启: {result.stdout.strip()}")
            # 等待容器恢复健康
            time.sleep(15)
            return True
        else:
            print(f"❌ WARP 容器重启失败: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        print("❌ WARP 容器重启超时")
        return False
    except FileNotFoundError:
        # docker 命令不存在，尝试使用 Docker API
        print("⚠️ docker 命令不可用，跳过重启")
        return False
    except Exception as e:
        print(f"❌ WARP 容器重启异常: {e}")
        return False


def ensure_warp_ready() -> bool:
    """
    确保 WARP 代理可用，必要时重启换 IP

    Returns:
        bool: WARP 是否可用
    """
    # 如果没有配置 WARP 代理，直接返回成功（不使用代理）
    if not os.getenv("HTTP_PROXY") and not os.getenv("HTTPS_PROXY"):
        print("ℹ️ 未配置 WARP 代理，跳过检测")
        return True

    print(f"🔍 检测 WARP 代理: {WARP_PROXY_HOST}:{WARP_PROXY_PORT}")

    for attempt in range(1, WARP_MAX_RETRIES + 1):
        success, info = check_warp_proxy()

        if success:
            print(f"✅ WARP 代理可用 - 出口 IP: {info}")
            return True

        print(f"⚠️ WARP 检测失败 [{attempt}/{WARP_MAX_RETRIES}]: {info}")

        if attempt < WARP_MAX_RETRIES:
            print(f"🔄 正在重启 WARP 容器换 IP...")
            if restart_warp_container():
                print(f"✅ WARP 已重启，等待恢复...")
            else:
                print(f"❌ WARP 重启失败，继续尝试...")

    print(f"❌ WARP 代理不可用，已达最大重试次数")
    return False


print("👷 Worker V27 (WARP Check) 启动！")

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

    for attempt in range(3):
        try:
            resp = requests.post(WEB_API_URL, json={
                "email": email,
                "game_title": game_title,
                "image_filename": filename or "default.png"
            }, timeout=5, proxies=no_proxy)

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

    # 1. 呼叫后端删除 (后端权限通常更高)
    try:
        print(f"📞 呼叫后端 API: {NUKE_API_URL}")
        res = requests.post(NUKE_API_URL, json={"email": email}, timeout=5, proxies=no_proxy)
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
        "hint": "请稍后重试",
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

def run_task(task_data):
    email = task_data.get("email")
    password = task_data.get("password")
    mode = task_data.get("mode")

    print(f"🚀 接到任务: {mode} - {email}")
    r.set(f"status:{email}", "🚀 初始化环境...", ex=3600)

    # ============================================================
    # 🌐 WARP 代理检测
    # 领取前先检测 WARP 是否可以访问 Epic Games
    # 如果不通则重启 WARP 容器换 IP，最多尝试 5 次
    # ============================================================
    if not ensure_warp_ready():
        r.set(f"status:{email}", "❌ 网络代理不可用", ex=3600)
        r.set(f"result:{email}", "warp_unavailable", ex=3600)
        r.set(f"hint:{email}", "WARP 代理无法连接 Epic Games，请联系管理员", ex=3600)
        print(f"❌ [{email}] WARP 代理不可用，任务终止")
        return

    env = os.environ.copy()
    env["EPIC_EMAIL"] = email
    env["EPIC_PASSWORD"] = password
    env["ENABLE_APSCHEDULER"] = "false"

    cmd = ["xvfb-run", "-a", "python3", "app/deploy.py"]

    is_login_success = False
    has_critical_error = False
    is_fatal_failure = False
    is_already_owned = False

    # 🔥 新增：记录最终的错误类型
    final_error_type = None

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, bufsize=1
        )

        for line in process.stdout:
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

            # 🛑 致命错误 A: 无法获取 Cookie
            if "context cookies is not available" in line:
                r.set(f"status:{email}", "❌ 登录失败：无效账号", ex=300)
                r.set(f"result:{email}", "fail", ex=3600)
                is_fatal_failure = True
                process.kill()
                nuke_account_immediately(email)
                return

            # 🛑 致命错误 B: 密码错误（兼容旧日志格式）
            if "invalid_account_credentials" in line or "账号或密码错误" in line:
                r.set(f"status:{email}", "❌ 密码错误", ex=300)
                r.set(f"result:{email}", "fail", ex=3600)
                process.kill()
                nuke_account_immediately(email)
                return

            if "Could not find Place Order button" in line:
                r.set(f"status:{email}", "⚠️ 找不到下单按钮", ex=3600)
                has_critical_error = True

            if "Timeout 30000ms exceeded" in line:
                r.set(f"status:{email}", "⚠️ 操作超时，重试中...", ex=3600)
                has_critical_error = True

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
                        r.set(f"pending_game:{email}", game_name, ex=3600)
                        scrape_and_download_image(game_name)
                except: pass

            # ============================================================
            # 🔥 游戏收集完成检测
            # 匹配多种完成日志格式：
            # - "🎉 任务完成（已领取或已在库中）"
            # - "🎉 购物车游戏领取成功"
            # - "✅ 所有周免游戏已在库中"
            # ============================================================
            if ("任务完成" in line or "购物车游戏领取成功" in line or "所有周免游戏已在库中" in line) and not is_fatal_failure:
                # 等待一小段时间确保游戏标题已解析
                time.sleep(0.5)

                if is_fatal_failure:
                    nuke_account_immediately(email)
                elif final_error_type and final_error_type in ERROR_TYPE_MESSAGES:
                    # 有明确的错误类型，使用对应的处理
                    error_info = ERROR_TYPE_MESSAGES[final_error_type]
                    r.set(f"status:{email}", error_info["status"], ex=3600)
                    if error_info.get("hint"):
                        r.set(f"hint:{email}", error_info["hint"], ex=3600)
                elif has_critical_error and not is_already_owned:
                    r.set(f"status:{email}", "❌ 任务异常结束", ex=3600)
                    r.set(f"result:{email}", "fail", ex=3600)
                else:
                    pending_game = r.get(f"pending_game:{email}")
                    if pending_game:
                        report_success(email, pending_game)
                    if is_already_owned or "已在库中" in line:
                        r.set(f"status:{email}", "✅ 任务完成（已在库中）", ex=3600)
                        r.set(f"result:{email}", "success_owned", ex=3600)
                    else:
                        r.set(f"status:{email}", "🎉 领取成功！", ex=3600)
                        r.set(f"result:{email}", "success_new", ex=3600)

        process.wait()

        # 正常结束，执行常规瘦身
        clean_user_profile(email)

        if mode == 'verify':
            if is_login_success and not is_fatal_failure and not has_critical_error:
                r.set(f"result:{email}", "success", ex=3600)
                r.set(f"status:{email}", "✅ 验证通过", ex=3600)
            else:
                if not r.get(f"result:{email}"):
                    r.set(f"result:{email}", "fail", ex=3600)
                    if not r.get(f"status:{email}"):
                        r.set(f"status:{email}", "❌ 验证失败", ex=3600)

    except Exception as e:
        print(f"Error: {e}")
        r.set(f"status:{email}", "❌ 系统错误", ex=3600)
        r.set(f"result:{email}", "fail", ex=3600)

def main_loop():
    while True:
        task = r.blpop("task_queue", timeout=10)
        if task:
            _, data_json = task
            try: run_task(json.loads(data_json))
            except: pass
        time.sleep(0.1)

if __name__ == "__main__":
    main_loop()