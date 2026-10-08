"""Optional disk caching, versioned separately from screening interpretation."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import threading
import zlib


SCHEMA = "screening-verified-results-v1"
FILENAME = "verified_results.json.gz"


def storage_revision():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


REVISION = storage_revision()


def cache_key(key):
    return hashlib.sha256(json.dumps(
        [REVISION, key], ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()


def load(directory, state, key):
    if directory is None or state.get("status") != "completed":
        return None
    try:
        data = json.loads(gzip.decompress((directory / FILENAME).read_bytes()))
    except (OSError, ValueError, EOFError, zlib.error):
        return None
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or data.get("key") != key
            or not isinstance(data.get("payload"), dict)
            or data["payload"].get("run_id") != state.get("run_id")):
        return None
    return data["payload"]


def save(directory, state, key, payload):
    if directory is None or state.get("status") != "completed":
        return
    path = directory / FILENAME
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        encoded = json.dumps({"schema": SCHEMA, "key": key, "payload": payload},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary.write_bytes(gzip.compress(encoded, compresslevel=3))
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        # Optional persistence cannot discard a successfully verified result.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
