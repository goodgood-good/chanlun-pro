from __future__ import annotations

import re


_ST_NAME_RE = re.compile(r"^(?:\*?ST|S\*ST)(?![A-Z])", re.IGNORECASE)


def is_st_name(name: str) -> bool:
    return bool(_ST_NAME_RE.match(name.strip()))
