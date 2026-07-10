"""marks_add_by_price 去重删错表(新R6-P1): 预删打在 TableByTVMarks(时间轴表)却把新标记
插入 TableByTVMarksPrice(价格主图表)→ ①价格标记去重形同虚设堆重复行 ②误删同键的合法
时间轴标记(跨表数据丢失)。live_monitor(monitor.py:352)对同一末根K线多信号/跨tick重复调
该方法 → /tv/marks 价格轴渲染重复标记。已修 marks_del_by_price(db.py:1042 注释)的漏网孪生。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.persistence.db import DB
from chanlun.db_models.tv_marks import TableByTVMarks
from chanlun.db_models.tv_marks_price import TableByTVMarksPrice


def _mk_db():
    real_cls = DB.__wrapped__
    d = object.__new__(real_cls)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TableByTVMarks.__table__.create(engine)
    TableByTVMarksPrice.__table__.create(engine)
    d.engine = engine
    d.Session = sessionmaker(bind=engine)
    return d


def _add(d, text="买", mark_time=1700000000, mark_label="A"):
    d.marks_add_by_price(
        "a", "SH.600000", "浦发", "d", mark_time, mark_label, text, "#fff", "#f00"
    )


def test_price_mark_same_key_dedup_keeps_one():
    d = _mk_db()
    _add(d, text="v1")
    _add(d, text="v2")
    rows = d.marks_query_by_price("a", "SH.600000")
    assert len(rows) == 1  # 修复前=2(去重删打在 TableByTVMarks 对价格表 no-op)
    assert rows[0].mark_text == "v2"


def test_price_mark_add_does_not_delete_timeline_mark():
    # 跨表误删: 预插一条同键 TableByTVMarks(时间轴), marks_add_by_price 不得删它
    d = _mk_db()
    with d.Session() as s:
        s.add(
            TableByTVMarks(
                market="a",
                stock_code="SH.600000",
                stock_name="浦发",
                frequency="d",
                mark_time=1700000000,
                mark_label="A",
                mark_shape="arrow_up",
                mark_color="#0f0",
            )
        )
        s.commit()
    _add(d)  # 同键价格标记
    with d.Session() as s:
        remain = (
            s.query(TableByTVMarks).filter(TableByTVMarks.market == "a").count()
        )
    assert remain == 1  # 修复前=0(误删时间轴标记, 跨表数据丢失)
