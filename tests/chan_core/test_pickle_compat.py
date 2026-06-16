# -*- coding: utf-8 -*-
import pickle, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" if (Path(__file__).resolve().parents[2] / "src").exists() else Path(__file__).resolve().parents[2] / "src"))
from chanlun.core.cl import CL
from chanlun.core import cl_interface
from chanlun.core.types import zhongshu, line as line_mod
from chanlun.recursive_bt.engine.engine import CL_CFG

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "SH.600519_d.parquet"

def test_facade_identity():
    # facade 旧路径与新路径是同一个类对象(pickle module-alias 生效的前提)
    assert cl_interface.ZS is zhongshu.ZS
    assert cl_interface.BI is line_mod.BI

def test_pickle_roundtrip_zs_bi():
    df = pd.read_parquet(FIX)
    cd = CL("SH.600519", "d", dict(CL_CFG)); cd.process_klines(df)
    for obj in (cd.get_bis()[0], cd.get_bi_zss()[0] if cd.get_bi_zss() else cd.get_bis()[0]):
        back = pickle.loads(pickle.dumps(obj))
        assert type(back) is type(obj)
