"""Conservative image references for local chart cleanup; never changes links."""
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse


_HASH = re.compile(r'(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])')
_URL = re.compile(r'https?://[^\s"\'<>()[\]{}]+')
_PATH = re.compile(r'/public/alert-chart/([a-f0-9]{64})\.png')
_TERMINAL = {'sent', 'failed', 'expired', 'suppressed'}


class UnknownImageReferences(ValueError):
    pass


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _text_references(text, *, now, key, allow_expired, require_json=False):
    if not isinstance(text, str):
        raise UnknownImageReferences('Image reference field is not text')
    try:
        decoded = json.loads(text)
        if require_json and not isinstance(decoded, (dict, list)):
            raise UnknownImageReferences('Retained reference document has an unknown shape')
        values = list(_strings(decoded)) if isinstance(decoded, (dict, list, str)) else [text]
    except (ValueError, TypeError):
        if require_json:
            raise UnknownImageReferences('Retained reference document cannot be decoded') from None
        values = [text]
    protected = set()
    for value in values:
        value = html.unescape(value).replace('\\/', '/')
        remaining = value
        for raw in _URL.findall(value):
            parsed = urlparse(raw)
            match = _PATH.fullmatch(parsed.path)
            if match is None:
                continue
            digest = match[1]
            expired = False
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (allow_expired and key is not None and set(query) == {'expires', 'signature'}
                        and len(query['expires']) == len(query['signature']) == 1
                        and query['expires'][0].isascii() and query['expires'][0].isdigit()):
                    expiry = int(query['expires'][0])
                    expected = hmac.new(key, f'{digest}:{expiry}'.encode(), hashlib.sha256).hexdigest()
                    expired = hmac.compare_digest(expected, query['signature'][0]) and expiry <= now
            except (ValueError, TypeError):
                pass
            if not expired:
                protected.add(digest)
            remaining = remaining.replace(raw, '')
        if '/public/alert-chart/' in remaining:
            # A local chart reference whose image cannot be identified must
            # preserve the entire store, not be mistaken for no references.
            raise UnknownImageReferences('Unrecognized local chart image reference')
        protected.update(_HASH.findall(remaining))
    return protected


def protected_image_hashes(jobs, outbox, *, now, signing_key, archive_roots=()):
    """Protect every retained job and every non-expired or uncertain link.

    Call after expired terminal jobs have been durably deleted. The caller
    owns read transactions. Missing schemas/read errors propagate so cleanup
    cannot use a partial or empty set. Never create a signing key while reading.
    """
    key = signing_key if isinstance(signing_key, bytes) and len(signing_key) == 32 else None
    protected = set()
    retained_jobs = set()
    count = size = 0

    def add(text, *, allow_expired=False, require_json=False):
        nonlocal count, size
        count += 1
        if not isinstance(text, str):
            raise UnknownImageReferences('Unreadable image reference')
        size += len(text.encode('utf-8'))
        if count > 300000 or size > 64 * 1024 * 1024:
            raise UnknownImageReferences('Image reference scan exceeded its bound')
        protected.update(_text_references(text, now=now, key=key, allow_expired=allow_expired,
                                          require_json=require_json))

    for row in jobs.execute('SELECT id,initial_image_hash,event,history_coverage,history_progress FROM jobs'):
        identity, digest = row[:2]
        if not isinstance(identity, str) or not identity:
            raise UnknownImageReferences('Unrecognized retained job identity')
        retained_jobs.add(identity)
        if digest:
            if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise UnknownImageReferences('Unrecognized retained image identity')
            protected.add(digest)
        add(row[2], require_json=True)
        add(row[3], require_json=True)
        add(row[4])
    # A committed first-image receipt may survive a crash before initial_hash
    # is written into its job. The recovery path binds this receipt without
    # republishing the PNG, so a retained job protects that link even if old.
    retained_receipts = {prefix + identity for identity in retained_jobs
                         for prefix in ('chart:', 'chart-complete:')}
    for identity, status, content in outbox.execute('SELECT id,status,content FROM outbox'):
        add(content, allow_expired=status in _TERMINAL and identity not in retained_receipts)
    for identity, text, status in outbox.execute('SELECT i.signal_key,i.url,o.status FROM inline_charts i '
                                      'LEFT JOIN outbox o ON o.id=i.outbox_id'):
        add(text, allow_expired=status in _TERMINAL and identity not in retained_jobs)
    for text, status in outbox.execute('SELECT p.events,o.status FROM signal_payloads p '
                                      'LEFT JOIN outbox o ON o.id=p.outbox_id'):
        add(text, allow_expired=status in _TERMINAL, require_json=True)
    for directory in archive_roots:
        directory = Path(directory)
        try:
            directory.stat()
        except FileNotFoundError:
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise UnknownImageReferences('Unverifiable archive root')
        def unreadable(error):
            raise error
        for folder, directories, files in os.walk(directory, onerror=unreadable, followlinks=False):
            if any((Path(folder) / name).is_symlink() for name in directories):
                raise UnknownImageReferences('Unverifiable archive directory')
            for name in files:
                if not name.endswith('.json'):
                    continue
                path = Path(folder) / name
                if path.is_symlink() or path.stat().st_size + size > 64 * 1024 * 1024:
                    raise UnknownImageReferences('Unverifiable retained archive reference')
                add(path.read_text(encoding='utf-8'), require_json=True)
    return protected
