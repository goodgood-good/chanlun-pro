"""Small IPC messages referring to authenticated, immutable job-local JSON."""
import hashlib
import json
from multiprocessing.connection import BUFSIZE
from pathlib import Path
import pickle
import re
import zlib

SCHEMA = 'screening-worker-inputs-v1'
TIMING_SCOPE = 'screening-period-task-v1'
MAX_CONTROL_BYTES = min(4096, BUFSIZE // 2)
MAX_INLINE_BYTES = 64 * 1024


def bounded_message(value):
    # One outstanding message per worker fits comfortably inside the pipe.
    # Large calendar arrays/results are kept in files, not in this channel.
    if len(pickle.dumps(value)) > MAX_CONTROL_BYTES:
        raise ValueError('Worker control message exceeds its bounded size')
    return value


def _store(directory, prefix, value):
    from .runner import write_json
    directory = Path(directory).resolve()
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    path = directory/f'{prefix}.{digest}.json'
    if not path.exists():
        write_json(path, value)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Immutable worker transport file changed')
    return {'file': str(path), 'sha256': digest}


def _read(reference, directory, prefix):
    digest = reference.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid worker transport digest')
    path = Path(reference['file'])
    expected = Path(directory).resolve()/f'{prefix}.{digest}.json'
    if path.is_symlink() or path.resolve() != expected:
        raise ValueError('Worker transport file escaped its task directory')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Worker transport checksum mismatch')
    return json.loads(raw), path


def store_job(stocks, settings, contexts, run_dir, revision, request):
    directory = str(Path(run_dir).resolve())
    return _store(directory, 'worker_inputs', {'schema': SCHEMA, 'run_dir': directory,
                  'revision': revision, 'stocks': stocks, 'settings': settings,
                  'contexts': contexts, 'request': request})


class JobInputReader:
    def __init__(self):
        self.key = None
        self.value = None

    def load(self, message):
        reference = message['inputs']
        key = (reference['file'], reference['sha256'], message['revision'])
        if key != self.key:
            directory = Path(reference['file']).parent
            value, _ = _read(reference, directory, 'worker_inputs')
            if (value.get('schema') != SCHEMA or value.get('revision') != message['revision']
                    or value.get('run_dir') != str(directory.resolve())):
                raise ValueError('Worker inputs belong to another run or revision')
            self.value, self.key = value, key
        index = message['index']
        if type(index) is not int or not 0 <= index < len(self.value['stocks']):
            raise ValueError('Worker symbol index is invalid')
        return {**self.value, 'ticket': message['ticket'], 'stock': self.value['stocks'][index]}


def store_reply(task, result, *, prefer_inline=False):
    ticket = task['ticket']
    if not re.fullmatch(r'[a-f0-9]{32}_\d+', ticket):
        raise ValueError('Invalid worker reply ticket')
    if prefer_inline:
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) <= MAX_INLINE_BYTES:
            message = {'ticket':ticket, 'result_inline':{
                'schema':'screening-inline-result-v1', 'bytes':len(raw),
                'sha256':hashlib.sha256(raw).hexdigest(), 'data':zlib.compress(raw, level=1)}}
            try:
                return bounded_message(message)
            except ValueError:
                pass  # Large replies retain the authenticated file protocol.
    reference = _store(task['run_dir'], f'worker_reply.{ticket}', result)
    return bounded_message({'ticket': ticket, 'result_file': reference})


def read_reply(message, ticket, run_dir):
    if not isinstance(message, dict) or message.get('ticket') != ticket:
        raise ValueError('Worker response belongs to another task')
    if 'result_inline' in message:
        if set(message) != {'ticket','result_inline'}:
            raise ValueError('Ambiguous inline worker response')
        value = message['result_inline']
        if (not isinstance(value, dict) or set(value) != {'schema','bytes','sha256','data'}
                or value['schema'] != 'screening-inline-result-v1'
                or type(value['bytes']) is not int or not 0 <= value['bytes'] <= MAX_INLINE_BYTES
                or not isinstance(value['data'], bytes)):
            raise ValueError('Invalid inline worker response')
        bounded_message(message)
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(value['data'], value['bytes'] + 1)
            if (len(raw) != value['bytes'] or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data
                    or hashlib.sha256(raw).hexdigest() != value['sha256']):
                raise ValueError('Inline worker response checksum mismatch')
            return json.loads(raw)
        except (zlib.error, UnicodeError) as exc:
            raise ValueError('Damaged inline worker response') from exc
    value, _ = _read(message['result_file'], run_dir, f'worker_reply.{ticket}')
    return value


def discard_reply(message, ticket, run_dir):
    """Best-effort cleanup after acceptance; never undo a valid result.

    Recheck directory and bytes before retries, since the file could have been
    replaced while a scanner/indexer held it. Run retention owns any remainder.
    """
    try:
        if isinstance(message, dict) and 'result_inline' in message:
            read_reply(message, ticket, run_dir)
            return True
        if not isinstance(message, dict) or message.get('ticket') != ticket:
            return False
        _, path = _read(message['result_file'], run_dir, f'worker_reply.{ticket}')
        path.unlink(missing_ok=True)
        return True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
