# -*- coding: utf-8 -*-
"""HTTP 探针：模拟前端拉 /tv/history，检查 recursive_levels 等字段实际内容。"""
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import requests
from chanlun import config as cl_config

BASE = "http://127.0.0.1:9900"
s = requests.Session()
r = s.post(BASE + "/login", data={"password": cl_config.LOGIN_PWD}, timeout=30)
print("login:", r.status_code)

now = int(time.time())
for res, days in [("1", 30), ("5", 90), ("30", 365)]:
    r = s.get(BASE + "/tv/history", params={
        "symbol": "a:SH.000001", "resolution": res,
        "from": now - days * 86400, "to": now, "firstDataRequest": "true",
    }, timeout=900)
    d = r.json()
    keys = sorted(d.keys())
    bars = len(d.get("t") or [])
    rl = d.get("recursive_levels")
    print(f"== res={res} status={d.get('s')} bars={bars} keys={len(keys)} ==")
    print("  has recursive_levels:", rl is not None, type(rl).__name__)
    if rl:
        for lv in rl:
            print(f"   L{lv.get('level')}: zss={len(lv.get('zss') or [])} "
                  f"zslxs={len(lv.get('zslxs') or [])} mmds={len(lv.get('mmds') or [])} "
                  f"bcs={len(lv.get('bcs') or [])}")
    for k in ("bis", "xds", "bi_zss", "xd_zss", "xd_mmds", "xd_bcs", "bi_mmds"):
        v = d.get(k)
        print(f"  {k}: {len(v) if isinstance(v, list) else v}")
