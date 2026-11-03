"""Compact, checksummed large JSON state snapshots with legacy JSON support.

The caller still atomically commits two independent files. Encoding happens
once per commit; no asynchronous buffer can expose an uncommitted event.
"""
import base64
import hashlib
import json

SCHEMA = 'chanlun-state-snapshot-v1'
MAX_BYTES = 256 * 1024 * 1024


def compact_state(value):
    # Preserve JSON's existing types, integer range, Unicode and NaN rejection.
    # One C encoder pass is faster than walking this large graph in Python.
    packed=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    if len(packed)<65536 or len(packed)>MAX_BYTES:
        return value
    from pyarrow import compress
    return {'schema':SCHEMA,'codec':'json-zstd','bytes':len(packed),'sha256':hashlib.sha256(packed).hexdigest(),
            'payload':base64.b64encode(compress(packed,codec='zstd',asbytes=True)).decode('ascii')}


def expand_state(value):
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:
        return value
    try:
        if (set(value)!={'schema','codec','bytes','sha256','payload'} or value['codec']!='json-zstd'
                or type(value['bytes']) is not int
                or not 0<value['bytes']<=MAX_BYTES or not isinstance(value['sha256'],str)):
            raise ValueError('Invalid state snapshot envelope')
        packed=base64.b64decode(value['payload'],validate=True)
        from pyarrow import decompress
        raw=decompress(packed,decompressed_size=value['bytes'],codec='zstd',asbytes=True)
        if len(raw)!=value['bytes'] or hashlib.sha256(raw).hexdigest()!=value['sha256']:
            raise ValueError('State snapshot content differs from its checksum')
        result=json.loads(raw)
        if not isinstance(result,dict):
            raise ValueError('State snapshot must contain an object')
        return result
    except (OSError,TypeError,ValueError,KeyError,UnicodeError) as exc:
        raise ValueError('Unreadable state snapshot') from exc
