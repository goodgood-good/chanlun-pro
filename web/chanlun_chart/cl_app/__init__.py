import datetime
import os
import pytz
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
from apscheduler.executors.tornado import TornadoExecutor
from apscheduler.schedulers.tornado import TornadoScheduler
from flask import Flask, redirect, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user
from chanlun import config, fun
from chanlun.security import get_flask_secret_key, verify_login_password
__all__ = ["create_app"]


def create_app(test_config=None):
    # 任务对象
    scheduler = TornadoScheduler(timezone=pytz.timezone("Asia/Shanghai"))
    scheduler.add_executor(TornadoExecutor())
    scheduler.my_task_list = {}

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
            if job_id not in scheduler.my_task_list.keys():
                scheduler.my_task_list[job_id] = {
                    "id": job_id,
                    "name": "--",
                    "update_dt": fun.datetime_to_str(datetime.datetime.now()),
                    "next_run_dt": "--",
                    "state": "未知",
                }
            scheduler.my_task_list[job_id]["update_dt"] = fun.datetime_to_str(
                datetime.datetime.now()
            )
            job = scheduler.get_job(event.job_id)
            if job is not None:
                scheduler.my_task_list[job_id]["name"] = job.name
                scheduler.my_task_list[job_id]["next_run_dt"] = fun.datetime_to_str(
                    job.next_run_time
                )
            scheduler.my_task_list[job_id]["state"] = state_map[event.code]
        return

    scheduler.add_listener(run_tasks_listener, EVENT_ALL)
    scheduler.start()

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

    from .alert_tasks import AlertTasks
    from .other_tasks import OtherTasks
    from .xuangu_tasks import XuanguTasks
    _alert_tasks = AlertTasks(scheduler)
    _alert_tasks.run()

    # 注册中枢-free 信号监控任务调度（signal_monitor 独立包，对原代码的唯一接线点）
    from chanlun.signal_monitor.scheduler import register_signal_jobs
    register_signal_jobs(scheduler)

    _xuangu_tasks = XuanguTasks(scheduler)

    from chanlun.recursive_bt.monitor.app_monitor import register_recursive_monitor_jobs
    _recursive_monitors = register_recursive_monitor_jobs(scheduler)

    # _other_tasks = OtherTasks(scheduler)

    __log = fun.get_logger()

    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    app.logger.addFilter(
        lambda record: "/static/" not in record.getMessage().lower()
    )  # 过滤静态资源请求日志

    # 强制 Jinja2 每次请求都从磁盘 re-render template,避免 web 长跑后
    # ``index.html`` 内 ``{{ static_version }}`` 等动态变量被内存缓存。
    # 性能代价微小(主页面 template,只在 / 请求时 render)。
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # 添加登录验证
    # secret_key 解析顺序：环境变量 CHANLUN_FLASK_SECRET_KEY > config.FLASK_SECRET_KEY > 数据目录持久化文件。
    app.secret_key = get_flask_secret_key()

    # CSRF 保护：图表页常长时间打开，禁用过期防止用户操作时报错（session 仍由 secret_key 签名兜底）。
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    from .csrf import csrf
    csrf.init_app(app)

    # 静态资源 cache-bust:Tornado static handler 给所有 /static/* 加
    # ``Cache-Control: max-age=31536000, immutable``,导致 charts.js / bundle.js
    # 等核心前端文件被浏览器**永久缓存**——后端修了字段但前端永远拉不到新版本。
    # 修法:在 Jinja2 模板里给 ``<script src>`` 加 ``?v={{ static_version }}``,
    # static_version = 关键文件 mtime 的 short hash,文件变即 bust。
    @app.context_processor
    def inject_static_version():
        import hashlib
        h = hashlib.md5()
        # 聚合前端 js/css 目录下所有文件 + bundle.js 的 mtime+size：任一前端文件
        # 改动都会让 static_version 变化，模板里带 ?v={{ static_version }} 的资源
        # 随之 cache-bust，用户改前端后普通刷新即可生效，无需手动硬刷新。
        targets = [
            os.path.join(app.static_folder, "datafeeds", "udf", "dist", "bundle.js"),
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
        return {"static_version": h.hexdigest()[:10]}

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login_opt"

    class LoginUser(UserMixin):
        def __init__(self) -> None:
            super().__init__()
            self.id = "cl_pro"

    @login_manager.user_loader
    def load_user(user_id):
        return LoginUser()

    @app.route("/login", methods=["GET", "POST"])
    def login_opt():
        # 未设置登录密码，默认直接进行登录
        if config.LOGIN_PWD == "":
            login_user(
                LoginUser(), remember=True, duration=datetime.timedelta(days=365)
            )
            return redirect("/")

        emsg = ""
        if request.method == "POST":
            password = request.form.get("password") or ""
            # 常量时间比较，并兼容 pbkdf2:/scrypt:/argon2: 形式的哈希配置。
            if verify_login_password(password, config.LOGIN_PWD):
                login_user(
                    LoginUser(), remember=True, duration=datetime.timedelta(days=365)
                )
                return redirect("/")
            else:
                emsg = "密码错误"

        return render_template("login.html", emsg=emsg)

    @app.route("/")
    @login_required
    def index_show():
        """
        首页
        """

        return render_template(
            "index.html",
            market_default_codes=market_default_codes,
            market_frequencys=market_frequencys,
            enable_sse=config.ENABLE_SSE_PUSH,
        )

    # 注册蓝图：TradingView 相关接口
    from .blueprints.tv import tv_bp
    app.register_blueprint(tv_bp)

    # 注册蓝图：自选、提醒、选股、设置、AI、板块概念、杂项、配置项、标的列表
    from .blueprints.zixuan import zixuan_bp
    from .blueprints.alert import alert_bp
    from .blueprints.xuangu import xuangu_bp
    from .blueprints.setting import setting_bp
    from .blueprints.ai import ai_bp
    from .blueprints.bkgn import bkgn_bp
    from .blueprints.other import other_bp
    from .blueprints.options import options_bp
    from .blueprints.symbols import symbols_bp

    app.register_blueprint(zixuan_bp)
    app.register_blueprint(alert_bp)
    app.register_blueprint(xuangu_bp)
    app.register_blueprint(setting_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(bkgn_bp)
    app.register_blueprint(other_bp)
    app.register_blueprint(options_bp)
    app.register_blueprint(symbols_bp)

    # 共享对象存入 app.extensions，供蓝图访问
    app.extensions = getattr(app, "extensions", {})
    app.extensions["scheduler"] = scheduler
    app.extensions["alert_tasks"] = _alert_tasks
    app.extensions["xuangu_tasks"] = _xuangu_tasks
    app.extensions["recursive_monitors"] = _recursive_monitors
    # app.extensions["other_tasks"] = _other_tasks

    # 全局安全响应头：仅添加可静态注入、不依赖业务上下文的几项；
    # CSP 因 dark.html 注入了百度统计脚本（hm.baidu.com）会被阻断，故暂不开启。
    @app.after_request
    def _set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        return resp

    return app
