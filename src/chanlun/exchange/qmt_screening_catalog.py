"""Stock-only QMT discovery; current membership never comes from quote ticks."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

SCHEMA = 'qmt-screening-stock-catalog-v1'
MAX_AGE_SECONDS = 3600
CN = ZoneInfo('Asia/Shanghai')
CODE = re.compile(r'(?:SH|SZ|BJ)\.\d{6}')
NATIVE_CODE = re.compile(r'\d{6}\.(?:SH|SZ|BJ)')


def _read(path, now):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('schema') != SCHEMA:
            return None
        rows, checked = value['stocks'], value['updated_at']
        if (not isinstance(rows, list) or not rows or type(value.get('count')) is not int
                or value['count'] != len(rows) or type(checked) not in (int, float)
                or not math.isfinite(checked) or not 0 <= now-checked <= MAX_AGE_SECONDS
                or datetime.fromtimestamp(checked, CN).date() != datetime.fromtimestamp(now, CN).date()):
            return None
        found = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('code'), str) or not CODE.fullmatch(row['code'])
                    or row['code'] in found or not isinstance(row.get('name'), str) or not row['name'].strip()
                    or row.get('type') not in {'stock_cn', 'etf_cn', 'index_cn'}):
                return None
            found[row['code']] = {key: row[key] for key in ('code', 'name', 'type')}
        return found, checked
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


def _save(path, rows, checked):
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{uuid4().hex}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({'schema': SCHEMA, 'stocks': rows, 'count': len(rows),
                                         'updated_at': checked}, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass  # Metadata caching cannot invalidate a freshly read directory.
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_stocks(native, *, root, now=None):
    """Caller holds the native lock and explicit full-market authorization.

    The live sector supplies membership. Only names/types may reuse a same-day,
    at-most-one-hour full catalog. New members always need fresh type/details.
    """
    started = time.monotonic()
    now = time.time() if now is None else now
    root = Path(root)
    cache_path = root/'cache/qmt_screening_catalog.json'
    metadata, checked = _read(cache_path, now) or ({}, now)
    listed = native.get_stock_list_in_sector('沪深京A股')
    if (not isinstance(listed, list) or not listed
            or any(not isinstance(code, str) or not NATIVE_CODE.fullmatch(code) for code in listed)
            or {code[-2:] for code in listed} != {'SH', 'SZ', 'BJ'}):
        raise ValueError('QMT 沪深京 A 股目录不完整，未使用不完整名单开始全市场选股')
    stocks, reused, inspected = [], 0, 0
    for native_code in dict.fromkeys(listed):
        code = native_code[-2:] + '.' + native_code[:-3]
        cached = metadata.get(code)
        if cached is not None:
            if cached['type'] == 'stock_cn':
                stocks.append(cached)
                reused += 1
            continue
        inspected += 1
        kinds = native.get_instrument_type(native_code)
        if not isinstance(kinds, dict):
            raise ValueError(f'QMT 未返回 {code} 的证券类型，目录尚未完成核验')
        if not kinds.get('stock'):
            continue
        detail = native.get_instrument_detail(native_code, False)
        if not isinstance(detail, dict) or not isinstance(detail.get('InstrumentName'), str) or not detail['InstrumentName'].strip():
            raise ValueError(f'QMT 未返回 {code} 的证券名称，目录尚未完成核验')
        stocks.append({'code': code, 'name': detail['InstrumentName'], 'type': 'stock_cn'})
    if not stocks or {row['code'][:2] for row in stocks} != {'SH', 'SZ', 'BJ'}:
        raise ValueError('QMT 股票分类结果未覆盖沪深京三个市场')
    # Reusing metadata must never move its freshness clock forward.
    _save(cache_path, stocks, checked if reused else now)
    return stocks, {'source': 'QMT_A_SHARE_SECTOR', 'listed_count': len(set(listed)),
                    'stock_count': len(stocks), 'metadata_reused': reused,
                    'native_type_reads': inspected, 'metadata_checked_at': checked if reused else now,
                    'elapsed_seconds': round(time.monotonic()-started, 3)}
