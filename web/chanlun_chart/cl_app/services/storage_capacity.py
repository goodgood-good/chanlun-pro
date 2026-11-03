"""Small read-only capacity snapshot; never deletes evidence or stops a run."""
from pathlib import Path
import shutil
import threading
import time

_lock=threading.Lock()
_snapshots={}


def snapshot(root):
    root=Path(root).resolve();key=str(root);now=time.monotonic()
    with _lock:
        cached=_snapshots.get(key)
        if cached and 0<=now-cached[0]<30:
            return dict(cached[1])
    try:
        path=root
        while not path.exists() and path.parent!=path:
            path=path.parent
        total,used,free=shutil.disk_usage(path)
        state='critical' if free<1024**3 else 'low' if free<max(5*1024**3,total*.05) else 'normal'
        result={'state':state,'free_bytes':free,'total_bytes':total,
                'free_gib':round(free/1024**3,2),'free_percent':round(free/total*100,1),
                'checked_at':time.time()}
    except OSError:
        result={'state':'unavailable','free_bytes':None,'total_bytes':None,'checked_at':time.time()}
    with _lock:
        if len(_snapshots)>=16:_snapshots.clear()
        _snapshots[key]=(now,result)
    return dict(result)
