"""Bounded by the runtime supervisor; only inspect the configured quote service."""
import argparse
from collections import Counter
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys


def inspect(native, expected_directory, port):
    native.enable_hello = False
    native.connect('127.0.0.1', port)
    actual = Path(native.get_data_dir()).resolve()
    expected = Path(expected_directory).resolve()
    if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        return {'ready': False, 'reason_code': 'QMT_DATA_DIRECTORY_MISMATCH'}
    stocks = native.get_stock_list_in_sector('沪深京A股')
    valid = (isinstance(stocks, list) and bool(stocks)
             and all(isinstance(code, str) and re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code) for code in stocks))
    counts = dict(Counter(code[-2:] for code in stocks)) if valid else {}
    ready = valid and set(counts) == {'SH', 'SZ', 'BJ'}
    return {'ready': ready, 'reason_code': 'READY' if ready else 'QMT_MARKET_CATALOG_NOT_READY',
            'listed_count': len(stocks) if isinstance(stocks, list) else 0, 'exchanges': counts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--port', required=True, type=int)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    try:
        # Native connection banners must not corrupt the supervisor JSON.
        with contextlib.redirect_stdout(io.StringIO()):
            from xtquant import xtdata
            result = inspect(xtdata, args.data_dir, args.port)
    except Exception as error:
        result = {'ready': False, 'reason_code': 'QMT_MARKET_DATA_PROBE_FAILED',
                  'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ready'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
