"""Process-wide safety boundary for the test suite.

This module is loaded before test collection, so imports cannot initialize the
application database with a developer or production configuration.
"""

import atexit
import os
import pathlib
import shutil
import tempfile
import pytest


_TEST_DATA_PATH = pathlib.Path(tempfile.mkdtemp(prefix="chanlun-pytest-")).resolve()
_TEMP_ROOT = pathlib.Path(tempfile.gettempdir()).resolve()
if _TEST_DATA_PATH.parent != _TEMP_ROOT:
    raise RuntimeError(f"pytest data path escaped the system temp directory: {_TEST_DATA_PATH}")

os.environ["CHANLUN_TESTING"] = "1"
os.environ["CHANLUN_TEST_DATA_PATH"] = str(_TEST_DATA_PATH)
os.environ["CHANLUN_LOG_DIR"] = str(_TEST_DATA_PATH / "logs")

from chanlun import config as _config

_config.DATA_PATH = str(_TEST_DATA_PATH)
_config.DB_TYPE = "sqlite"
_config.DB_DATABASE = "chanlun_pytest"
_config.DB_HOST = "pytest.invalid"
_config.DB_USER = ""
_config.DB_PWD = ""


@pytest.fixture(autouse=True)
def _isolate_native_instrument_metadata(monkeypatch):
    """Vendor metadata is explicit test input, never the developer's live feed."""
    from xtquant import xtdata
    for name in ("get_instrument_type", "get_instrument_detail"):
        function = getattr(xtdata, name, None)
        if getattr(function, "__module__", "").startswith("xtquant"):
            monkeypatch.setattr(xtdata, name, lambda *_args, **_kwargs: None)


@atexit.register
def _cleanup_test_data_path() -> None:
    shutil.rmtree(_TEST_DATA_PATH, ignore_errors=True)
