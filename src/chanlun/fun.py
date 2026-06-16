import datetime
import logging
import time
from functools import wraps

import pytz
from tzlocal import get_localzone

from chanlun.config import get_data_path

# 统一时区
__tz = pytz.timezone(str(get_localzone()))


def get_logger(filename=None, level=logging.INFO) -> logging.Logger:
    """获取日志记录器；filename 非空时同时写入文件。重复调用不会重复添加 handler。"""
    log_path = get_data_path() / "logs"
    if log_path.is_dir() is False:
        log_path.mkdir(parents=True)
    logger = logging.getLogger(f"{filename}")
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s : %(message)s")
    stream_handler = logging.StreamHandler()

    # 防止重复 addHandler（getLogger 返回同一个实例，多次调用会叠加 handler）
    stream_exists = False
    file_exists = False
    for _h in logger.handlers:
        if isinstance(_h, logging.StreamHandler):
            stream_exists = True
        if isinstance(_h, logging.FileHandler):
            file_exists = True
    if stream_exists is False:
        logger.addHandler(stream_handler)

    if filename and file_exists is False:
        file_handler = logging.FileHandler(
            filename=str(log_path / filename), encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


def singleton(cls):
    """类装饰器：保证每个类只实例化一次（进程级单例）。"""
    instance = {}

    @wraps(cls)
    def wrapper(*args, **kwargs):
        if cls not in instance:
            instance[cls] = cls(*args, **kwargs)
        return instance[cls]

    return wrapper


def timeint_to_str(_t, _format="%Y-%m-%d %H:%M:%S", tz=__tz):
    """时间戳（int）转格式化字符串。"""
    time_arr = time.localtime(int(_t))
    return time.strftime(_format, time_arr)


def timeint_to_datetime(_t, _format="%Y-%m-%d %H:%M:%S", tz=__tz):
    """时间戳（int）转 datetime 对象。"""
    time_arr = time.localtime(int(_t))
    return str_to_datetime(time.strftime(_format, time_arr), _format, tz=tz)


def str_to_timeint(_t, _format="%Y-%m-%d %H:%M:%S"):
    """格式化字符串转 Unix 时间戳（int）。"""
    return int(time.mktime(time.strptime(_t, _format)))


def str_to_datetime(_s, _format="%Y-%m-%d %H:%M:%S", tz=__tz):
    """格式化字符串转 datetime 对象（带时区）。"""
    return datetime.datetime.strptime(_s, _format).astimezone(tz)


def datetime_to_str(_dt: datetime.datetime, _format="%Y-%m-%d %H:%M:%S"):
    """datetime 对象转格式化字符串。"""
    return _dt.strftime(_format)


def datetime_to_int(_dt: datetime.datetime):
    """datetime 对象转 Unix 时间戳（int）。"""
    return int(_dt.timestamp())


def str_add_seconds_to_str(_s, _seconds, _format="%Y-%m-%d %H:%M:%S"):
    """
    字符串日期时间，加上秒数，在返回新的字符串日期
    """
    _time = int(time.mktime(time.strptime(_s, _format)))
    _time += _seconds
    _time = time.localtime(int(_time))
    return time.strftime(_format, _time)


def now_dt():
    """
    返回当前日期字符串
    """
    return datetime_to_str(datetime.datetime.now(tz=__tz))


def reverse_decimal_to_power_of_ten(decimal_number):
    """将小数转换为其小数位数对应的 10 的幂次方（如 0.001 → 1000）。"""
    if decimal_number <= 0 or decimal_number >= 1:
        return 1000
    decimal_str = str(decimal_number)
    if "." in decimal_str:
        num_zeros = len(decimal_str) - decimal_str.index(".") - 1
    if "e-" in decimal_str:
        # 科学计数法（如 1e-8）直接读指数位
        num_zeros = int(decimal_str[decimal_str.index("e-") + 2 :])
    return 10**num_zeros


if __name__ == "__main__":
    for i in range(1, 10):
        dn = 1 / (10**i)
        print(dn, reverse_decimal_to_power_of_ten(dn))
