# -*- coding: utf-8 -*-
"""R16-C3/C4: config._load_dotenv 的 .env 编码脆弱。

C3(HIGH): read_text(encoding='utf-8') 对非 UTF-8 .env(Windows 记事本 ANSI/GBK 存中文)
抛 UnicodeDecodeError, except OSError 接不住 → import chanlun.config 崩 → web/live_monitor/
select 全部入口(chanlun.config 在导入图最底层)一起炸。修复=utf-8-sig 读 + UnicodeDecodeError
时 gbk 兜底。

C4(MED): .env 带 UTF-8 BOM(记事本"UTF-8"常见默认)时首行 KEY 被解码成 '﻿KEY',
os.environ 写进被污染的变量名 → 调用方按真名读取不到, 静默回落默认值(如登录密码永不生效)。
修复=utf-8-sig 自动剥 BOM。
"""
import os

import chanlun.config as config


def test_load_dotenv_utf8_bom_first_key_not_polluted(tmp_path, monkeypatch):
    """C4: 带 BOM 的 .env 首行 KEY 不得被污染成 \\ufeffKEY。"""
    env = tmp_path / ".env"
    env.write_bytes("﻿MYTEST_BOMKEY=secret123\n".encode("utf-8"))  # utf-8 编码含 BOM 前缀
    monkeypatch.delenv("MYTEST_BOMKEY", raising=False)
    monkeypatch.delenv("﻿MYTEST_BOMKEY", raising=False)
    config._load_dotenv(env)
    # 修复前: 键名是 '﻿MYTEST_BOMKEY', 真名读不到
    assert os.environ.get("MYTEST_BOMKEY") == "secret123"


def test_load_dotenv_non_utf8_does_not_crash(tmp_path, monkeypatch):
    """C3: 非 UTF-8(GBK 中文注释)不得抛 UnicodeDecodeError 炸启动。"""
    env = tmp_path / ".env"
    env.write_bytes("# 中文注释含非UTF8字节\nMYTEST_GBKKEY=val\n".encode("gbk"))
    monkeypatch.delenv("MYTEST_GBKKEY", raising=False)
    config._load_dotenv(env)  # 修复前: UnicodeDecodeError
    assert os.environ.get("MYTEST_GBKKEY") == "val"


def test_load_dotenv_plain_utf8_unaffected(tmp_path, monkeypatch):
    """回归: 普通无 BOM UTF-8 .env 行为不变。"""
    env = tmp_path / ".env"
    env.write_text("MYTEST_PLAIN=hello\n", encoding="utf-8")
    monkeypatch.delenv("MYTEST_PLAIN", raising=False)
    config._load_dotenv(env)
    assert os.environ.get("MYTEST_PLAIN") == "hello"