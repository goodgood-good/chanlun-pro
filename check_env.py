"""
检查当前环境是否OK
"""

import os
import importlib
import socket
import sys


def check_env(
    *,
    version_info=None,
    importer=None,
    connection_factory=None,
    output=print,
):
    version_info = version_info or sys.version_info
    importer = importer or importlib.import_module
    connection_factory = connection_factory or socket.create_connection

    # 检查 Python 版本
    version = f"{version_info[0]}.{version_info[1]}"
    output(f"当前Python版本：{version}")
    allow_version = ["3.10", "3.11", "3.12", "3.13"]
    if version not in allow_version:
        output(f"当前Python不在支持的列表中：{allow_version}")
        return False

    try:
        pymysql = importer("pymysql")
        redis = importer("redis")
    except Exception as exc:
        output(f"依赖导入失败：{exc}")
        return False

    # 检查 环境变量是否设置正确
    try:
        importer("chanlun.core.cl_interface")
    except Exception:
        output("无法导入 chanlun 模块，环境变量未设置或设置错误")
        output(f"当前的环境变量如下：{sys.path}")
        output(
            f"需要将 PYTHONPATH 环境变量设置为 {os.path.join(os.getcwd(), 'src')} 目录"
        )
        return False

    # 检查配置文件
    try:
        config = importer("chanlun.config")
    except Exception:
        output(
            "无法导入 config , 请在 src/chanlun 目录， 复制 config.py.demo 文件粘贴为 config.py"
        )
        return False

    # 检查代理是否设置
    if getattr(config, "PROXY_HOST", "") != "":
        proxy_connection = None
        try:
            proxy_connection = connection_factory(
                (config.PROXY_HOST, config.PROXY_PORT), timeout=3.0
            )
        except Exception:
            output("当前设置的 VPN 代理不可用，如不使用数字货币行情，可忽略")
        finally:
            if proxy_connection is not None:
                try:
                    proxy_connection.close()
                except Exception:
                    pass

    # 检查 Redis
    try:
        if getattr(config, "REDIS_HOST", "") != "":
            R = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            R.get("check")
    except Exception:
        output("Redis 连接失败，请检查是否有安装并启动 Redis 服务端，并且配置正确")
        output("Redis 不是必须的，不使用可以忽略")

    # 检查 MySQL
    db_connection = None
    try:
        db_type = config.DB_TYPE
        if db_type == "mysql":
            db_connection = pymysql.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PWD,
                database=config.DB_DATABASE,
                connect_timeout=5,
            )
        elif db_type != "sqlite":
            output(f"不支持的数据库类型：{db_type}")
            return False
    except Exception:
        output(
            "MySQL 连接失败，请检查是否安装并运行 MySQL，并且检查配置的 ip、端口、用户名、密码、数据库 是否正确"
        )
        return False
    finally:
        if db_connection is not None:
            try:
                db_connection.close()
            except Exception:
                pass

    output("环境OK")
    return True


def main():
    return 0 if check_env() else 1


if __name__ == "__main__":
    raise SystemExit(main())
