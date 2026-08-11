from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests

from .corpus_integrity import probe_file
from .corpus_types import CorpusFile


MAX_RESPONSE_BYTES = 100 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_FETCH_CODES = frozenset(
    {
        "authentication_required",
        "fetch_failed",
        "fetch_timeout",
        "rate_limited",
        "response_too_large",
        "source_not_found",
    }
)
_SAFE_RESULT_CODES = _SAFE_FETCH_CODES | frozenset(
    {
        "empty_response",
        "invalid_expected_sha256",
        "invalid_payload",
        "invalid_response_type",
        "invalid_source_url",
        "media_type_mismatch",
        "missing_source_url",
        "path_outside_root",
        "payload_changed_before_replace",
        "destination_changed_before_replace",
        "repair_failed",
        "repaired",
        "replace_failed",
        "semantic_verification_failed",
        "semantic_verification_required",
        "sha256_mismatch",
    }
)


def _report_source_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if parsed.scheme not in ("http", "https") or not host:
        return ""
    host_text = f"[{host}]" if ":" in host else host
    netloc = f"{host_text}:{port}" if port is not None else host_text
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


@dataclass(frozen=True)
class RepairTarget:
    url: str
    destination: Path
    expected_sha256: str = ""
    expected_media_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "destination", Path(self.destination))
        object.__setattr__(self, "expected_sha256", self.expected_sha256.casefold())


@dataclass(frozen=True)
class RepairResult:
    destination: str
    source_url: str
    ok: bool
    code: str
    bytes_written: int = 0
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_url", _report_source_url(self.source_url))
        if self.code not in _SAFE_RESULT_CODES:
            object.__setattr__(self, "code", "repair_failed")


class RepairFetchError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _SAFE_FETCH_CODES else "fetch_failed"
        super().__init__(self.code)


class RequestsFetcher:
    def __init__(
        self,
        *,
        session=None,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if connect_timeout <= 0 or read_timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("fetch bounds must be positive")
        self._session = session or requests.Session()
        self._timeout = (float(connect_timeout), float(read_timeout))
        self._max_response_bytes = int(max_response_bytes)

    def __call__(self, url: str) -> bytes:
        try:
            with self._session.get(
                url,
                allow_redirects=True,
                stream=True,
                timeout=self._timeout,
            ) as response:
                if response.status_code in (401, 403):
                    raise RepairFetchError("authentication_required")
                if response.status_code == 404:
                    raise RepairFetchError("source_not_found")
                if response.status_code == 429:
                    raise RepairFetchError("rate_limited")
                if response.status_code >= 300:
                    raise RepairFetchError("fetch_failed")
                try:
                    content_length = int(response.headers.get("Content-Length", "0"))
                except (TypeError, ValueError):
                    content_length = 0
                if content_length > self._max_response_bytes:
                    raise RepairFetchError("response_too_large")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise RepairFetchError("response_too_large")
                    chunks.append(bytes(chunk))
                return b"".join(chunks)
        except RepairFetchError:
            raise
        except requests.Timeout as exc:
            raise RepairFetchError("fetch_timeout") from exc
        except requests.RequestException as exc:
            raise RepairFetchError("fetch_failed") from exc


Fetch = Callable[[str], bytes]
Verifier = Callable[[RepairTarget, CorpusFile, bytes], str | None]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _destination_label(destination: Path, root: Path) -> str:
    try:
        return destination.relative_to(root).as_posix()
    except ValueError:
        return str(destination)


def _result(
    target: RepairTarget,
    destination: Path,
    root: Path,
    *,
    ok: bool,
    code: str,
    bytes_written: int = 0,
    sha256: str = "",
) -> RepairResult:
    return RepairResult(
        destination=_destination_label(destination, root),
        source_url=_report_source_url(target.url),
        ok=ok,
        code=code,
        bytes_written=bytes_written,
        sha256=sha256,
    )


def _requires_semantic_verification(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type == "application/json"


def _destination_snapshot(path: Path) -> tuple[object, ...]:
    if not path.exists():
        return (False,)
    item = probe_file(path)
    return (
        True,
        item.size,
        item.sha256,
        item.media_type,
        item.valid,
        item.error_code,
    )


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _restore_backup_without_overwrite(
    backup: Path,
    destination: Path,
) -> bool:
    if not backup.exists():
        return False

    try:
        os.link(backup, destination)
        return True
    except FileExistsError:
        return _same_file(backup, destination)
    except OSError:
        return False


def _repair_one(
    target: RepairTarget,
    fetch: Fetch,
    root: Path,
    verify: Verifier | None,
) -> RepairResult:
    destination = target.destination.resolve()
    if not _is_within(destination, root) or destination == root:
        return _result(target, destination, root, ok=False, code="path_outside_root")
    if not target.url.strip():
        return _result(target, destination, root, ok=False, code="missing_source_url")
    try:
        parsed_url = urlsplit(target.url)
        host = parsed_url.hostname
        parsed_url.port
    except (TypeError, ValueError):
        return _result(target, destination, root, ok=False, code="invalid_source_url")
    if (
        parsed_url.scheme not in ("http", "https")
        or not parsed_url.netloc
        or not host
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        return _result(target, destination, root, ok=False, code="invalid_source_url")
    if target.expected_sha256 and not _SHA256_RE.fullmatch(target.expected_sha256):
        return _result(target, destination, root, ok=False, code="invalid_expected_sha256")

    initial_destination = _destination_snapshot(destination)

    try:
        payload = fetch(target.url)
    except RepairFetchError as exc:
        return _result(target, destination, root, ok=False, code=exc.code)
    except TimeoutError:
        return _result(target, destination, root, ok=False, code="fetch_timeout")
    except Exception:
        return _result(target, destination, root, ok=False, code="fetch_failed")

    if not isinstance(payload, (bytes, bytearray)):
        return _result(target, destination, root, ok=False, code="invalid_response_type")
    payload = bytes(payload)
    if not payload:
        return _result(target, destination, root, ok=False, code="empty_response")
    if len(payload) > MAX_RESPONSE_BYTES:
        return _result(target, destination, root, ok=False, code="response_too_large")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    backup: str | None = None
    backup_needs_restore = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=destination.name + ".repair-",
            suffix=destination.suffix,
            dir=destination.parent,
            delete=False,
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name

        corpus_file = probe_file(Path(temporary))
        if not corpus_file.valid:
            return _result(target, destination, root, ok=False, code="invalid_payload")
        payload_hash = hashlib.sha256(payload).hexdigest()
        if target.expected_sha256 and payload_hash != target.expected_sha256:
            return _result(target, destination, root, ok=False, code="sha256_mismatch")
        if target.expected_media_type and corpus_file.media_type != target.expected_media_type:
            return _result(target, destination, root, ok=False, code="media_type_mismatch")
        if verify is None and not target.expected_sha256 and _requires_semantic_verification(
            corpus_file.media_type
        ):
            return _result(
                target,
                destination,
                root,
                ok=False,
                code="semantic_verification_required",
            )
        if verify is not None:
            try:
                verification_code = verify(target, corpus_file, payload)
            except Exception:
                return _result(
                    target,
                    destination,
                    root,
                    ok=False,
                    code="semantic_verification_failed",
                )
            if verification_code:
                return _result(
                    target,
                    destination,
                    root,
                    ok=False,
                    code="semantic_verification_failed",
                )

        final_file = probe_file(Path(temporary))
        if (
            not final_file.valid
            or final_file.size != len(payload)
            or final_file.sha256 != payload_hash
        ):
            return _result(
                target,
                destination,
                root,
                ok=False,
                code="payload_changed_before_replace",
            )

        if _destination_snapshot(destination) != initial_destination:
            return _result(
                target,
                destination,
                root,
                ok=False,
                code="destination_changed_before_replace",
            )

        if initial_destination[0]:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=destination.name + ".repair-backup-",
                suffix=destination.suffix,
                dir=destination.parent,
                delete=False,
            ) as stream:
                backup = stream.name
            os.unlink(backup)
            backup_needs_restore = True
            try:
                os.replace(destination, backup)
            except FileNotFoundError:
                return _result(
                    target,
                    destination,
                    root,
                    ok=False,
                    code="destination_changed_before_replace",
                )
            except OSError:
                return _result(
                    target,
                    destination,
                    root,
                    ok=False,
                    code="replace_failed",
                )

            if _destination_snapshot(Path(backup)) != initial_destination:
                return _result(
                    target,
                    destination,
                    root,
                    ok=False,
                    code="destination_changed_before_replace",
                )

        try:
            os.link(temporary, destination)
        except FileExistsError:
            return _result(
                target,
                destination,
                root,
                ok=False,
                code="destination_changed_before_replace",
            )
        except OSError:
            return _result(target, destination, root, ok=False, code="replace_failed")

        installed = probe_file(destination)
        if (
            not installed.valid
            or installed.size != len(payload)
            or installed.sha256 != payload_hash
        ):
            return _result(
                target,
                destination,
                root,
                ok=False,
                code="destination_changed_before_replace",
            )
        backup_needs_restore = False
        return _result(
            target,
            destination,
            root,
            ok=True,
            code="repaired",
            bytes_written=len(payload),
            sha256=payload_hash,
        )
    finally:
        if backup is not None and backup_needs_restore:
            try:
                restored = _restore_backup_without_overwrite(
                    Path(backup),
                    destination,
                )
            except BaseException:
                restored = False
            if restored:
                backup_needs_restore = False
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        if backup is not None and not backup_needs_restore:
            try:
                os.unlink(backup)
            except FileNotFoundError:
                pass


def repair_targets(
    targets: Sequence[RepairTarget],
    fetch: Fetch | None,
    allowed_root: Path,
    verify: Verifier | None = None,
) -> tuple[RepairResult, ...]:
    root = Path(allowed_root).resolve()
    bounded_fetch = fetch if fetch is not None else RequestsFetcher()
    results: list[RepairResult] = []
    for target in targets:
        try:
            results.append(_repair_one(target, bounded_fetch, root, verify))
        except Exception:
            try:
                destination = target.destination.resolve()
            except Exception:
                destination = root / "invalid-destination"
            results.append(
                _result(target, destination, root, ok=False, code="repair_failed")
            )
    return tuple(results)


def write_repair_report(
    path: Path,
    results: Sequence[RepairResult],
    allowed_root: Path,
) -> Path:
    root = Path(allowed_root).resolve()
    target = Path(path).resolve()
    if not _is_within(target, root) or target == root:
        raise ValueError("report path outside allowed_root")
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = tuple(
        sorted(
            results,
            key=lambda item: (
                item.destination,
                item.source_url,
                item.ok,
                item.code,
                item.bytes_written,
                item.sha256,
            ),
        )
    )
    repaired = sum(item.ok for item in ordered)
    payload = {
        "schema": "current",
        "summary": {
            "attempted": len(ordered),
            "failed": len(ordered) - repaired,
            "repaired": repaired,
        },
        "results": [asdict(item) for item in ordered],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"

    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=target.name + ".",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target
