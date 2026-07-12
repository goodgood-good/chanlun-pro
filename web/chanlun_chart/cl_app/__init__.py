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
from collections.abc import Mapping
from decimal import Decimal
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
from flask import Flask, g, has_request_context, redirect, render_template, request, session
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError, generate_csrf
from chanlun import config, fun
from chanlun.security import (
    get_flask_secret_key,
    get_login_password,
    get_web_host,
    is_https_enabled,
    validate_web_security_config,
    verify_login_password,
)
__all__ = ["create_app"]


_TASK_HISTORY_LIMIT = 500
_TASK_TERMINAL_STATES = {"已完成", "执行异常", "未执行", "删除作业"}


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
    return [dict(task) for task in task_map.values()]


def create_app(test_config=None, start_scheduler=False):
    # App factories must be side-effect free by default. The single-process
    # desktop entrypoint opts in explicitly; tests and generic WSGI imports do not.
    app = Flask(__name__, instance_relative_config=True)
    https_enabled = is_https_enabled()
    secure_cookie_setting = os.environ.get(
        "CHANLUN_SESSION_COOKIE_SECURE", ""
    ).strip().lower()
    secure_cookie_enabled = https_enabled or secure_cookie_setting in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.config.from_mapping(
        WEB_HOST=get_web_host(),
        VALIDATE_WEB_SECURITY=True,
        SCHEDULER_ENABLED=bool(start_scheduler),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie_enabled,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=secure_cookie_enabled,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
        REMEMBER_COOKIE_DURATION=datetime.timedelta(hours=12),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        MAX_FORM_PARTS=500,
        WTF_CSRF_TIME_LIMIT=12 * 60 * 60,
        READINESS_MARKETS=os.environ.get("CHANLUN_READINESS_MARKETS", "a"),
        DECISION_SUPPORT_ENABLED=False,
        DECISION_SUPPORT_ACCOUNT_PROVIDER=None,
        DECISION_SUPPORT_DYNAMIC_MONITOR=None,
        DECISION_SUPPORT_LLM_PROVIDER=None,
        DECISION_SUPPORT_MONITOR_CONFIG=None,
        DECISION_SUPPORT_MANUAL_CHECK_DIR=None,
        DECISION_SUPPORT_PAPER_INITIAL_CASH=None,
        DECISION_SUPPORT_PAPER_FEE_SCHEDULE=None,
        DECISION_SUPPORT_PAPER_CALENDAR_PROVIDER=None,
        DECISION_SUPPORT_PAPER_LEDGER_PATH=None,
        DECISION_SUPPORT_TRUSTED_BAR_STORE_PATH=None,
        DECISION_SUPPORT_PAPER_RISK_STATE_PATH=None,
        DECISION_SUPPORT_EXIT_EVALUATION_STORE_PATH=None,
        DECISION_SUPPORT_EXIT_EVIDENCE_POLICY_PATH=None,
        DECISION_SUPPORT_PAPER_STRATEGY_REGISTRY_PATH=None,
        DECISION_SUPPORT_PAPER_STRATEGY_EPOCH=None,
        DECISION_SUPPORT_PAPER_STRATEGY_ENGINE_BUILD_FINGERPRINT=None,
        DECISION_SUPPORT_PAPER_SCANNER_ALGORITHM_FINGERPRINT=None,
        DECISION_SUPPORT_PAPER_STRUCTURE_ALGORITHM_FINGERPRINT=None,
        DECISION_SUPPORT_PAPER_ACCOUNT_ALGORITHM_FINGERPRINT=None,
        DECISION_SUPPORT_PAPER_BAR_PROVIDER_FINGERPRINT=None,
    )
    if test_config:
        app.config.update(test_config)
    if https_enabled:
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["REMEMBER_COOKIE_SECURE"] = True
    if app.config.get("VALIDATE_WEB_SECURITY", True):
        validate_web_security_config(app.config["WEB_HOST"], get_login_password())
    scheduler_enabled = bool(app.config.get("SCHEDULER_ENABLED", False))
    app.logger.addFilter(
        lambda record: "/static/" not in record.getMessage().lower()
    )

    # 任务对象
    from .services.scheduler_executor import RestartableDaemonPoolExecutor

    scheduler = BackgroundScheduler(
        timezone=pytz.timezone("Asia/Shanghai"),
        executors={
            "default": RestartableDaemonPoolExecutor(
                max_workers=10,
                max_pending=100,
            )
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
        frequency_maps,
        resolution_maps,
        market_frequencys,
        market_default_codes,
        market_session,
        market_timezone,
        market_types,
    )
    from .services import constants as constants_service
    from .services import stock_list as stock_list_service
    from .services import readiness as readiness_service

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

    from .alert_tasks import AlertTasks
    from .xuangu_tasks import XuanguTasks
    _alert_tasks = AlertTasks(scheduler)
    _xuangu_tasks = XuanguTasks(scheduler)
    _recursive_monitors = []
    _decision_support_composition = None
    _decision_support_jobs = {}
    _decision_support_runtime_error = None

    # _other_tasks = OtherTasks(scheduler)

    __log = fun.get_logger()

    # 强制 Jinja2 每次请求都从磁盘 re-render template,避免 web 长跑后
    # ``index.html`` 内 ``{{ static_version }}`` 等动态变量被内存缓存。
    # 性能代价微小(主页面 template,只在 / 请求时 render)。
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # 添加登录验证
    # secret_key 解析顺序：环境变量 CHANLUN_FLASK_SECRET_KEY > config.FLASK_SECRET_KEY > 数据目录持久化文件。
    app.secret_key = get_flask_secret_key()

    # CSRF token 与最长登录会话使用同一 12 小时边界。
    from .csrf import csrf
    csrf.init_app(app)

    # 静态资源 cache-bust:Tornado static handler 给所有 /static/* 加
    # ``Cache-Control: max-age=31536000, immutable``,导致 charts.js / bundle.js
    # 等核心前端文件被浏览器**永久缓存**——后端修了字段但前端永远拉不到新版本。
    # 修法:在 Jinja2 模板里给 ``<script src>`` 加 ``?v={{ static_version }}``,
    # static_version = 关键文件 mtime 的 short hash,文件变即 bust。
    @app.before_request
    def _set_csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(24)

    @app.before_request
    def _enforce_runtime_web_security():
        if not app.config.get("VALIDATE_WEB_SECURITY", True):
            return None
        try:
            validate_web_security_config(
                request.remote_addr or "", get_login_password()
            )
        except ValueError:
            return {"status": "security_misconfigured"}, 503
        return None

    @app.context_processor
    def inject_static_version():
        import hashlib
        h = hashlib.md5()
        # 聚合前端 js/css、datafeed bundle 与固定 Charting Library 入口的 mtime+size：
        # 改动都会让 static_version 变化，模板里带 ?v={{ static_version }} 的资源
        # 随之 cache-bust，用户改前端后普通刷新即可生效，无需手动硬刷新。
        targets = [
            os.path.join(app.static_folder, "datafeeds", "udf", "dist", "bundle.js"),
            os.path.join(
                app.static_folder,
                "charting_library",
                "charting_library.standalone.js",
            ),
            os.path.join(
                app.static_folder, "charting_library", "sameorigin.html"
            ),
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
            "static_version": h.hexdigest()[:10],
            "csp_nonce": getattr(g, "csp_nonce", ""),
        }

    @app.after_request
    def _set_security_headers(resp):
        static_filename = str((request.view_args or {}).get("filename", ""))
        is_charting_vendor_shell = (
            request.endpoint == "static"
            and static_filename.replace("\\", "/")
            == "charting_library/sameorigin.html"
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
            "/ai/",
            "/decision-support/",
            "/get_zixuan_",
            "/get_stock_zixuan/",
            "/alert_list/",
            "/alert_records/",
            "/xuangu/task_list/",
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

    def _current_login_user_id() -> str:
        secret = app.secret_key
        secret_bytes = secret if isinstance(secret, bytes) else str(secret).encode()
        password = get_login_password() or ""
        digest = hmac.new(
            secret_bytes,
            password.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"cl_pro:{digest}"

    class LoginUser(UserMixin):
        def __init__(self, user_id=None) -> None:
            super().__init__()
            self.id = user_id or _current_login_user_id()

    @login_manager.user_loader
    def load_user(user_id):
        expected = _current_login_user_id()
        if not isinstance(user_id, str) or not hmac.compare_digest(user_id, expected):
            return None
        return LoginUser(expected)

    from .services.login_rate_limit import LoginRateLimiter

    login_rate_limiter = LoginRateLimiter()

    @app.route("/login", methods=["GET", "POST"])
    def login_opt():
        configured_password = get_login_password() or ""
        remember_duration = app.config["REMEMBER_COOKIE_DURATION"]

        if configured_password == "":
            session.clear()
            login_user(
                LoginUser(),
                remember=True,
                duration=remember_duration,
            )
            return redirect("/")

        emsg = ""
        if request.method == "POST":
            client_key = request.remote_addr or "unknown"
            if login_rate_limiter.is_blocked(client_key):
                return render_template("login.html", emsg="尝试次数过多，请稍后再试"), 429

            password = request.form.get("password") or ""
            if verify_login_password(password, configured_password):
                login_rate_limiter.clear(client_key)
                session.clear()
                login_user(
                    LoginUser(),
                    remember=True,
                    duration=remember_duration,
                )
                return redirect("/")

            login_rate_limiter.record_failure(client_key)
            emsg = "密码错误"

        return render_template("login.html", emsg=emsg)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout_opt():
        session.clear()
        logout_user()
        return redirect("/login")

    @app.route("/api/session")
    @login_required
    def api_session():
        return {"ok": True, "csrf_token": generate_csrf()}

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
            runtime_component = runtime_status()
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
        return health_snapshot("readyz", request.args.get("market") or "a")

    @app.route("/")
    @login_required
    def index_show():
        requested_market = (request.args.get("market") or "a").strip().lower()
        initial_market = requested_market if requested_market in market_types else "a"

        selected_default_codes = market_default_codes.cached_snapshot()
        selected_frequencies = market_frequencys.cached_snapshot()
        selected_default_codes.update(
            market_default_codes.snapshot((initial_market,))
        )
        selected_frequencies.update(
            market_frequencys.snapshot((initial_market,))
        )

        return render_template(
            "index.html",
            market_default_codes=selected_default_codes,
            market_frequencys=selected_frequencies,
            initial_market=initial_market,
            enable_sse=config.ENABLE_SSE_PUSH,
        )

    from .blueprints.tv import tv_bp
    from .blueprints.zixuan import zixuan_bp
    from .blueprints.alert import alert_bp
    from .blueprints.xuangu import xuangu_bp
    from .blueprints.setting import setting_bp
    from .blueprints.ai import ai_bp
    from .blueprints.bkgn import bkgn_bp
    from .blueprints.other import other_bp
    from .blueprints.options import options_bp
    from .blueprints.symbols import symbols_bp
    from .blueprints.decision_support import decision_support_bp

    for blueprint in (
        tv_bp,
        zixuan_bp,
        alert_bp,
        xuangu_bp,
        setting_bp,
        ai_bp,
        bkgn_bp,
        other_bp,
        options_bp,
        symbols_bp,
        decision_support_bp,
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
    runtime_state = {
        "started": False,
        "stopping": False,
        "status": "stopped",
        "error": None,
        "active_starts": 0,
        "shutdown_complete": False,
        "generation": 0,
        "stop_event": threading.Event(),
        "scheduler_enabled": None,
        "metadata": None,
        "ticks": None,
        "symbols": None,
    }

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
        nonlocal metadata_warmup_thread
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
                scheduler.start()
                _alert_tasks.run()

                from chanlun.signal_monitor.scheduler import register_signal_jobs
                register_signal_jobs(scheduler)

                from chanlun.recursive_bt.monitor.app_monitor import (
                    register_recursive_monitor_jobs,
                )
                monitors = register_recursive_monitor_jobs(scheduler)
                _recursive_monitors.clear()
                if isinstance(monitors, dict):
                    _recursive_monitors.extend(monitors.values())
                elif monitors:
                    _recursive_monitors.extend(monitors)
                if app.config.get("DECISION_SUPPORT_ENABLED", False):
                    install_decision_support_runtime(monitors)

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
        except Exception as exc:
            with runtime_lock:
                runtime_state["error"] = str(exc)[:200]
            shutdown_runtime_services()
            raise
        finally:
            with runtime_lock:
                runtime_state["active_starts"] -= 1

    def shutdown_runtime_services():
        with runtime_cleanup_lock:
            with runtime_lock:
                runtime_state["stop_event"].set()
                if (
                    not runtime_state["started"]
                    and runtime_state["active_starts"] == 0
                    and runtime_state["status"] == "stopped"
                    and runtime_state["shutdown_complete"]
                ):
                    return
                runtime_state["stopping"] = True
                runtime_state["status"] = "stopping"

            cleanup_errors = []

            def _cleanup(label, operation):
                try:
                    operation()
                except Exception as exc:
                    cleanup_errors.append(f"{label}: {exc}")
                    app.logger.exception("runtime cleanup failed: %s", label)

            if scheduler.running:
                _cleanup("scheduler", lambda: scheduler.shutdown(wait=False))

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
                lambda: constants_service.shutdown_market_metadata_loaders(
                    timeout=0.1
                ),
            )

            with runtime_lock:
                runtime_state.update(
                    {
                        "started": False,
                        "stopping": False,
                        "status": "stopped",
                        "shutdown_complete": True,
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

    from chanlun.decision_support.event_store import DecisionEventStore
    from chanlun.decision_support.certified_runtime import (
        CertifiedCorpusRuntime,
    )
    from chanlun.persistence.db import db as persistence_db
    from .services.decision_support import (
        build_persistent_decision_support_facade,
    )

    certified_corpus_runtime = CertifiedCorpusRuntime(
        pathlib.Path(__file__).resolve().parents[3]
        / "audit"
        / "chanlun_lesson_corpus_v3"
    )
    decision_support_store = DecisionEventStore(lambda: persistence_db.Session())
    decision_support_facade = build_persistent_decision_support_facade(
        decision_support_store,
        certified_corpus_runtime=certified_corpus_runtime,
    )

    def decision_support_runtime_status():
        return {
            "enabled": bool(app.config.get("DECISION_SUPPORT_ENABLED", False)),
            "installed": _decision_support_composition is not None,
            "jobs": tuple(sorted(_decision_support_jobs)),
            "error": _decision_support_runtime_error,
        }

    def install_decision_support_runtime(monitors=None):
        nonlocal decision_support_facade
        nonlocal _decision_support_composition
        nonlocal _decision_support_runtime_error
        if not app.config.get("DECISION_SUPPORT_ENABLED", False):
            return None
        if _decision_support_composition is not None:
            return _decision_support_composition

        account_provider = app.config.get(
            "DECISION_SUPPORT_ACCOUNT_PROVIDER"
        )
        if not callable(account_provider):
            _decision_support_runtime_error = "account_provider_unavailable"
            raise RuntimeError(
                "DECISION_SUPPORT_ACCOUNT_PROVIDER account_provider is required"
            )

        dynamic_monitor = app.config.get("DECISION_SUPPORT_DYNAMIC_MONITOR")
        if dynamic_monitor is None and isinstance(monitors, Mapping):
            dynamic_monitor = monitors.get("a")
        if dynamic_monitor is None:
            _decision_support_runtime_error = "a_share_monitor_unavailable"
            raise RuntimeError(
                "an explicit A-share dynamic monitor is required"
            )

        from chanlun.decision_support.llm_provider import ConfiguredProvider
        from chanlun.decision_support.monitor import (
            MonitorConfig,
            register_decision_support_jobs,
        )
        from chanlun.decision_support.paper_adapter import PaperFeeSchedule
        from chanlun.decision_support.paper_runtime import (
            register_paper_research_jobs,
        )
        from chanlun.decision_support.paper_read_model import (
            PaperResearchReadModel,
        )
        from chanlun.decision_support.manual_check_workflow import (
            FileManualCheckStore,
        )
        from chanlun.decision_support.production import (
            build_production_decision_support,
        )
        from chanlun.decision_support.strategy_run import (
            STRATEGY_RUN_MUTATION_LEASE_PROTOCOL,
            STRATEGY_RUN_SWITCH_CAPABILITY,
        )

        llm_provider = app.config.get("DECISION_SUPPORT_LLM_PROVIDER")
        if llm_provider is None:
            llm_provider = ConfiguredProvider.from_config()
        raw_monitor_config = app.config.get(
            "DECISION_SUPPORT_MONITOR_CONFIG"
        )
        if raw_monitor_config is None:
            monitor_config = MonitorConfig(enabled=True)
        elif type(raw_monitor_config) is MonitorConfig:
            monitor_config = raw_monitor_config
        else:
            monitor_config = MonitorConfig.from_mapping(raw_monitor_config)
        if not monitor_config.enabled:
            _decision_support_runtime_error = "monitor_config_disabled"
            raise RuntimeError(
                "decision-support monitor_config must be enabled"
            )

        project_root = pathlib.Path(__file__).resolve().parents[3]
        paper_initial_cash = None
        paper_fee_schedule = None
        paper_calendar_provider = None
        paper_paths: dict[str, object] = {}
        paper_strategy_config: dict[str, object] = {}
        if monitor_config.paper_enabled:
            paper_initial_cash = app.config.get(
                "DECISION_SUPPORT_PAPER_INITIAL_CASH"
            )
            if (
                not isinstance(paper_initial_cash, Decimal)
                or not paper_initial_cash.is_finite()
                or paper_initial_cash <= 0
            ):
                _decision_support_runtime_error = (
                    "paper_initial_cash_unavailable"
                )
                raise RuntimeError(
                    "DECISION_SUPPORT_PAPER_INITIAL_CASH must be a positive "
                    "finite Decimal"
                )
            paper_fee_schedule = app.config.get(
                "DECISION_SUPPORT_PAPER_FEE_SCHEDULE"
            )
            if not isinstance(paper_fee_schedule, PaperFeeSchedule):
                _decision_support_runtime_error = (
                    "paper_fee_schedule_unavailable"
                )
                raise RuntimeError(
                    "DECISION_SUPPORT_PAPER_FEE_SCHEDULE is required"
                )
            paper_calendar_provider = app.config.get(
                "DECISION_SUPPORT_PAPER_CALENDAR_PROVIDER"
            )
            paper_strategy_epoch = app.config.get(
                "DECISION_SUPPORT_PAPER_STRATEGY_EPOCH"
            )
            if (
                isinstance(paper_strategy_epoch, bool)
                or not isinstance(paper_strategy_epoch, int)
                or paper_strategy_epoch <= 0
            ):
                _decision_support_runtime_error = (
                    "paper_strategy_epoch_unavailable"
                )
                raise RuntimeError(
                    "DECISION_SUPPORT_PAPER_STRATEGY_EPOCH must be a positive "
                    "integer"
                )
            paper_strategy_registry_path = app.config.get(
                "DECISION_SUPPORT_PAPER_STRATEGY_REGISTRY_PATH"
            )
            if (
                not isinstance(paper_strategy_registry_path, (str, pathlib.Path))
                or (
                    isinstance(paper_strategy_registry_path, str)
                    and not paper_strategy_registry_path.strip()
                )
            ):
                _decision_support_runtime_error = (
                    "paper_strategy_registry_unavailable"
                )
                raise RuntimeError(
                    "DECISION_SUPPORT_PAPER_STRATEGY_REGISTRY_PATH is required"
                )
            strategy_fingerprint_keys = {
                "paper_strategy_engine_build_fingerprint": (
                    "DECISION_SUPPORT_PAPER_STRATEGY_ENGINE_BUILD_FINGERPRINT"
                ),
                "paper_scanner_algorithm_fingerprint": (
                    "DECISION_SUPPORT_PAPER_SCANNER_ALGORITHM_FINGERPRINT"
                ),
                "paper_structure_algorithm_fingerprint": (
                    "DECISION_SUPPORT_PAPER_STRUCTURE_ALGORITHM_FINGERPRINT"
                ),
                "paper_account_algorithm_fingerprint": (
                    "DECISION_SUPPORT_PAPER_ACCOUNT_ALGORITHM_FINGERPRINT"
                ),
                "paper_bar_provider_fingerprint": (
                    "DECISION_SUPPORT_PAPER_BAR_PROVIDER_FINGERPRINT"
                ),
            }
            strategy_fingerprints: dict[str, str] = {}
            for argument_name, config_name in strategy_fingerprint_keys.items():
                fingerprint = app.config.get(config_name)
                if (
                    not isinstance(fingerprint, str)
                    or len(fingerprint) != 71
                    or not fingerprint.startswith("sha256:")
                    or any(
                        character not in "0123456789abcdef"
                        for character in fingerprint[7:]
                    )
                ):
                    _decision_support_runtime_error = (
                        "paper_strategy_fingerprint_unavailable"
                    )
                    raise RuntimeError(
                        config_name + " must use sha256:<64 lowercase hex>"
                    )
                strategy_fingerprints[argument_name] = fingerprint
            paper_strategy_config = {
                "paper_strategy_registry_path": (
                    paper_strategy_registry_path
                ),
                "paper_strategy_epoch": paper_strategy_epoch,
                **strategy_fingerprints,
            }
            paper_paths = {
                "paper_ledger_path": app.config.get(
                    "DECISION_SUPPORT_PAPER_LEDGER_PATH"
                )
                or project_root
                / "audit"
                / "decision_support_paper_ledger.sqlite3",
                "trusted_bar_store_path": app.config.get(
                    "DECISION_SUPPORT_TRUSTED_BAR_STORE_PATH"
                )
                or project_root
                / "audit"
                / "decision_support_trusted_bars.sqlite3",
                "paper_risk_state_path": app.config.get(
                    "DECISION_SUPPORT_PAPER_RISK_STATE_PATH"
                )
                or project_root
                / "audit"
                / "decision_support_paper_risk.sqlite3",
                "exit_evaluation_store_path": app.config.get(
                    "DECISION_SUPPORT_EXIT_EVALUATION_STORE_PATH"
                )
                or project_root
                / "audit"
                / "decision_support_exit_evaluations.sqlite3",
                "exit_evidence_policy_path": app.config.get(
                    "DECISION_SUPPORT_EXIT_EVIDENCE_POLICY_PATH"
                )
                or project_root
                / "config"
                / "decision_support"
                / "exit_evidence_policy.json",
            }

        rule_set_path = app.config.get("DECISION_SUPPORT_RULE_SET_PATH")
        if rule_set_path is None:
            rule_set_path = (
                project_root
                / "config"
                / "decision_support"
                / "rule_cards.json"
            )
        composition_factory = app.config.get(
            "DECISION_SUPPORT_COMPOSITION_FACTORY"
        ) or build_production_decision_support
        job_registrar = app.config.get("DECISION_SUPPORT_JOB_REGISTRAR")
        if job_registrar is None:
            job_registrar = (
                register_paper_research_jobs
                if monitor_config.paper_enabled
                else register_decision_support_jobs
            )
        manual_check_dir = app.config.get(
            "DECISION_SUPPORT_MANUAL_CHECK_DIR"
        )
        if manual_check_dir is None:
            manual_check_dir = (
                project_root
                / "audit"
                / "decision_support_manual_checks"
            )

        try:
            manual_check_store = FileManualCheckStore(manual_check_dir)
            composition = composition_factory(
                dynamic_monitor=dynamic_monitor,
                corpus_runtime=certified_corpus_runtime,
                rule_set_path=rule_set_path,
                store=decision_support_store,
                account_provider=account_provider,
                llm_provider=llm_provider,
                monitor_config=monitor_config,
                clock=app.config.get("DECISION_SUPPORT_CLOCK"),
                manual_check_store=manual_check_store,
                paper_initial_cash=paper_initial_cash,
                paper_fee_schedule=paper_fee_schedule,
                paper_calendar_provider=paper_calendar_provider,
                **paper_paths,
                **paper_strategy_config,
            )
            if monitor_config.paper_enabled:
                paper_runtime = getattr(composition, "paper_runtime", None)
                if paper_runtime is None:
                    raise RuntimeError(
                        "paper-enabled composition has no paper runtime"
                    )
                strategy_run = getattr(composition, "strategy_run", None)
                strategy_status_provider = getattr(
                    strategy_run,
                    "status_payload",
                    None,
                )
                mutation_lease_provider = getattr(
                    strategy_run,
                    "mutation_lease",
                    None,
                )
                strategy_status = (
                    strategy_status_provider()
                    if callable(strategy_status_provider)
                    else None
                )
                if (
                    not isinstance(strategy_status, Mapping)
                    or not isinstance(strategy_status.get("run_id"), str)
                    or not strategy_status.get("run_id")
                    or not isinstance(strategy_status.get("fingerprint"), str)
                    or len(strategy_status.get("fingerprint", "")) != 71
                    or not strategy_status.get("fingerprint", "").startswith(
                        "sha256:"
                    )
                    or any(
                        character not in "0123456789abcdef"
                        for character in strategy_status.get(
                            "fingerprint",
                            "",
                        )[7:]
                    )
                    or strategy_status.get("state") != "active"
                    or strategy_status.get("epoch")
                    != paper_strategy_config["paper_strategy_epoch"]
                    or strategy_status.get("evidence_scope")
                    != "current_epoch_only"
                    or strategy_status.get("store_bindings_complete") is not True
                    or strategy_status.get("switch_capability")
                    != STRATEGY_RUN_SWITCH_CAPABILITY
                    or strategy_status.get("rolling_switch_supported") is not False
                    or strategy_status.get("mutation_lease_protocol")
                    != STRATEGY_RUN_MUTATION_LEASE_PROTOCOL
                    or strategy_status.get("inflight_mutation_count") != 0
                    or isinstance(
                        strategy_status.get("inflight_mutation_count"),
                        bool,
                    )
                    or strategy_status.get("mutations_drained") is not True
                    or not callable(mutation_lease_provider)
                ):
                    raise RuntimeError(
                        "paper strategy-run store bindings are unavailable"
                    )
                jobs = job_registrar(
                    scheduler,
                    paper_runtime,
                    composition.runtime,
                    strategy_run=strategy_run,
                )
                required_jobs = {"bar", "review", "admission"}
                paper_read_model = PaperResearchReadModel(
                    getattr(composition, "paper_ledger", None),
                    exit_store=getattr(
                        composition,
                        "exit_evaluation_store",
                        None,
                    ),
                    runtime=paper_runtime,
                    policy_provider=getattr(
                        composition,
                        "paper_gateway",
                        None,
                    ),
                    strategy_run=strategy_run,
                )
            else:
                jobs = job_registrar(scheduler, composition.runtime)
                required_jobs = {"scan", "review"}
                paper_read_model = None
            if not isinstance(jobs, Mapping) or not required_jobs.issubset(jobs):
                raise RuntimeError(
                    "required decision-support jobs were not registered"
                )
            facade = build_persistent_decision_support_facade(
                decision_support_store,
                certified_corpus_runtime=certified_corpus_runtime,
                review_provider=composition.review_provider,
                promotion_provider=composition.promotion_provider,
                rule_evidence_resolver=composition.rule_evidence_resolver,
                clock=app.config.get("DECISION_SUPPORT_CLOCK"),
                strategy_run=getattr(composition, "strategy_run", None),
            )
        except Exception:
            for job_id in (
                "decision_support_scan",
                "decision_support_bar_cycle",
                "decision_support_review",
                "decision_support_paper_admission",
            ):
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
            if _decision_support_runtime_error is None:
                _decision_support_runtime_error = "composition_failed"
            raise

        _decision_support_composition = composition
        _decision_support_jobs.clear()
        _decision_support_jobs.update(jobs)
        _decision_support_runtime_error = None
        decision_support_facade = facade
        app.extensions.update(
            {
                "decision_support_composition": composition,
                "decision_support_jobs": _decision_support_jobs,
                "decision_support_facade": facade,
                "decision_support_manual_check_workflow": (
                    composition.manual_check_workflow
                ),
                "decision_support_paper_runtime": getattr(
                    composition,
                    "paper_runtime",
                    None,
                ),
                "decision_support_paper_ledger": getattr(
                    composition,
                    "paper_ledger",
                    None,
                ),
                "decision_support_trusted_bar_store": getattr(
                    composition,
                    "trusted_bar_store",
                    None,
                ),
                "decision_support_paper_gateway": getattr(
                    composition,
                    "paper_gateway",
                    None,
                ),
                "decision_support_paper_risk_state": getattr(
                    composition,
                    "paper_risk_state",
                    None,
                ),
                "decision_support_exit_evaluation_store": getattr(
                    composition,
                    "exit_evaluation_store",
                    None,
                ),
                "decision_support_exit_evaluation_service": getattr(
                    composition,
                    "exit_evaluation_service",
                    None,
                ),
                "decision_support_paper_exit_cycle": getattr(
                    composition,
                    "paper_exit_cycle",
                    None,
                ),
                "decision_support_paper_read_model": paper_read_model,
            }
        )
        return composition

    app.extensions.update(
        {
            "scheduler": scheduler,
            "alert_tasks": _alert_tasks,
            "xuangu_tasks": _xuangu_tasks,
            "recursive_monitors": _recursive_monitors,
            "readiness": readiness_registry,
            "metadata_warmup_thread": metadata_warmup_thread,
            "login_rate_limiter": login_rate_limiter,
            "health_snapshot": health_snapshot,
            "runtime_status": runtime_status,
            "start_runtime_services": start_runtime_services,
            "shutdown_runtime_services": shutdown_runtime_services,
            "shutdown_scheduler": shutdown_scheduler,
            "decision_support_facade": decision_support_facade,
            "decision_support_store": decision_support_store,
            "decision_support_composition": _decision_support_composition,
            "decision_support_jobs": _decision_support_jobs,
            "decision_support_manual_check_workflow": None,
            "decision_support_paper_runtime": None,
            "decision_support_paper_ledger": None,
            "decision_support_trusted_bar_store": None,
            "decision_support_paper_gateway": None,
            "decision_support_paper_risk_state": None,
            "decision_support_exit_evaluation_store": None,
            "decision_support_exit_evaluation_service": None,
            "decision_support_paper_exit_cycle": None,
            "decision_support_paper_read_model": None,
            "decision_support_runtime_status": decision_support_runtime_status,
            "install_decision_support_runtime": install_decision_support_runtime,
            "certified_corpus_runtime": certified_corpus_runtime,
        }
    )
    if scheduler_enabled:
        start_runtime_services(enable_scheduler=True)
        atexit.register(shutdown_runtime_services)

    return app
