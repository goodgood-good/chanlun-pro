"""
检查当前环境是否OK
"""

import os
import sys
import telnetlib

import pymysql
import redis


def check_env():
    # 检查 Python 版本
    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    print(f"当前Python版本：{version}")
    allow_version = ["3.10", "3.11", "3.12", "3.13"]
    if version not in allow_version:
        print(f"当前Python不在支持的列表中：{allow_version}")
        return

    # 检查 环境变量是否设置正确
    try:
        from chanlun.core import cl_interface  # noqa: F401
    except Exception:
        print("无法导入 chanlun 模块，环境变量未设置或设置错误")
        print(f"当前的环境变量如下：{sys.path}")
        print(f"需要当 PYTHONPATH 环境变量设置为 {os.getcwd()}\src 目录")
        return

    # 检查 环境变量是否设置正确
    try:
        from chanlun import config
    except Exception:
        print(
            "无法导入 config , 请在 src/chanlun 目录， 复制 config.py.demo 文件粘贴为 config.py"
        )
        return

    # 检查代理是否设置
    if config.PROXY_HOST != "":
        try:
            telnetlib.Telnet(config.PROXY_HOST, config.PROXY_PORT)
        except Exception:
            print("当前设置的 VPN 代理不可用，如不使用数字货币行情，可忽略")

    # 检查 Redis
    try:
        if config.REDIS_HOST != "":
            R = redis.Redis(
                host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=True
            )
            R.get("check")
    except Exception:
        print("Redis 连接失败，请检查是否有安装并启动 Redis 服务端，并且配置正确")
        print("Redis 不是必须的，不使用可以忽略")
    # 检查 MySQL
    try:
        if config.DB_TYPE == "mysql":
            pymysql.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PWD,
                database=config.DB_DATABASE,
            )
    except Exception:
        print(
            "MySQL 连接失败，请检查是否安装并运行 MySQL，并且检查配置的 ip、端口、用户名、密码、数据库 是否正确"
        )

    print("环境OK")


if __name__ == "__main__":
    check_env()
