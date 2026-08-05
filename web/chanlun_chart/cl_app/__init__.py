import atexit
import datetime
import hashlib
import hmac
import json
import os
import threading
import pathlib
import pytz
import secrets
import subprocess
from collections.abc import Mapping
from contextlib import closing
from types import MappingProxyType
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
    abort,
    g,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    session,
)
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
from chanlun.decision_support.trading_system.v3_trading_session import (
    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    authoritative_trading_session_evidence,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    calculate_forward_application_source_revision,
    is_content_addressed_application_source_revision,
)
from .services.job_names import job_display_name
__all__ = ["create_app"]


_TASK_HISTORY_LIMIT = 500
_TASK_TERMINAL_STATES = {"已完成", "执行异常", "未执行", "删除作业"}
_SHARED_RUNTIME_OWNER_LOCK = threading.RLock()
_SHARED_RUNTIME_OWNER: object | None = None


def _human_review_historical_paths() -> tuple[pathlib.Path, pathlib.Path]:
    """Return the current release sidecar and the explicit legacy fallback."""

    repository_root = pathlib.Path(__file__).resolve().parents[3]
    backtests = repository_root / "audit" / "chanlun_trading_system_backtest"
    return (
        backtests
        / "recent_year_current_sector_no3p_mwd_strength"
        / "human_review_screen.json",
        backtests
        / "recent_year_current_sector_no3p"
        / "human_review_screen.json",
    )


def _default_human_review_historical_report() -> pathlib.Path:
    """Prefer the page sidecar produced beside the current formal release."""

    current, legacy = _human_review_historical_paths()
    return current if current.is_file() else legacy


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
        # 任务编号属于稳定协议；名称只负责面向用户展示。按编号重新映射可以
        # 同时覆盖升级前已经写入任务注册表的旧英文名称。
        row["name"] = job_display_name(row.get("id"), row.get("name"))
        snapshot.append(row)
    return snapshot


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
        # The historical alert surfaces use MACD divergence and legacy
        # bi/segment MMD rules.  They are not the strict first/second/third
        # class point authority used by the chart, screening, holdings monitor
        # and replay.  Keep them available only as an explicit compatibility
        # opt-in so one running app cannot publish two conflicting meanings of
        # "buy/sell signal".
        LEGACY_ALERT_TASKS_ENABLED=(
            os.environ.get("CHANLUN_LEGACY_ALERT_TASKS_ENABLED", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        ),
        LEGACY_SIGNAL_MONITOR_ENABLED=(
            os.environ.get("CHANLUN_LEGACY_SIGNAL_MONITOR_ENABLED", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        ),
        TRADING_SCREENING_BACKGROUND_ENABLED=True,
        TRADING_SCREENING_PRIORITY_MONITOR_ENABLED=True,
        TRADING_SCREENING_PRIORITY_MONITOR_MAX_SYMBOLS=int(
            os.environ.get(
                "CHANLUN_TRADING_SCREENING_PRIORITY_MONITOR_MAX_SYMBOLS",
                "16",
            )
        ),
        TRADING_SCREENING_PRIORITY_MONITOR_INTERVAL_SECONDS=60,
        # A system-local, market-independent watchlist group declares manual
        # holdings.  It is only a monitoring fact: no broker/account access or
        # order capability is inferred from membership.
        TRADING_SCREENING_MANUAL_HOLDING_GROUP=os.environ.get(
            "CHANLUN_TRADING_SCREENING_MANUAL_HOLDING_GROUP",
            "我的持仓",
        ).strip(),
        HOLDING_GROUP_MONITOR_ENABLED=True,
        HOLDING_GROUP_MONITOR_INTERVAL_SECONDS=int(
            os.environ.get("CHANLUN_HOLDING_GROUP_MONITOR_INTERVAL_SECONDS", "60")
        ),
        HOLDING_GROUP_MONITOR_START_DELAY_SECONDS=int(
            os.environ.get("CHANLUN_HOLDING_GROUP_MONITOR_START_DELAY_SECONDS", "8")
        ),
        HOLDING_GROUP_MONITOR_WORKERS=int(
            os.environ.get(
                "CHANLUN_HOLDING_GROUP_MONITOR_WORKERS",
                str(max(2, min(8, (os.cpu_count() or 4) // 2))),
            )
        ),
        ALERT_CHART_PUBLIC_BASE_URL=str(
            os.environ.get("CHANLUN_ALERT_CHART_PUBLIC_BASE_URL")
            or getattr(config, "ALERT_CHART_PUBLIC_BASE_URL", "")
            or ""
        ).strip().rstrip("/"),
        ALERT_CHART_TTL_SECONDS=int(
            os.environ.get("CHANLUN_ALERT_CHART_TTL_SECONDS", str(30 * 24 * 60 * 60))
        ),
        ALERT_CHART_CAPTURE_BASE_URL=str(
            os.environ.get(
                "CHANLUN_ALERT_CHART_CAPTURE_BASE_URL",
                "http://127.0.0.1:9900",
            )
        ).strip().rstrip("/"),
        ALERT_CHART_ROOT=(
            config.get_data_path() / "monitor" / "dingtalk_chart_images"
        ),
        TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH=int(
            os.environ.get(
                "CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH",
                "64",
            )
        ),
        # Post-close full-universe coverage is start-to-start throttled to one
        # batch per minute.  Keep the ordinary discovery lane aligned with the
        # total 64-symbol budget; leaving its dataclass default at 32 idles the
        # ten structure workers for most of every interval and roughly doubles
        # the time needed to publish the next-session preselection.
        TRADING_SCREENING_SYMBOLS_PER_REFRESH=int(
            os.environ.get(
                "CHANLUN_TRADING_SCREENING_SYMBOLS_PER_REFRESH",
                "64",
            )
        ),
        TRADING_SCREENING_NATIVE_PROCESS_ISOLATION=True,
        TRADING_SCREENING_NATIVE_STARTUP_TIMEOUT_SECONDS=45.0,
        TRADING_SCREENING_NATIVE_IDLE_TIMEOUT_SECONDS=210.0,
        TRADING_SCREENING_NATIVE_RESTART_BACKOFF_SECONDS=30.0,
        # One authenticated worker process can saturate only one CPU core.
        # Use half the logical CPUs (capped at eight) for stock structure work,
        # leaving capacity for QMT, Flask, charts and the operating system.
        TRADING_SCREENING_STOCK_WORKERS=int(
            os.environ.get(
                "CHANLUN_TRADING_SCREENING_STOCK_WORKERS",
                # Use roughly five eighths of logical CPUs, capped at ten.
                # On the current 10-core/16-thread host this assigns one
                # worker per physical core while retaining six logical CPUs
                # for QMT, Flask, chart rendering and notification delivery.
                str(
                    min(
                        10,
                        max(1, (((os.cpu_count() or 4) * 5) + 7) // 8),
                    )
                ),
            )
        ),
        FORWARD_SCHEDULER_MONITOR_ENABLED=True,
        FORWARD_SCHEDULER_MONITOR_TTL_SECONDS=30.0,
        # app.py is the long-running owner of forward business scheduling.
        # Windows Task Scheduler is retained only for QMT/app bootstrap and
        # recovery.  Set WINDOWS only during a bounded migration rollback.
        FORWARD_SCHEDULER_MODE=os.environ.get(
            "CHANLUN_FORWARD_SCHEDULER_MODE", "APP"
        ).strip().upper(),
        FORWARD_QMT_LOCAL_DATA_DIR=os.environ.get(
            "CHANLUN_QMT_LOCAL_DATA_DIR", ""
        ).strip(),
        # app.py is also the sole owner of the interactive QMT runtime.  The
        # legacy Windows QMT task is supported only as a bounded migration
        # rollback and must be removed after the app-owned health gate passes.
        QMT_RUNTIME_MODE=os.environ.get(
            "CHANLUN_QMT_RUNTIME_MODE", "APP"
        ).strip().upper(),
        QMT_RUNTIME_HELPER=os.environ.get(
            "CHANLUN_QMT_RUNTIME_HELPER", ""
        ).strip(),
        QMT_RUNTIME_STARTUP_TIMEOUT_SECONDS=120,
        QMT_RUNTIME_WARMUP_SECONDS=90,
        QMT_RUNTIME_RECOVERY_COOLDOWN_SECONDS=300,
        QMT_RUNTIME_OBSERVATION_MAX_AGE_SECONDS=180,
        TRADING_SESSION_OFFICIAL_CALENDAR_PATH=(
            DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH
        ),
        HUMAN_REVIEW_HISTORICAL_REPORT=(
            _default_human_review_historical_report()
        ),
        HUMAN_REVIEW_CURRENT_HISTORICAL_REPORT=(
            _human_review_historical_paths()[0]
        ),
        HUMAN_REVIEW_FORWARD_ROOT=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review_forward"
        ),
        HUMAN_REVIEW_FEEDBACK_LEDGER=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review"
            / "feedback_ledger.json"
        ),
        HUMAN_REVIEW_PAPER_LEDGER=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review"
            / "paper_ledger.json"
        ),
        HUMAN_REVIEW_PARAMETER_SNAPSHOT=(
            pathlib.Path(__file__).resolve().parents[3]
            / "audit"
            / "chanlun_trading_system_backtest"
            / "recent_year_current_sector_no3p"
            / "parameter_snapshot_human_review.json"
        ),
        HUMAN_REVIEW_LIVE_ARCHIVE_ROOT=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review"
            / "live_screens"
        ),
        HUMAN_REVIEW_FORWARD_MARKOUT=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review_forward"
            / "forward_review_markout.json"
        ),
        HUMAN_REVIEW_FORWARD_WARMUP_LINEAGE=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_human_review_forward"
            / "forward_warmup_structure_lineage_rollup.json"
        ),
        QMT_SECTOR_CAPTURE_LEDGER=(
            pathlib.Path(__file__).resolve().parents[3]
            / ".cache"
            / "chanlun_v3_qmt_sector_ledger"
            / "qmt_gics3_catalog_ledger.json"
        ),
    )
    if test_config:
        app.config.update(test_config)
    if app.testing and (
        not test_config
        or "TRADING_SCREENING_BACKGROUND_ENABLED" not in test_config
    ):
        # Runtime tests opt in explicitly. This keeps the default app factory
        # side-effect free and prevents real market scans in unrelated tests.
        app.config["TRADING_SCREENING_BACKGROUND_ENABLED"] = False
    if app.testing and (
        not test_config
        or "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION" not in test_config
    ):
        app.config["TRADING_SCREENING_NATIVE_PROCESS_ISOLATION"] = False
    if app.testing and (
        not test_config
        or "FORWARD_SCHEDULER_MONITOR_ENABLED" not in test_config
    ):
        # Reading Windows Task Scheduler is an explicit integration-test opt-in.
        # Generic app-factory tests remain host-independent and side-effect free.
        app.config["FORWARD_SCHEDULER_MONITOR_ENABLED"] = False
    if app.testing and (
        not test_config or "FORWARD_SCHEDULER_MODE" not in test_config
    ):
        # Unit tests must opt into the process-owning scheduler explicitly.
        app.config["FORWARD_SCHEDULER_MODE"] = "DISABLED"
    if app.testing and (
        not test_config or "QMT_RUNTIME_MODE" not in test_config
    ):
        # Host-process control is always an explicit integration-test opt-in.
        app.config["QMT_RUNTIME_MODE"] = "DISABLED"
    if app.testing and (
        not test_config
        or "TRADING_SESSION_OFFICIAL_CALENDAR_PATH" not in test_config
    ):
        # Unit tests opt into the immutable annual artifact explicitly.  This
        # preserves injected calendar-provider tests and prevents a real
        # filesystem artifact from silently replacing their fixture evidence.
        app.config["TRADING_SESSION_OFFICIAL_CALENDAR_PATH"] = None
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
    from .services.research_runtime_attestation import (
        build_scheduler_attestation,
    )

    app.extensions["research_required_job_executors"] = MappingProxyType({})

    def research_scheduler_attestation():
        required_job_executors = app.extensions[
            "research_required_job_executors"
        ]
        if type(required_job_executors) is not MappingProxyType:
            raise RuntimeError(
                "research required-job mapping must be immutable"
            )
        return build_scheduler_attestation(
            scheduler,
            required_job_executors,
        )

    app.extensions[
        "research_scheduler_attestation"
    ] = research_scheduler_attestation

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
    _alert_tasks = AlertTasks(
        scheduler,
        enabled=bool(app.config.get("LEGACY_ALERT_TASKS_ENABLED", False)),
    )
    _xuangu_tasks = XuanguTasks(scheduler)
    _recursive_monitors = []
    holding_group_monitor = None

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
            # Direct ``app.py`` / PyCharm launches are the normal production
            # owner in this project.  Bind readiness and persistent native fact
            # caches to the exact dirty working tree instead of reporting only
            # HEAD and disabling caches merely because no wrapper set an env var.
            return calculate_forward_application_source_revision(project_root)
        except (OSError, RuntimeError):
            pass
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

    def _readyz_snapshot(market, forward_session=None):
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
            runtime_probe = app.extensions.get("runtime_status")
            runtime_component = (
                runtime_probe()
                if callable(runtime_probe)
                else runtime_status()
            )
            runtime_component["required"] = True
        else:
            runtime_component = {
                "required": False,
                "ready": True,
                "status": "disabled",
                "error": None,
            }

        qmt_runtime_required = bool(
            scheduler_required
            and market == "a"
            and str(app.config.get("QMT_RUNTIME_MODE", "APP")).upper()
            == "APP"
        )
        if qmt_runtime_required:
            qmt_runtime_probe = app.extensions.get("app_qmt_runtime")
            try:
                if qmt_runtime_probe is None or not hasattr(
                    qmt_runtime_probe, "snapshot"
                ):
                    raise RuntimeError("app-owned QMT runtime unavailable")
                qmt_runtime_component = dict(qmt_runtime_probe.snapshot())
                qmt_runtime_component["required"] = True
            except Exception as exc:
                app.logger.exception("readiness QMT runtime snapshot failed")
                qmt_runtime_component = {
                    "schema": "chanlun-qmt-runtime-readiness/v1",
                    "contract_id": (
                        "chanlun-qmt-runtime/app-runtime-contract/v1"
                    ),
                    "execution_owner": "APP_RUNTIME",
                    "required": True,
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "QMT_RUNTIME_OBSERVATION_UNAVAILABLE",
                    "reason_codes": [
                        "QMT_RUNTIME_OBSERVATION_UNAVAILABLE"
                    ],
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "automated_order_authorized": False,
                    "live_status": "LIVE_DISABLED",
                }
        else:
            qmt_runtime_component = {
                "schema": "chanlun-qmt-runtime-readiness/v1",
                "contract_id": "chanlun-qmt-runtime/app-runtime-contract/v1",
                "execution_owner": None,
                "required": False,
                "ready": True,
                "status": "disabled",
                "reason_code": "QMT_RUNTIME_MANAGEMENT_DISABLED",
                "reason_codes": [],
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }

        screening_required = bool(
            scheduler_required
            and runtime_component.get("status") == "running"
            and market == "a"
            and app.config.get("TRADING_SCREENING_BACKGROUND_ENABLED", True)
        )
        if screening_required:
            screening_service = app.extensions.get(
                "decision_support_trading_screening"
            )
            try:
                if screening_service is None or not hasattr(
                    screening_service, "health_snapshot"
                ):
                    raise RuntimeError("trading screening service unavailable")
                screening_component = dict(
                    screening_service.health_snapshot()
                )
                screening_component["required"] = True
                screening_component["ready"] = bool(
                    screening_component.get("ready")
                )
                screening_component["status"] = (
                    "ready"
                    if screening_component["ready"]
                    else "not_ready"
                )
            except Exception as exc:
                app.logger.exception("readiness trading screening snapshot failed")
                screening_component = {
                    "required": True,
                    "ready": False,
                    "status": "not_ready",
                    "last_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "reasons": ["screening_health_failed"],
                }
        else:
            screening_component = {
                "required": False,
                "ready": True,
                "status": "disabled",
                "reasons": [],
            }

        holding_monitor_required = bool(
            scheduler_required
            and runtime_component.get("status") == "running"
            and app.config.get("HOLDING_GROUP_MONITOR_ENABLED", True)
        )
        if holding_monitor_required:
            monitor_service = app.extensions.get("holding_group_monitor")
            try:
                if monitor_service is None or not callable(
                    getattr(monitor_service, "health_snapshot", None)
                ):
                    raise RuntimeError("holding group monitor unavailable")
                holding_monitor_component = dict(
                    monitor_service.health_snapshot()
                )
                holding_monitor_component["required"] = True
            except Exception as exc:
                app.logger.exception("holding monitor readiness snapshot failed")
                holding_monitor_component = {
                    "schema": "chanlun-holding-group-monitor/v1",
                    "required": True,
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "HOLDING_MONITOR_HEALTH_UNAVAILABLE",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "automated_order_authorized": False,
                    "live_status": "LIVE_DISABLED",
                }
        else:
            holding_monitor_component = {
                "schema": "chanlun-holding-group-monitor/v1",
                "required": False,
                "ready": True,
                "status": "disabled",
                "reason_code": "HOLDING_MONITOR_DISABLED",
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }

        forward_scheduler_required = bool(
            screening_required
            and app.config.get("FORWARD_SCHEDULER_MONITOR_ENABLED", True)
        )
        forward_scheduler_contract_id = (
            "chanlun-v3-forward-scheduler/app-runtime-contract/v1"
            if str(app.config.get("FORWARD_SCHEDULER_MODE", "APP")).upper()
            == "APP"
            else "chanlun-v3-forward-scheduler/windows-task-contract/v1"
        )
        if forward_scheduler_required:
            forward_scheduler_probe = app.extensions.get(
                "forward_scheduler_probe"
            )
            try:
                if forward_scheduler_probe is None or not hasattr(
                    forward_scheduler_probe, "snapshot"
                ):
                    raise RuntimeError("forward scheduler probe unavailable")
                forward_scheduler_component = dict(
                    forward_scheduler_probe.snapshot()
                )
                forward_scheduler_component["required"] = True
            except Exception as exc:
                app.logger.exception("forward scheduler observation failed")
                forward_scheduler_component = {
                    "schema": (
                        "chanlun-v3-forward-scheduler-readiness/v1"
                    ),
                    "contract_id": (
                        forward_scheduler_contract_id
                    ),
                    "required": True,
                    "ready": False,
                    "status": "unresolved",
                    "reason_code": (
                        "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"
                    ),
                    "reason_codes": [
                        "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"
                    ],
                    "tasks": [],
                    "task_count": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "automated_order_authorized": False,
                    "live_status": "LIVE_DISABLED",
                }
        else:
            forward_scheduler_component = {
                "schema": "chanlun-v3-forward-scheduler-readiness/v1",
                "contract_id": (
                    forward_scheduler_contract_id
                ),
                "required": False,
                "ready": True,
                "status": "disabled",
                "reason_code": "FORWARD_SCHEDULER_MONITOR_DISABLED",
                "reason_codes": [],
                "tasks": [],
                "task_count": 0,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }

        if screening_required:
            screening_review_ready = bool(
                screening_component.get(
                    "screening_review_ready",
                    screening_component.get("forward_review_ready", False),
                )
            )
            screening_review_reason = str(
                screening_component.get(
                    "screening_review_reason_code",
                    screening_component.get(
                        "forward_review_reason_code",
                        "SCREENING_REVIEW_READINESS_UNAVAILABLE",
                    ),
                )
            )
            human_review_service = app.extensions.get(
                "decision_support_human_review"
            )
            try:
                capture_probe = getattr(
                    human_review_service,
                    "forward_archive_capture_readiness",
                )
                capture_component = dict(
                    capture_probe(session=forward_session)
                )
            except Exception as exc:
                app.logger.exception("forward archive capture readiness failed")
                capture_component = {
                    "required": None,
                    "requirement_resolved": False,
                    "trading_session_status": "UNRESOLVED",
                    "trading_session_reason_code": (
                        "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
                    ),
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "SECTOR_CAPTURE_READINESS_UNAVAILABLE",
                    "session": (
                        None
                        if forward_session is None
                        else forward_session.isoformat()
                    ),
                    "receipt_proven": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "live_status": "LIVE_DISABLED",
                }
            sector_capture_ready = bool(capture_component.get("ready"))
            sector_capture_reason = str(
                capture_component.get("reason_code")
                or "SECTOR_CAPTURE_READINESS_UNAVAILABLE"
            )
            forward_archive_ready = (
                screening_review_ready and sector_capture_ready
            )
            forward_archive_reason = (
                screening_review_reason
                if not screening_review_ready
                else sector_capture_reason
            )
            forward_archive_component = {
                **capture_component,
                "required": capture_component.get("required"),
                "requirement_resolved": capture_component.get(
                    "requirement_resolved",
                    capture_component.get("required") is not None,
                ),
                "ready": forward_archive_ready,
                "status": "ready" if forward_archive_ready else "not_ready",
                "reason_code": (
                    "READY" if forward_archive_ready else forward_archive_reason
                ),
                "screening_review_ready": screening_review_ready,
                "screening_review_reason_code": screening_review_reason,
                "sector_capture_ready": sector_capture_ready,
                "sector_capture_reason_code": sector_capture_reason,
            }
            try:
                delivery_probe = getattr(
                    human_review_service,
                    "forward_delivery_readiness_nonblocking",
                    None,
                )
                if not callable(delivery_probe):
                    delivery_probe = getattr(
                        human_review_service,
                        "forward_delivery_readiness",
                    )
                forward_delivery_component = dict(
                    delivery_probe(session=forward_session)
                )
            except Exception as exc:
                app.logger.exception("forward delivery readiness failed")
                forward_delivery_component = {
                    "required": None,
                    "requirement_resolved": False,
                    "trading_session_status": "UNRESOLVED",
                    "trading_session_reason_code": (
                        "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
                    ),
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "FORWARD_DELIVERY_READINESS_UNAVAILABLE",
                    "session": (
                        None
                        if forward_session is None
                        else forward_session.isoformat()
                    ),
                    "capture_ready": False,
                    "evaluation_ready": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "paper_status": "REVIEW_REQUIRED",
                    "live_status": "LIVE_DISABLED",
                }
        else:
            forward_archive_component = {
                "required": False,
                "ready": True,
                "status": "disabled",
                "reason_code": "SCREENING_DISABLED",
                "session": (
                    None
                    if forward_session is None
                    else forward_session.isoformat()
                ),
                "screening_review_ready": False,
                "screening_review_reason_code": "SCREENING_DISABLED",
                "sector_capture_ready": False,
                "sector_capture_reason_code": "SCREENING_DISABLED",
                "receipt_proven": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
            forward_delivery_component = {
                "required": False,
                "ready": False,
                "status": "disabled",
                "reason_code": "SCREENING_DISABLED",
                "session": (
                    None
                    if forward_session is None
                    else forward_session.isoformat()
                ),
                "capture_ready": False,
                "evaluation_ready": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "paper_status": "REVIEW_REQUIRED",
                "live_status": "LIVE_DISABLED",
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
        if qmt_runtime_required and not qmt_runtime_component["ready"]:
            reasons.append("qmt_runtime_not_ready")
        if screening_required and not screening_component["ready"]:
            reasons.append("trading_screening_not_ready")
        # 持仓预警属于只读研究观察面。其失败必须在独立组件中完整暴露，
        # 但不能让图表 Web/QMT 本身被宣告不可用；这与 forward research
        # evidence 的 readiness 口径一致。

        ready = not reasons
        payload = {
            "status": "ready" if ready else "not_ready",
            "revision": build_revision,
            "pid": os.getpid(),
            "market": market,
            "components": {
                "scheduler": scheduler_component,
                "runtime": runtime_component,
                "qmt_runtime": qmt_runtime_component,
                "metadata": metadata_component,
                "symbols": symbols_component,
                "ticks": ticks_component,
                "trading_screening": screening_component,
                "holding_group_monitor": holding_monitor_component,
                "forward_scheduler": forward_scheduler_component,
                "forward_archive": forward_archive_component,
                "forward_delivery": forward_delivery_component,
            },
            "reasons": reasons,
        }
        return payload, 200 if ready else 503

    def health_snapshot(kind, market="a", forward_session=None):
        if kind == "livez":
            return {"status": "alive", "revision": build_revision}, 200
        if kind == "healthz":
            return {"status": "ok", "revision": build_revision}, 200
        if kind == "readyz":
            parsed_forward_session = forward_session
            if isinstance(forward_session, str):
                try:
                    parsed_forward_session = datetime.date.fromisoformat(
                        forward_session
                    )
                    if parsed_forward_session.isoformat() != forward_session:
                        raise ValueError("forward session is not canonical")
                except ValueError:
                    return {
                        "status": "not_ready",
                        "revision": build_revision,
                        "pid": os.getpid(),
                        "market": market,
                        "components": {},
                        "reasons": ["invalid_forward_session"],
                    }, 400
            elif forward_session is not None and not isinstance(
                forward_session, datetime.date
            ):
                return {
                    "status": "not_ready",
                    "revision": build_revision,
                    "pid": os.getpid(),
                    "market": market,
                    "components": {},
                    "reasons": ["invalid_forward_session"],
                }, 400
            return _readyz_snapshot(market, parsed_forward_session)
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
            request.args.get("forward_session"),
        )

    @app.route("/")
    @login_required
    def index_show():
        requested_market = (request.args.get("market") or "a").strip().lower()
        initial_market = requested_market if requested_market in market_types else "a"
        review_chart_lock = None
        review_values = {
            "candidate_id": str(request.args.get("review_candidate_id") or ""),
            "source_sha256": str(request.args.get("review_source_sha256") or ""),
            "review_as_of": str(request.args.get("review_as_of") or ""),
        }
        if any(review_values.values()):
            if not all(review_values.values()):
                abort(404)
            service = app.extensions.get("decision_support_human_review")
            validator = getattr(service, "validate_chart_lock", None)
            human_error = None
            try:
                if not callable(validator):
                    raise ValueError("human review service unavailable")
                review_chart_lock = validator(
                    candidate_id=review_values["candidate_id"],
                    source_sha256=review_values["source_sha256"],
                    review_as_of=int(review_values["review_as_of"]),
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                human_error = exc
            if review_chart_lock is None:
                try:
                    from .services.research_audit import (
                        validate_risk_point_chart_lock,
                    )

                    review_chart_lock = validate_risk_point_chart_lock(
                        app.config.get(
                            "RESEARCH_AUDIT_ROOT",
                            pathlib.Path(__file__).resolve().parents[3],
                        ),
                        point_id=review_values["candidate_id"],
                        source_sha256=review_values["source_sha256"],
                        review_as_of=int(review_values["review_as_of"]),
                    )
                except (TypeError, ValueError, RuntimeError):
                    app.logger.debug(
                        "causal chart lock rejected by human and risk validators: %r",
                        human_error,
                    )
                    abort(404)
            requested_code = str(request.args.get("code") or "")
            if (
                initial_market != "a"
                or requested_code != review_chart_lock.get("symbol")
            ):
                abort(404)
            if review_chart_lock.get("lock_kind") == "RISK_POINT_AUDIT":
                requested_intervals = tuple(
                    value.strip()
                    for value in str(request.args.get("intervals") or "").split(",")
                    if value.strip()
                )
                if requested_intervals != (
                    str(review_chart_lock.get("chart_interval")),
                ):
                    abort(404)

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
            enable_sse=(config.ENABLE_SSE_PUSH and review_chart_lock is None),
            review_chart_lock=review_chart_lock,
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
    # Constructed after the QMT/official trading-session provider exists.  The
    # lifecycle closures deliberately reference this late-bound controller so
    # app factories remain side-effect free until start_runtime_services().
    app_forward_scheduler = None
    app_qmt_runtime = None

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
            if app_qmt_runtime is not None:
                # QMT must be established before any metadata/tick/native
                # screening worker can consume its local runtime.
                app_qmt_runtime.startup()
                _ensure_start_is_current()
            native_gateway_startup = getattr(trading_gateway, "startup", None)
            if callable(native_gateway_startup):
                # A restored coverage snapshot may make every scan currently
                # not due.  Without an explicit, request-free handshake the
                # lazy native process then remains stopped forever and
                # /readyz incorrectly reports the otherwise healthy app as
                # unavailable until the next market event happens to wake it.
                native_gateway_startup()
                _ensure_start_is_current()
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
            if app.config.get("TRADING_SCREENING_BACKGROUND_ENABLED", True):
                decision_support_trading_screening.start_background()
                _ensure_start_is_current()

            if enable_scheduler:
                if app.config.get("LEGACY_ALERT_TASKS_ENABLED", False):
                    _alert_tasks.run()

                if app.config.get("LEGACY_SIGNAL_MONITOR_ENABLED", False):
                    from chanlun.signal_monitor.scheduler import (
                        register_signal_jobs,
                    )

                    register_signal_jobs(scheduler)

                if holding_group_monitor is not None:
                    holding_group_monitor.register_job(scheduler)

                recursive_root = getattr(config, "RECURSIVE_MONITOR_CONFIG", {})
                if (
                    type(recursive_root) is dict
                    and recursive_root.get("enabled") is True
                ):
                    from chanlun.recursive_bt.monitor.app_monitor import (
                        register_recursive_monitor_jobs,
                    )

                    monitors = register_recursive_monitor_jobs(scheduler)
                else:
                    monitors = {}
                _recursive_monitors.clear()
                if isinstance(monitors, dict):
                    _recursive_monitors.extend(monitors.values())
                elif monitors:
                    _recursive_monitors.extend(monitors)
                if app_qmt_runtime is not None:
                    app_qmt_runtime.register_jobs()
                if app_forward_scheduler is not None:
                    app_forward_scheduler.register_jobs()
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
                    # App factories are side-effect free by default. An app that
                    # never claimed the process-wide services must not stop the
                    # owner app's SSE, cache, metadata, or revalidation workers.
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
            if app_forward_scheduler is not None:
                _cleanup("app-forward-scheduler", app_forward_scheduler.stop)
            if app_qmt_runtime is not None:
                _cleanup("app-qmt-runtime", app_qmt_runtime.stop)
            _cleanup(
                "trading-screening",
                lambda: decision_support_trading_screening.shutdown_background(
                    wait=True, timeout=1.0
                ),
            )
            native_gateway_close = getattr(trading_gateway, "close", None)
            if callable(native_gateway_close):
                _cleanup("trading-screening-native-gateway", native_gateway_close)

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

    def _trading_screening_clock():
        configured = app.config.get("TRADING_SCREENING_CLOCK") or app.config.get(
            "EARLY_SCREENING_CLOCK"
        )
        if callable(configured):
            return configured()
        return datetime.datetime.now(pytz.timezone("Asia/Shanghai"))

    from chanlun.decision_support.trading_system.human_assisted_decision import (
        HumanAssistedDecisionCore,
    )
    from chanlun.notifications import DingTalkWebhookNotifier

    from .services.research_audit import (
        ResearchAuditUnavailable,
        build_research_audit_snapshot,
    )
    from .services.trading_notifications import SignalNotificationDispatcher
    from .services.trading_screening import (
        TradingScreeningConfig,
        TradingScreeningService,
    )
    from .services.trading_screening_gateway import NativeTradingDataGateway
    from .services.trading_screening_process import (
        NativeTradingDataGatewayProcessProxy,
        NativeWorkerProcessConfig,
        native_sector_snapshot_cache_revision,
    )
    from .services.human_review_screening import HumanReviewScreeningService
    from .services.holding_group_monitor import (
        HoldingGroupMonitorConfig,
        HoldingGroupMonitorService,
        build_non_a_monitor_universe,
    )

    def _trading_screening_exchange():
        from chanlun.exchange import Market, get_exchange

        return get_exchange(Market.A)

    def _trading_screening_universe(_exchange):
        # QMT GICS3 current components are authoritative for sector-first
        # selection; never fall back to ExchangeQMT.all_stocks/get_full_tick.
        return ()

    def _trading_screening_watchlist():
        from chanlun.persistence.db import db

        holding_group = str(
            app.config.get("TRADING_SCREENING_MANUAL_HOLDING_GROUP") or "我的持仓"
        ).strip()
        values = []
        for group in db.zx_get_global_groups():
            group_name = group.zx_group
            if group_name == holding_group:
                continue
            for stock in db.zx_get_global_group_stocks(group_name):
                if stock.market != "a":
                    continue
                values.append(
                    {
                        "code": stock.stock_code,
                        "name": stock.stock_name,
                        "group": group_name,
                    }
                )
        return values

    decision_support_human_review: HumanReviewScreeningService | None = None

    def _trading_screening_manual_holdings_snapshot():
        from chanlun.zixuan import MANUAL_HOLDING_ZX_GROUP, ZiXuan

        holding_group = str(
            app.config.get("TRADING_SCREENING_MANUAL_HOLDING_GROUP")
            or MANUAL_HOLDING_ZX_GROUP
        ).strip()
        rows = ZiXuan("a").zx_stocks(holding_group)
        positions = [
            {
                "market": str(row.get("market") or ""),
                "code": str(row.get("code") or ""),
                "name": str(row.get("name") or row.get("code") or ""),
                "monitoring_scope": (
                    "A_SHARE_STRICT_DECISION_CORE"
                    if row.get("market") == "a"
                    else "NON_A_AUXILIARY_STRUCTURE_RADAR"
                ),
                "decision_mode": (
                    "UNIFIED_HUMAN_ASSISTED_DECISION_CORE"
                    if row.get("market") == "a"
                    else "APPROXIMATE_STRUCTURE_OBSERVATION"
                ),
            }
            for row in rows
            if row.get("market") and row.get("code")
        ]
        positions.sort(key=lambda row: (row["market"], row["code"]))
        a_share_priority_count = sum(
            row["monitoring_scope"] == "A_SHARE_STRICT_DECISION_CORE"
            for row in positions
        )
        auxiliary_count = sum(
            row["monitoring_scope"] == "NON_A_AUXILIARY_STRUCTURE_RADAR"
            for row in positions
        )
        covered_count = a_share_priority_count + auxiliary_count
        return {
            "schema": "chanlun-local-manual-holdings/v1",
            "source": "LOCAL_GLOBAL_WATCHLIST_GROUP",
            "group_name": holding_group,
            "group_scope": "GLOBAL_ACROSS_MARKETS",
            "available": True,
            "status": "ready",
            "positions": positions,
            "declared_count": len(positions),
            "priority_monitor_count": a_share_priority_count,
            "cross_market_monitor_count": auxiliary_count,
            "covered_monitor_count": covered_count,
            "unsupported_market_count": len(positions) - covered_count,
            "quantity_available": False,
            "cost_basis_available": False,
            "sellable_quantity_available": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }

    if not callable(
        app.config.get("TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER")
    ):
        app.config[
            "TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"
        ] = _trading_screening_manual_holdings_snapshot

    def _trading_screening_holdings():
        configured = app.config.get("TRADING_SCREENING_HOLDINGS_PROVIDER")
        if callable(configured):
            return configured()
        manual_snapshot = _trading_screening_manual_holdings_snapshot()
        manually_declared = tuple(
            row["code"]
            for row in manual_snapshot["positions"]
            if row["market"] == "a"
        )
        service = decision_support_human_review
        virtual = () if service is None else service.virtual_holding_codes()
        return tuple(dict.fromkeys((*manually_declared, *virtual)))

    def _non_a_monitor_universe():
        """Read global groups on every scan so UI edits need no app restart.

        US symbols from every group are monitored. Other non-A markets retain
        the previous holding-only behavior until their broader selection
        contracts are explicitly enabled.
        """

        from chanlun.persistence.db import db
        from chanlun.zixuan import MANUAL_HOLDING_ZX_GROUP

        holding_group = str(
            app.config.get("TRADING_SCREENING_MANUAL_HOLDING_GROUP")
            or MANUAL_HOLDING_ZX_GROUP
        ).strip()
        group_members: dict[str, list[dict[str, object]]] = {}
        for group in db.zx_get_global_groups():
            group_name = str(group.zx_group)
            group_members[group_name] = [
                {
                    "market": str(stock.market),
                    "code": str(stock.stock_code),
                    "name": str(stock.stock_name or stock.stock_code),
                }
                for stock in db.zx_get_global_group_stocks(group_name)
            ]
        return build_non_a_monitor_universe(
            group_members,
            holding_group=holding_group,
            expanded_watchlist_markets=frozenset({"us"}),
        )

    recursive_monitor_config = getattr(config, "RECURSIVE_MONITOR_CONFIG", {})
    recursive_common = (
        recursive_monitor_config.get("common", {})
        if isinstance(recursive_monitor_config, Mapping)
        else {}
    )
    if not isinstance(recursive_common, Mapping):
        recursive_common = {}
    trading_screening_dingtalk_webhook = str(
        app.config.get("TRADING_SCREENING_DINGTALK_WEBHOOK")
        or app.config.get("EARLY_SCREENING_DINGTALK_WEBHOOK")
        or os.environ.get("CHANLUN_DINGTALK_WEBHOOK")
        or recursive_common.get("dingtalk_webhook")
        or ""
    ).strip()
    trading_screening_dingtalk_keyword = str(
        app.config.get("TRADING_SCREENING_DINGTALK_KEYWORD")
        or app.config.get("EARLY_SCREENING_DINGTALK_KEYWORD")
        or os.environ.get("CHANLUN_DINGTALK_KEYWORD")
        or "买卖通知"
    ).strip()
    dry_run_value = app.config.get(
        "TRADING_SCREENING_NOTIFICATION_DRY_RUN",
        os.environ.get("CHANLUN_NOTIFICATION_DRY_RUN", ""),
    )
    trading_screening_dry_run = (
        dry_run_value
        if type(dry_run_value) is bool
        else str(dry_run_value).strip().lower() in {"1", "true", "yes", "on"}
    )
    alert_chart_image_service = None
    alert_chart_public_base_url = str(
        app.config.get("ALERT_CHART_PUBLIC_BASE_URL") or ""
    ).strip()
    if alert_chart_public_base_url:
        from .services.alert_chart_images import (
            AlertChartImageService,
            SignedAlertChartStore,
        )
        from .services.tradingview_chart_capture import (
            TradingViewClientScreenshotRenderer,
        )

        signing_secret = hmac.new(
            app.secret_key
            if isinstance(app.secret_key, bytes)
            else str(app.secret_key).encode("utf-8"),
            b"chanlun-alert-chart-public-route/v1",
            hashlib.sha256,
        ).digest()
        alert_chart_store = SignedAlertChartStore(
            root=pathlib.Path(app.config["ALERT_CHART_ROOT"]),
            public_base_url=alert_chart_public_base_url,
            secret=signing_secret,
            ttl_seconds=int(app.config["ALERT_CHART_TTL_SECONDS"]),
        )

        def _alert_capture_session_cookie() -> str:
            serializer = app.session_interface.get_signing_serializer(app)
            if serializer is None:
                raise RuntimeError("alert capture session signer is unavailable")
            return serializer.dumps(
                {
                    "_user_id": _current_login_user_id(),
                    "_fresh": True,
                }
            )

        tradingview_capture = TradingViewClientScreenshotRenderer(
            base_url=str(app.config["ALERT_CHART_CAPTURE_BASE_URL"]),
            session_cookie_provider=_alert_capture_session_cookie,
        )
        alert_chart_image_service = AlertChartImageService(
            alert_chart_store,
            browser_renderer=tradingview_capture,
        )
        app.extensions["alert_chart_image_store"] = alert_chart_store
        app.extensions["alert_chart_image_service"] = alert_chart_image_service

        @app.get("/public/alert-chart/<artifact_id>.png")
        def public_alert_chart(artifact_id: str):
            image_path = alert_chart_store.resolve(
                artifact_id,
                expires=request.args.get("expires"),
                signature=request.args.get("signature"),
            )
            if image_path is None:
                abort(404)
            response = send_file(
                image_path,
                mimetype="image/png",
                as_attachment=False,
                conditional=True,
                max_age=3600,
            )
            response.headers["Cache-Control"] = "public, max-age=3600, immutable"
            response.headers["X-Robots-Tag"] = "noindex, noarchive"
            return response

    raw_trading_notifier = (
        DingTalkWebhookNotifier(
            webhook=trading_screening_dingtalk_webhook,
            keyword=trading_screening_dingtalk_keyword,
            dry_run=trading_screening_dry_run,
            # One final app-wide transport gate covers strict A-share signals
            # and the auxiliary cross-market monitor.  It persists only
            # message hashes, never webhook credentials or message bodies.
            dedupe_state_path=(
                config.get_data_path()
                / "monitor"
                / "dingtalk_outbound_dedupe.json"
            ),
            rich_content_provider=alert_chart_image_service,
        )
        if trading_screening_dingtalk_webhook or trading_screening_dry_run
        else None
    )
    if app.config.get("HOLDING_GROUP_MONITOR_ENABLED", True):
        holding_group_monitor = HoldingGroupMonitorService(
            # The provider queries global groups every run. US watchlist edits
            # therefore take effect within one monitor interval without an app
            # restart. A shares remain exclusively in the strict decision core.
            positions_provider=_non_a_monitor_universe,
            notifier=raw_trading_notifier,
            state_root=(config.get_data_path() / "monitor"),
            config=HoldingGroupMonitorConfig(
                interval_seconds=int(
                    app.config["HOLDING_GROUP_MONITOR_INTERVAL_SECONDS"]
                ),
                start_delay_seconds=int(
                    app.config["HOLDING_GROUP_MONITOR_START_DELAY_SECONDS"]
                ),
                max_workers=int(app.config["HOLDING_GROUP_MONITOR_WORKERS"]),
                op_level="1m",
                mid_level="5m",
                big_level="30m",
            ),
        )
    trading_notification_dispatcher = (
        SignalNotificationDispatcher(
            raw_trading_notifier,
            state_path=(
                config.get_data_path()
                / "decision_support"
                / "trading_notification_state.json"
            ),
        )
        if raw_trading_notifier is not None
        else None
    )
    trading_gateway = app.config.get("TRADING_SCREENING_GATEWAY")
    if trading_gateway is None:
        if app.config.get("TRADING_SCREENING_NATIVE_PROCESS_ISOLATION", True):
            # Manual/unversioned launches stay cache-disabled.  Official
            # launches bind the persistent sector snapshot to its complete but
            # UI-independent native producer: trading/structure/QMT changes
            # invalidate it, while a template or JavaScript-only deploy does
            # not force a 10-15 minute sector replay.
            sector_cache_revision = native_sector_snapshot_cache_revision(
                build_revision
            )
            trading_gateway = NativeTradingDataGatewayProcessProxy(
                watchlist_provider=_trading_screening_watchlist,
                holdings_provider=_trading_screening_holdings,
                log_path=(
                    config.get_data_path()
                    / "decision_support"
                    / "trading_screening_native_worker.log"
                ),
                process_config=NativeWorkerProcessConfig(
                    startup_timeout_seconds=float(
                        app.config[
                            "TRADING_SCREENING_NATIVE_STARTUP_TIMEOUT_SECONDS"
                        ]
                    ),
                    native_idle_timeout_seconds=float(
                        app.config[
                            "TRADING_SCREENING_NATIVE_IDLE_TIMEOUT_SECONDS"
                        ]
                    ),
                    restart_backoff_seconds=float(
                        app.config[
                            "TRADING_SCREENING_NATIVE_RESTART_BACKOFF_SECONDS"
                        ]
                    ),
                ),
                sector_cache_path=(
                    config.get_data_path()
                    / "decision_support"
                    / "trading_screening_sector_snapshot.json"
                    if sector_cache_revision is not None
                    else None
                ),
                sector_cache_revision=sector_cache_revision,
                worker_environment={"CHANLUN_BUILD_REVISION": build_revision},
                structure_worker_count=int(
                    app.config["TRADING_SCREENING_STOCK_WORKERS"]
                ),
            )
        else:
            from chanlun.decision_support.trading_system.higher_timeframe_gate import (
                QmtHigherTimeframeGateSource,
            )
            from chanlun.exchange.qmt_screening_sector_source import (
                QmtSectorCompositeSource,
                QmtSectorStrengthSource,
                build_qmt_gics3_sector_catalog,
                qmt_trading_session_evidence,
                qmt_trading_sessions,
            )

            sector_frames = QmtSectorCompositeSource()
            sector_strength = QmtSectorStrengthSource()
            higher_timeframe = QmtHigherTimeframeGateSource(
                exchange_provider=_trading_screening_exchange,
                sector_frame_provider=sector_frames.frame,
                trading_calendar_provider=qmt_trading_sessions,
            )
            trading_gateway = NativeTradingDataGateway(
                exchange_provider=_trading_screening_exchange,
                sector_exchange_provider=_trading_screening_exchange,
                universe_provider=_trading_screening_universe,
                sector_provider=build_qmt_gics3_sector_catalog,
                sector_frame_provider=sector_frames.frame,
                sector_strength_provider=sector_strength.strengths,
                higher_timeframe_provider=higher_timeframe.gates,
                trading_session_provider=qmt_trading_session_evidence,
                watchlist_provider=_trading_screening_watchlist,
                holdings_provider=_trading_screening_holdings,
            )
    official_calendar_path = app.config.get(
        "TRADING_SESSION_OFFICIAL_CALENDAR_PATH"
    )
    qmt_calendar_provider = getattr(
        trading_gateway,
        "trading_session_evidence",
        None,
    )
    if not callable(qmt_calendar_provider):
        raise TypeError("trading screening calendar provider is unavailable")
    if official_calendar_path is None:
        trading_session_provider = qmt_calendar_provider
    else:
        def trading_session_provider(*, session, observed_at):
            return authoritative_trading_session_evidence(
                session=session,
                observed_at=observed_at,
                calendar_path=pathlib.Path(official_calendar_path),
                fallback_provider=qmt_calendar_provider,
            )

    if (
        app.config.get("TRADING_SCREENING_NATIVE_PROCESS_ISOLATION", True)
        and not app.config.get("TESTING", False)
        and (
            ".run." in build_revision
            or is_content_addressed_application_source_revision(build_revision)
        )
    ):
        # Prime recent immutable official/QMT calendar verdicts before the background
        # universe scan can occupy the serialized native worker.  This keeps
        # readiness non-blocking without masking a missed prior-session
        # Capture/Evaluate delivery as merely "calendar unavailable".
        calendar_provider = trading_session_provider
        if callable(calendar_provider):
            calendar_observed_at = _trading_screening_clock()
            try:
                if (
                    not isinstance(calendar_observed_at, datetime.datetime)
                    or calendar_observed_at.tzinfo is None
                    or calendar_observed_at.utcoffset() is None
                ):
                    raise ValueError(
                        "trading calendar warmup clock must be timezone-aware"
                    )
                calendar_observed_at = calendar_observed_at.astimezone(
                    datetime.timezone(datetime.timedelta(hours=8))
                )
                for days_ago in range(1, 11):
                    calendar_session = (
                        calendar_observed_at.date()
                        - datetime.timedelta(days=days_ago)
                    )
                    evidence = calendar_provider(
                        session=calendar_session,
                        observed_at=calendar_observed_at,
                    )
                    if (
                        isinstance(evidence, Mapping)
                        and evidence.get("classification") == "UNRESOLVED"
                    ):
                        break
            except Exception as exc:
                app.logger.warning(
                    "trading calendar warmup unavailable: %s: %s",
                    type(exc).__name__,
                    str(exc)[:160],
                )
    audit_root = app.config.get(
        "RESEARCH_AUDIT_ROOT",
        pathlib.Path(__file__).resolve().parents[3],
    )
    try:
        audit_snapshot = build_research_audit_snapshot(audit_root)
        backtest_verdict = {
            **audit_snapshot["verdict"],
            "evidence_grade": audit_snapshot["data_evidence"]["grade"],
        }
    except ResearchAuditUnavailable:
        backtest_verdict = {
            "live_ready": False,
            "status": "evidence_unavailable",
            "evidence_grade": "invalid",
        }
    decision_support_trading_screening = TradingScreeningService(
        market_data=trading_gateway,
        sector_catalog=trading_gateway,
        engine=HumanAssistedDecisionCore(),
        cache_path=(
            config.get_data_path()
            / "decision_support"
            / "trading_screening_snapshot.json"
        ),
        human_review_archive_root=pathlib.Path(
            app.config["HUMAN_REVIEW_LIVE_ARCHIVE_ROOT"]
        ),
        clock=_trading_screening_clock,
        notifier=trading_notification_dispatcher,
        config=TradingScreeningConfig(
            refresh_interval_seconds=int(
                app.config.get("TRADING_SCREENING_REFRESH_SECONDS", 60)
            ),
            priority_monitoring_enabled=bool(
                app.config.get(
                    "TRADING_SCREENING_PRIORITY_MONITOR_ENABLED",
                    True,
                )
            ),
            max_priority_monitor_symbols_per_refresh=int(
                app.config.get(
                    "TRADING_SCREENING_PRIORITY_MONITOR_MAX_SYMBOLS",
                    16,
                )
            ),
            max_symbols_per_refresh=int(
                app.config.get(
                    "TRADING_SCREENING_SYMBOLS_PER_REFRESH",
                    64,
                )
            ),
            max_total_symbols_per_refresh=int(
                app.config.get(
                    "TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH",
                    16,
                )
            ),
            priority_monitor_interval_seconds=int(
                app.config.get(
                    "TRADING_SCREENING_PRIORITY_MONITOR_INTERVAL_SECONDS",
                    60,
                )
            ),
            max_structure_age_seconds=int(
                app.config.get("TRADING_SCREENING_MAX_STRUCTURE_AGE_SECONDS", 864000)
            ),
            stock_worker_count=(
                int(app.config["TRADING_SCREENING_STOCK_WORKERS"])
                if app.config.get("TRADING_SCREENING_NATIVE_PROCESS_ISOLATION", True)
                else 1
            ),
        ),
        backtest_verdict=backtest_verdict,
    )
    trading_screening_snapshot_path = (
        config.get_data_path()
        / "decision_support"
        / "trading_screening_snapshot.json"
    )
    repository_root = pathlib.Path(__file__).resolve().parents[3]
    from .services.app_qmt_runtime import AppQmtRuntimeController
    from .services.app_forward_scheduler import (
        AppForwardSchedulerController,
        discover_qmt_local_data_dir,
        evaluation_readiness_from_health,
    )
    from .services.forward_scheduler import ForwardSchedulerProbe

    qmt_runtime_mode = str(
        app.config.get("QMT_RUNTIME_MODE", "APP")
    ).strip().upper()
    if qmt_runtime_mode not in {"APP", "WINDOWS", "DISABLED"}:
        raise ValueError("QMT_RUNTIME_MODE must be APP, WINDOWS or DISABLED")
    if qmt_runtime_mode == "APP":
        def _prepare_qmt_runtime_change(action):
            native_gateway_close = getattr(trading_gateway, "close", None)
            if not callable(native_gateway_close):
                return
            try:
                native_gateway_close()
            except Exception:
                app.logger.exception(
                    "failed to quiesce native screening before QMT %s",
                    action,
                )

        def _resume_after_qmt_runtime_change(action):
            try:
                decision_support_trading_screening.ensure_refresh()
            except Exception:
                app.logger.exception(
                    "failed to wake native screening after QMT %s",
                    action,
                )

        configured_helper = str(
            app.config.get("QMT_RUNTIME_HELPER") or ""
        ).strip()
        app_qmt_runtime = AppQmtRuntimeController(
            scheduler=scheduler,
            repository_root=repository_root,
            clock=_trading_screening_clock,
            helper_script=(
                pathlib.Path(configured_helper)
                if configured_helper
                else None
            ),
            before_change=_prepare_qmt_runtime_change,
            after_change=_resume_after_qmt_runtime_change,
            startup_timeout_seconds=int(
                app.config.get("QMT_RUNTIME_STARTUP_TIMEOUT_SECONDS", 120)
            ),
            warmup_seconds=int(
                app.config.get("QMT_RUNTIME_WARMUP_SECONDS", 90)
            ),
            recovery_cooldown_seconds=int(
                app.config.get("QMT_RUNTIME_RECOVERY_COOLDOWN_SECONDS", 300)
            ),
            observation_max_age_seconds=int(
                app.config.get("QMT_RUNTIME_OBSERVATION_MAX_AGE_SECONDS", 180)
            ),
        )

    forward_windows_scheduler_probe = ForwardSchedulerProbe(
        audit_script=repository_root
        / "ops"
        / "audit_v3_forward_paper_tasks.ps1",
        ttl_seconds=float(
            app.config.get("FORWARD_SCHEDULER_MONITOR_TTL_SECONDS", 30.0)
        ),
    )
    forward_scheduler_mode = str(
        app.config.get("FORWARD_SCHEDULER_MODE", "APP")
    ).strip().upper()
    if forward_scheduler_mode not in {"APP", "WINDOWS", "DISABLED"}:
        raise ValueError(
            "FORWARD_SCHEDULER_MODE must be APP, WINDOWS or DISABLED"
        )
    if forward_scheduler_mode == "APP":
        def _app_forward_capture_readiness(*, session, observed_at):
            payload, _status = health_snapshot(
                "readyz",
                market="a",
                forward_session=session,
            )
            delivery = payload.get("components", {}).get(
                "forward_delivery", {}
            )
            return {
                # A sector receipt is merely an input.  Adoption/success must
                # prove that the forward ledger itself contains CAPTURE.
                "ready": bool(delivery.get("capture_ready")),
                "reason_code": str(
                    delivery.get("reason_code")
                    or "FORWARD_CAPTURE_DELIVERY_UNAVAILABLE"
                ),
                "observed_at": observed_at.isoformat(),
            }

        def _app_forward_evaluation_readiness(*, session, observed_at):
            """Reuse the exact readiness facts formerly polled by PowerShell."""

            payload, _status = health_snapshot(
                "readyz",
                market="a",
                forward_session=session,
            )
            return evaluation_readiness_from_health(
                payload,
                session=session,
                observed_at=observed_at,
            )

        try:
            forward_qmt_data_dir = discover_qmt_local_data_dir(
                app.config.get("FORWARD_QMT_LOCAL_DATA_DIR") or None
            )
        except (OSError, RuntimeError, ValueError) as exc:
            # Keep the web page available but fail the paper-admission gate
            # closed.  The controller snapshot carries the exact missing-QMT
            # reason until configuration is repaired.
            app.logger.error(
                "app-owned forward QMT data directory unresolved: %s",
                str(exc)[:200],
            )
            forward_qmt_data_dir = None
        app_forward_scheduler = AppForwardSchedulerController(
            scheduler=scheduler,
            repository_root=repository_root,
            forward_root=pathlib.Path(
                app.config["HUMAN_REVIEW_FORWARD_ROOT"]
            ),
            qmt_local_data_dir=forward_qmt_data_dir,
            trading_session_provider=trading_session_provider,
            capture_readiness_provider=_app_forward_capture_readiness,
            evaluation_readiness_provider=(
                _app_forward_evaluation_readiness
            ),
            clock=_trading_screening_clock,
        )
        forward_scheduler_probe = app_forward_scheduler
    else:
        # WINDOWS is a rollback mode.  DISABLED is reserved for isolated tests;
        # its monitor is disabled by default, so no host process is spawned.
        forward_scheduler_probe = forward_windows_scheduler_probe
    decision_support_human_review = HumanReviewScreeningService(
        repository_root=repository_root,
        historical_report=pathlib.Path(
            app.config["HUMAN_REVIEW_HISTORICAL_REPORT"]
        ),
        preferred_historical_report=pathlib.Path(
            app.config["HUMAN_REVIEW_CURRENT_HISTORICAL_REPORT"]
        ),
        forward_root=pathlib.Path(app.config["HUMAN_REVIEW_FORWARD_ROOT"]),
        feedback_ledger=pathlib.Path(
            app.config["HUMAN_REVIEW_FEEDBACK_LEDGER"]
        ),
        sector_ledger=pathlib.Path(app.config["QMT_SECTOR_CAPTURE_LEDGER"]),
        paper_ledger=pathlib.Path(app.config["HUMAN_REVIEW_PAPER_LEDGER"]),
        parameter_snapshot=pathlib.Path(
            app.config["HUMAN_REVIEW_PARAMETER_SNAPSHOT"]
        ),
        live_screening_snapshot=trading_screening_snapshot_path,
        live_archive_root=pathlib.Path(
            app.config["HUMAN_REVIEW_LIVE_ARCHIVE_ROOT"]
        ),
        forward_markout_report=pathlib.Path(
            app.config["HUMAN_REVIEW_FORWARD_MARKOUT"]
        ),
        forward_warmup_lineage_report=pathlib.Path(
            app.config["HUMAN_REVIEW_FORWARD_WARMUP_LINEAGE"]
        ),
        sector_capture_due=datetime.time(9, 10),
        trading_session_provider=trading_session_provider,
        forward_scheduler_provider=(
            forward_scheduler_probe.snapshot
            if app.config.get("FORWARD_SCHEDULER_MONITOR_ENABLED", True)
            # Disabling the readiness/UI observation must never turn into a
            # semantic bypass for new virtual intents.  Generic tests keep the
            # host-independent monitor disabled, so inject a deterministic
            # invalid observation instead of starting PowerShell; the shared
            # validator then fails the paper path closed.
            else lambda **_kwargs: {}
        ),
    )

    app.extensions.update(
        {
            "scheduler": scheduler,
            "alert_tasks": _alert_tasks,
            "xuangu_tasks": _xuangu_tasks,
            "recursive_monitors": _recursive_monitors,
            "holding_group_monitor": holding_group_monitor,
            "readiness": readiness_registry,
            "metadata_warmup_thread": metadata_warmup_thread,
            "login_rate_limiter": login_rate_limiter,
            "health_snapshot": health_snapshot,
            "runtime_status": runtime_status,
            "start_runtime_services": start_runtime_services,
            "shutdown_runtime_services": shutdown_runtime_services,
            "shutdown_scheduler": shutdown_scheduler,
            "decision_support_trading_screening": (
                decision_support_trading_screening
            ),
            "decision_support_trading_screening_gateway": trading_gateway,
            "decision_support_human_review": decision_support_human_review,
            "forward_scheduler_probe": forward_scheduler_probe,
            "forward_windows_scheduler_probe": (
                forward_windows_scheduler_probe
            ),
            "app_forward_scheduler": app_forward_scheduler,
            "app_qmt_runtime": app_qmt_runtime,
        }
    )
    if scheduler_enabled:
        start_runtime_services(enable_scheduler=True)
        atexit.register(shutdown_runtime_services)

    return app
