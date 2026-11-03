"""Small, content-addressed frozen evidence for recent notification audits."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from tempfile import TemporaryDirectory
from uuid import uuid4

from .dingtalk_notifications import signal_key


def same_notification_event(event, saved):
    """Match the occurrence and frozen inputs, allowing presentation hints."""
    fields = ('id', 'stage', 'point_type', 'code', 'market', 'frequency',
              'recorded_at', 'source_closed_at', 'anchor_at', 'anchor_price',
              'lifecycle_cycle', 'point_id', 'hypothesis_id', 'signal_type',
              'evidence_run_id', 'evidence', 'warning_assessment',
              'warning_hypothesis','warning_reference','invalidation_reference',
              'price_basis_revision', 'stop_loss', 'available_at')
    return all(event.get(key) == saved.get(key) for key in fields)


def evidence_sources(event):
    sources, pending, seen = [], [event], set()
    while pending:
        source = pending.pop(0)
        key = (source.get('evidence_run_id'), source.get('source_closed_at'), source.get('stage'),
               (source.get('evidence') or {}).get('snapshot_sha256'))
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
        for field in ('invalidation_reference', 'warning_reference'):
            if source.get(field):
                pending.append(source[field])
        if len(sources) > 8:
            raise ValueError('notification evidence reference chain is too long')
    return sources


class NotificationEvidence:
    lifetime = 7 * 86400

    def __init__(self, root, *, now=None):
        self.root = Path(root).resolve()
        self.now = now or time.time

    def _folder(self, name):
        folder = self.root / name
        if folder.resolve().parent != self.root or folder.is_symlink():
            raise ValueError("notification archive folder is invalid")
        return folder

    def _object(self, source, digest, suffix):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("notification evidence checksum is missing")
        folder = self._folder("objects")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (digest + suffix)
        if target.resolve().parent != folder.resolve() or target.is_symlink():
            raise ValueError("notification evidence object path is invalid")
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("notification evidence object is corrupt")
        else:
            temporary = folder / f".{uuid4().hex}.tmp"
            try:
                # Independent bytes keep the archive immutable if a source is
                # later edited, and do not change its polling timestamps.
                shutil.copyfile(source, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise ValueError("notification evidence source checksum mismatch")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        os.utime(target, (self.now(), self.now()))
        return target.name

    def capture(self, event, runs_root, created, *, allow_partial=False):
        from chanlun.screening.evidence import evidence_paths

        identity = hashlib.sha256(signal_key(event).encode()).hexdigest()
        folder = self._folder("events")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (identity + ".json")
        if target.resolve().parent != folder or target.is_symlink():
            raise ValueError("notification archive event path is invalid")
        if target.exists():
            previous = self.read(identity)
            if previous["event"] != event or previous["created_at"] != created:
                raise ValueError("notification evidence identity changed")
            return identity
        root = Path(runs_root).resolve()
        frozen = []
        sources = evidence_sources(event)
        partial=False
        for source_index,source in enumerate(sources):
            run = source.get("evidence_run_id", "")
            manifest = source.get("evidence") or {}
            if not isinstance(run, str) or not re.fullmatch(r"[a-f0-9]{32}", run):
                raise ValueError("notification evidence run is missing")
            directory = (root / run).resolve()
            if directory.parent != root:
                raise ValueError("notification evidence run escaped its root")
            if (directory / "evidence").resolve().parent != directory:
                raise ValueError("notification source folder escaped its run")
            paths = evidence_paths(
                directory,
                source["code"],
                source["frequency"],
                source.get("market", "a"),
                manifest.get('namespace'),
            )
            objects = {}
            unavailable=False
            for path, kind, suffix in zip(
                paths, ("parquet", "snapshot"), (".parquet", ".json.gz")
            ):
                if (
                    path.resolve().parent != (directory / "evidence").resolve()
                    or path.is_symlink()
                ):
                    raise ValueError("notification source path is invalid")
                digest=manifest.get(kind+'_sha256')
                preserved=root.parent/'warning_evidence/objects'/(str(digest)+suffix)
                candidate=path if path.exists() else preserved if preserved.is_file() else path
                try:
                    objects[kind] = self._object(candidate,digest,suffix)
                except FileNotFoundError:
                    if not allow_partial or source_index==0:raise
                    unavailable=True;partial=True;objects={};break
            revision = None
            try:
                status = json.loads(
                    (directory / "status.json").read_text(encoding="utf-8")
                )
                if (
                    status.get("run_id") == run
                    and isinstance(status.get("source_revision"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", status["source_revision"])
                ):
                    revision = status["source_revision"]
            except (OSError, ValueError, AttributeError):
                pass
            frozen.append(
                {
                    "source": deepcopy(source),
                    "objects": objects,
                    "algorithm_revision": revision,
                    **({'unavailable':True,'reason':'ORIGINAL_SOURCE_FILE_MISSING'} if unavailable else {}),
                }
            )
        value = {
            "schema": "notification-evidence-v2" if partial else "notification-evidence-v1",
            "created_at": created,
            "expires_at": created + self.lifetime,
            "event": deepcopy(event),
            "evidence": frozen,
            **({'partial_sources':True} if partial else {}),
        }
        temporary = folder / f".{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return identity

    def read(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("invalid notification evidence identity")
        events = self._folder("events")
        path = events / (identity + ".json")
        if path.resolve().parent != events or path.is_symlink():
            raise ValueError("notification archive event path is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        names = self._references(value, identity)
        if self.now() >= value["expires_at"]:
            raise ValueError("notification evidence is expired")
        objects = self._folder("objects")
        for name in names:
            source = objects / name
            if (
                source.resolve().parent != objects
                or source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != name[:64]
            ):
                raise ValueError("notification evidence checksum mismatch")
        return value

    @contextmanager
    def materialized_runs(self, event):
        """Mount checksum-verified originals without relocating display caches."""
        from chanlun.screening.evidence import evidence_paths
        identity = hashlib.sha256(signal_key(event).encode()).hexdigest()
        saved = self.read(identity)
        if not same_notification_event(event, saved['event']):
            raise ValueError('archive belongs to another notification occurrence')
        with TemporaryDirectory(prefix='chanlun-notification-evidence-') as temporary:
            root = Path(temporary).resolve()
            for entry in saved['evidence']:
                if entry.get('unavailable') is True:
                    continue
                source = entry['source']
                run = source.get('evidence_run_id', '')
                if not isinstance(run, str) or not re.fullmatch(r'[a-f0-9]{32}', run):
                    raise ValueError('invalid archived run')
                paths = evidence_paths(root / run, source['code'], source['frequency'],
                    source.get('market', 'a'), source['evidence'].get('namespace'))
                for target, kind in zip(paths, ('parquet', 'snapshot')):
                    if not target.resolve().is_relative_to(root):
                        raise ValueError('invalid archived evidence path')
                    data = (self.root / 'objects' / entry['objects'][kind]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != source['evidence'][kind + '_sha256']:
                        raise ValueError('archive changed while reading')
                    if target.exists() and target.read_bytes() != data:
                        raise ValueError('conflicting archived evidence paths')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            yield root, saved

    def _references(self, value, identity):
        if (
            not isinstance(value, dict)
            or value.get("schema") not in {"notification-evidence-v1","notification-evidence-v2"}
            or not isinstance(value.get("event"), dict)
            or any(
                type(value.get(k)) not in (int, float) or not math.isfinite(value[k])
                for k in ("created_at", "expires_at")
            )
            or value["expires_at"] != value["created_at"] + self.lifetime
            or hashlib.sha256(signal_key(value["event"]).encode()).hexdigest()
            != identity
        ):
            raise ValueError("notification evidence record is mismatched")
        expected = evidence_sources(value['event'])
        entries = value.get("evidence")
        if not isinstance(entries, list) or len(entries) != len(expected):
            raise ValueError("notification evidence references are incomplete")
        names = set()
        for entry, source in zip(entries, expected):
            if not isinstance(entry, dict) or entry.get("source") != source:
                raise ValueError("notification evidence source is mismatched")
            if entry.get('unavailable') is True:
                if (value.get('schema')!='notification-evidence-v2' or not value.get('partial_sources')
                        or entry is entries[0] or entry.get('objects')!={}):
                    raise ValueError('Invalid unavailable evidence declaration')
                continue
            for kind, suffix in (("parquet", ".parquet"), ("snapshot", ".json.gz")):
                digest = source["evidence"][kind + "_sha256"]
                name = entry["objects"][kind]
                if (
                    not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or name != digest + suffix
                ):
                    raise ValueError("notification evidence reference is invalid")
                names.add(name)
        return names

    def cleanup(self):
        events = self._folder("events")
        objects = self._folder("objects")
        protected = set()
        for path in events.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            if path.resolve().parent != events.resolve() or path.is_symlink():
                raise ValueError("notification archive path is invalid")
            value = json.loads(path.read_text(encoding="utf-8"))
            names = self._references(value, path.stem)
            if self.now() >= value["expires_at"]:
                path.unlink()
            else:
                protected.update(names)
        for path in objects.iterdir() if objects.exists() else ():
            if (
                path.name not in protected
                and re.fullmatch(r"[a-f0-9]{64}\.(?:parquet|json\.gz)", path.name)
                and path.resolve().parent == objects.resolve()
                and not path.is_symlink()
                and self.now() - path.stat().st_mtime > 3600
            ):
                path.unlink(missing_ok=True)
        for folder in (events, objects):
            for path in folder.glob(".*.tmp"):
                if (
                    re.fullmatch(r"\.[a-f0-9]{32}\.tmp", path.name)
                    and path.resolve().parent == folder.resolve()
                    and not path.is_symlink()
                    and self.now() - path.stat().st_mtime > 3600
                ):
                    path.unlink(missing_ok=True)
