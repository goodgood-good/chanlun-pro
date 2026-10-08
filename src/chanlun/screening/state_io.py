"""Read published state through brief Windows atomic-replacement locks."""
from pathlib import Path
import json
import time


def read_state_json(path):
    for attempt in range(15):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 14:
                raise
            time.sleep(min(0.05 * 2 ** attempt, 0.5))
