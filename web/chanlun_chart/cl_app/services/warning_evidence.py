"""Keep a warning's original immutable bytes while its hypothesis is active."""

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import os
import re
import shutil
import time
from uuid import uuid4


def _objects(runs_root):
    return Path(runs_root).parent / "warning_evidence" / "objects"


def preserve(event, runs_root):
    from chanlun.screening.evidence import evidence_paths

    manifest = event.get("evidence") or {}
    source = evidence_paths(
        Path(runs_root) / event["evidence_run_id"],
        event["code"],
        event["frequency"],
        event.get("market", "a"),
        manifest.get("namespace"),
    )
    folder = _objects(runs_root)
    folder.mkdir(parents=True, exist_ok=True)
    for path, kind, suffix in zip(
        source, ("parquet", "snapshot"), (".parquet", ".json.gz")
    ):
        digest = manifest.get(kind + "_sha256")
        if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("Missing warning source hash")
        target = folder / (digest + suffix)
        if not target.exists():
            if not path.is_file():
                candidate = (
                    Path(runs_root).parent
                    / "notification_evidence/objects"
                    / (digest + suffix)
                )
                if candidate.is_file():
                    path = candidate
            temporary = folder / (uuid4().hex + ".tmp")
            try:
                shutil.copyfile(path, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise ValueError("Warning source changed")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if (
            target.is_symlink()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Corrupt warning origin")
        os.utime(target, None)


def load_preserved(event, runs_root):
    from chanlun.screening.evidence import evidence_paths, load_evidence

    manifest = event.get("evidence") or {}
    with TemporaryDirectory(prefix="chanlun-warning-origin-") as temporary:
        root = Path(temporary)
        directory = root / event["evidence_run_id"]
        paths = evidence_paths(
            directory,
            event["code"],
            event["frequency"],
            event.get("market", "a"),
            manifest.get("namespace"),
        )
        for target, kind, suffix in zip(
            paths, ("parquet", "snapshot"), (".parquet", ".json.gz")
        ):
            digest = manifest.get(kind + "_sha256", "")
            if not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError("Missing warning source hash")
            candidates = [
                _objects(runs_root) / (digest + suffix),
                Path(runs_root).parent
                / "notification_evidence/objects"
                / (digest + suffix),
            ]
            source = next(
                (p for p in candidates if p.is_file() and not p.is_symlink()), None
            )
            if source is None:
                raise FileNotFoundError("Original warning evidence is unavailable")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Corrupt warning origin")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return load_evidence(directory, event["code"], event["frequency"], manifest)


def cleanup(runs_root, state, *, now=None):
    from .warning_lifecycle import warning_reference

    protected = set()
    values = [
        *state.get("signals", {}).values(),
        *state.get("unverified_signals", {}).values(),
        *state.get("pending_notifications", {}).values(),
        *state.get("events", []),
    ]
    for item in values:
        try:
            manifest = warning_reference(item).get("evidence") or {}
        except (ValueError, TypeError, KeyError):
            continue
        protected.update(manifest.get(k) for k in ("parquet_sha256", "snapshot_sha256"))
    now = time.time() if now is None else now
    folder = _objects(runs_root)
    for path in folder.glob("*"):
        if (
            not path.is_symlink()
            and path.resolve().parent == folder.resolve()
            and re.fullmatch(r"[a-f0-9]{64}\.(?:parquet|json\.gz)", path.name)
            and path.name[:64] not in protected
            and now - path.stat().st_mtime > 8 * 86400
        ):
            path.unlink(missing_ok=True)
