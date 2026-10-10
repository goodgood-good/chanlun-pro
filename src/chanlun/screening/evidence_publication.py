"""Reuse identical immutable evidence bytes within a bounded worker lifetime.

Every run keeps its own ordinary evidence paths and checksums. The optional
index contains paths and hashes only, and never owns evidence retention.
"""
from collections import OrderedDict
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from uuid import uuid4

from .cache import input_fingerprint


MAX_REFERENCES = 128
_references = OrderedDict()
_lock = threading.Lock()
_stripes = tuple(threading.Lock() for _ in range(32))


def clear_publication_cache():
    with _lock:
        _references.clear()


def _identity(frame, fingerprint, snapshot_bytes):
    # Calculation keys exclude acquisition diagnostics. Evidence preserves ALL
    # serializable frame metadata, so diagnostic changes must publish new bytes.
    try:
        attributes = json.dumps(frame.attrs, sort_keys=True, ensure_ascii=False,
                                allow_nan=False).encode('utf-8')
    except (TypeError, ValueError):
        return None
    digest = hashlib.sha256(fingerprint.encode())
    digest.update(attributes)
    digest.update(snapshot_bytes)
    return digest.hexdigest()


def _verified_copy(source, temporary, checksum):
    try:
        if source.is_symlink() or not source.is_file():
            return False
        # Published files are replaced atomically, never edited in place. A
        # hard link preserves bytes even when retention removes the older run.
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        # Check the actual linked/copied destination, including replacement or
        # corruption of the source between lookup and publication.
        return hashlib.sha256(temporary.read_bytes()).hexdigest() == checksum
    except OSError:
        return False


def save_evidence(run_dir, code, frequency, frame, snapshot):
    from .markets import storage_symbol

    market = frame.attrs.get('screening_market', 'a')
    directory = Path(run_dir).resolve()
    base = directory / 'evidence' / f'{storage_symbol(code, market)}_{frequency}'
    base.parent.mkdir(parents=True, exist_ok=True)
    parquet, packed = Path(str(base) + '.parquet'), Path(str(base) + '.json.gz')
    nonce = f'.{os.getpid()}.{uuid4().hex}.tmp'
    temporary = (Path(str(parquet) + nonce), Path(str(packed) + nonce))
    fingerprint = input_fingerprint(frame, code, frequency)
    snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode('utf-8')
    identity = _identity(frame, fingerprint, snapshot_bytes)
    slot = (os.getpid(), str(directory.parent), market, code, frequency)
    with _stripes[hash(slot) % len(_stripes)]:
        with _lock:
            previous = _references.pop(slot, None)
        try:
            reusable = previous is not None and identity is not None and previous['identity'] == identity
            if reusable:
                metadata = previous['metadata']
                reusable = all(_verified_copy(source, target, checksum)
                    for source, target, checksum in zip(previous['paths'], temporary,
                        (metadata['parquet_sha256'], metadata['snapshot_sha256'])))
            if not reusable:
                for path in temporary:
                    path.unlink(missing_ok=True)
                frame.to_parquet(temporary[0], index=False)
                temporary[1].write_bytes(gzip.compress(snapshot_bytes, compresslevel=3, mtime=0))
                metadata = {
                    'status': 'complete', 'market': market, 'chart_saved': bool(snapshot.get('chart')),
                    'parquet_bytes': temporary[0].stat().st_size, 'snapshot_bytes': temporary[1].stat().st_size,
                    'parquet_sha256': hashlib.sha256(temporary[0].read_bytes()).hexdigest(),
                    'snapshot_sha256': hashlib.sha256(temporary[1].read_bytes()).hexdigest(),
                    'input_fingerprint': fingerprint,
                }
            os.replace(temporary[0], parquet)
            os.replace(temporary[1], packed)
            if identity is not None:
                with _lock:
                    _references[slot] = {'identity': identity, 'paths': (parquet, packed),
                                         'metadata': dict(metadata)}
                    while len(_references) > MAX_REFERENCES:
                        _references.popitem(last=False)
            return dict(metadata)
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)
