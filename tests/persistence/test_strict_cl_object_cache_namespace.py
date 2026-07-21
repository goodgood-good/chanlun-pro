from __future__ import annotations

from chanlun.file_db_mixins.cl_object_cache import (
    CL_OBJECT_SCHEMA_VERSION,
    _versioned_config_key,
)


def test_strict_cl_object_cache_uses_independent_schema_namespace() -> None:
    assert CL_OBJECT_SCHEMA_VERSION == "strict-v3"
    assert _versioned_config_key("abc123") == "strict-v3_abc123"


def test_legacy_and_strict_pickle_keys_cannot_collide() -> None:
    config_md5 = "same-config"

    assert _versioned_config_key(config_md5) != config_md5
    assert _versioned_config_key(config_md5).endswith(config_md5)
