"""
消息推送与配置工具：钉钉/飞书消息发送，代理/凭证配置读取（DB 缓存优先）。
"""

import json
import sys
import threading
from typing import Dict, Union

from chanlun import config
from chanlun.persistence.db import db


class _StdoutNoiseFilter:
    """进程级 stdout 包装，按行丢弃第三方库(如 pytdx)漏删的纯数字调试 print。

    pytdx 的 get_instrument_quote_list 解析期货报价时每条 print(pos)(游标位置)。
    多线程下无法用 redirect_stdout 可靠抑制(全局 sys.stdout 会被各线程互相覆盖、
    甚至泄漏成 StringIO)，故在进程级包一层，按完整行判断：整行仅含数字的丢弃，
    其余原样输出。用线程局部缓冲拼行，避免多线程 write 交错串行。项目日志走
    logging(每行含时间/级别等文本)，不会被误吞。
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def write(self, s):
        buf = getattr(self._local, "buf", "") + s
        out = []
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip().isdigit():
                continue  # 丢弃纯数字行(pytdx 漏删的 print(pos))
            out.append(line)
            out.append("\n")
        self._local.buf = buf
        if out:
            self._real.write("".join(out))
        return len(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        # isatty / fileno / encoding 等属性透传给真实 stdout。
        return getattr(self._real, name)


def install_stdout_noise_filter() -> None:
    """进程级安装一次 stdout 噪音过滤(幂等)，在 app 启动早期调用。"""
    if not isinstance(sys.stdout, _StdoutNoiseFilter):
        sys.stdout = _StdoutNoiseFilter(sys.stdout)


def config_get_proxy() -> Dict[str, str]:
    """
    Get HTTP proxy configuration.

    Priority: DB cache key `req_proxy` overrides defaults if present.

    Returns a dict with `host` and `port`.
    """
    db_proxy = db.cache_get("req_proxy")
    if db_proxy is not None and db_proxy.get("host") and db_proxy.get("port"):
        return db_proxy
    return {"host": config.PROXY_HOST, "port": config.PROXY_PORT}


def config_get_feishu_keys(market: str) -> Dict[str, str]:
    """
    Get Feishu app credentials and target user.

    DB cache `fs_keys` overrides defaults if present and complete.
    """
    from chanlun.security import decrypt_str
    db_fs_key = db.cache_get("fs_keys")
    if db_fs_key is not None:
        # fs_app_secret 在 Web 设置页按唯一当前格式加密落库，这里严格解密。
        app_secret_plain = decrypt_str(db_fs_key.get("fs_app_secret"))
        if (
            db_fs_key.get("fs_app_id")
            and app_secret_plain
            and db_fs_key.get("fs_user_id")
        ):
            return {
                "app_id": db_fs_key["fs_app_id"],
                "app_secret": app_secret_plain,
                "user_id": db_fs_key["fs_user_id"],
            }
    keys = config.FEISHU_KEYS.get("default", {}).copy()
    if market in config.FEISHU_KEYS.keys():
        keys = config.FEISHU_KEYS[market].copy()
    keys["user_id"] = config.FEISHU_KEYS["user_id"]
    return keys


def send_fs_msg(market: str, title: str, contents: Union[str, list]) -> bool:
    """
    发送飞书消息（富文本 post）。
    """
    import lark_oapi as lark  # 延迟导入可选依赖
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageResponse,
    )
    fs_key = config_get_feishu_keys(market)
    if (
        fs_key is None
        or fs_key["app_id"] == ""
        or fs_key["app_secret"] == ""
        or fs_key["user_id"] == ""
    ):
        return True  # 未配置时不执行操作
    # 创建client
    client = (
        lark.Client.builder()
        .app_id(fs_key["app_id"])
        .app_secret(fs_key["app_secret"])
        .log_level(lark.LogLevel.WARNING)
        .build()
    )
    # 飞书消息格式参考：https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json
    if isinstance(contents, str):
        msg_content = {
            "zh_cn": {
                "title": title,
                "content": [[{"tag": "text", "text": f"{contents} \n"}]],
            }
        }
    else:
        msg_content = {
            "zh_cn": {
                "title": title,
                "content": [[]],
            }
        }
        for _c in contents:
            if _c.startswith("img_"):  # 支持图片消息
                msg_content["zh_cn"]["content"][0].append(
                    {"tag": "img", "image_key": f"{_c}"}
                )
            else:
                msg_content["zh_cn"]["content"][0].append(
                    {"tag": "text", "text": f"{_c} \n"}
                )

    msg_content = json.dumps(msg_content, ensure_ascii=False)
    # 构造请求对象
    request: CreateMessageRequest = (
        CreateMessageRequest.builder()
        .receive_id_type("user_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(fs_key["user_id"])
            .msg_type("post")
            .content(msg_content)
            .build()
        )
        .build()
    )

    # 发起请求
    try:
        response: CreateMessageResponse = client.im.v1.message.create(request)
    except Exception as exc:
        # lark sdk 内部已经做了重试与超时控制，这里再兜一层，避免飞书侧异常打断主流程。
        lark.logger.error(f"client.im.v1.message.create raised: {exc}")
        return False
    # 处理失败返回：必须返回 False，让调用方知道发送未成功，可补救（如重试 / 落库）。
    if not response.success():
        lark.logger.error(
            f"client.im.v1.message.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
        )
        return False
    return True
