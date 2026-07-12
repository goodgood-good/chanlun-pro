import os
import pathlib

from chanlun import config


def test_pytest_uses_an_ephemeral_sqlite_database():
    isolated_path = os.environ.get("CHANLUN_TEST_DATA_PATH")

    assert os.environ.get("CHANLUN_TESTING") == "1"
    assert isolated_path
    assert config.DB_TYPE == "sqlite"
    assert config.DB_DATABASE == "chanlun_pytest"
    assert pathlib.Path(config.DATA_PATH).resolve() == pathlib.Path(isolated_path).resolve()