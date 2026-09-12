import atexit
import datetime
import hashlib
import hmac
import os
import threading
import pathlib
import pytz
import secrets
import subprocess
from apscheduler.events import (
    EVENT_ALL,
    EVENT_EXECUTOR_ADDED,
    EVENT_EXECUTOR_REMOVED,
    EVENT_JOB_ADDED,
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_MODIFIED,
    EVENT_JOB_REMOVED,
    EVENT_JOB_SUBMITTED,
    EVENT_JOBSTORE_ADDED,
    EVENT_JOBSTORE_REMOVED,
)
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    session,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, generate_csrf
from chanlun import config, fun
from chanlun.security import (
    get_flask_secret_key,
    get_login_accounts,
    get_web_host,
    is_https_enabled,
    normalize_login_username,
    validate_web_security_config,
    verify_login_password,
)

__all__ = ["create_app"]


_TASK_HISTORY_LIMIT = 500
_TASK_TERMINAL_STATES = {"已完成", "执行异常", "未执行", "删除作业"}
_SHARED_RUNTIME_OWNER_LOCK = threading.RLock()
_SHARED_RUNTIME_OWNER: object | None = None
def _configured_login_accounts():
    """Resolve the configured named Web accounts."""

    return get_login_accounts()




def _trim_task_history(task_map, limit: int = _TASK_HISTORY_LIMIT) -> None:
    terminal_ids = [
        task_id
        for task_id, task in task_map.items()
        if task.get("state") in _TASK_TERMINAL_STATES
    ]
    excess = max(0, len(terminal_ids) - max(0, int(limit)))
    for task_id in terminal_ids[:excess]:
        task_map.pop(task_id, None)


def _scheduler_task_snapshot(scheduler):
    lock = getattr(scheduler, "my_task_lock", None)
    if lock is None:
        task_map = dict(scheduler.my_task_list)
    else:
        with lock:
            task_map = dict(scheduler.my_task_list)
    snapshot = []
    for task in task_map.values():
        row = dict(task)
        row["name"] = str(row.get("name") or "").strip() or "--"
        snapshot.append(row)
    return snapshot


def create_app(test_config=None, start_scheduler=False):
    # 应用工厂默认不得产生副作用；单进程桌面入口会显式启用，测试和通用 WSGI 导入不会启用。
    app = Flask(__name__, instance_relative_config=True)
    https_enabled = is_https_enabled()
    secure_cookie_setting = (
        os.environ.get("CHANLUN_SESSION_COOKIE_SECURE", "").strip().lower()
    )
    secure_cookie_enabled = https_enabled or secure_cookie_setting in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.config.from_mapping(
        WEB_HOST=get_web_host(),
        CHART_ONLY_MODE=(
            os.environ.get("CHANLUN_CHART_ONLY", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
        VALIDATE_WEB_SECURITY=True,
        SCHEDULER_ENABLED=bool(start_scheduler),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie_enabled,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=secure_cookie_enabled,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
        # 可信浏览器在应用或浏览器重启后保持登录；使用期间有效期滚动延长，但修改密码
        # 或显式退出仍会撤销现有登录身份。
        REMEMBER_COOKIE_DURATION=datetime.timedelta(days=30),
        REMEMBER_COOKIE_REFRESH_EACH_REQUEST=True,
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        MAX_FORM_PARTS=500,
        WTF_CSRF_TIME_LIMIT=12 * 60 * 60,
        READINESS_MARKETS=os.environ.get("CHANLUN_READINESS_MARKETS", "a"),
        SYMBOL_CATALOG_VALIDATION_CODES=os.environ.get(
            "CHANLUN_SYMBOL_CATALOG_VALIDATION_CODES",
            "",
        ).strip(),
        SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED=(
            os.environ.get(
                "CHANLUN_SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED",
                "0",
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        ),
        # 生产实时选股只发技术/手工买卖提醒，不读取正式研究账本。正式研究材料仍可在
        # 离线研究和回放入口使用，但不能成为生产监听的隐藏依赖。
        # 开发与策略验证默认不运行全市场预选。只有最终验收/生产运行显式设置环境变量
        # 为 1 才开启盘后完整覆盖，避免每次代码修改都重新处理五千余只标的。
        # 仅供一次明确运维启动使用：在当前逻辑的完整快照发布前绕过常规盘后窗口。
        # 环境变量不写入项目配置，完成后即使进程仍存活也会自动恢复时段闸门。
        # 低频候选必须在下一次 1m 监听到期前停止接纳新任务；剩余标的下一轮继续。
        # 系统本地且独立于市场的自选组用于声明手工持仓；它只是一项监听事实，不能由成员
        # 关系推断券商/账户访问权或下单能力。
        # 只有明确的人工关注组进入分钟级优先监听。旧版通用选股生成的结果组不再被
        # 隐式并入；它们仍保留在自选数据库中，且不影响独立的全市场收盘后扫描。
        # This gate is independent from A-share screening authorization.
        # 修改与策略验证阶段每轮仍只处理 12 只。仅在大范围和完整覆盖两个独立
        # 授权同时开启时使用固定 240 只批次，让十二个结构进程各自保持约二十个
        # 连续任务，摊薄每轮发布和长尾等待；盘中 5m 实时候选仍独立限制为 48 只。
        # Scheduling the full armed/triggered universe is cheap; the absolute
        # round deadline still limits how much native work may actually start.
        # QMT 的历史补数 RPC 在正常情况下也可能接近 150 秒才返回，结构进程必须给它
        # 留出完整窗口；过早终止会触发 30 秒退避，并让同批后续标的被连带记为不可用。
        # Web 与实时 Tick 已隔离且实时请求繁忙时不排队，因此这里延长等待不会拖死网页。
        # 原生结构库的长期内存不会完全归还给 Windows。当前候选池约两千只，过早在
        # 1024 次请求回收会使进程永远无法走完一次缓存轮回；默认允许覆盖完整候选池。
        # 32-GiB 生产机上 1536 MiB × 12 会与 MiniQMT 一起触发系统提交耗尽，因此在
        # 1280 MiB 的安全请求边界提前回收；显式环境变量仍可按更大主机容量调高。
        # QMT 本地 RPC 以等待为主，结构进程按四分之三逻辑 CPU 扩张以覆盖等待
        # 时间；上限十二个并保留其余 CPU 给 Web、实时监听和 QMT。另有控制进程。
        # app.py 是前向业务调度的唯一所有者。
        # app.py 也是交互式 QMT 运行时的唯一所有者。
    )
    if test_config:
        app.config.update(test_config)
    from .services.chart_only import apply_chart_only_mode

    apply_chart_only_mode(app.config)
    if https_enabled:
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["REMEMBER_COOKIE_SECURE"] = True
    if app.config.get("VALIDATE_WEB_SECURITY", True):
        validate_web_security_config(
            app.config["WEB_HOST"],
            accounts=_configured_login_accounts(),
        )
    scheduler_enabled = bool(app.config.get("SCHEDULER_ENABLED", False))
    app.logger.addFilter(lambda record: "/static/" not in record.getMessage().lower())

    # 任务对象
    from .services.scheduler_executor import RestartableDaemonPoolExecutor

    scheduler = BackgroundScheduler(
        timezone=pytz.timezone("Asia/Shanghai"),
        executors={
            "default": RestartableDaemonPoolExecutor(
                max_workers=8,
                max_pending=64,
            ),
        },
    )
    scheduler.my_task_list = {}
    scheduler.my_task_lock = threading.RLock()




    def run_tasks_listener(event):
        state_map = {
            EVENT_EXECUTOR_ADDED: "已添加",
            EVENT_EXECUTOR_REMOVED: "删除调度",
            EVENT_JOBSTORE_ADDED: "已添加",
            EVENT_JOBSTORE_REMOVED: "删除存储",
            EVENT_JOB_ADDED: "已添加",
            EVENT_JOB_REMOVED: "删除作业",
            EVENT_JOB_MODIFIED: "修改作业",
            EVENT_JOB_SUBMITTED: "运行中",
            EVENT_JOB_MAX_INSTANCES: "等待运行",
            EVENT_JOB_EXECUTED: "已完成",
            EVENT_JOB_ERROR: "执行异常",
            EVENT_JOB_MISSED: "未执行",
        }
        if event.code not in state_map.keys():
            return
        if hasattr(event, "job_id"):
            job_id = event.job_id
            with scheduler.my_task_lock:
                if job_id not in scheduler.my_task_list:
                    scheduler.my_task_list[job_id] = {
                        "id": job_id,
                        "name": "--",
                        "update_dt": fun.datetime_to_str(datetime.datetime.now()),
                        "next_run_dt": "--",
                        "state": "未知",
                    }
                task = scheduler.my_task_list[job_id]
                task["update_dt"] = fun.datetime_to_str(datetime.datetime.now())
                job = scheduler.get_job(event.job_id)
                if job is not None:
                    task["name"] = job.name
                    task["next_run_dt"] = fun.datetime_to_str(job.next_run_time)
                task["state"] = state_map[event.code]
                _trim_task_history(scheduler.my_task_list)
        return

    scheduler.add_listener(run_tasks_listener, EVENT_ALL)

    # 统一从 services.constants 引用常量，降低耦合
    from .services.constants import (
        market_frequencys,
        market_default_codes,
        market_types,
    )
    from .services import constants as constants_service
    from .services import stock_list as stock_list_service
    from .services import readiness as readiness_service
    from .services import chart_initial_build as chart_initial_build_service

    # Install the independent identity-catalog admission before a preload
    # thread, synchronous search fallback, or disk hydration can run.
    stock_list_service.configure_symbol_catalog(
        validation_codes=app.config.get("SYMBOL_CATALOG_VALIDATION_CODES"),
        full_catalog_authorized=app.config.get(
            "SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED", False
        ),
    )

    configured_readiness_markets = app.config.get("READINESS_MARKETS", "a")
    if isinstance(configured_readiness_markets, str):
        configured_readiness_markets = configured_readiness_markets.split(",")
    readiness_markets = tuple(
        dict.fromkeys(
            str(market).strip().lower()
            for market in configured_readiness_markets
            if str(market).strip().lower() in market_types
        )
    ) or ("a",)
    app.config["READINESS_MARKETS"] = readiness_markets

    readiness_registry = readiness_service.ReadinessRegistry()
    metadata_warmup_thread = None



    __log = fun.get_logger()

    # 强制 Jinja2 每次请求都从磁盘 re-render template,避免 web 长跑后
    # ``index.html`` 内 ``{{ static_asset_token }}`` 等动态变量被内存缓存。
    # 性能代价微小(主页面 template,只在 / 请求时 render)。
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # 添加登录验证
    # 密钥解析顺序：环境变量 CHANLUN_FLASK_SECRET_KEY > config.FLASK_SECRET_KEY > 数据目录持久化文件。
    app.secret_key = get_flask_secret_key()

    # 跨站请求伪造令牌与当前浏览器会话使用同一 12 小时边界；记住登录 Cookie 独立续期。
    from .csrf import csrf

    csrf.init_app(app)

    # 静态资源 cache-bust:Tornado static handler 给所有 /static/* 加
    # ``Cache-Control: max-age=31536000, immutable``,导致 charts.js / bundle.js
    # 等核心前端文件被浏览器**永久缓存**——后端修了字段但前端永远拉不到新版本。
    # 修法:在 Jinja2 模板里给 ``<script src>`` 加 ``?asset={{ static_asset_token }}``,
    # 静态资源令牌取关键文件修改时间的短哈希，文件变化即刷新缓存。
    @app.before_request
    def _set_csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(24)

    @app.before_request
    def _enforce_runtime_web_security():
        if not app.config.get("VALIDATE_WEB_SECURITY", True):
            return None
        try:
            validate_web_security_config(
                request.remote_addr or "",
                accounts=_configured_login_accounts(),
            )
        except ValueError:
            return {"status": "security_misconfigured"}, 503
        return None

    @app.context_processor
    def inject_static_asset_token():
        import hashlib

        h = hashlib.md5()
        # 聚合前端 js/css、datafeed bundle 与固定 Charting Library 入口的 mtime+size：
        # 改动都会让 static_asset_token 变化，模板里带 ?asset={{ static_asset_token }} 的资源
        # 随之 cache-bust，用户改前端后普通刷新即可生效，无需手动硬刷新。
        targets = [
            os.path.join(app.static_folder, "favicon.ico"),
            os.path.join(app.static_folder, "chanlun-mark.png"),
            os.path.join(app.static_folder, "datafeeds", "udf", "dist", "bundle.js"),
            os.path.join(
                app.static_folder,
                "charting_library",
                "charting_library.standalone.js",
            ),
            os.path.join(app.static_folder, "charting_library", "sameorigin.html"),
        ]
        for sub in ("js", "css"):
            sub_dir = os.path.join(app.static_folder, sub)
            for root, _dirs, fnames in os.walk(sub_dir):
                for fn in fnames:
                    if fn.endswith((".js", ".css")):
                        targets.append(os.path.join(root, fn))
        for f in sorted(targets):
            try:
                h.update(f.encode())
                h.update(str(os.path.getmtime(f)).encode())
                h.update(str(os.path.getsize(f)).encode())
            except OSError:
                pass
        return {
            "static_asset_token": h.hexdigest()[:10],
            "csp_nonce": getattr(g, "csp_nonce", ""),
        }

    @app.after_request
    def _set_security_headers(resp):
        static_filename = str((request.view_args or {}).get("filename", ""))
        is_charting_vendor_shell = (
            request.endpoint == "static"
            and static_filename.replace("\\", "/") == "charting_library/sameorigin.html"
        )
        if not is_charting_vendor_shell:
            nonce = getattr(g, "csp_nonce", "")
            script_sources = ["'self'", f"'nonce-{nonce}'", "'unsafe-eval'"]
            resp.headers.setdefault(
                "Content-Security-Policy",
                "; ".join(
                    [
                        "default-src 'self'",
                        f"script-src {' '.join(script_sources)}",
                        "style-src 'self' 'unsafe-inline'",
                        "img-src 'self' data: blob: https:",
                        "font-src 'self' data: http://at.alicdn.com https://at.alicdn.com",
                        "connect-src 'self' ws: wss: https:",
                        "frame-src 'self'",
                        "worker-src 'self' blob:",
                        "object-src 'none'",
                        "base-uri 'self'",
                        "frame-ancestors 'self'",
                        "form-action 'self'",
                    ]
                ),
            )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        return resp

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login_opt"

    def _is_api_request() -> bool:
        if request.method != "GET" and request.path not in {"/login", "/logout"}:
            return True
        api_prefixes = (
            "/api/",
            "/ticks",
            "/tv/",
            "/symbols/",
            "/get_zixuan_",
            "/get_stock_zixuan/",
            "/a/bkgn_",
            "/get_cl_config/",
        )
        return request.path.startswith(api_prefixes)

    @login_manager.unauthorized_handler
    def _unauthorized():
        if _is_api_request():
            return {
                "ok": False,
                "s": "error",
                "code": "authentication_required",
                "errmsg": "Authentication required.",
            }, 401
        return redirect("/login")

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(error):
        if _is_api_request():
            return {
                "ok": False,
                "s": "error",
                "code": "csrf_failed",
                "errmsg": "CSRF token is missing or expired.",
            }, 400
        return render_template("login.html", emsg=error.description), 400

    from .services.account_preferences import (
        InvalidAccountPreferences,
        empty_preferences,
        load_preferences_for_user,
        save_preferences_for_user,
        storage_scope_for_username,
        storage_user_id_for_username,
    )

    def _login_session_user_id(account) -> str:
        secret = app.secret_key
        secret_bytes = secret if isinstance(secret, bytes) else str(secret).encode()
        digest = hmac.new(
            secret_bytes,
            (
                "chanlun-pro-login-session\0"
                + account.username
                + "\0"
                + account.password_hash
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"cl_pro:{storage_scope_for_username(account.username)[:24]}:{digest}"

    class LoginUser(UserMixin):
        def __init__(self, account, user_id=None) -> None:
            super().__init__()
            self.username = account.username
            self.storage_scope = storage_scope_for_username(account.username)
            self.storage_user_id = storage_user_id_for_username(account.username)
            self.id = user_id or _login_session_user_id(account)

    @login_manager.user_loader
    def load_user(user_id):
        if not isinstance(user_id, str):
            return None
        accounts = _configured_login_accounts()
        for account in accounts:
            expected = _login_session_user_id(account)
            if hmac.compare_digest(user_id, expected):
                return LoginUser(account, expected)
        return None

    @app.get("/favicon.ico")
    def favicon():
        """Serve the conventional browser favicon path without an avoidable 404."""
        return app.send_static_file("favicon.ico")

    from .services.login_rate_limit import LoginRateLimiter

    login_rate_limiter = LoginRateLimiter()

    @app.route("/login", methods=["GET", "POST"])
    def login_opt():
        accounts = _configured_login_accounts()
        remember_duration = app.config["REMEMBER_COOKIE_DURATION"]

        emsg = ""
        submitted_username = ""
        if request.method == "POST":
            client_key = request.remote_addr or "unknown"
            if login_rate_limiter.is_blocked(client_key):
                return render_template(
                    "login.html",
                    emsg="尝试次数过多，请稍后再试",
                    login_username=request.form.get("username") or "",
                ), 429

            submitted_username = normalize_login_username(
                request.form.get("username") or ""
            )
            account = next(
                (
                    candidate
                    for candidate in accounts
                    if hmac.compare_digest(candidate.username, submitted_username)
                ),
                None,
            )
            password = request.form.get("password") or ""
            if account is not None and verify_login_password(
                password, account.password_hash
            ):
                login_rate_limiter.clear(client_key)
                session.clear()
                login_user(
                    LoginUser(account),
                    remember=True,
                    duration=remember_duration,
                )
                return redirect("/")

            login_rate_limiter.record_failure(client_key)
            emsg = "用户名或密码错误"

        if not submitted_username and len(accounts) == 1:
            submitted_username = accounts[0].username
        return render_template(
            "login.html",
            emsg=emsg,
            login_username=submitted_username,
        )

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout_opt():
        session.clear()
        logout_user()
        return redirect("/login")

    @app.route("/api/session")
    @login_required
    def api_session():
        return {
            "ok": True,
            "csrf_token": generate_csrf(),
            "account": {"username": current_user.username},
        }

    @app.route("/api/chart/preferences", methods=["GET", "PUT"])
    @login_required
    def api_chart_preferences():
        if request.method == "GET":
            preferences, exists, updated_at = load_preferences_for_user(current_user)
            return {
                "ok": True,
                "preferences": preferences,
                "exists": exists,
                "updated_at": updated_at,
            }
        try:
            preferences = save_preferences_for_user(
                current_user,
                request.get_json(silent=True),
            )
        except InvalidAccountPreferences as exc:
            return {
                "ok": False,
                "code": "invalid_chart_preferences",
                "message": str(exc),
            }, 400
        return {"ok": True, "preferences": preferences}

    @app.context_processor
    def inject_account_context():
        if not current_user.is_authenticated:
            return {"account_username": "", "account_preferences_bootstrap": None}
        try:
            preferences, exists, updated_at = load_preferences_for_user(current_user)
        except Exception:
            app.logger.exception("账号图表偏好读取失败")
            preferences, exists, updated_at = empty_preferences(), False, None
        return {
            "account_username": current_user.username,
            "account_preferences_bootstrap": {
                "username": current_user.username,
                "account_key": current_user.storage_scope[:24],
                "preferences": preferences,
                "exists": exists,
                "updated_at": updated_at,
            },
        }

    def _runtime_revision() -> str:
        configured = os.environ.get("CHANLUN_BUILD_REVISION", "").strip()
        if configured:
            return configured
        project_root = pathlib.Path(__file__).resolve().parents[3]
        try:
            completed = subprocess.run(
                ["git", "-C", str(project_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return completed.stdout.strip() or "unknown"

    build_revision = _runtime_revision()

    def _readyz_snapshot(market):
        market = (market or "a").strip().lower()
        if market not in market_types:
            return {
                "status": "not_ready",
                "revision": build_revision,
                "pid": os.getpid(),
                "market": market,
                "components": {},
                "reasons": ["invalid_market"],
            }, 400
        if market not in readiness_markets:
            return {
                "status": "not_ready",
                "revision": build_revision,
                "pid": os.getpid(),
                "market": market,
                "components": {},
                "reasons": ["market_not_monitored"],
            }, 400

        metadata_ready = False
        try:
            metadata_ready = all(
                mapping.status(market).get("ready", False)
                for mapping in (
                    constants_service.market_frequencys,
                    constants_service.market_default_codes,
                )
            )
        except Exception:
            app.logger.exception("readiness metadata snapshot failed")
            metadata_ready = False
        metadata_component = {
            "ready": metadata_ready,
            "status": "ready" if metadata_ready else "not_ready",
        }

        try:
            symbol_state = stock_list_service.get_symbol_readiness(market)
            symbol_count = int(symbol_state.get("count", 0))
            symbols_component = {
                "market": market,
                "ready": bool(symbol_state.get("ready")),
                "status": str(symbol_state.get("status") or "not_ready"),
                "count": max(0, symbol_count),
                "catalog_mode": str(
                    symbol_state.get("catalog_mode")
                    or stock_list_service.BOUNDED_VALIDATION_CATALOG
                ),
                "admitted_count": max(
                    0, int(symbol_state.get("admitted_count", 0))
                ),
                "full_catalog_authorized": bool(
                    symbol_state.get("full_catalog_authorized", False)
                ),
                "error": (
                    str(symbol_state.get("last_error"))[:200]
                    if symbol_state.get("last_error")
                    else None
                ),
            }
        except Exception:
            app.logger.exception("readiness symbol snapshot failed")
            symbols_component = {
                "market": market,
                "ready": False,
                "status": "not_ready",
                "count": 0,
                "catalog_mode": stock_list_service.BOUNDED_VALIDATION_CATALOG,
                "admitted_count": 0,
                "full_catalog_authorized": False,
                "error": "symbol_readiness_failed",
            }

        ticks_component = readiness_registry.ticks_snapshot(market)
        scheduler_required = bool(app.config.get("SCHEDULER_ENABLED", False))
        scheduler_ready = bool(scheduler.running) if scheduler_required else True
        scheduler_component = {
            "required": scheduler_required,
            "ready": scheduler_ready,
            "status": (
                "running"
                if scheduler_required and scheduler_ready
                else "stopped"
                if scheduler_required
                else "disabled"
            ),
        }
        if scheduler_required:
            runtime_probe = app.extensions.get("runtime_status")
            runtime_component = (
                runtime_probe() if callable(runtime_probe) else runtime_status()
            )
            runtime_component["required"] = True
        else:
            runtime_component = {
                "required": False,
                "ready": True,
                "status": "disabled",
                "error": None,
            }

        reasons = []
        if not metadata_component["ready"]:
            reasons.append("metadata_not_ready")
        if not symbols_component["ready"]:
            reasons.append("symbols_not_ready")
        if ticks_component["required"] and not ticks_component["ready"]:
            if ticks_component["status"] == "unknown":
                reasons.append("ticks_not_ready")
            elif ticks_component["status"] == "stale":
                reasons.append("ticks_stale")
            else:
                reasons.append("ticks_dependency_error")
        if scheduler_required and not runtime_component["ready"]:
            if runtime_component["status"] == "starting":
                reasons.append("runtime_starting")
            elif runtime_component.get("error"):
                reasons.append("runtime_start_failed")
            else:
                reasons.append("runtime_not_running")
        if scheduler_required and not scheduler_ready:
            reasons.append("scheduler_not_running")
        ready = not reasons
        payload = {
            "status": "ready" if ready else "not_ready",
            "runtime_ready": ready,
            "status_scope": "PROCESS_RUNTIME",
            "revision": build_revision,
            "pid": os.getpid(),
            "market": market,
            "components": {
                "scheduler": scheduler_component,
                "runtime": runtime_component,
                "metadata": metadata_component,
                "symbols": symbols_component,
                "ticks": ticks_component,
            },
            "reasons": reasons,
        }
        return payload, 200 if ready else 503

    def health_snapshot(kind, market="a"):
        if kind == "livez":
            return {"status": "alive", "revision": build_revision}, 200
        if kind == "healthz":
            return {"status": "ok", "revision": build_revision}, 200
        if kind == "readyz":
            return _readyz_snapshot(market)
        return {"status": "not_found", "revision": build_revision}, 404

    @app.route("/livez")
    def livez():
        return health_snapshot("livez")[0]

    @app.route("/healthz")
    def healthz():
        return health_snapshot("healthz")[0]

    @app.route("/readyz")
    def readyz():
        return health_snapshot(
            "readyz",
            request.args.get("market") or "a",
        )

    @app.route("/")
    @login_required
    def index_show():
        requested_market = (request.args.get("market") or "a").strip().lower()
        initial_market = requested_market if requested_market in market_types else "a"
        selected_default_codes = market_default_codes.cached_snapshot()
        selected_frequencies = market_frequencys.cached_snapshot()
        selected_default_codes.update(market_default_codes.snapshot((initial_market,)))
        selected_frequencies.update(market_frequencys.snapshot((initial_market,)))

        return render_template(
            "index.html",
            market_default_codes=selected_default_codes,
            market_frequencys=selected_frequencies,
            initial_market=initial_market,
            enable_sse=config.ENABLE_SSE_PUSH,
        )

    from .blueprints.tv import tv_bp
    from .blueprints.zixuan import zixuan_bp
    from .blueprints.jobs import jobs_bp
    from .blueprints.setting import setting_bp
    from .blueprints.bkgn import bkgn_bp
    from .blueprints.other import other_bp
    from .blueprints.options import options_bp
    from .blueprints.symbols import symbols_bp

    for blueprint in (
        tv_bp,
        zixuan_bp,
        jobs_bp,
        setting_bp,
        bkgn_bp,
        other_bp,
        options_bp,
        symbols_bp,
    ):
        app.register_blueprint(blueprint)

    from chanlun.persistence import file_db as file_db_service
    from .services import chart_cache as chart_cache_service
    from .services import chart_revalidate as chart_revalidate_service
    from .handlers import sse_stream as sse_stream_service

    file_db_service.allow_lazy_pickle_writes()
    chart_cache_service.allow_lazy_chart_cache_writes()

    runtime_lock = threading.RLock()
    runtime_cleanup_lock = threading.Lock()
    runtime_owner_token = object()
    runtime_state = {
        "started": False,
        "stopping": False,
        "status": "stopped",
        "error": None,
        "active_starts": 0,
        "shutdown_complete": True,
        "owns_shared_runtime": False,
        "generation": 0,
        "stop_event": threading.Event(),
        "scheduler_enabled": None,
        "scheduler_start_attempted": False,
        "metadata": None,
        "ticks": None,
        "symbols": None,
    }
    # 在 QMT 与正式交易日提供器创建后再构造。生命周期闭包有意引用这个后绑定控制器，
    # 使应用工厂在 ``start_runtime_services()`` 前保持无副作用。

    def _probe_ticks(market):
        default_code = (
            constants_service.market_default_codes.cached_snapshot((market,)).get(
                market
            )
            or ""
        )
        if not default_code:
            raise RuntimeError("default market code is not ready")
        from chanlun.exchange import get_exchange, market_now_trading
        from chanlun.market import Market

        exchange = get_exchange(Market(market))
        values = exchange.ticks([default_code]) or {}
        usable = {
            code: tick
            for code, tick in values.items()
            if tick is not None and getattr(tick, "last", None) is not None
        }
        if usable:
            return usable
        if market_now_trading(exchange, market) is False:
            return {"__market_closed__": True}
        return {}

    def start_runtime_services(enable_scheduler=True):
        global _SHARED_RUNTIME_OWNER
        nonlocal metadata_warmup_thread
        if app.config.get("CHART_ONLY_MODE", False):
            # Desktop/WSGI callers cannot implicitly resume paused producers.
            enable_scheduler = False
        with runtime_lock:
            if runtime_state["status"] == "running":
                if runtime_state["scheduler_enabled"] != bool(enable_scheduler):
                    raise RuntimeError(
                        "runtime services already running with a different scheduler mode"
                    )
                return
            if runtime_state["status"] == "starting":
                raise RuntimeError("runtime services are starting")
            if runtime_state["stopping"] or runtime_state["active_starts"]:
                raise RuntimeError("runtime services are stopping")
            with _SHARED_RUNTIME_OWNER_LOCK:
                if (
                    _SHARED_RUNTIME_OWNER is not None
                    and _SHARED_RUNTIME_OWNER is not runtime_owner_token
                ):
                    raise RuntimeError(
                        "runtime services are owned by another app instance"
                    )
                _SHARED_RUNTIME_OWNER = runtime_owner_token
                runtime_state["owns_shared_runtime"] = True
            runtime_state["generation"] += 1
            start_generation = runtime_state["generation"]
            start_stop_event = threading.Event()
            runtime_state["stop_event"] = start_stop_event
            runtime_state["started"] = True
            runtime_state["status"] = "starting"
            runtime_state["error"] = None
            runtime_state["active_starts"] += 1
            runtime_state["shutdown_complete"] = False
            runtime_state["scheduler_enabled"] = bool(enable_scheduler)
            app.config["SCHEDULER_ENABLED"] = bool(enable_scheduler)

        def _ensure_start_is_current():
            with runtime_lock:
                is_current = (
                    runtime_state["generation"] == start_generation
                    and runtime_state["status"] != "stopping"
                )
            if start_stop_event.is_set() or not is_current:
                raise RuntimeError("runtime services are stopping")

        try:
            constants_service.start_market_metadata_loaders()
            _ensure_start_is_current()
            chart_cache_service.start_chart_cache_runtime()
            _ensure_start_is_current()
            chart_initial_build_service.start_initial_build_runtime()
            _ensure_start_is_current()
            file_db_service.start_pickle_writes()
            _ensure_start_is_current()
            metadata_handles = [
                readiness_service.start_metadata_warmup(constants_service, market)
                for market in readiness_markets
            ]
            metadata_warmup_thread = metadata_handles[0]
            runtime_state["metadata"] = metadata_handles
            _ensure_start_is_current()
            runtime_state["symbols"] = stock_list_service.start_symbol_preload_thread()
            _ensure_start_is_current()
            runtime_state["ticks"] = [
                readiness_service.start_ticks_warmup(
                    readiness_registry, _probe_ticks, market
                )
                for market in readiness_markets
            ]
            _ensure_start_is_current()

            chart_revalidate_service.start_revalidation_runtime()
            _ensure_start_is_current()
            sse_stream_service.start_sse_runtime()
            _ensure_start_is_current()
            if enable_scheduler:
                with runtime_lock:
                    runtime_state["scheduler_start_attempted"] = True
                scheduler.start()

            _ensure_start_is_current()
            app.extensions["metadata_warmup_thread"] = metadata_warmup_thread
            with runtime_lock:
                if (
                    start_stop_event.is_set()
                    or runtime_state["generation"] != start_generation
                    or runtime_state["status"] != "starting"
                ):
                    raise RuntimeError("runtime services are stopping")
                runtime_state["status"] = "running"
        except BaseException as exc:
            with runtime_lock:
                runtime_state["error"] = str(exc)[:200]
            shutdown_runtime_services()
            raise
        finally:
            with runtime_lock:
                runtime_state["active_starts"] -= 1
                if (
                    runtime_state["active_starts"] == 0
                    and runtime_state["status"] == "stopped"
                    and runtime_state["owns_shared_runtime"]
                ):
                    with _SHARED_RUNTIME_OWNER_LOCK:
                        if _SHARED_RUNTIME_OWNER is runtime_owner_token:
                            _SHARED_RUNTIME_OWNER = None
                    runtime_state["owns_shared_runtime"] = False

    def shutdown_runtime_services():
        global _SHARED_RUNTIME_OWNER
        with runtime_cleanup_lock:
            with runtime_lock:
                if (
                    not runtime_state["started"]
                    and runtime_state["status"] == "stopped"
                    and runtime_state["shutdown_complete"]
                ):
                    return
                if not runtime_state["owns_shared_runtime"]:
                    # 应用工厂默认无副作用；未取得进程级服务所有权的应用不得停止所有者应用的
                    # 服务器推送、缓存、元数据或重校验工作线程。
                    runtime_state["shutdown_complete"] = True
                    return
                runtime_state["stop_event"].set()
                runtime_state["stopping"] = True
                runtime_state["status"] = "stopping"

            cleanup_errors = []

            def _cleanup(label, operation):
                try:
                    operation()
                except Exception as exc:
                    cleanup_errors.append(f"{label}: {exc}")
                    app.logger.exception("runtime cleanup failed: %s", label)

            def _shutdown_scheduler_resources():
                try:
                    if scheduler.running:
                        scheduler.shutdown(wait=False)
                    elif runtime_state.get("scheduler_start_attempted"):
                        resource_errors = []
                        for alias, executor in tuple(
                            getattr(scheduler, "_executors", {}).items()
                        ):
                            try:
                                executor.shutdown(wait=True)
                            except Exception as exc:
                                resource_errors.append(f"executor {alias}: {exc}")
                        for alias, jobstore in tuple(
                            getattr(scheduler, "_jobstores", {}).items()
                        ):
                            try:
                                jobstore.shutdown()
                            except Exception as exc:
                                resource_errors.append(f"jobstore {alias}: {exc}")
                        if resource_errors:
                            raise RuntimeError("; ".join(resource_errors))
                    with runtime_lock:
                        runtime_state["scheduler_start_attempted"] = False
                except Exception:
                    raise

            _cleanup("scheduler", _shutdown_scheduler_resources)
            _cleanup("chart-initial-build", chart_initial_build_service.shutdown_initial_build_runtime)
            def _handles_for(key):
                value = runtime_state.get(key)
                if isinstance(value, (list, tuple)):
                    return list(value)
                return [] if value is None else [value]

            for key in ("metadata", "ticks", "symbols"):
                for handle in _handles_for(key):
                    if hasattr(handle, "stop"):
                        _cleanup(f"stop-{key}", handle.stop)
            for key in ("metadata", "ticks", "symbols"):
                for handle in _handles_for(key):
                    if hasattr(handle, "join"):
                        _cleanup(
                            f"join-{key}",
                            lambda h=handle: h.join(timeout=1.0),
                        )

            _cleanup("sse", sse_stream_service.shutdown_sse_runtime)
            _cleanup(
                "revalidation",
                lambda: chart_revalidate_service.shutdown_revalidation(
                    wait=False, timeout=1.0
                ),
            )
            _cleanup(
                "symbol-preload",
                lambda: stock_list_service.shutdown_symbol_preload(timeout=1.0),
            )
            _cleanup(
                "chart-cache",
                lambda: chart_cache_service.shutdown_chart_cache_runtime(wait=False),
            )
            _cleanup(
                "pickle-writes",
                lambda: file_db_service.shutdown_pickle_writes(
                    wait=False, cancel_pending=True
                ),
            )
            _cleanup(
                "metadata-loaders",
                lambda: constants_service.shutdown_market_metadata_loaders(timeout=0.1),
            )
            with runtime_lock:
                runtime_state.update(
                    {
                        "started": False,
                        "stopping": False,
                        "status": "stopped",
                        "shutdown_complete": not cleanup_errors,
                        "scheduler_enabled": None,
                        "error": (
                            "; ".join(cleanup_errors)[:200]
                            if cleanup_errors
                            else runtime_state.get("error")
                        ),
                        "metadata": None,
                        "ticks": None,
                        "symbols": None,
                    }
                )
                if runtime_state["active_starts"] == 0:
                    with _SHARED_RUNTIME_OWNER_LOCK:
                        if _SHARED_RUNTIME_OWNER is runtime_owner_token:
                            _SHARED_RUNTIME_OWNER = None
                    runtime_state["owns_shared_runtime"] = False

    def runtime_status():
        with runtime_lock:
            status = str(runtime_state["status"])
            return {
                "ready": status == "running",
                "status": status,
                "error": runtime_state.get("error"),
            }

    def shutdown_scheduler():
        shutdown_runtime_services()

    app.extensions.update({
        "scheduler": scheduler,
        "readiness": readiness_registry,
        "metadata_warmup_thread": metadata_warmup_thread,
        "login_rate_limiter": login_rate_limiter,
        "health_snapshot": health_snapshot,
        "runtime_status": runtime_status,
        "start_runtime_services": start_runtime_services,
        "shutdown_runtime_services": shutdown_runtime_services,
        "shutdown_scheduler": shutdown_scheduler,
    })
    if scheduler_enabled:
        start_runtime_services(enable_scheduler=True)
        atexit.register(shutdown_runtime_services)
    return app
