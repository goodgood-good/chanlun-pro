"""Process-wide safety boundary for the test suite.

This module is loaded before test collection, so imports cannot initialize the
application database with a developer or production configuration.
"""

import atexit
import os
import pathlib
import shutil
import tempfile


_TEST_DATA_PATH = pathlib.Path(tempfile.mkdtemp(prefix="chanlun-pytest-")).resolve()
_TEMP_ROOT = pathlib.Path(tempfile.gettempdir()).resolve()
if _TEST_DATA_PATH.parent != _TEMP_ROOT:
    raise RuntimeError(f"pytest data path escaped the system temp directory: {_TEST_DATA_PATH}")

os.environ["CHANLUN_TESTING"] = "1"
os.environ["CHANLUN_TEST_DATA_PATH"] = str(_TEST_DATA_PATH)

from chanlun import config as _config

_config.DATA_PATH = str(_TEST_DATA_PATH)
_config.DB_TYPE = "sqlite"
_config.DB_DATABASE = "chanlun_pytest"
_config.DB_HOST = "pytest.invalid"
_config.DB_USER = ""
_config.DB_PWD = ""


@atexit.register
def _cleanup_test_data_path() -> None:
    shutil.rmtree(_TEST_DATA_PATH, ignore_errors=True)