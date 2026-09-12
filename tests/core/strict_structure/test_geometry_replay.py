"""Pure replay must preserve every source field, event and scope boundary."""

import os
import pickle
import subprocess
import sys


from chanlun.core.strict_structure.geometry_replay import ReplayKey
















def test_a_persisted_prefix_key_rehashes_when_loaded_in_another_process(tmp_path):
    saved = tmp_path / "prefix-key.pkl"
    saved.write_bytes(pickle.dumps({ReplayKey(("source", "market", 42)): "same-evidence"}))
    script = """import pickle, sys
from chanlun.core.strict_structure.geometry_replay import ReplayKey
with open(sys.argv[1], 'rb') as stream:
    cache = pickle.load(stream)
assert cache[ReplayKey(('source', 'market', 42))] == 'same-evidence'
"""
    subprocess.run([sys.executable, "-c", script, str(saved)], check=True,
                   env={**os.environ, "PYTHONHASHSEED": "1", "PYTHONPATH": os.pathsep.join(sys.path)}, capture_output=True, text=True)
