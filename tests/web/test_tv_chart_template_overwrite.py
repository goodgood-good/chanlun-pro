"""tv_chart_save: study_template(chart_type="template")按名覆盖, 不得堆积重复行(新R5-H3-3)。

/tv/<v>/study_templates POST → db.tv_chart_save("template", ...)(blueprints/tv.py:1398),
但覆盖分支 db.py:1092 只认 ["drawing","study_template"] 不含 "template" → 同名模板每次重存
都 insert 新行:①tv_chart_list 返回重复名撑爆 TradingView 模板下拉;②tv_chart_get_by_name
用 .first() 无 order_by 取最旧行 → 用户改完模板重存后加载到陈旧原版, 编辑静默丢失。
(注:覆盖列表里的 "study_template" 是死枝——无调用方传它, 端点全程用 "template"。)
可达=默认 web 9900 TradingView"保存指标模板"覆盖同名。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.persistence.db import DB
from chanlun.db_models.tv_charts import TableByTVCharts


def _mk_db():
    # 绕过 @fun.singleton(DB 实为 wrapper 函数, 真类在 __wrapped__); StaticPool 保证
    # in-memory 单连接复用, 表跨 Session 存活
    real_cls = DB.__wrapped__
    d = object.__new__(real_cls)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByTVCharts.__table__.create(engine)
    d.engine = engine
    d.Session = sessionmaker(bind=engine)
    return d


def test_study_template_same_name_overwrites_not_duplicate():
    d = _mk_db()
    d.tv_chart_save("template", "c1", "u1", "myTpl", "v1", "", "")
    d.tv_chart_save("template", "c1", "u1", "myTpl", "v2", "", "")
    rows = d.tv_chart_list("template", "c1", "u1")
    assert len(rows) == 1  # 修复前=2(堆积重复行)
    got = d.tv_chart_get_by_name("template", "myTpl", "c1", "u1")
    assert got.content == "v2"  # 修复前 .first() 取最旧 → "v1"(编辑丢失)


def test_study_template_different_name_still_inserts():
    d = _mk_db()
    d.tv_chart_save("template", "c1", "u1", "tplA", "a", "", "")
    d.tv_chart_save("template", "c1", "u1", "tplB", "b", "", "")
    assert len(d.tv_chart_list("template", "c1", "u1")) == 2


def test_chart_type_layouts_still_allow_multiple():
    # 防呆: 修复只让 template 覆盖, "chart"(TV 允许多份布局)不得被误折叠
    d = _mk_db()
    d.tv_chart_save("chart", "c1", "u1", "layout", "a", "SH.600000", "D")
    d.tv_chart_save("chart", "c1", "u1", "layout", "b", "SH.600000", "D")
    assert len(d.tv_chart_list("chart", "c1", "u1")) == 2
