"""Keep verified calculations through changes to execution-only modules.

Unknown dependencies remain significant. This is an explicit exclusion of
reviewed process/storage plumbing, not an allowlist of algorithm entry points.
The full runtime revision still owns workers, frozen runs and notifications.
"""
from functools import lru_cache
import hashlib
from pathlib import Path
import re

EXECUTION_ONLY = frozenset({
    'exchange/quote_capacity.py', 'exchange/longbridge_quote.py',
    'exchange/longbridge_history_limit.py', 'exchange/usmart_rate_limit.py',
    'persistence/state_snapshot.py',
    'screening/compute_capacity.py', 'screening/cache_retention.py',
    'screening/worker_pool.py', 'screening/worker_lifetime.py',
    'screening/worker_host.py', 'screening/worker_transport.py',
    'screening/pending_tasks.py', 'screening/state_io.py',
})


def computation_digest(root,project):
    files=[path for path in root.rglob('*.py') if path.relative_to(root).as_posix() not in EXECUTION_ONLY]
    files += [*root.parent.joinpath('xtquant').rglob('*.py'),
              *root.joinpath('exchange/data').glob('a_share*.json'),project/'pyproject.toml',project/'poetry.lock']
    digest=hashlib.sha256(b'screening-calculation-contract-v1')
    for path in sorted(files):
        digest.update(path.relative_to(project).as_posix().encode());digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=4)
def calculation_contract(runtime_revision):
    from .runner import source_revision
    # Explicit offline/test revisions retain their existing isolation. Only a
    # worker running the exact current full source can use the split contract.
    if not re.fullmatch(r'[a-f0-9]{64}',str(runtime_revision)) or runtime_revision!=source_revision():
        return runtime_revision
    root=Path(__file__).resolve().parents[1]
    return computation_digest(root,root.parents[1])
