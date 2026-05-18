import logging
import os
import threading
from logging.handlers import RotatingFileHandler


def _resolve_level(env_name: str, default: int) -> int:
    """从环境变量读取日志级别，支持 'DEBUG' / 'INFO' / 'WARNING' / 'ERROR' / 'CRITICAL'。

    取不到或值非法时回退到 default。
    """
    raw = os.environ.get(env_name)
    if not raw:
        return default
    level = logging.getLevelName(raw.strip().upper())
    if isinstance(level, int):
        return level
    return default



class LogUtil:
    """
    一个简单易用的日志工具类。
    - 日志会同时输出到控制台和文件。
    - 日志文件会自动分割（当大小达到5MB时）。
    - 通过调用静态方法 LogUtil.info("...") 来使用。

    级别可通过环境变量动态控制（无需改代码）：
    - LOG_LEVEL：        根 logger 级别，默认 INFO；排查时 export LOG_LEVEL=DEBUG 全开
    - LOG_CONSOLE_LEVEL：控制台级别，默认 INFO（关键节点日志可见；高频日志已被降级为 DEBUG，不会刷屏）
    - LOG_FILE_LEVEL：   文件级别，  默认 INFO；排查时 export LOG_FILE_LEVEL=DEBUG 全开
    示例：
        export LOG_LEVEL=DEBUG           # 排查时全开（根 logger + 文件 + 控制台均开 DEBUG）
        export LOG_CONSOLE_LEVEL=DEBUG   # 仅控制台全开（连高频 DEBUG 都展示）
        export LOG_CONSOLE_LEVEL=WARNING # 极简模式，只看告警和错误
    """
    _logger = None
    # 首次初始化加锁：避免多线程并发首调时各自挂一遍 handler 致日志重复。
    _init_lock = threading.Lock()

    # 日志格式中 %(filename)s:%(lineno)d 显示的是 logging 调用点的位置。
    # 业务代码通过 LogUtil.info(...) -> logger.info(...) 调用，默认会打印成
    # log_util.py:<本文件中 logger.xxx 的行>，看不到真实调用方。
    # 调用栈：业务代码 -> LogUtil.info -> logger.info  ←  stacklevel=3 指回业务代码
    _STACKLEVEL = 3

    @staticmethod
    def get_logger():
        """获取全局单例 logger，首次调用时完成初始化（控制台 + 滚动文件双 handler）。

        首次初始化在 ``_init_lock`` 内做 double-checked locking，确保并发首调
        时只构建一次、不会重复挂 handler。
        """
        if LogUtil._logger is not None:
            return LogUtil._logger
        with LogUtil._init_lock:
            if LogUtil._logger is not None:
                return LogUtil._logger
            LogUtil._logger = LogUtil._build_logger()
            return LogUtil._logger

    @staticmethod
    def _build_logger():
        """构建全局 logger（仅由 get_logger 在 _init_lock 内调用一次）。"""
        # 固定名称保证全项目取到同一实例
        logger = logging.getLogger("Logger")
        # 根 logger 默认 INFO：DEBUG 记录在 isEnabledFor 处 short-circuit，
        # hot loop 里的 _log.debug 开销归零；排查时 export LOG_LEVEL=DEBUG 全开。
        logger.setLevel(_resolve_level("LOG_LEVEL", logging.INFO))

        # handlers 非空说明已初始化（多次 import 防重复挂载）
        if logger.handlers:
            return logger

        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )

        # 控制台默认 INFO：高频 DEBUG 日志（K线缓存命中、线段构造等）不会刷屏；
        # 需要全量调试时：export LOG_CONSOLE_LEVEL=DEBUG
        console_level = _resolve_level("LOG_CONSOLE_LEVEL", logging.INFO)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)

        log_dir = 'logs'
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        log_path = os.path.join(log_dir, 'app.log')

        file_level = _resolve_level("LOG_FILE_LEVEL", logging.INFO)
        file_handler = RotatingFileHandler(
            filename=log_path,
            maxBytes=5 * 1024 * 1024,  # 单文件上限 5 MB，超出后自动滚动
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

        return logger

    @staticmethod
    def debug(message, *args, **kwargs):
        # message 支持传入「无参可调用对象」以延迟构建：仅当 DEBUG 真正开启
        # 时才求值。logger.debug 内部的 isEnabledFor 短路只能省掉格式化与
        # handler 派发，省不掉调用点已经 eager 构建好的 f-string —— 故热路径
        # （如线段计算）用 LogUtil.debug(lambda: f"...") 把 f-string 的构建
        # 也一并延迟（实测线段计算中此项一度占 ~41% 耗时）。
        logger = LogUtil.get_logger()
        if callable(message):
            if not logger.isEnabledFor(logging.DEBUG):
                return
            message = message()
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        logger.debug(message, *args, **kwargs)

    @staticmethod
    def info(message, *args, **kwargs):
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        LogUtil.get_logger().info(message, *args, **kwargs)

    @staticmethod
    def warning(message, *args, **kwargs):
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        LogUtil.get_logger().warning(message, *args, **kwargs)

    @staticmethod
    def error(message, *args, **kwargs):
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        LogUtil.get_logger().error(message, *args, **kwargs)

    @staticmethod
    def exception(message, *args, **kwargs):
        # 仅应在 except 块中调用：ERROR 级别，自动附带当前异常堆栈（exc_info=True）。
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        kwargs.setdefault("exc_info", True)
        LogUtil.get_logger().exception(message, *args, **kwargs)

    @staticmethod
    def critical(message, *args, **kwargs):
        kwargs.setdefault("stacklevel", LogUtil._STACKLEVEL)
        LogUtil.get_logger().critical(message, *args, **kwargs)
