# -*- coding: utf-8 -*-
"""C9: legacy monitor.monitoring_code 中同一笔的背驰与买卖点共享判重键
(market,code,frequency,'bi',line_dt) → 循环里后到的买卖点命中背驰刚落的记录且
bi_is_done/bi_is_td 相同 → 判重条件全 False 被静默吞没, 用户勾选的买卖点警报永久漏发。
修复: 把信号 jh["type"] 并入去重键(dedup_type), 使背驰与买卖点各自独立记录/发送。
"""
import datetime as _dt

from chanlun import monitor as monitor_mod


class _K:
    def __init__(self, date):
        self.date = date


class _Start:
    def __init__(self, date):
        self.k = _K(date)


class _End:
    def ld(self):
        return {"macd": 1.0}


class _Bi:
    def __init__(self, date):
        self.type = "up"
        self.start = _Start(date)
        self.end = _End()

    def is_done(self):
        return True

    def bc_exists(self, types, op):
        return "pz" in types      # 盘整背驰存在

    def mmd_exists(self, types, op):
        return "3buy" in types    # 三买点存在


class _CD:
    def __init__(self, bi):
        self._bi = bi
        self._kd = _K(_dt.datetime(2026, 7, 9, 10, 30))

    def get_bis(self):
        return [self._bi]

    def get_frequency(self):
        return "30m"

    def get_xds(self):
        return []

    def get_src_klines(self):
        return [self._kd]


class _RecObj:
    def __init__(self, d):
        self.bi_is_done = d["is_done"]
        self.bi_is_td = d["is_td"]


class _FakeDB:
    def __init__(self):
        self.records = []

    def alert_record_query_by_code(self, market, code, frequency, line_type, line_dt):
        hits = [
            r for r in self.records
            if (r["market"], r["code"], r["frequency"], r["line_type"], r["line_dt"])
            == (market, code, frequency, line_type, line_dt)
        ]
        return _RecObj(hits[-1]) if hits else None

    def alert_record_save(self, market, task_name, code, name, frequency, msg,
                          is_done, is_td, line_type, line_dt):
        self.records.append({
            "market": market, "code": code, "frequency": frequency,
            "line_type": line_type, "line_dt": line_dt,
            "is_done": is_done, "is_td": is_td, "msg": msg,
        })
        return True

    def marks_add_by_price(self, *a, **k):
        return True


class _FakeEx:
    def klines(self, code, f):
        return [1]  # 非空即可, web_batch_get_cl_datas 被 stub 忽略

    def stock_owner_plate(self, code):
        return {"HY": [], "GN": []}


def test_beichi_and_mmd_on_same_bi_both_alert(monkeypatch):
    bi = _Bi(_dt.datetime(2026, 7, 9, 9, 30))
    cd = _CD(bi)
    fake_db = _FakeDB()

    monkeypatch.setattr(monitor_mod, "get_exchange", lambda _m: _FakeEx())
    monkeypatch.setattr(monitor_mod, "Market", lambda m: m)
    monkeypatch.setattr(monitor_mod, "web_batch_get_cl_datas",
                        lambda *a, **k: [cd])
    monkeypatch.setattr(monitor_mod, "bi_td", lambda b, c: False)
    monkeypatch.setattr(monitor_mod, "db", fake_db)

    check_cl = {
        "bi_types": ["up"], "bi_beichi": ["pz"], "bi_mmd": ["3buy"],
        "xd_types": [], "xd_beichi": [], "xd_mmd": [],
    }
    monitor_mod.monitoring_code(
        task_name="t", market="us", code="AAPL", name="x",
        frequencys=["30m"], check_cl_types=check_cl, is_send_msg=False,
    )
    # 修复前: 两信号共享 (…,'bi',line_dt) → 只落 1 条(买卖点被吞)。
    # 修复后: dedup_type 区分 → 背驰 + 买卖点各落 1 条。
    # 修复前: 两信号共享 (…,'bi',line_dt) → 只落 1 条(买卖点被吞)。
    # 修复后: dedup_type 区分 → 背驰 + 买卖点各落 1 条, 去重键互异。
    assert len(fake_db.records) == 2
    line_types = [r["line_type"] for r in fake_db.records]
    assert len(set(line_types)) == 2
    assert all(lt.startswith("bi|") for lt in line_types)