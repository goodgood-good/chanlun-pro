from __future__ import annotations

import base64
from pathlib import Path
import re
import struct


ROOT = Path(__file__).resolve().parents[2]
XM_SELECT = ROOT / "web/chanlun_chart/cl_app/static/xm-select.js"
EMBEDDED_WOFF2 = re.compile(
    rb"data:application/x-font-woff2;charset=utf-8;base64,([A-Za-z0-9+/=]+)"
)


def test_xm_select_embedded_icon_font_is_complete_woff2() -> None:
    source = XM_SELECT.read_bytes()
    assert b"@Version: 1.2.4" in source
    matches = EMBEDDED_WOFF2.findall(source)
    assert len(matches) == 1

    font = base64.b64decode(matches[0], validate=True)
    assert len(font) >= 48
    assert font[:4] == b"wOF2"
    assert struct.unpack(">I", font[8:12])[0] == len(font)
