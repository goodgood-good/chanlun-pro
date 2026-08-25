"""Stable source identity shared by screening, replay and forward review.

Parameters and data hashes cannot distinguish two runs made by different
decision implementations.  This module binds the complete structure/decision
package plus the active QMT and orchestration adapters, and validates archived
snapshots without requiring them to match today's workspace.  Historical
snapshots therefore remain valid evidence, but form a different cohort after
any decision-source change.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version as package_version
from pathlib import Path
import platform
import re
import subprocess
from typing import Mapping

from chanlun.decision_support.fingerprints import sha256_json


DECISION_SOURCE_SNAPSHOT_SCHEMA = "chanlun-decision-source-snapshot"
REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA = "chanlun-replay-decision-source-snapshot"
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTENT_ADDRESSED_APPLICATION_REVISION = re.compile(
    r"^[0-9a-f]{40}\.tree\.[0-9a-f]{24}$"
)
_DEPLOYMENT_APPLICATION_REVISION = re.compile(
    r"^(?P<source>[0-9a-f]{40}\.tree\.[0-9a-f]{24})"
    r"\.run\.[0-9a-f]{32}$"
)
_SOURCE_DIRECTORIES = (
    "src/chanlun/core",
    "src/chanlun/decision_support/trading_system",
)
_SOURCE_FILES = (
    "src/chanlun/exchange/exchange_qmt.py",
    "src/chanlun/exchange/price_basis.py",
    "src/chanlun/exchange/qmt_screening_sector_source.py",
    "tools/audit_qmt_warmup_convergence.py",
    "tools/run_forward_paper.py",
    "tools/snapshot_qmt_gics3_sector_ledger.py",
    "tools/validate_trading_screening_review.py",
    "web/chanlun_chart/cl_app/services/human_review_screening.py",
    "web/chanlun_chart/cl_app/services/trading_screening.py",
    "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
    "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
    "web/chanlun_chart/cl_app/services/trading_screening_process.py",
    "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py",
)
FORWARD_PIPELINE_TOOL_PATHS = (
    "tools/audit_qmt_warmup_convergence.py",
    "tools/run_forward_paper.py",
    "tools/snapshot_qmt_gics3_sector_ledger.py",
    "tools/snapshot_qmt_pit_metadata.py",
)
FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA = "chanlun-forward-implementation-provenance"
# 所有可能改变结构、选股、订单、成交或记账的实现文件，都必须能使历史回放失效。
# 但回放进程从未导入的下游前向复核持久化与界面适配器，不应导致回放失效；
# 它们仍由上方完整集成快照覆盖。
_REPLAY_SOURCE_DIRECTORIES = (
    "src/chanlun/core",
    "src/chanlun/decision_support/trading_system/backtest",
)
_REPLAY_TRADING_SYSTEM_DIRECTORY = "src/chanlun/decision_support/trading_system"
_REPLAY_EXCLUDED_TRADING_SYSTEM_FILES = frozenset(
    {
        "candidate_warmup_diagnostics.py",
        "forward_warmup_structure_lineage.py",
        "human_paper_accounting.py",
        "human_paper_ledger.py",
        "human_paper_valuation.py",
        "forward_paper.py",
        "forward_review_markout.py",
        "live_human_review.py",
    }
)
_REPLAY_SOURCE_FILES = (
    "src/chanlun/__init__.py",
    "src/chanlun/config.py",
    "src/chanlun/decision_support/__init__.py",
    "src/chanlun/decision_support/corpus_types.py",
    "src/chanlun/decision_support/fingerprints.py",
    "src/chanlun/exchange/__init__.py",
    "src/chanlun/exchange/exchange.py",
    "src/chanlun/exchange/exchange_qmt.py",
    "src/chanlun/exchange/kline_precision.py",
    "src/chanlun/exchange/price_basis.py",
    "src/chanlun/exchange/qmt_screening_sector_source.py",
    "src/chanlun/market.py",
    "src/chanlun/tools/__init__.py",
    "src/chanlun/tools/log_util.py",
    "tools/qmt_research_contract.py",
    "tools/backtest_qmt_fixed_year.py",
    "tools/audit_qmt_prefix_invariance.py",
    "tools/finalize_qmt_pit_fixed_year.py",
    "tools/snapshot_qmt_pit_metadata.py",
    "tools/snapshot_qmt_gics3_sector_ledger.py",
)


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def calculate_forward_application_source_revision(
    project_root: Path | None = None,
) -> str:
    """Hash the exact on-disk implementation used by Capture and Evaluate.

    This deliberately includes tracked modifications, untracked source files
    and tracked deletions.  A Git commit alone cannot prove that two scheduled
    invocations executed the same dirty working tree.
    """

    root = _default_project_root() if project_root is None else project_root.resolve()

    def git(*arguments: str, stdin: str | None = None) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or "git command failed"
            raise RuntimeError(message[:240])
        return completed.stdout

    head = git("rev-parse", "HEAD").strip()
    if not head:
        raise RuntimeError("forward application git revision is unavailable")
    paths = set(
        git(
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "web/chanlun_chart",
            "ops",
            "windows_run.bat",
            *FORWARD_PIPELINE_TOOL_PATHS,
        ).splitlines()
    )
    runtime_config = "src/chanlun/config.py"
    if (root / runtime_config).is_file():
        paths.add(runtime_config)
    ordered_paths = tuple(sorted(value for value in paths if value))
    existing = tuple(value for value in ordered_paths if (root / value).is_file())
    hash_by_path = {
        value: hashlib.sha256((root / value).read_bytes()).hexdigest()
        for value in existing
    }
    manifest = [f"HEAD\t{head}"]
    manifest.extend(
        f"{path}\t{hash_by_path.get(path, 'deleted')}" for path in ordered_paths
    )
    digest = hashlib.sha256("\n".join(manifest).encode("utf-8")).hexdigest()
    return f"{head}.tree.{digest[:24]}"


def is_content_addressed_application_source_revision(value: object) -> bool:
    """Return whether ``value`` proves the exact on-disk application tree."""

    return bool(
        isinstance(value, str)
        and _CONTENT_ADDRESSED_APPLICATION_REVISION.fullmatch(value.strip())
    )


def content_addressed_source_revision_from_build(
    value: object,
) -> str | None:
    """Resolve the exact source identity from a current build identity.

    A direct launch uses the source identity itself.  The deployment wrapper
    appends a 32-hex run nonce so readiness can distinguish consecutive
    processes built from the same bytes.  No other historical or informal
    build-revision shape is accepted.
    """

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if _CONTENT_ADDRESSED_APPLICATION_REVISION.fullmatch(normalized):
        return normalized
    match = _DEPLOYMENT_APPLICATION_REVISION.fullmatch(normalized)
    return None if match is None else match.group("source")


def forward_implementation_provenance_document(
    *,
    application_source_revision: str,
    project_root: Path | None = None,
) -> dict[str, object]:
    """Build the common content-addressed Capture/Evaluate identity."""

    root = _default_project_root() if project_root is None else project_root.resolve()
    stable: dict[str, object] = {
        "schema": FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
        "application_source_revision": application_source_revision,
        "forward_scheduler_module_sha256": _sha256_file(
            root
            / "web"
            / "chanlun_chart"
            / "cl_app"
            / "services"
            / "app_forward_scheduler.py"
        ),
        "forward_python_tool_sha256": _sha256_file(
            root / "tools" / "run_forward_paper.py"
        ),
        "sector_capture_tool_sha256": _sha256_file(
            root / "tools" / "snapshot_qmt_gics3_sector_ledger.py"
        ),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "pandas_version": package_version("pandas"),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def current_forward_implementation_provenance(
    project_root: Path | None = None,
) -> dict[str, object]:
    """Recompute the executable identity from disk without a process cache."""

    root = _default_project_root() if project_root is None else project_root.resolve()
    return forward_implementation_provenance_document(
        application_source_revision=calculate_forward_application_source_revision(root),
        project_root=root,
    )


def _snapshot_from_paths(
    root: Path,
    *,
    schema: str,
    paths: set[Path],
) -> dict[str, object]:
    files = tuple(
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in sorted(paths, key=lambda value: value.as_posix())
    )
    stable: dict[str, object] = {"schema": schema, "files": files}
    return {**stable, "aggregate_sha256": sha256_json(stable)}


def current_decision_source_snapshot(
    project_root: Path | None = None,
) -> dict[str, object]:
    """Hash every source file that can alter screening or replay decisions."""

    root = _default_project_root() if project_root is None else project_root.resolve()
    paths: set[Path] = set()
    for relative in _SOURCE_DIRECTORIES:
        directory = root / relative
        if not directory.is_dir():
            raise FileNotFoundError(f"decision source directory is missing: {relative}")
        paths.update(
            path.resolve() for path in directory.rglob("*.py") if path.is_file()
        )
    for relative in _SOURCE_FILES:
        path = (root / relative).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"decision source file is missing: {relative}")
        paths.add(path)
    return _snapshot_from_paths(
        root,
        schema=DECISION_SOURCE_SNAPSHOT_SCHEMA,
        paths=paths,
    )


def current_replay_decision_source_snapshot(
    project_root: Path | None = None,
) -> dict[str, object]:
    """Hash the dependency scope that can alter historical replay outputs.

    Complete forward integration identity deliberately remains a separate
    contract.  This prevents edits to paper-ledger persistence or web adapters
    from falsely invalidating a twelve-minute historical replay that never
    imports those modules.
    """

    root = _default_project_root() if project_root is None else project_root.resolve()
    paths: set[Path] = set()
    for relative in _REPLAY_SOURCE_DIRECTORIES:
        directory = root / relative
        if not directory.is_dir():
            raise FileNotFoundError(f"replay source directory is missing: {relative}")
        paths.update(
            path.resolve() for path in directory.rglob("*.py") if path.is_file()
        )
    trading_system = root / _REPLAY_TRADING_SYSTEM_DIRECTORY
    if not trading_system.is_dir():
        raise FileNotFoundError(
            "replay source directory is missing: " + _REPLAY_TRADING_SYSTEM_DIRECTORY
        )
    paths.update(
        path.resolve()
        for path in trading_system.glob("*.py")
        if path.is_file() and path.name not in _REPLAY_EXCLUDED_TRADING_SYSTEM_FILES
    )
    for relative in _REPLAY_SOURCE_FILES:
        path = (root / relative).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"replay source file is missing: {relative}")
        paths.add(path)
    return _snapshot_from_paths(
        root,
        schema=REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA,
        paths=paths,
    )


def _source_snapshot_id(
    value: object,
    *,
    expected_schema: str,
    label: str,
) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is missing")
    if set(value) != {"schema", "files", "aggregate_sha256"}:
        raise ValueError(f"{label} shape changed")
    raw_files = value.get("files")
    if (
        value.get("schema") != expected_schema
        or not isinstance(raw_files, (tuple, list))
        or not raw_files
    ):
        raise ValueError(f"{label} contract changed")
    files: list[dict[str, str]] = []
    for raw in raw_files:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
            raise ValueError(f"{label} file entry is malformed")
        path = raw.get("path")
        digest = raw.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path == "."
            or "\\" in path
            or path.startswith("/")
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or _SHA256_ID.fullmatch(digest) is None
        ):
            raise ValueError(f"{label} file identity is invalid")
        files.append({"path": path, "sha256": digest})
    paths = tuple(item["path"] for item in files)
    if paths != tuple(sorted(set(paths))):
        raise ValueError(f"{label} files are not canonical")
    stable: dict[str, object] = {
        "schema": expected_schema,
        "files": tuple(files),
    }
    expected = sha256_json(stable)
    if value.get("aggregate_sha256") != expected:
        raise ValueError(f"{label} aggregate changed")
    return expected


def decision_source_snapshot_id(value: object) -> str:
    """Validate an archived snapshot and return its stable aggregate identity."""

    return _source_snapshot_id(
        value,
        expected_schema=DECISION_SOURCE_SNAPSHOT_SCHEMA,
        label="decision source snapshot",
    )


def replay_decision_source_snapshot_id(value: object) -> str:
    """Validate a historical-replay dependency snapshot."""

    return _source_snapshot_id(
        value,
        expected_schema=REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA,
        label="replay decision source snapshot",
    )


def decision_source_snapshot_matches_current(
    value: object,
    project_root: Path | None = None,
) -> bool:
    """Return whether a valid archive exactly equals the current workspace."""

    try:
        archived = decision_source_snapshot_id(value)
        current = current_decision_source_snapshot(project_root)
    except (OSError, TypeError, ValueError):
        return False
    # JSON 往返会把元组变成列表。聚合基于规范 JSON 表示，因此独立校验后的
    # 标识相等既更严格，也不受内存表示形式影响。
    return archived == current["aggregate_sha256"]


def replay_decision_source_snapshot_matches_current(
    value: object,
    project_root: Path | None = None,
) -> bool:
    """Return whether an archived replay cohort matches replay dependencies."""

    try:
        archived = replay_decision_source_snapshot_id(value)
        current = current_replay_decision_source_snapshot(project_root)
    except (OSError, TypeError, ValueError):
        return False
    return archived == current["aggregate_sha256"]


__all__ = (
    "DECISION_SOURCE_SNAPSHOT_SCHEMA",
    "FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA",
    "FORWARD_PIPELINE_TOOL_PATHS",
    "REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA",
    "calculate_forward_application_source_revision",
    "content_addressed_source_revision_from_build",
    "current_forward_implementation_provenance",
    "current_decision_source_snapshot",
    "current_replay_decision_source_snapshot",
    "decision_source_snapshot_id",
    "decision_source_snapshot_matches_current",
    "forward_implementation_provenance_document",
    "is_content_addressed_application_source_revision",
    "replay_decision_source_snapshot_id",
    "replay_decision_source_snapshot_matches_current",
)
