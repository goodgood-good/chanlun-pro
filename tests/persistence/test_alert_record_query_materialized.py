"""R9-db896: alert_record_query 必须物化返回 list 而非惰性 Query。

db.py:887-896 是全类唯一在 `with self.Session() as session:` 块内 return 惰性
Query(.limit(100) 未 .all())的方法; session 随 with 退出即 close, 返回的 Query
在 web alert.py:263 列表推导迭代时才触发 SQL——在已关闭 session 上 autobegin 借
连接不归还, 绕过本类其余方法的 try/rollback 纪律。生产 DB_TYPE=mysql 下连接失效
(gone-away)即在 blueprint 内裸抛 500; 类型注解 List[TableByAlertRecord] 也与惰性
Query 不符。修复=with 块内 .all() 物化。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

from chanlun.persistence.db import db  # noqa: E402


def test_alert_record_query_returns_materialized_list():
    # 空表(不存在的 market)即可判别惰性 Query vs 物化 list, 无需造数据
    r = db.alert_record_query("__nonexist_market_probe__")
    assert isinstance(r, list), (
        "alert_record_query 应在 with session 块内 .all() 物化为 list, "
        f"实际返回 {type(r).__module__}.{type(r).__name__}"
        "(惰性 Query 逃逸出已关闭 session)"
    )


def test_alert_record_query_task_name_filter_still_materialized():
    # 带 task_name 过滤分支同样必须物化
    r = db.alert_record_query("__nonexist_market_probe__", task_name="__nope__")
    assert isinstance(r, list)