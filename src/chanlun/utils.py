"""
消息推送与配置工具：钉钉/飞书消息发送，代理/凭证配置读取（DB 缓存优先）。
"""

import base64
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.parse
from typing import Dict, Optional, Union

import requests

from chanlun import config
from chanlun.persistence.db import db
from chanlun.tools.log_util import LogUtil


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


def config_get_dingding_keys(market: str) -> Optional[Dict[str, str]]:
    """
    Return DingTalk robot keys for the given market.

    DB cache `dd_keys` overrides defaults when present and complete.
    Market mapping:
    - a -> `config.DINGDING_KEY_A`
    - hk -> `config.DINGDING_KEY_HK`
    - us -> `config.DINGDING_KEY_US`
    - futures -> `config.DINGDING_KEY_FUTURES`
    - currency -> `config.DINGDING_KEY_CURRENCY`
    """
    db_dd_key = db.cache_get("dd_keys")
    if db_dd_key is not None and db_dd_key.get("token") and db_dd_key.get("secret"):
        return db_dd_key
    if market == "a":
        return config.DINGDING_KEY_A
    if market == "hk":  # fixed wrong duplicate 'a' condition
        return config.DINGDING_KEY_HK
    if market == "us":
        return config.DINGDING_KEY_US
    if market == "futures":
        return config.DINGDING_KEY_FUTURES
    if market == "currency":
        return config.DINGDING_KEY_CURRENCY

    return None


def config_get_feishu_keys(market: str) -> Dict[str, str]:
    """
    Get Feishu app credentials and target user.

    DB cache `fs_keys` overrides defaults if present and complete.
    """
    from chanlun.security import decrypt_str
    db_fs_key = db.cache_get("fs_keys")
    if db_fs_key is not None:
        # fs_app_secret 在 web 设置页加密落库，这里解密；历史明文记录会被 decrypt_str 原样返回。
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


# 旧版的API接口已经下架，不能新增了，后续使用 飞书的 消息接口
def send_dd_msg(market: str, msg: Union[str, Dict[str, str]]) -> bool:
    """
    发送钉钉消息（自定义机器人）。

    - `msg` 为字符串时发送文本消息。
    - `msg` 为字典时发送 markdown 消息，形如 `{"title": "标题", "text": "markdown内容"}`。
    """
    dd_info = config_get_dingding_keys(market)
    if dd_info is None or dd_info["token"] == "" or dd_info["secret"] == "":
        return True  # no-op when not configured

    url = "https://oapi.dingtalk.com/robot/send?access_token=%s&timestamp=%s&sign=%s"

    def sign():
        timestamp = str(round(time.time() * 1000))
        secret = dd_info["secret"]
        secret_enc = secret.encode("utf-8")
        string_to_sign = "{}\n{}".format(timestamp, secret)
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(
            secret_enc, string_to_sign_enc, digestmod=hashlib.sha256
        ).digest()
        _sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, _sign

    t, s = sign()
    url = url % (dd_info["token"], t, s)
    # 加 (connect, read) 双段超时，避免外网网络抖动时整条任务被卡住。
    request_timeout = (5, 10)
    try:
        if isinstance(msg, str):
            res = requests.post(
                url,
                json={
                    "msgtype": "text",
                    "text": {"content": msg},
                },
                timeout=request_timeout,
            )
        else:
            res = requests.post(
                url,
                json={"msgtype": "markdown", "markdown": msg},
                timeout=request_timeout,
            )
    except requests.RequestException as exc:
        LogUtil.error(f"send_dd_msg request failed: {exc}")
        return False

    # 钉钉成功返回 errcode == 0，否则视为失败但不抛出，避免上层任务被打断。
    try:
        body = res.json()
        if body.get("errcode", 0) != 0:
            LogUtil.error(
                f"send_dd_msg api error, errcode={body.get('errcode')}, errmsg={body.get('errmsg')}"
            )
            return False
    except ValueError:
        LogUtil.error(
            f"send_dd_msg got non-json response, status={res.status_code}, text={res.text[:200]}"
        )
        return False

    return True


def send_fs_msg(market: str, title: str, contents: Union[str, list]) -> bool:
    """
    发送飞书消息（富文本 post）。
    """
    import lark_oapi as lark  # lazy import: optional extra
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
        return True  # no-op when not configured
    # 创建client
    client = (
        lark.Client.builder()
        .app_id(fs_key["app_id"])
        .app_secret(fs_key["app_secret"])
        .log_level(lark.LogLevel.WARNING)
        .build()
    )
    # https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json
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


if __name__ == "__main__":
    send_fs_msg(
        "us", "这里是选股的测试消息", ["运行完成", "选出300只股票", "用时1000小时"]
    )