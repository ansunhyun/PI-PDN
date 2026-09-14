"""Atomic public outputs: direct batch files, native archives and JPEG captures.

Internal native solver/PNG evidence remains fully validated before publication.
Expanded solver projects and trees remain in work; JSON/CSV are preserved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid

try:
    from .preprocess.public_artifacts import jpeg_bytes, validate_reference_archive, validate_public_stackup, validate_stackup_xml
    from .marker_evaluation import evaluate_center_markers, aggregate_status
except ImportError:
    from preprocess.public_artifacts import jpeg_bytes, validate_reference_archive, validate_public_stackup, validate_stackup_xml
    from marker_evaluation import evaluate_center_markers, aggregate_status
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

try:
    from .admin_config import normalize_pcb_capture_policy
except ImportError:  # Support direct execution from the customer core folder.
    from admin_config import normalize_pcb_capture_policy  # type: ignore[no-redef]

try:
    from .result_export import (
        WEB_RESULT_FILENAMES,
        BatchExecutionInput,
        export_eden_web_results,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from result_export import (  # type: ignore[no-redef]
        WEB_RESULT_FILENAMES,
        BatchExecutionInput,
        export_eden_web_results,
    )

try:
    from .tdr_runtime import (
        StrictTdrRuntimeError,
        validate_strict_tdr_artifact_package,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from tdr_runtime import (  # type: ignore[no-redef]
        StrictTdrRuntimeError,
        validate_strict_tdr_artifact_package,
    )

try:
    from .preprocess.reference_preprocessor import (
        ReferencePreprocessError,
        load_reference_preprocess_manifest,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from preprocess.reference_preprocessor import (  # type: ignore[no-redef]
        ReferencePreprocessError,
        load_reference_preprocess_manifest,
    )

try:
    from .pcb_capture import (
        JOB_OVERVIEW_DIRNAME,
        PcbCaptureContractError,
        validate_job_overview_package,
        validate_strict_capture_package,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from pcb_capture import (  # type: ignore[no-redef]
        JOB_OVERVIEW_DIRNAME,
        PcbCaptureContractError,
        validate_job_overview_package,
        validate_strict_capture_package,
    )

try:
    from .customer_pre_solve import (
        CustomerPreSolveError,
        validate_customer_pre_solve,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from customer_pre_solve import (  # type: ignore[no-redef]
        CustomerPreSolveError,
        validate_customer_pre_solve,
    )

try:
    from .channel.array_model_result import (
        ARRAY_MODEL_RESULT_SUFFIX,
        ArrayModelResultError,
        build_customer_array_model_result,
        validate_customer_array_model_result,
    )
except ImportError:  # Support direct execution from the customer core folder.
    from channel.array_model_result import (  # type: ignore[no-redef]
        ARRAY_MODEL_RESULT_SUFFIX,
        ArrayModelResultError,
        build_customer_array_model_result,
        validate_customer_array_model_result,
    )


PROVISIONAL_PUBLISH_CONTRACT = "si-tdr-provisional-fullbatch-publish/7"
PUBLISH_ATTEMPT_SCHEMA = "si-tdr-publish-attempt/1"
PUBLISH_CONTRACT_STATUS = "eden-05-atomic-attempt-state-machine-license-free-validated"
PUBLISH_SAFETY_POLICY = (
    "fail-if-outputs-exists; actual-same-volume-device-check; "
    "atomic-directory-rename; post-rename-exact-validation"
)
SNAPSHOT_SCHEMA = "si-tdr-directory-snapshots/1"
STRICT_TDR_MODE = "customer-strict-native-aedt-tdr"
STRICT_SYZ_SCHEMA = "si-tdr-strict-syz-solve/1"
STRICT_PCB_SCHEMA = "si-tdr-strict-pcb-capture-manifest/2"
STRICT_PCB_MODE = "customer-strict-snp-batch-union"
# 배치 PCB 캡처는 그 sNp 채널 경로 한 장이다(9.4a).
STRICT_PCB_CAPTURE_VIEW = "route"
PUBLIC_TDR_IMAGE_SUFFIX = "_TDR.jpg"
PUBLIC_TDR_CSV_SUFFIX = "_TDR.csv"
PUBLIC_PCB_IMAGE_SUFFIX = "_PCB_Capture.jpg"
# Job 단위 전체 보드 overview는 outputs 루트에 앞/뒷면 1쌍으로 공개된다(9.4b).
PUBLIC_PCB_TOP_IMAGE = "top.jpg"
PUBLIC_PCB_BOTTOM_IMAGE = "bottom.jpg"
PUBLIC_OVERVIEW_IMAGE_NAMES = (PUBLIC_PCB_TOP_IMAGE, PUBLIC_PCB_BOTTOM_IMAGE)
# 9.4.6: 비압축 SIWave 세트(<Batch>.siw + <Batch>.siwaveresults/)와 참조 보드
# (<ref>.siw + <ref>.aedb/)는 공개 대상이다.  .siw는 아래 blanket 거부에서
# 빠지는 대신 validate_staged_publish_tree가 허용 위치를 정확히 고정한다.
FORBIDDEN_PUBLIC_SUFFIXES = {
    ".snp", ".siw", ".aedt", ".png",
    ".pyaedt",
}
FORBIDDEN_PUBLIC_DIRECTORY_NAMES = {
    ".aedtresults",
    ".aedb",
    "artifacts",
    "evidence",
    "generated_config",
    "manifests",
    "pcb",
    "reports",
    "temp",
    "tmp",
    "cache",
    "backup",
    "view_states",
    "work",
} | {
    name.casefold() for name in WEB_RESULT_FILENAMES
}
FORBIDDEN_TREE_SUFFIXES = {
    ".lock",
    ".tmp",
    ".temp",
    ".bak",
    ".backup",
    ".cache",
    ".pyaedt",
    ".siwaveresults",
}
# Ansys가 스스로 만드는 결과 디렉터리. 내부에 edb.def.bak이나 svcache/*.tmp 같은
# 자체 구성원을 포함하므로 이 트리 안에서는 temp/backup 이름 규칙을 적용하지 않는다.
TOOL_INTERNAL_TREE_SUFFIXES = (".aedb", ".aedtresults", ".siwaveresults")
# FB-06 customer_pre_solve.directory_evidence와 같은 규칙. EDB가 남기는 0바이트
# edb.def.tmp는 양쪽 모두에서 존재하지 않는 것으로 다룬다.
EDB_IGNORED_EMPTY_SIDECAR = "edb.def.tmp"
# Ansys 트리 안에서도 계속 거부하는 접미사. AEDT가 만드는 temp/cache/.tmp/.bak과
# 달리 lock 파일은 프로젝트가 열려 있다는 뜻이다.
TOOL_INTERNAL_FORBIDDEN_SUFFIXES = {".lock"}


def _is_ignored_edb_sidecar(name: str, size: int) -> bool:
    return name.casefold() == EDB_IGNORED_EMPTY_SIDECAR and size == 0
FORBIDDEN_TREE_DIRECTORY_NAMES = {
    "temp",
    "tmp",
    "cache",
    "backup",
    ".cache",
}
WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
SAFE_PUBLIC_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class FullBatchPublishError(RuntimeError):
    """Raised when a strict run cannot be safely published."""


class ExistingOutputsConflict(FullBatchPublishError):
    """Raised when publishing would overwrite an existing outputs tree."""


@dataclass(frozen=True)
class FullBatchPublishResult:
    contract: str
    output_dir: Path
    manifest_path: Path
    public_files: tuple[str, ...]


_ATTEMPT_TRANSITIONS = {
    "building": {"building", "validation-failed", "validated-ready-to-publish"},
    "validated-ready-to-publish": {"validated-ready-to-publish", "publish-failed", "published"},
    "validation-failed": set(),
    "publish-failed": set(),
    "published": set(),
}


def _attempt_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_attempt_error(exc: BaseException, *, phase: str) -> dict[str, str]:
    message = " ".join(str(exc).split())[:2048]
    if not message:
        message = "operation failed without a diagnostic message"
    return {
        "type": type(exc).__name__,
        "phase": phase,
        "message": message,
    }


class _PublishAttemptManifest:
    """Atomically persist the work-only EDEN publish-attempt state machine."""

    def __init__(self, path: Path, payload: dict[str, Any]):
        self.path = path
        self.payload = payload
        self._failure_context: Callable[[], Mapping[str, Any]] | None = None
        _write_json_atomic(self.path, self.payload)

    @classmethod
    def create(
        cls,
        *,
        attempt_dir: Path,
        request_path: Path,
        outputs_dir: Path,
        staging_tree: Path,
    ) -> "_PublishAttemptManifest":
        created_at = _attempt_timestamp()
        payload: dict[str, Any] = {
            "attemptSchema": PUBLISH_ATTEMPT_SCHEMA,
            "contract": PROVISIONAL_PUBLISH_CONTRACT,
            "contractStatus": PUBLISH_CONTRACT_STATUS,
            "attemptId": attempt_dir.name,
            "status": "building",
            "phase": "attempt-created",
            "createdAt": created_at,
            "updatedAt": created_at,
            "requestPath": str(request_path),
            "outputsPath": str(outputs_dir),
            "stagingTree": str(staging_tree),
            "stagingTreeState": "not-created",
            "safetyPolicy": PUBLISH_SAFETY_POLICY,
            "statusHistory": [
                {
                    "status": "building",
                    "phase": "attempt-created",
                    "at": created_at,
                }
            ],
        }
        return cls(attempt_dir / "publish_manifest.json", payload)

    def set_failure_context(
        self, provider: Callable[[], Mapping[str, Any]]
    ) -> None:
        self._failure_context = provider

    def transition(
        self,
        status: str,
        *,
        phase: str,
        updates: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        current = str(self.payload.get("status") or "")
        if status not in _ATTEMPT_TRANSITIONS.get(current, set()):
            raise FullBatchPublishError(
                f"invalid publish-attempt state transition: {current!r} -> {status!r}"
            )
        timestamp = _attempt_timestamp()
        if updates:
            self.payload.update(dict(updates))
        self.payload.update(
            {
                "status": status,
                "phase": phase,
                "updatedAt": timestamp,
            }
        )
        if error is not None:
            self.payload["error"] = _safe_attempt_error(error, phase=phase)
        self.payload.setdefault("statusHistory", []).append(
            {"status": status, "phase": phase, "at": timestamp}
        )
        _write_json_atomic(self.path, self.payload)

    def run_build_phase(
        self,
        phase: str,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.payload.get("phase") != phase:
            self.transition("building", phase=phase)
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            updates: dict[str, Any] = {
                "stagingTreeState": (
                    "partial-preserved"
                    if Path(str(self.payload["stagingTree"])).exists()
                    else "not-created"
                )
            }
            if self._failure_context is not None:
                try:
                    updates["partialBuildEvidence"] = dict(self._failure_context())
                except Exception:
                    updates["partialBuildEvidence"] = {
                        "status": "unavailable-during-failure-reporting"
                    }
            self.transition(
                "validation-failed",
                phase=phase,
                updates=updates,
                error=exc,
            )
            raise


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FullBatchPublishError(f"{label} is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FullBatchPublishError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FullBatchPublishError(f"{label} root must be an object: {path}")
    return payload


def _fullbatch_capture_policy(
    batches: Sequence[BatchExecutionInput], *, job_root: Path
) -> dict[str, dict[str, bool]]:
    """Resolve one capture policy and reject per-batch configuration drift."""

    resolved: dict[str, dict[str, bool]] | None = None
    for batch in batches:
        config_path = _contained_path(
            batch.config_path,
            root=job_root,
            label="strict batch Run Config",
        )
        config = _json_object(config_path, label="strict batch Run Config")
        try:
            policy = normalize_pcb_capture_policy(
                config.get("pcbCapture"), allow_extra_fields=True
            )
        except ValueError as exc:
            raise FullBatchPublishError(
                f"strict batch PCB capture policy is invalid: {config_path}: {exc}"
            ) from exc
        if resolved is None:
            resolved = policy
        elif policy != resolved:
            raise FullBatchPublishError(
                "strict FullBatch PCB capture policy differs between batches"
            )
    if resolved is None:
        raise FullBatchPublishError("strict FullBatch has no PCB capture policy")
    return resolved


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise FullBatchPublishError(f"artifact path cannot be inspected: {path}") from exc
    return path.is_symlink() or bool(
        int(getattr(stat, "st_file_attributes", 0))
        & WINDOWS_REPARSE_POINT_ATTRIBUTE
    )


def _validate_tree_component(
    name: str,
    *,
    label: str,
    is_directory: bool,
    allow_tool_internal: bool = False,
) -> None:
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(ord(character) < 32 for character in name)
        or any(character in '<>:"/\\|?*' for character in name)
    ):
        raise FullBatchPublishError(f"{label} contains an unsafe path component: {name!r}")
    if _is_windows_reserved_name(name):
        raise FullBatchPublishError(
            f"{label} contains a Windows reserved path component: {name!r}"
        )
    folded = name.casefold()
    suffix = Path(name).suffix.casefold()
    if is_directory and folded.endswith(".pyaedt"):
        raise FullBatchPublishError(f"{label} contains a forbidden temp/cache member: {name!r}")
    if is_directory and folded.endswith(TOOL_INTERNAL_TREE_SUFFIXES):
        # 공개 대상 Ansys 트리 루트(.aedb/.aedtresults/.siwaveresults)는 허용하고
        # 내부는 tool-internal 규칙(lock 거부 유지)으로 걷는다.  구조 자체는
        # exact allowlist가 고정한다.
        if suffix in TOOL_INTERNAL_FORBIDDEN_SUFFIXES or name.endswith("~"):
            raise FullBatchPublishError(
                f"{label} contains a forbidden temp/cache member: {name!r}"
            )
        return
    if allow_tool_internal:
        # Ansys 트리 안에서도 lock 파일은 거부한다. 열려 있는 프로젝트를 공개하면
        # 재현 불가능한 결과가 나간다.
        if suffix in TOOL_INTERNAL_FORBIDDEN_SUFFIXES or name.endswith("~"):
            raise FullBatchPublishError(
                f"{label} contains a forbidden temp/cache member: {name!r}"
            )
        return
    if (
        folded in FORBIDDEN_TREE_DIRECTORY_NAMES
        or suffix in FORBIDDEN_TREE_SUFFIXES
        or name.endswith("~")
    ):
        raise FullBatchPublishError(f"{label} contains a forbidden temp/cache member: {name!r}")


def _directory_tree_evidence(
    path: Path,
    *,
    job_root: Path | None,
    label: str,
    started_at_ns: int | None = None,
    require_edb_def: bool = False,
) -> dict[str, Any]:
    raw = path
    if _is_link_or_reparse(raw):
        raise FullBatchPublishError(f"{label} cannot be a symlink or reparse point: {raw}")
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise FullBatchPublishError(f"{label} cannot be resolved: {raw}") from exc
    if not root.is_dir():
        raise FullBatchPublishError(f"{label} is not a directory: {root}")
    if job_root is not None:
        contained_root = job_root.resolve()
        if contained_root != root and contained_root not in root.parents:
            raise FullBatchPublishError(f"{label} is outside the Job root: {root}")

    directories: list[str] = []
    files: list[dict[str, Any]] = []
    folded_paths: dict[str, str] = {}
    def _is_tool_internal(name: str) -> bool:
        return name.casefold().endswith(TOOL_INTERNAL_TREE_SUFFIXES)

    stack = [(root, _is_tool_internal(root.name))]
    while stack:
        current, tool_internal = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise FullBatchPublishError(f"{label} cannot be enumerated: {current}") from exc
        sibling_names: dict[str, str] = {}
        child_directories: list[tuple[Path, bool]] = []
        for entry in entries:
            folded_name = entry.name.casefold()
            if folded_name in sibling_names:
                raise FullBatchPublishError(
                    f"{label} has a case-insensitive sibling collision: "
                    f"{sibling_names[folded_name]!r}, {entry.name!r}"
                )
            sibling_names[folded_name] = entry.name
            child = Path(entry.path)
            if _is_link_or_reparse(child):
                raise FullBatchPublishError(
                    f"{label} contains a symlink or reparse point: {child}"
                )
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise FullBatchPublishError(f"{label} member cannot be inspected: {child}") from exc
            _validate_tree_component(
                entry.name,
                label=label,
                is_directory=is_directory,
                allow_tool_internal=tool_internal,
            )
            try:
                relative = child.relative_to(root).as_posix()
            except ValueError as exc:
                raise FullBatchPublishError(f"{label} member escapes its root: {child}") from exc
            pure = PurePosixPath(relative)
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                raise FullBatchPublishError(f"{label} member path is unsafe: {relative!r}")
            folded_relative = relative.casefold()
            prior = folded_paths.get(folded_relative)
            if prior is not None:
                raise FullBatchPublishError(
                    f"{label} has a case-insensitive path collision: {prior!r}, {relative!r}"
                )
            folded_paths[folded_relative] = relative
            if is_directory:
                directories.append(relative)
                child_directories.append(
                    (child, tool_internal or _is_tool_internal(entry.name))
                )
                continue
            if not is_file:
                raise FullBatchPublishError(f"{label} contains a non-file member: {child}")
            stat = child.stat()
            if tool_internal and _is_ignored_edb_sidecar(entry.name, stat.st_size):
                folded_paths.pop(folded_relative, None)
                continue
            if stat.st_size <= 0 and not tool_internal:
                raise FullBatchPublishError(f"{label} contains an empty file: {child}")
            if started_at_ns is not None and stat.st_mtime_ns < started_at_ns:
                raise FullBatchPublishError(f"{label} contains a stale file: {child}")
            files.append(
                {
                    "relativePath": relative,
                    "size": stat.st_size,
                    "sha256": _sha256(child),
                }
            )
        stack.extend(reversed(child_directories))

    directories.sort()
    files.sort(key=lambda item: str(item["relativePath"]))
    if not files:
        raise FullBatchPublishError(f"{label} is empty: {root}")
    if require_edb_def:
        edb_def = next(
            (item for item in files if item["relativePath"] == "edb.def"),
            None,
        )
        if edb_def is None or int(edb_def["size"]) <= 0:
            raise FullBatchPublishError(f"{label} requires a non-empty root edb.def: {root}")
    return {
        "path": str(root),
        "directories": directories,
        "fileManifest": files,
        "fileCount": len(files),
        "totalBytes": sum(int(item["size"]) for item in files),
        "treeSha256": hashlib.sha256(
            _canonical_json(
                {"directories": directories, "fileManifest": files}
            ).encode("utf-8")
        ).hexdigest(),
    }


def _contained_path(path: Path, *, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FullBatchPublishError(f"{label} cannot be resolved: {path}") from exc
    if root != resolved and root not in resolved.parents:
        raise FullBatchPublishError(f"{label} is outside the Job root: {resolved}")
    return resolved


def _source_file(
    value: Any,
    *,
    run_dir: Path,
    job_root: Path,
    label: str,
    started_at_ns: int,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FullBatchPublishError(f"strict FullBatch is missing {label}")
    raw = Path(value)
    path = raw if raw.is_absolute() else run_dir / raw
    if _is_link_or_reparse(path):
        raise FullBatchPublishError(
            f"strict FullBatch artifact cannot be a symlink or reparse point: "
            f"{label}: {path}"
        )
    resolved = _contained_path(path, root=job_root, label=label)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise FullBatchPublishError(f"strict FullBatch artifact is empty: {label}: {resolved}")
    if resolved.stat().st_mtime_ns < started_at_ns:
        raise FullBatchPublishError(f"strict FullBatch artifact is stale: {label}: {resolved}")
    return resolved


def _same_path(value: Any, expected: Path, *, run_dir: Path, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FullBatchPublishError(f"strict completion record is missing {label}")
    raw = Path(value)
    actual = (raw if raw.is_absolute() else run_dir / raw).resolve()
    if actual != expected.resolve():
        raise FullBatchPublishError(
            f"strict completion record {label} path mismatch: "
            f"recorded={actual}, expected={expected.resolve()}"
        )


def _recorded_file(
    evidence: Any,
    *,
    expected: Path,
    run_dir: Path,
    job_root: Path,
    label: str,
    started_at_ns: int,
    size_key: str,
) -> Path:
    if not isinstance(evidence, Mapping):
        raise FullBatchPublishError(f"strict completion evidence is missing: {label}")
    path = _source_file(
        evidence.get("path"),
        run_dir=run_dir,
        job_root=job_root,
        label=label,
        started_at_ns=started_at_ns,
    )
    if path != expected.resolve():
        raise FullBatchPublishError(
            f"strict completion artifact path mismatch for {label}: "
            f"recorded={path}, expected={expected.resolve()}"
        )
    if "exists" in evidence and evidence.get("exists") is not True:
        raise FullBatchPublishError(f"strict completion evidence says artifact is absent: {label}")
    recorded_size = evidence.get(size_key)
    if isinstance(recorded_size, bool) or not isinstance(recorded_size, int):
        raise FullBatchPublishError(f"strict completion evidence has no valid size: {label}")
    actual_size = path.stat().st_size
    if recorded_size != actual_size:
        raise FullBatchPublishError(
            f"strict completion artifact size mismatch for {label}: "
            f"recorded={recorded_size}, actual={actual_size}"
        )
    recorded_hash = evidence.get("sha256")
    if not isinstance(recorded_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", recorded_hash):
        raise FullBatchPublishError(f"strict completion evidence has no valid SHA-256: {label}")
    actual_hash = _sha256(path)
    if recorded_hash.casefold() != actual_hash:
        raise FullBatchPublishError(
            f"strict completion artifact SHA-256 mismatch for {label}: {path}"
        )
    return path


def _validate_syz_completion(
    *,
    run_dir: Path,
    config: Mapping[str, Any],
    batch_name: str,
    target_edb: Path,
    pre_solve_completion: Mapping[str, Any],
    job_root: Path,
    started_at_ns: int,
) -> tuple[Path, dict[str, Any]]:
    record_path = run_dir / "channel_solve.json"
    _source_file(
        str(record_path),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} SYZ completion record",
        started_at_ns=started_at_ns,
    )
    record = _json_object(record_path, label="strict SYZ completion record")
    if record.get("schema") != STRICT_SYZ_SCHEMA or record.get("status") != "ok":
        raise FullBatchPublishError(
            f"strict SYZ did not complete successfully for batch {batch_name}"
        )
    embedded_pre_solve = record.get("customerPreSolveValidation") or {}
    if (
        not isinstance(embedded_pre_solve, Mapping)
        or embedded_pre_solve.get("batchId") != batch_name
        or _canonical_json(embedded_pre_solve.get("targetEdb"))
        != _canonical_json(pre_solve_completion.get("targetEdb"))
    ):
        raise FullBatchPublishError(
            f"strict SYZ pre-solve target AEDB evidence differs for batch {batch_name}"
        )
    setup_path = run_dir / "syz_setup.json"
    _source_file(
        str(setup_path),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} SYZ setup record",
        started_at_ns=started_at_ns,
    )
    setup = _json_object(setup_path, label="strict SYZ setup record")
    if setup.get("schema") != "si-tdr-strict-syz-setup/1" or setup.get("status") != "ok":
        raise FullBatchPublishError(
            f"strict SYZ setup did not complete successfully for batch {batch_name}"
        )
    _same_path(
        setup.get("targetEdb"),
        target_edb,
        run_dir=run_dir,
        label="syz_setup.targetEdb",
    )
    solver = record.get("solver") or {}
    if not isinstance(solver, Mapping) or solver.get("returnCode") != 0:
        raise FullBatchPublishError(
            f"strict SYZ solver return evidence is not successful for batch {batch_name}"
        )
    ports = config.get("ports") or {}
    if not isinstance(ports, Mapping):
        raise FullBatchPublishError(f"strict Run Config ports is invalid: {batch_name}")
    port_count = ports.get("touchstonePortCount")
    port_order = ports.get("portOrder")
    if (
        isinstance(port_count, bool)
        or not isinstance(port_count, int)
        or port_count <= 0
        or not isinstance(port_order, list)
        or len(port_order) != port_count
    ):
        raise FullBatchPublishError(
            f"strict Run Config port count/order is incomplete for batch {batch_name}"
        )

    expected_touchstone = (run_dir / "touchstone" / f"{batch_name}.s{port_count}p").resolve()
    expected_siw = (run_dir / f"{batch_name}.siw").resolve()
    expected_siwz = (run_dir / f"{batch_name}.siwz").resolve()
    artifacts = record.get("artifacts") or {}
    if not isinstance(artifacts, Mapping):
        raise FullBatchPublishError(f"strict SYZ artifacts is invalid: {batch_name}")
    touchstone = _recorded_file(
        artifacts.get("touchstone"),
        expected=expected_touchstone,
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} solved Touchstone",
        started_at_ns=started_at_ns,
        size_key="sizeBytes",
    )
    siw = _recorded_file(
        artifacts.get("siw"),
        expected=expected_siw,
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} solved SIW",
        started_at_ns=started_at_ns,
        size_key="sizeBytes",
    )
    siwz = _recorded_file(
        artifacts.get("siwz"),
        expected=expected_siwz,
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} solved SIWZ",
        started_at_ns=started_at_ns,
        size_key="sizeBytes",
    )
    touchstone_evidence = artifacts.get("touchstone") or {}
    if (
        touchstone_evidence.get("portCount") != port_count
        or touchstone_evidence.get("portOrder") != port_order
    ):
        raise FullBatchPublishError(
            f"strict SYZ Touchstone port evidence differs from Run Config: {batch_name}"
        )
    exported = record.get("exportedTouchstoneFiles")
    if not isinstance(exported, list) or len(exported) != 1:
        raise FullBatchPublishError(
            f"strict SYZ must export exactly one Touchstone for batch {batch_name}"
        )
    for field, expected in (
        ("requestedTouchstone", touchstone),
        ("requestedSiw", siw),
        ("requestedSiwz", siwz),
    ):
        _same_path(record.get(field), expected, run_dir=run_dir, label=field)
    _same_path(exported[0], touchstone, run_dir=run_dir, label="exportedTouchstoneFiles[0]")
    # 9.4.6: 비압축 SIWave 결과 트리(<Batch>.siwaveresults)도 공개 소스다.
    siwave_results = _validate_results_directory(
        artifacts.get("siwaveResults"),
        expected=(run_dir / f"{batch_name}.siwaveresults"),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} SIWave results",
        started_at_ns=started_at_ns,
    )
    return touchstone, {
        "record": str(record_path.resolve()),
        "setupRecord": str(setup_path.resolve()),
        "schema": STRICT_SYZ_SCHEMA,
        "touchstone": str(touchstone),
        "siw": str(siw),
        "siwz": str(siwz),
        "siwaveResults": str(siwave_results),
        "portCount": port_count,
        "targetEdb": str(target_edb),
    }


def _validate_results_directory(
    evidence: Any,
    *,
    expected: Path,
    run_dir: Path,
    job_root: Path,
    label: str,
    started_at_ns: int,
) -> Path:
    if not isinstance(evidence, Mapping):
        raise FullBatchPublishError(f"strict completion evidence is missing: {label}")
    path_value = evidence.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise FullBatchPublishError(f"strict completion evidence has no path: {label}")
    raw = Path(path_value)
    path = _contained_path(
        raw if raw.is_absolute() else run_dir / raw,
        root=job_root,
        label=label,
    )
    if path != expected.resolve() or not path.is_dir():
        raise FullBatchPublishError(
            f"strict results path mismatch for {label}: {path}"
        )
    actual = _directory_tree_evidence(
        path,
        job_root=job_root,
        label=label,
        started_at_ns=started_at_ns,
    )
    recorded_identity = _validate_recorded_tree_identity(
        evidence, label=label, allow_tool_internal=True
    )
    if (
        evidence.get("path") != actual["path"]
        or _canonical_json(recorded_identity)
        != _canonical_json(_tree_identity(actual))
    ):
        raise FullBatchPublishError(
            f"strict recursive tree evidence mismatch for {label}: {path}"
        )
    return path


def _validate_tdr_completion(
    transient: Mapping[str, Any],
    *,
    run_dir: Path,
    batch_name: str,
    solved_touchstone: Path,
    job_root: Path,
    started_at_ns: int,
) -> tuple[list[Path], Path, dict[str, Any]]:
    if transient.get("status") != "ok" or transient.get("buildMode") != STRICT_TDR_MODE:
        raise FullBatchPublishError(
            f"strict TDR did not complete successfully for batch {batch_name}"
        )
    artifacts = transient.get("artifacts") or {}
    if not isinstance(artifacts, Mapping):
        raise FullBatchPublishError(f"strict TDR artifacts is invalid: {batch_name}")
    if artifacts.get("freshness") != "all exact targets were absent before this strict run":
        raise FullBatchPublishError(f"strict TDR freshness evidence is invalid: {batch_name}")
    try:
        package_validation = validate_strict_tdr_artifact_package(
            transient,
            run_dir=run_dir,
            job_root=job_root,
            expected_batch_id=batch_name,
            started_at_ns=started_at_ns,
        )
    except (StrictTdrRuntimeError, OSError, RuntimeError, ValueError) as exc:
        raise FullBatchPublishError(
            f"strict native TDR artifact package validation failed for batch "
            f"{batch_name}: {exc}"
        ) from exc

    circuit_dir = (run_dir / "circuit").resolve()
    expected_project = circuit_dir / f"{batch_name}.aedt"
    expected_archive = circuit_dir / f"{batch_name}.aedtz"
    project = _recorded_file(
        artifacts.get("project"),
        expected=expected_project,
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} AEDT project",
        started_at_ns=started_at_ns,
        size_key="size",
    )
    archive = _recorded_file(
        artifacts.get("archive"),
        expected=expected_archive,
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} AEDT archive",
        started_at_ns=started_at_ns,
        size_key="size",
    )
    _same_path(transient.get("projectPath"), project, run_dir=run_dir, label="projectPath")
    _same_path(transient.get("archivePath"), archive, run_dir=run_dir, label="archivePath")

    staged_touchstone = _source_file(
        transient.get("touchstonePath"),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} Circuit Touchstone",
        started_at_ns=started_at_ns,
    )
    if (
        staged_touchstone.name != solved_touchstone.name
        or _sha256(staged_touchstone) != _sha256(solved_touchstone)
    ):
        raise FullBatchPublishError(
            f"strict Circuit Touchstone differs from the solved batch artifact: {batch_name}"
        )

    reports = list(package_validation["reports"])
    _validate_internal_report_names(reports, batch_name=batch_name)

    waveform = Path(package_validation["waveformCsv"])
    results = _validate_results_directory(
        artifacts.get("aedtResults"),
        expected=expected_project.with_suffix(".aedtresults"),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} AEDT results",
        started_at_ns=started_at_ns,
    )
    # 9.4.6: 공개 <Batch>.aedt는 자기 DB <Batch>.aedb와 한 세트다(Circuit
    # 프로젝트 자기 DB, 보드 레이아웃 EDB 개명본이 아니다).
    circuit_aedb = _validate_results_directory(
        artifacts.get("aedb"),
        expected=expected_project.with_suffix(".aedb"),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} Circuit project AEDB",
        started_at_ns=started_at_ns,
    )
    return reports, waveform, {
        "record": str((run_dir / "tdr_transient.json").resolve()),
        "mode": STRICT_TDR_MODE,
        "project": str(project),
        "archive": str(archive),
        "aedtResults": str(results),
        "aedb": str(circuit_aedb),
        "nativeReportCount": len(reports),
        "nativeReportArtifactValidation": {
            "status": "verified",
            "imageAnalyses": package_validation["imageAnalyses"],
            "waveformAnalysis": package_validation["waveformAnalysis"],
            "liveVisualConfirmation": package_validation[
                "liveVisualConfirmation"
            ],
        },
        "publicationEligibility": "verified-for-EDEN-04",
    }


def _strict_reference_sources(job_root: Path, *, require_archive: bool = True) -> dict[str, Any]:
    """Locate and revalidate the single ready reference preprocess manifest.

    참조 보드 쌍(<ref>.siw + <ref>.aedb/)은 내부 증거로 검증하고,
    공개할 <ref>.siwz 아카이브를 원본 SIW identity와 대조한다.
    load_reference_preprocess_manifest가 경로·크기·sha256 evidence와
    manifestId를 재검증하고, ready manifest가 정확히 하나여야 한다.
    """

    reference_root = job_root / "work" / "reference_preprocess"
    manifest_path = reference_root / "reference_preprocess_manifest.json"
    candidates = [manifest_path] if manifest_path.is_file() else []
    ready = []
    failures: list[str] = []
    for path in candidates:
        try:
            ready.append(load_reference_preprocess_manifest(path))
        except ReferencePreprocessError as exc:
            failures.append(f"{path}: {exc}")
    if len(ready) != 1:
        raise FullBatchPublishError(
            "exactly one ready reference preprocess manifest is required for "
            f"publication; found {len(ready)} (invalid: {failures})"
        )
    result = ready[0]
    siw = _contained_path(
        result.reference_siw, root=job_root, label="reference SIW"
    )
    aedb = _contained_path(
        result.reference_aedb, root=job_root, label="reference AEDB"
    )
    for name, expected_suffix in ((siw.name, ".siw"), (aedb.name, ".aedb")):
        _validate_tree_component(
            name,
            label="public reference artifact",
            is_directory=expected_suffix == ".aedb",
        )
        if Path(name).suffix.casefold() != expected_suffix:
            raise FullBatchPublishError(
                f"public reference artifact suffix differs: {name!r}"
            )
    if siw.stem.casefold() != aedb.stem.casefold():
        raise FullBatchPublishError(
            "public reference SIW/AEDB basenames must share one stem: "
            f"{siw.name!r}, {aedb.name!r}"
        )
    if not siw.is_file() or siw.stat().st_size <= 0:
        raise FullBatchPublishError(f"reference SIW is missing or empty: {siw}")
    if not (aedb / "edb.def").is_file():
        raise FullBatchPublishError(f"reference AEDB has no edb.def: {aedb}")
    archive = None
    if require_archive:
        try:
            archive = validate_reference_archive(siw, job_root / "work" / "reference_archive")
            validate_public_stackup(aedb, job_root / "work" / "reference_archive")
        except (ValueError, OSError) as exc:
            raise FullBatchPublishError(f"invalid reference archive: {exc}") from exc
    return {
        "siw": siw,
        "aedb": aedb,
        "siwz": archive,
        "manifest": result.manifest_path,
        "manifestId": result.manifest_id,
    }


def prepare_public_reference_archive(job_root: Path, aedt_version: str) -> Path:
    try:
        from .preprocess.public_artifacts import create_reference_archive, export_public_stackup
        from .syz_runtime import resolve_siwave_executable
    except ImportError:
        from preprocess.public_artifacts import create_reference_archive, export_public_stackup
        from syz_runtime import resolve_siwave_executable
    sources = _strict_reference_sources(job_root, require_archive=False)
    archive = create_reference_archive(
        reference_siw=sources["siw"], reference_aedb=sources["aedb"],
        directory=job_root / "work" / "reference_archive",
        executable=resolve_siwave_executable(aedt_version))
    export_public_stackup(sources["aedb"], job_root / "work" / "reference_archive", aedt_version)
    return archive


def _validate_public_reference_record(expected_public_reference: Any) -> dict[str, Any]:
    if not isinstance(expected_public_reference, Mapping):
        raise FullBatchPublishError("public reference archive record is missing")
    archive = expected_public_reference.get("siwz")
    if not isinstance(archive, Mapping):
        raise FullBatchPublishError("public reference archive evidence is missing")
    name, size, digest = archive.get("path"), archive.get("size"), archive.get("sha256")
    if (not isinstance(name, str) or "/" in name or "\\" in name
            or Path(name).suffix.casefold() != ".siwz"):
        raise FullBatchPublishError("public reference archive must be a root SIWZ basename")
    _validate_tree_component(name, label="public reference archive", is_directory=False)
    if (isinstance(size, bool) or not isinstance(size, int) or size <= 0
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None):
        raise FullBatchPublishError("public reference archive identity is invalid")
    return dict(archive)


def _batch_name(config_path: Path, config: Mapping[str, Any]) -> str:
    syz = config.get("syz") or {}
    value = syz.get("touchstoneBaseName") if isinstance(syz, Mapping) else None
    if not isinstance(value, str) or not value or value != value.strip():
        raise FullBatchPublishError(
            "strict Generated Run Config requires an exact "
            f"syz.touchstoneBaseName: {config_path}"
        )
    if (
        Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or re.search(r"\.s\d+p$", value, re.IGNORECASE)
    ):
        raise FullBatchPublishError(
            "strict syz.touchstoneBaseName must be an extension-free basename: "
            f"{value!r}"
        )
    return value


def _is_windows_reserved_name(name: str) -> bool:
    trimmed = name.rstrip(". ")
    device_base = trimmed.split(".", 1)[0].casefold()
    return device_base in WINDOWS_RESERVED_NAMES


def _safe_batch_directory(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    if (
        not normalized
        or SAFE_PUBLIC_NAME.fullmatch(normalized) is None
        or _is_windows_reserved_name(normalized)
        or normalized.casefold() in FORBIDDEN_PUBLIC_DIRECTORY_NAMES
    ):
        raise FullBatchPublishError(f"batch name cannot form a public directory: {name!r}")
    return normalized


def _validate_public_batch_basename(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or _is_windows_reserved_name(name)
        or any(ord(character) < 32 for character in name)
        or any(character in '<>:"/\\|?*' for character in name)
    ):
        raise FullBatchPublishError(
            f"Batch identity cannot form exact public artifact names: {name!r}"
        )


def _validate_internal_report_names(
    reports: Sequence[Path], *, batch_name: str
) -> None:
    report_names: dict[str, str] = {}
    for path in reports:
        if path.suffix != ".jpg":
            raise FullBatchPublishError(f"native TDR report must be JPG: {path}")
        stem = path.stem
        if (
            path.name != f"{stem}.jpg"
            or SAFE_PUBLIC_NAME.fullmatch(stem) is None
            or stem.endswith((".", " "))
            or _is_windows_reserved_name(stem)
        ):
            raise FullBatchPublishError(
                f"native TDR Group filename is unsafe in batch {batch_name}: {path.name!r}"
            )
        folded = path.name.casefold()
        if folded in report_names:
            raise FullBatchPublishError(
                "native TDR report filename collision in batch "
                f"{batch_name}: {report_names[folded]!r}, {path.name!r}"
            )
        report_names[folded] = path.name


def _validate_public_report_names(
    reports: Sequence[Path], *, batch_name: str
) -> None:
    expected = f"{batch_name}{PUBLIC_TDR_IMAGE_SUFFIX}"
    if len(reports) != 1 or reports[0].name != expected:
        raise FullBatchPublishError(
            "public TDR report must use the exact Batch basename: "
            f"batch={batch_name!r}, expected={expected!r}, "
            f"actual={[path.name for path in reports]!r}"
        )


def _resolved_run_dir(batch: BatchExecutionInput, config: Mapping[str, Any]) -> Path:
    if batch.run_dir is None:
        raise FullBatchPublishError(
            f"strict FullBatch batch has no run directory: {batch.config_path}"
        )
    run_dir = batch.run_dir.resolve()
    context_path = run_dir / "run_context.json"
    if context_path.is_file():
        context = _json_object(context_path, label="strict run context")
        workspace = context.get("workspace") or {}
        if isinstance(workspace, Mapping) and workspace.get("runDir"):
            run_dir = Path(str(workspace["runDir"])).resolve()
    return run_dir


def _validate_final_aedb_source(
    *,
    config: Mapping[str, Any],
    config_path: Path,
    run_dir: Path,
    batch_name: str,
    pre_solve_completion: Mapping[str, Any],
    job_root: Path,
    started_at_ns: int,
) -> tuple[Path, dict[str, Any]]:
    if pre_solve_completion.get("batchId") != batch_name:
        raise FullBatchPublishError(
            f"FB-06 target AEDB batch identity differs: {batch_name}"
        )
    layout = config.get("layout") or {}
    if not isinstance(layout, Mapping):
        raise FullBatchPublishError(
            f"Generated Run Config layout is invalid for batch {batch_name}"
        )
    reference_value = layout.get("referenceEdb")
    if not isinstance(reference_value, str) or not reference_value.strip():
        raise FullBatchPublishError(
            f"Generated Run Config layout.referenceEdb is missing: {batch_name}"
        )
    reference_raw = Path(reference_value)
    reference = _contained_path(
        reference_raw if reference_raw.is_absolute() else config_path.parent / reference_raw,
        root=job_root,
        label=f"batch {batch_name} layout.referenceEdb",
    )
    if not reference.is_dir():
        raise FullBatchPublishError(
            f"Generated Run Config layout.referenceEdb is not an AEDB: {reference}"
        )

    target_evidence = pre_solve_completion.get("targetEdb") or {}
    if not isinstance(target_evidence, Mapping):
        raise FullBatchPublishError(
            f"FB-06 target AEDB evidence is missing for batch {batch_name}"
        )
    target_value = target_evidence.get("path")
    if not isinstance(target_value, str) or not target_value.strip():
        raise FullBatchPublishError(
            f"FB-06 target AEDB path is missing for batch {batch_name}"
        )
    target_raw = Path(target_value)
    target = _contained_path(
        target_raw if target_raw.is_absolute() else run_dir / target_raw,
        root=job_root,
        label=f"batch {batch_name} FB-06 target AEDB",
    )
    expected_target = (run_dir / reference.name).resolve()
    if target != expected_target or target.parent != run_dir:
        raise FullBatchPublishError(
            "FB-06 target AEDB is not the exact current-run copy derived from "
            f"layout.referenceEdb: recorded={target}, expected={expected_target}"
        )
    newest_mtime = target_evidence.get("newestMtimeNs")
    if (
        isinstance(newest_mtime, bool)
        or not isinstance(newest_mtime, int)
        or newest_mtime < started_at_ns
    ):
        raise FullBatchPublishError(
            f"FB-06 target AEDB freshness evidence is stale: {target}"
        )
    tree = _directory_tree_evidence(
        target,
        job_root=job_root,
        label=f"batch {batch_name} final AEDB",
        require_edb_def=True,
    )
    if (
        target_evidence.get("fileCount") != tree["fileCount"]
        or target_evidence.get("sizeBytes") != tree["totalBytes"]
        or not isinstance(target_evidence.get("sha256"), str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", str(target_evidence.get("sha256")))
        is None
    ):
        raise FullBatchPublishError(
            f"FB-06 target AEDB aggregate evidence differs: {target}"
        )
    return target, {
        **tree,
        "authoritativeFb06Sha256": str(target_evidence["sha256"]).casefold(),
        "sourceIdentity": "customer_pre_solve_validation.targetEdb",
        "generatedRunConfigReferenceEdb": str(reference),
    }


def _strict_batch_sources(
    batch: BatchExecutionInput,
    *,
    job_root: Path,
    started_at_ns: int,
    capture_policy: Mapping[str, Any],
) -> tuple[
    str,
    list[Path],
    Path,
    dict[str, Path],
    dict[str, Path],
    dict[str, Any] | None,
    dict[str, Any],
]:
    if int(batch.status_code) != 0:
        raise FullBatchPublishError(
            f"strict FullBatch batch failed before publish: {batch.config_path}: "
            f"status={batch.status_code}, error={batch.error}"
        )
    config_path = _contained_path(
        batch.config_path, root=job_root, label="strict batch Run Config"
    )
    config = _json_object(config_path, label="strict batch Run Config")
    if not isinstance(config.get("customerComponentHandling"), Mapping):
        raise FullBatchPublishError(
            f"FullBatch publisher accepts only strict customer batches: {config_path}"
        )
    batch_name = _batch_name(config_path, config)
    _validate_public_batch_basename(batch_name)
    run_dir = _contained_path(
        _resolved_run_dir(batch, config), root=job_root, label="strict batch run directory"
    )
    if not run_dir.is_dir():
        raise FullBatchPublishError(f"strict batch run directory is missing: {run_dir}")

    context_path = run_dir / "run_context.json"
    context = _json_object(context_path, label="strict run context")
    try:
        pre_solve_completion = validate_customer_pre_solve(
            context, started_at_ns=started_at_ns
        )
    except (CustomerPreSolveError, OSError, RuntimeError, ValueError) as exc:
        raise FullBatchPublishError(
            f"FB-06 customer component/Port validation failed for batch {batch_name}: {exc}"
        ) from exc

    component_manifest_path = run_dir / "customer_component_manifest.json"
    component_manifest = _json_object(
        component_manifest_path, label=f"batch {batch_name} component manifest"
    )
    try:
        array_model_result_payload = build_customer_array_model_result(
            component_manifest
        )
    except ArrayModelResultError as exc:
        raise FullBatchPublishError(
            f"customer Array model result cannot be derived for batch {batch_name}: {exc}"
        ) from exc
    array_model_result: dict[str, Any] | None = None
    if array_model_result_payload is not None:
        array_model_result = {
            "payload": array_model_result_payload,
            "componentManifest": component_manifest_path,
            "componentManifestSize": component_manifest_path.stat().st_size,
            "componentManifestSha256": _sha256(component_manifest_path),
        }

    target_edb, aedb_tree_evidence = _validate_final_aedb_source(
        config=config,
        config_path=config_path,
        run_dir=run_dir,
        batch_name=batch_name,
        pre_solve_completion=pre_solve_completion,
        job_root=job_root,
        started_at_ns=started_at_ns,
    )

    solved_touchstone, syz_completion = _validate_syz_completion(
        run_dir=run_dir,
        config=config,
        batch_name=batch_name,
        target_edb=target_edb,
        pre_solve_completion=pre_solve_completion,
        job_root=job_root,
        started_at_ns=started_at_ns,
    )
    transient_path = run_dir / "tdr_transient.json"
    _source_file(
        str(transient_path),
        run_dir=run_dir,
        job_root=job_root,
        label=f"batch {batch_name} TDR completion record",
        started_at_ns=started_at_ns,
    )
    transient = _json_object(transient_path, label="strict TDR record")
    reports, waveform_path, tdr_completion = _validate_tdr_completion(
        transient,
        run_dir=run_dir,
        batch_name=batch_name,
        solved_touchstone=solved_touchstone,
        job_root=job_root,
        started_at_ns=started_at_ns,
    )

    pcb_manifest_path = run_dir / "pcb_capture" / "pcb_capture_manifest.json"
    try:
        pcb_by_view = validate_strict_capture_package(
            pcb_manifest_path,
            job_root=job_root,
            started_at_ns=started_at_ns,
            expected_batch_id=batch_name,
            expected_capture_policy=capture_policy,
        )
    except (PcbCaptureContractError, OSError, RuntimeError, ValueError) as exc:
        raise FullBatchPublishError(
            f"strict PCB package validation failed for batch {batch_name}: {exc}"
        ) from exc
    reproduction_sources = {
        "touchstone": solved_touchstone,
        "siw": Path(str(syz_completion["siw"])),
        "siwaveResults": Path(str(syz_completion["siwaveResults"])),
        "siwz": Path(str(syz_completion["siwz"])),
        "aedb": Path(str(tdr_completion["aedb"])),
        "aedt": Path(str(tdr_completion["project"])),
        "aedtResults": Path(str(tdr_completion["aedtResults"])),
        "aedtz": Path(str(tdr_completion["archive"])),
    }
    return (
        batch_name,
        reports,
        waveform_path,
        pcb_by_view,
        reproduction_sources,
        array_model_result,
        {
        "batch": batch_name,
        "fb06CustomerComponentAndPorts": pre_solve_completion,
        "finalAedbSource": aedb_tree_evidence,
        "syz": syz_completion,
        "tdr": tdr_completion,
        "publication": {
            "publishedBy": PROVISIONAL_PUBLISH_CONTRACT,
            "publishedKinds": [
                "touchstone",
                "siwz",
                "aedtz",
            ],
            "internalOnlyKinds": ["siw", "siwaveResults", "aedb", "aedt", "aedtResults",
                                  "completion records", "work/evidence", "original PNG captures"],
        },
        },
    )


def _copy_public_file(
    source: Path,
    destination: Path,
    *,
    public_relative_path: str,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise FullBatchPublishError(f"published artifact copy is missing or empty: {destination}")
    source_hash = _sha256(source)
    destination_hash = _sha256(destination)
    if destination_hash != source_hash:
        raise FullBatchPublishError(f"published artifact hash mismatch: {destination}")
    return {
        "kind": "file",
        "source": str(source),
        "destinationRelativeToOutputs": public_relative_path,
        "size": destination.stat().st_size,
        "sha256": destination_hash,
    }


def _copy_public_jpeg(source: Path, destination: Path, *, public_relative_path: str) -> dict[str, Any]:
    if destination.suffix != ".jpg":
        raise FullBatchPublishError("public capture destination must be .jpg")
    data, analysis = jpeg_bytes(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {"kind": "jpeg", "source": str(source),
            "destinationRelativeToOutputs": public_relative_path,
            "size": len(data), "sha256": _sha256(destination), "conversion": analysis}


def _tree_identity(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: evidence[key]
        for key in (
            "directories",
            "fileManifest",
            "fileCount",
            "totalBytes",
            "treeSha256",
        )
    }


def _validate_recorded_tree_identity(
    evidence: Any, *, label: str, allow_tool_internal: bool = False
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise FullBatchPublishError(f"{label} tree evidence is missing")
    directories = evidence.get("directories")
    files = evidence.get("fileManifest")
    if not isinstance(directories, list) or not isinstance(files, list) or not files:
        raise FullBatchPublishError(f"{label} tree manifest is incomplete")
    normalized_directories: list[str] = []
    normalized_files: list[dict[str, Any]] = []
    folded: dict[str, str] = {}
    for relative in directories:
        if not isinstance(relative, str) or "\\" in relative:
            raise FullBatchPublishError(f"{label} directory path is unsafe: {relative!r}")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(
            part in {"", ".", ".."} for part in pure.parts
        ):
            raise FullBatchPublishError(f"{label} directory path is unsafe: {relative!r}")
        for part in pure.parts:
            _validate_tree_component(
                part,
                label=label,
                is_directory=True,
                allow_tool_internal=allow_tool_internal,
            )
        normalized = pure.as_posix()
        prior = folded.get(normalized.casefold())
        if prior is not None:
            raise FullBatchPublishError(
                f"{label} tree has a case-insensitive path collision: "
                f"{prior!r}, {normalized!r}"
            )
        folded[normalized.casefold()] = normalized
        normalized_directories.append(normalized)
    for item in files:
        if not isinstance(item, Mapping):
            raise FullBatchPublishError(f"{label} file manifest entry is invalid")
        relative = item.get("relativePath")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(relative, str) or "\\" in relative:
            raise FullBatchPublishError(f"{label} file path is unsafe: {relative!r}")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(
            part in {"", ".", ".."} for part in pure.parts
        ):
            raise FullBatchPublishError(f"{label} file path is unsafe: {relative!r}")
        for index, part in enumerate(pure.parts):
            _validate_tree_component(
                part,
                label=label,
                is_directory=index < len(pure.parts) - 1,
                allow_tool_internal=allow_tool_internal,
            )
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or (size == 0 and not allow_tool_internal)
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
        ):
            raise FullBatchPublishError(
                f"{label} file size/hash evidence is invalid: {relative!r}"
            )
        normalized = pure.as_posix()
        prior = folded.get(normalized.casefold())
        if prior is not None:
            raise FullBatchPublishError(
                f"{label} tree has a case-insensitive path collision: "
                f"{prior!r}, {normalized!r}"
            )
        folded[normalized.casefold()] = normalized
        normalized_files.append(
            {
                "relativePath": normalized,
                "size": size,
                "sha256": sha256.casefold(),
            }
        )
    normalized_directories.sort()
    normalized_files.sort(key=lambda item: str(item["relativePath"]))
    normalized = {
        "directories": normalized_directories,
        "fileManifest": normalized_files,
        "fileCount": len(normalized_files),
        "totalBytes": sum(int(item["size"]) for item in normalized_files),
        "treeSha256": hashlib.sha256(
            _canonical_json(
                {
                    "directories": normalized_directories,
                    "fileManifest": normalized_files,
                }
            ).encode("utf-8")
        ).hexdigest(),
    }
    try:
        recorded_identity = _tree_identity(evidence)
    except KeyError as exc:
        raise FullBatchPublishError(f"{label} tree aggregate evidence is incomplete") from exc
    if _canonical_json(normalized) != _canonical_json(recorded_identity):
        raise FullBatchPublishError(f"{label} tree aggregate evidence differs")
    return normalized


def _copy_public_directory(
    source: Path,
    destination: Path,
    *,
    public_relative_path: str,
    job_root: Path,
    label: str,
    started_at_ns: int | None = None,
    require_edb_def: bool = False,
) -> dict[str, Any]:
    source_evidence = _directory_tree_evidence(
        source,
        job_root=job_root,
        label=f"{label} source",
        started_at_ns=started_at_ns,
        require_edb_def=require_edb_def,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FullBatchPublishError(
            f"published directory destination already exists: {destination}"
        )
    def _ignore_edb_sidecar(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            candidate = Path(directory) / name
            try:
                if candidate.is_file() and _is_ignored_edb_sidecar(
                    name, candidate.stat().st_size
                ):
                    skipped.add(name)
            except OSError:
                continue
        return skipped

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=shutil.copy2,
        ignore=_ignore_edb_sidecar,
    )
    destination_evidence = _directory_tree_evidence(
        destination,
        job_root=None,
        label=f"{label} staging copy",
        require_edb_def=require_edb_def,
    )
    if _canonical_json(_tree_identity(source_evidence)) != _canonical_json(
        _tree_identity(destination_evidence)
    ):
        raise FullBatchPublishError(
            f"published directory copy differs from its source: {public_relative_path}"
        )
    return {
        "kind": "directory",
        "source": str(source),
        "destinationRelativeToOutputs": public_relative_path,
        "sourceTree": source_evidence,
        "destinationTree": destination_evidence,
    }


def _public_reference_values(payloads: Mapping[str, Mapping[str, Any]]) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    def add_list(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)

    request = payloads["request.json"]
    image = request.get("Image") or {}
    if isinstance(image, Mapping):
        add(image.get("pcbTopImage"))
        add(image.get("pcbBtmImage"))

    result = payloads["result.json"]
    detail = payloads["result_detail.json"]
    collections = [result.get("summary") or [], detail.get("batches") or [], detail.get("result") or []]
    for collection in collections:
        for item in collection:
            if not isinstance(item, Mapping):
                continue
            add_list(item.get("sNp"))
            add(item.get("TDRImage"))
            add_list(item.get("TDRImage"))
            add(item.get("TDRCsv"))
            add(item.get("PCBImages"))
            add_list(item.get("PCBImages"))
    return values


def _validate_public_reference(
    value: str,
    *,
    root: Path,
    allowed_artifacts: set[str],
) -> str:
    if "\\" in value:
        raise FullBatchPublishError(f"public JSON reference must use POSIX separators: {value}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise FullBatchPublishError(f"public JSON reference escapes outputs: {value}")
    relative = pure.as_posix()
    if relative not in allowed_artifacts:
        raise FullBatchPublishError(f"public JSON reference is not allowlisted: {value}")
    candidate = (root / Path(*pure.parts)).resolve()
    resolved_root = root.resolve()
    if resolved_root != candidate and resolved_root not in candidate.parents:
        raise FullBatchPublishError(f"public JSON reference resolves outside outputs: {value}")
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise FullBatchPublishError(f"public JSON reference is missing or empty: {value}")
    return relative


def _result_item_references(
    item: Mapping[str, Any],
    *,
    label: str,
    require_touchstone: bool,
    require_pcb_route: bool,
) -> set[str]:
    images = item.get("TDRImage")
    pcb_images = item.get("PCBImages")
    if not require_touchstone:
        if not isinstance(images, str) or not images:
            raise FullBatchPublishError(f"{label} TDRImage must be one path string")
        if (require_pcb_route and (not isinstance(pcb_images, str) or not pcb_images)) or (not require_pcb_route and pcb_images is not None):
            raise FullBatchPublishError(f"{label} PCBImages must be one path string or null when disabled")
        images = [images]
        pcb_images = [pcb_images] if pcb_images else []
    csv_path = item.get("TDRCsv")
    touchstone = item.get("sNp")
    if (
        not isinstance(images, list)
        or not images
        or any(not isinstance(value, str) or not value for value in images)
        or not isinstance(pcb_images, list)
        or (
            require_pcb_route
            and (
                len(pcb_images) != 1
                or any(not isinstance(value, str) or not value for value in pcb_images)
            )
        )
        or (not require_pcb_route and bool(pcb_images))
        or not isinstance(csv_path, str)
        or not csv_path
        or (
            require_touchstone
            and (
                not isinstance(touchstone, list)
                or len(touchstone) != 1
                or not isinstance(touchstone[0], str)
                or not touchstone[0]
            )
        )
    ):
        raise FullBatchPublishError(f"{label} has an incomplete public artifact mapping")
    values = {
        *(str(value) for value in images),
        csv_path,
        *(str(value) for value in pcb_images),
    }
    if require_touchstone:
        values.add(str(touchstone[0]))
    return values


def _marker_evaluations(sources: Any, job_root: Path) -> dict[str, Any]:
    if not isinstance(sources, Mapping) or set(sources) != {"config", "transient", "waveform"}:
        raise FullBatchPublishError("central Marker sources are incomplete")
    paths = {}
    for kind, entry in sources.items():
        if not isinstance(entry, Mapping):
            raise FullBatchPublishError("central Marker source is not an object")
        path = _contained_path(Path(str(entry.get("path", ""))), root=job_root, label="Marker source")
        if not path.is_file() or entry.get("size") != path.stat().st_size or entry.get("sha256") != _sha256(path):
            raise FullBatchPublishError("central Marker source hash/size differs")
        paths[kind] = path
    config = _json_object(paths["config"], label="Marker config")
    transient = _json_object(paths["transient"], label="Marker TDR record")
    try:
        return evaluate_center_markers(transient, paths["waveform"], config["tdr"]["channels"])
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise FullBatchPublishError(f"invalid central Marker evaluation: {exc}") from exc


def _validate_public_web_metadata(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    batch_records: Mapping[str, Mapping[str, Any]],
) -> None:
    title = payloads["title.json"]
    request = payloads["request.json"]
    setting = payloads["setting.json"]
    model_info = request.get("modelInfo") or {}
    request_data = request.get("requestData") or {}
    if not isinstance(model_info, Mapping) or not isinstance(request_data, Mapping):
        raise FullBatchPublishError("public request.json metadata mappings are invalid")
    model_name = model_info.get("name")
    required_request_fields = (
        "socName",
        "design",
        "Stackup",
        "bom",
        "channelCsv",
        "purpose",
    )
    if (
        not isinstance(model_name, str)
        or not model_name.strip()
        or title.get("model") != model_name
        or any(
            not isinstance(request_data.get(field), str)
            or not str(request_data.get(field)).strip()
            for field in required_request_fields
        )
    ):
        raise FullBatchPublishError(
            "public title/request metadata is incomplete or inconsistent"
        )

    tool = setting.get("tool") or {}
    rows = setting.get("setting") or []
    if (
        not isinstance(tool, Mapping)
        or tool.get("version") != "2025.2"
        or setting.get("stackup") != "stackup.xml"
        or not isinstance(rows, list)
        or len(rows) != len(batch_records)
    ):
        raise FullBatchPublishError("public setting.json summary is incomplete")
    expected_batches = {str(record["batch"]) for record in batch_records.values()}
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise FullBatchPublishError(f"public setting[{index}] is invalid")
        batch_id = row.get("Batch")
        syz = row.get("syzProfile") or {}
        tdr_profiles = row.get("tdrProfiles") or []
        tdr = row.get("tdr") or {}
        if (
            not isinstance(batch_id, str)
            or batch_id not in expected_batches
            or batch_id in seen
            or row.get("aedtVersion") != "2025.2"
            or "frequencySweep" in row
            or row.get("terminalLayerPolicy") != "endpoint-pin-same-layer"
            or row.get("referenceLayer") is not None
            or not isinstance(syz, Mapping)
            or not isinstance(tdr_profiles, list)
            or not tdr_profiles
            or not isinstance(tdr, Mapping)
        ):
            raise FullBatchPublishError(
                f"public setting[{index}] strict Batch contract is invalid"
            )
        seen.add(batch_id)
        if (
            not isinstance(syz.get("profileId"), str)
            or row.get("syzProfileId") != syz.get("profileId")
            or row.get("syzTemplateId") != syz.get("profileId")
            or not isinstance(syz.get("settings"), Mapping)
        ):
            raise FullBatchPublishError(
                f"public setting[{index}] SYZ Profile contract is invalid"
            )
        for file_key in ("sws", "sfsdf"):
            file_record = syz.get(file_key) or {}
            if (
                not isinstance(file_record, Mapping)
                or set(file_record) != {"fileName", "sha256"}
                or not isinstance(file_record.get("fileName"), str)
                or not isinstance(file_record.get("sha256"), str)
                or len(str(file_record.get("sha256"))) != 64
            ):
                raise FullBatchPublishError(
                    f"public setting[{index}] {file_key} provenance is invalid"
                )
        profile_ids: list[str] = []
        profile_settings: list[Mapping[str, Any]] = []
        for profile_index, profile in enumerate(tdr_profiles):
            if not isinstance(profile, Mapping):
                raise FullBatchPublishError(
                    f"public setting[{index}] TDR Profile[{profile_index}] is invalid"
                )
            profile_id = profile.get("profileId")
            profile_file = profile.get("file") or {}
            profile_setting = profile.get("settings") or {}
            if (
                not isinstance(profile_id, str)
                or not profile_id
                or not isinstance(profile_file, Mapping)
                or set(profile_file) != {"fileName", "sha256"}
                or not isinstance(profile_setting, Mapping)
                or any(
                    field not in profile_setting or profile_setting.get(field) is None
                    for field in (
                        "riseTimePs",
                        "pulseRepetition",
                        "pulseWidth",
                        "timeDelay",
                    )
                )
            ):
                raise FullBatchPublishError(
                    f"public setting[{index}] TDR Profile[{profile_index}] is incomplete"
                )
            profile_ids.append(profile_id)
            profile_settings.append(profile_setting)
        if sorted(set(profile_ids)) != row.get("tdrProfileIds"):
            raise FullBatchPublishError(
                f"public setting[{index}] TDR Profile IDs are inconsistent"
            )
        if len({
            _canonical_json(dict(value)) for value in profile_settings
        }) != 1:
            if any(tdr.get(field) is not None for field in (
                "riseTimePs", "pulseRepetition", "pulseWidth", "timeDelay"
            )):
                raise FullBatchPublishError(
                    f"public setting[{index}] exposes ambiguous scalar TDR settings"
                )
        else:
            uniform = profile_settings[0]
            if any(
                tdr.get(field) != uniform.get(field)
                for field in (
                    "riseTimePs",
                    "pulseRepetition",
                    "pulseWidth",
                    "timeDelay",
                )
            ):
                raise FullBatchPublishError(
                    f"public setting[{index}] scalar TDR settings differ from its Profile"
                )
    if seen != expected_batches:
        raise FullBatchPublishError("public setting.json Batch set is incomplete")


def _validate_direct_public_allowlist(
    allowed: set[str],
    *,
    expected_public_batches: Sequence[Mapping[str, Any]],
    expected_public_reference: Mapping[str, Any],
    capture_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    capture_policy = normalize_pcb_capture_policy(capture_policy)
    route_enabled = bool((capture_policy.get("route") or {}).get("enabled"))
    overview_enabled = bool(
        (capture_policy.get("overview") or {}).get("enabled")
    )
    expected_web = set(WEB_RESULT_FILENAMES)
    folded_paths: dict[str, str] = {}
    for relative in sorted(allowed):
        folded = relative.casefold()
        prior = folded_paths.get(folded)
        if prior is not None:
            raise FullBatchPublishError(
                "publish allowlist has a case-insensitive path collision: "
                f"{prior!r}, {relative!r}"
            )
        folded_paths[folded] = relative

    archive_record = _validate_public_reference_record(expected_public_reference)
    reference_files = {str(archive_record["path"]), "stackup.xml"}
    reference_info = {"siwz": archive_record, "files": reference_files, "directories": set()}
    batches: dict[str, dict[str, Any]] = {}
    folded_folders: dict[str, str] = {}
    folded_batch_ids: dict[str, str] = {}
    expected_files = set(expected_web) | reference_files
    if overview_enabled:
        expected_files.update(PUBLIC_OVERVIEW_IMAGE_NAMES)
    for index, record in enumerate(expected_public_batches):
        if not isinstance(record, Mapping):
            raise FullBatchPublishError(f"public batch artifact record[{index}] is invalid")
        batch = record.get("batch")
        folder = record.get("publicFolder")
        port_count = record.get("portCount")
        if not isinstance(batch, str) or not batch:
            raise FullBatchPublishError(f"public batch artifact record[{index}] has no Batch")
        _validate_public_batch_basename(batch)
        if folder != ".":
            raise FullBatchPublishError("public files must be directly in outputs")
        if isinstance(port_count, bool) or not isinstance(port_count, int) or port_count <= 0:
            raise FullBatchPublishError(
                f"public batch artifact record[{index}] has an invalid portCount"
            )
        prior_batch = folded_batch_ids.get(batch.casefold())
        if prior_batch is not None:
            raise FullBatchPublishError(
                f"public Batch identity case collision: {prior_batch!r}, {batch!r}"
            )
        folded_batch_ids[batch.casefold()] = batch

        web = record.get("webArtifacts") or {}
        customer = record.get("customerArtifacts") or {}
        reproduction = record.get("reproductionArtifacts") or {}
        source_mappings = record.get("sourceMappings") or {}
        if (
            not isinstance(web, Mapping)
            or not isinstance(customer, Mapping)
            or not isinstance(reproduction, Mapping)
            or not isinstance(source_mappings, Mapping)
        ):
            raise FullBatchPublishError(
                f"public batch artifact record[{index}] mappings are incomplete"
            )
        reports = web.get("tdrImages")
        csv_path = web.get("tdrCsv")
        pcb_images = web.get("pcbImages")
        if (
            not isinstance(reports, list)
            or not reports
            or any(not isinstance(value, str) for value in reports)
            or not isinstance(csv_path, str)
            or not isinstance(pcb_images, list)
            or (route_enabled and len(pcb_images) != 1)
            or (not route_enabled and bool(pcb_images))
            or any(not isinstance(value, str) for value in pcb_images)
        ):
            raise FullBatchPublishError(
                f"public Batch Web artifact set is incomplete: {batch}"
            )
        expected_web_files = {str(value) for value in reports}
        expected_web_files.add(csv_path)
        expected_web_files.update(str(value) for value in pcb_images)
        expected_report = f"{batch}{PUBLIC_TDR_IMAGE_SUFFIX}"
        expected_csv = f"{batch}{PUBLIC_TDR_CSV_SUFFIX}"
        expected_pcb = (
            f"{batch}{PUBLIC_PCB_IMAGE_SUFFIX}"
            if route_enabled
            else None
        )
        if (
            reports != [expected_report]
            or csv_path != expected_csv
            or pcb_images != ([expected_pcb] if expected_pcb is not None else [])
            or web.get("routeImage") != expected_pcb
        ):
            raise FullBatchPublishError(
                f"public Batch JPG/CSV/PCB paths differ from the direct contract: {batch}"
            )
        report_paths = [Path(PurePosixPath(str(report)).name) for report in reports]
        _validate_public_report_names(report_paths, batch_name=batch)
        if len(expected_web_files) != len(reports) + 1 + int(route_enabled):
            raise FullBatchPublishError(
                f"public Batch Web artifact paths are duplicated: {batch}"
            )
        for report in reports:
            pure = PurePosixPath(str(report))
            if len(pure.parts) != 1:
                raise FullBatchPublishError(
                    f"public Batch report is not a direct child: {report}"
                )

        if set(customer) - {"arrayModelResult"}:
            raise FullBatchPublishError(
                f"public Batch customer artifact set is invalid: {batch}"
            )
        customer_files: set[str] = set()
        customer_artifacts: dict[str, Any] = {}
        array_result = customer.get("arrayModelResult")
        if array_result is not None:
            if not isinstance(array_result, Mapping):
                raise FullBatchPublishError(
                    f"public Array model result record is invalid: {batch}"
                )
            expected_array_path = f"{batch}{ARRAY_MODEL_RESULT_SUFFIX}"
            source_manifest = array_result.get("sourceComponentManifest")
            size = array_result.get("size")
            sha256 = array_result.get("sha256")
            if (
                set(array_result)
                != {
                    "path",
                    "size",
                    "sha256",
                    "generatedSource",
                    "sourceComponentManifest",
                }
                or array_result.get("path") != expected_array_path
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
                or not isinstance(array_result.get("generatedSource"), str)
                or not str(array_result.get("generatedSource")).strip()
                or not isinstance(source_manifest, Mapping)
                or set(source_manifest) != {"path", "size", "sha256"}
                or not isinstance(source_manifest.get("path"), str)
                or not str(source_manifest.get("path")).strip()
                or isinstance(source_manifest.get("size"), bool)
                or not isinstance(source_manifest.get("size"), int)
                or source_manifest.get("size") <= 0
                or not isinstance(source_manifest.get("sha256"), str)
                or re.fullmatch(
                    r"[0-9a-fA-F]{64}", str(source_manifest.get("sha256"))
                )
                is None
            ):
                raise FullBatchPublishError(
                    f"public Array model result evidence differs: {batch}"
                )
            customer_files.add(expected_array_path)
            customer_artifacts["arrayModelResult"] = {
                "path": expected_array_path,
                "size": size,
                "sha256": sha256.casefold(),
                "generatedSource": str(array_result["generatedSource"]),
                "sourceComponentManifest": {
                    "path": str(source_manifest["path"]),
                    "size": int(source_manifest["size"]),
                    "sha256": str(source_manifest["sha256"]).casefold(),
                },
            }

        exact_reproduction_files = {
            "touchstone": f"{batch}.s{port_count}p",
            "siwz": f"{batch}.siwz",
            "aedtz": f"{batch}.aedtz",
        }
        if set(reproduction) != set(exact_reproduction_files):
            raise FullBatchPublishError(f"unexpected public reproduction artifacts: {batch}")
        for key, expected in exact_reproduction_files.items():
            if reproduction.get(key) != expected:
                raise FullBatchPublishError(
                    f"public {key} path differs for batch {batch}: "
                    f"{reproduction.get(key)!r} != {expected!r}"
                )

        directory_artifacts: dict[str, dict[str, Any]] = {}
        expected_directories = set()
        reproduction_files = set(exact_reproduction_files.values())
        expected_touchstone = exact_reproduction_files["touchstone"]
        web_reference_files = expected_web_files | {expected_touchstone}
        batch_files = web_reference_files | reproduction_files | customer_files
        overlap = web_reference_files & reproduction_files
        if overlap != {expected_touchstone}:
            raise FullBatchPublishError(
                f"public Batch Web/reproduction artifact categories overlap: "
                f"{batch}: {sorted(overlap)}"
            )
        expected_files.update(batch_files)
        reproduction_destinations = {
            **exact_reproduction_files,
            **{
                key: value["path"] for key, value in directory_artifacts.items()
            },
        }
        if set(source_mappings) != set(reproduction_destinations):
            raise FullBatchPublishError(
                f"public source mapping kinds differ for batch {batch}"
            )
        normalized_source_mappings: dict[str, dict[str, str]] = {}
        for key, destination in reproduction_destinations.items():
            mapping = source_mappings.get(key) or {}
            # 9.4.6: 배치 aedb는 Circuit 프로젝트 자기 DB이므로 모든 kind가
            # source basename == 공개 Batch 이름 규칙을 따른다.
            expected_policy = "source-basename-must-equal-public-batch-identity"
            if (
                not isinstance(mapping, Mapping)
                or not isinstance(mapping.get("source"), str)
                or not str(mapping.get("source")).strip()
                or mapping.get("destinationRelativeToOutputs") != destination
                or mapping.get("namingPolicy") != expected_policy
            ):
                raise FullBatchPublishError(
                    f"public source mapping differs for batch {batch} {key}"
                )
            if Path(str(mapping["source"])).name != PurePosixPath(
                destination
            ).name:
                raise FullBatchPublishError(
                    f"public source basename mapping differs for batch {batch} {key}"
                )
            normalized_source_mappings[key] = {
                "source": str(mapping["source"]),
                "destinationRelativeToOutputs": destination,
                "namingPolicy": expected_policy,
            }
        batches[batch] = {
            "batch": batch,
            "portCount": port_count,
            "webFiles": web_reference_files,
            "presentationFiles": expected_web_files,
            "customerFiles": customer_files,
            "customerArtifacts": customer_artifacts,
            "touchstone": expected_touchstone,
            "reproductionFiles": reproduction_files,
            "allFiles": batch_files,
            "directories": expected_directories,
            "directoryArtifacts": directory_artifacts,
            "sourceMappings": normalized_source_mappings,
        }
    if allowed != expected_files:
        raise FullBatchPublishError(
            "publish allowlist differs from the EDEN-04 artifact records: "
            f"missing={sorted(expected_files - allowed)}, "
            f"extra={sorted(allowed - expected_files)}"
        )
    return batches, reference_info


def _validate_copy_evidence(
    *,
    root: Path,
    source_job_root: Path,
    batch_records: Mapping[str, Mapping[str, Any]],
    reference_info: Mapping[str, Any],
    copy_evidence: Sequence[Mapping[str, Any]],
    overview_enabled: bool,
) -> None:
    directory_roots = {
        str(directory["path"]): key
        for record in batch_records.values()
        for key, directory in record["directoryArtifacts"].items()
    }
    directory_member_files = {
        relative
        for record in batch_records.values()
        for relative in record["reproductionFiles"]
        if any(relative.startswith(base + "/") for base in directory_roots)
    }
    expected_file_destinations = (
        {
            relative
            for record in batch_records.values()
            for relative in record["allFiles"]
        }
        | {str(reference_info["siwz"]["path"]), "stackup.xml"}
    ) - directory_member_files
    if overview_enabled:
        expected_file_destinations.update(PUBLIC_OVERVIEW_IMAGE_NAMES)
    expected_sources = {
        str(mapping["destinationRelativeToOutputs"]): str(mapping["source"])
        for record in batch_records.values()
        for mapping in record["sourceMappings"].values()
    }
    expected_sources.update(
        {
            str(array_result["path"]): str(array_result["generatedSource"])
            for record in batch_records.values()
            for array_result in [record["customerArtifacts"].get("arrayModelResult")]
            if isinstance(array_result, Mapping)
        }
    )
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    jpeg_paths = {
        path for batch in batch_records.values() for path in batch["presentationFiles"]
        if path.endswith(PUBLIC_PCB_IMAGE_SUFFIX)
    } | (set(PUBLIC_OVERVIEW_IMAGE_NAMES) if overview_enabled else set())
    for index, record in enumerate(copy_evidence):
        if not isinstance(record, Mapping):
            raise FullBatchPublishError(f"publish copyEvidence[{index}] is invalid")
        kind = record.get("kind")
        relative = record.get("destinationRelativeToOutputs")
        source_value = record.get("source")
        if not isinstance(relative, str) or not isinstance(source_value, str):
            raise FullBatchPublishError(f"publish copyEvidence[{index}] paths are invalid")
        source = _contained_path(
            Path(source_value), root=source_job_root, label=f"copy source {relative}"
        )
        expected_source_value = expected_sources.get(relative)
        if (
            expected_source_value is not None
            and source != Path(expected_source_value).resolve()
        ):
            raise FullBatchPublishError(
                f"copyEvidence source differs from the public source mapping: {relative}"
            )
        destination = root / Path(*PurePosixPath(relative).parts)
        if (relative in jpeg_paths) != (kind == "jpeg"):
            raise FullBatchPublishError(f"capture conversion kind differs: {relative}")
        if kind == "jpeg":
            if relative not in expected_file_destinations or relative in seen_files:
                raise FullBatchPublishError(f"JPEG destination is invalid: {relative}")
            if _is_link_or_reparse(source) or not source.is_file():
                raise FullBatchPublishError(f"JPEG source is invalid: {source}")
            data, conversion = jpeg_bytes(source)
            if (record.get("conversion") != conversion or record.get("size") != len(data)
                    or record.get("sha256") != hashlib.sha256(data).hexdigest()
                    or not destination.is_file() or destination.read_bytes() != data):
                raise FullBatchPublishError(f"JPEG conversion evidence drift: {relative}")
            seen_files.add(relative)
            continue
        if kind == "file":
            if relative not in expected_file_destinations or relative in seen_files:
                raise FullBatchPublishError(f"file copyEvidence destination is invalid: {relative}")
            if _is_link_or_reparse(source) or not source.is_file():
                raise FullBatchPublishError(f"file copyEvidence source is invalid: {source}")
            size = record.get("size")
            sha256 = record.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
                or not destination.is_file()
                or source.stat().st_size != size
                or destination.stat().st_size != size
                or _sha256(source) != sha256.casefold()
                or _sha256(destination) != sha256.casefold()
            ):
                raise FullBatchPublishError(f"file copyEvidence drift: {relative}")
            seen_files.add(relative)
            continue
        if kind == "directory":
            directory_kind = directory_roots.get(relative)
            if directory_kind is None or relative in seen_directories:
                raise FullBatchPublishError(
                    f"directory copyEvidence destination is invalid: {relative}"
                )
            source_tree = _directory_tree_evidence(
                source,
                job_root=source_job_root,
                label=f"directory copy source {relative}",
                require_edb_def=directory_kind == "referenceAedb",
            )
            destination_tree = _directory_tree_evidence(
                destination,
                job_root=None,
                label=f"directory staging copy {relative}",
                require_edb_def=directory_kind == "referenceAedb",
            )
            recorded_source = _validate_recorded_tree_identity(
                record.get("sourceTree"),
                allow_tool_internal=True,
                label=f"copy source {relative}"
            )
            recorded_destination = _validate_recorded_tree_identity(
                record.get("destinationTree"),
                allow_tool_internal=True,
                label=f"copy destination {relative}",
            )
            if not (
                _canonical_json(_tree_identity(source_tree))
                == _canonical_json(recorded_source)
                == _canonical_json(recorded_destination)
                == _canonical_json(_tree_identity(destination_tree))
            ):
                raise FullBatchPublishError(f"directory copyEvidence drift: {relative}")
            seen_directories.add(relative)
            continue
        raise FullBatchPublishError(f"publish copyEvidence[{index}] kind is invalid")
    if seen_files != expected_file_destinations:
        raise FullBatchPublishError(
            "file copyEvidence set differs: "
            f"missing={sorted(expected_file_destinations - seen_files)}, "
            f"extra={sorted(seen_files - expected_file_destinations)}"
        )
    if seen_directories != set(directory_roots):
        raise FullBatchPublishError(
            "directory copyEvidence set differs: "
            f"missing={sorted(set(directory_roots) - seen_directories)}, "
            f"extra={sorted(seen_directories - set(directory_roots))}"
        )


def validate_staged_publish_tree(
    root: Path,
    *,
    allowed_relative_paths: Sequence[str],
    expected_batch_count: int,
    expected_public_batches: Sequence[Mapping[str, Any]],
    expected_copy_evidence: Sequence[Mapping[str, Any]],
    source_job_root: Path,
    expected_public_reference: Mapping[str, Any],
    expected_capture_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        capture_policy = normalize_pcb_capture_policy(expected_capture_policy)
    except ValueError as exc:
        raise FullBatchPublishError(
            f"public PCB capture policy is invalid: {exc}"
        ) from exc
    route_enabled = bool(capture_policy["route"]["enabled"])
    overview_enabled = bool(capture_policy["overview"]["enabled"])
    root = root.resolve()
    normalized_allowed: list[str] = []
    for value in allowed_relative_paths:
        if not isinstance(value, str) or "\\" in value:
            raise FullBatchPublishError(f"publish allowlist path is unsafe: {value!r}")
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or any(
            part in {"", ".", ".."} for part in pure.parts
        ):
            raise FullBatchPublishError(f"publish allowlist path is unsafe: {value!r}")
        normalized_allowed.append(pure.as_posix())
    allowed = set(normalized_allowed)
    if len(allowed) != len(normalized_allowed):
        raise FullBatchPublishError("publish allowlist contains duplicate paths")
    expected_web = set(WEB_RESULT_FILENAMES)
    if not expected_web.issubset(allowed):
        raise FullBatchPublishError("publish allowlist is missing a Web JSON filename")
    overview_paths = set(PUBLIC_OVERVIEW_IMAGE_NAMES)
    if overview_enabled and not overview_paths.issubset(allowed):
        raise FullBatchPublishError(
            "publish allowlist is missing the whole-board overview top.jpg/bottom.jpg"
        )
    if not overview_enabled and overview_paths.intersection(allowed):
        raise FullBatchPublishError(
            "publish allowlist contains disabled whole-board overview images"
        )
    if len(expected_public_batches) != expected_batch_count:
        raise FullBatchPublishError(
            "public batch artifact record count differs from expected batches"
        )
    batch_records, reference_info = _validate_direct_public_allowlist(
        allowed,
        expected_public_batches=expected_public_batches,
        expected_public_reference=expected_public_reference,
        capture_policy=capture_policy,
    )
    allowed_siw_paths: set[str] = set()
    actual: set[str] = set()
    actual_directories: set[str] = set()
    for path in root.rglob("*"):
        if _is_link_or_reparse(path):
            raise FullBatchPublishError(
                f"public outputs cannot contain symlinks or reparse points: {path}"
            )
        if path.is_dir():
            relative_directory = path.relative_to(root).as_posix()
            actual_directories.add(relative_directory)
            if (
                len(PurePosixPath(relative_directory).parts) <= 2
                and path.name.casefold() in FORBIDDEN_PUBLIC_DIRECTORY_NAMES
            ):
                raise FullBatchPublishError(f"forbidden public directory: {path}")
            continue
        relative = path.relative_to(root).as_posix()
        actual.add(relative)
        suffix = path.suffix.casefold()
        # Ansys 생성 트리(.aedb/.aedtresults/.siwaveresults) 내부 파일은 그 트리의
        # recursive manifest가 검증하며, 이름·크기 규칙은 도구 산출물을 따른다
        # (예: siwaveresults 내부의 0000/0000.siw, 0바이트 로그 — R21/R22 관측).
        inside_tool_tree = any(
            part.casefold().endswith(TOOL_INTERNAL_TREE_SUFFIXES)
            for part in PurePosixPath(relative).parts[:-1]
        )
        if suffix in FORBIDDEN_PUBLIC_SUFFIXES and not inside_tool_tree:
            raise FullBatchPublishError(f"forbidden solver artifact in outputs: {relative}")
        if (
            suffix == ".siw"
            and not inside_tool_tree
            and relative not in allowed_siw_paths
        ):
            # 9.4.6: .siw는 참조 보드와 배치 자기 SIW의 정확한 위치에서만 공개된다.
            raise FullBatchPublishError(
                f"SIW outside the published reference/batch contract: {relative}"
            )
        if path.stat().st_size <= 0 and not inside_tool_tree:
            raise FullBatchPublishError(f"empty public output: {relative}")
    if actual != allowed:
        raise FullBatchPublishError(
            "published tree differs from the exact allowlist: "
            f"missing={sorted(allowed - actual)}, extra={sorted(actual - allowed)}"
        )
    allowed_directories = {
        relative
        for record in batch_records.values()
        for relative in record["directories"]
    } | set(reference_info["directories"])
    if actual_directories != allowed_directories:
        raise FullBatchPublishError(
            "published directory tree differs from the exact allowlist: "
            f"missing={sorted(allowed_directories - actual_directories)}, "
            f"extra={sorted(actual_directories - allowed_directories)}"
        )
    for record in batch_records.values():
        for key, directory in record["directoryArtifacts"].items():
            actual_tree = _directory_tree_evidence(
                root / Path(*PurePosixPath(directory["path"]).parts),
                job_root=None,
                label=f"public batch {record['batch']} {key}",
            )
            if _canonical_json(_tree_identity(actual_tree)) != _canonical_json(
                directory["tree"]
            ):
                raise FullBatchPublishError(
                    f"public batch {record['batch']} {key} tree differs from manifest"
                )
        array_result = record["customerArtifacts"].get("arrayModelResult")
        if isinstance(array_result, Mapping):
            public_path = root / Path(
                *PurePosixPath(str(array_result["path"])).parts
            )
            source_manifest_record = array_result["sourceComponentManifest"]
            source_manifest_path = _contained_path(
                Path(str(source_manifest_record["path"])),
                root=source_job_root,
                label=f"batch {record['batch']} Array source component manifest",
            )
            if (
                not public_path.is_file()
                or public_path.stat().st_size != array_result["size"]
                or _sha256(public_path) != array_result["sha256"]
                or not source_manifest_path.is_file()
                or source_manifest_path.stat().st_size
                != source_manifest_record["size"]
                or _sha256(source_manifest_path)
                != source_manifest_record["sha256"]
            ):
                raise FullBatchPublishError(
                    f"public Array model result evidence drifted: {record['batch']}"
                )
            public_payload = _json_object(
                public_path,
                label=f"batch {record['batch']} public Array model result",
            )
            source_manifest = _json_object(
                source_manifest_path,
                label=f"batch {record['batch']} source component manifest",
            )
            try:
                validate_customer_array_model_result(
                    public_payload,
                    component_manifest=source_manifest,
                    expected_batch_id=str(record["batch"]),
                )
            except ArrayModelResultError as exc:
                raise FullBatchPublishError(
                    f"public Array model result is invalid for batch {record['batch']}: {exc}"
                ) from exc
    reference_archive_path = root / str(reference_info["siwz"]["path"])
    try:
        validate_stackup_xml(root / "stackup.xml")
    except (ValueError, OSError) as exc:
        raise FullBatchPublishError(f"invalid public stackup XML: {exc}") from exc
    if (not reference_archive_path.is_file()
            or reference_archive_path.stat().st_size != reference_info["siwz"]["size"]
            or _sha256(reference_archive_path) != str(reference_info["siwz"]["sha256"]).casefold()):
        raise FullBatchPublishError("public reference SIWZ differs from recorded evidence")
    _validate_copy_evidence(
        root=root,
        source_job_root=source_job_root,
        batch_records=batch_records,
        reference_info=reference_info,
        copy_evidence=expected_copy_evidence,
        overview_enabled=overview_enabled,
    )

    payloads = {
        name: _json_object(root / name, label=f"public {name}")
        for name in WEB_RESULT_FILENAMES
    }
    _validate_public_web_metadata(payloads, batch_records=batch_records)
    result = payloads["result.json"]
    detail = payloads["result_detail.json"]
    if result.get("status") != "completed" or int(result.get("exitCode", -1)) != 0:
        raise FullBatchPublishError("public result.json does not represent completed FullBatch")
    if detail.get("status") != "completed" or int(detail.get("exitCode", -1)) != 0:
        raise FullBatchPublishError(
            "public result_detail.json does not represent completed FullBatch"
        )
    counts = result.get("counts") or {}
    if (
        int(counts.get("batches", -1)) != expected_batch_count
        or int(counts.get("completedBatches", -1)) != expected_batch_count
        or int(counts.get("failedBatches", -1)) != 0
    ):
        raise FullBatchPublishError("public FullBatch counts are incomplete")
    summaries = result.get("summary") or []
    detail_batches = detail.get("batches") or []
    if len(summaries) != expected_batch_count or len(detail_batches) != expected_batch_count:
        raise FullBatchPublishError("public FullBatch batch list is incomplete")
    if not all(isinstance(item, Mapping) and item.get("is_done") is True for item in summaries):
        raise FullBatchPublishError("public FullBatch contains an incomplete batch")
    forbidden_internal_keys = {
        "runDirectory",
        "runConfig",
        "generationManifest",
        "runContext",
        "channelSolveRecord",
        "tdrTransientRecord",
        "pcbCaptureManifest",
        "tdrImageRecord",
        "tdrMarkersRecord",
    }
    for item in detail_batches:
        if isinstance(item, Mapping) and forbidden_internal_keys.intersection(item):
            raise FullBatchPublishError("public result_detail exposes internal work artifacts")
    if detail.get("generationManifest") is not None:
        raise FullBatchPublishError("public result_detail exposes generationManifest")
    logs = detail.get("logs") or {}
    if isinstance(logs, Mapping) and any(value is not None for value in logs.values()):
        raise FullBatchPublishError("public result_detail exposes work/log paths")
    summary_batches: dict[str, tuple[str, Mapping[str, Any]]] = {}
    folded_batch_ids: dict[str, str] = {}
    for index, item in enumerate(summaries):
        if not isinstance(item, Mapping):
            raise FullBatchPublishError(f"public result summary[{index}] is invalid")
        batch_id = item.get("Batch")
        folder = item.get("PublicFolder")
        if not isinstance(batch_id, str) or not batch_id:
            raise FullBatchPublishError(f"public result summary[{index}] has no Batch")
        if folder != "." or batch_id not in batch_records:
            raise FullBatchPublishError(
                f"public result summary[{index}] has an invalid PublicFolder"
            )
        folded_id = batch_id.casefold()
        if folded_id in folded_batch_ids:
            raise FullBatchPublishError(
                "public result has a case-insensitive Batch collision: "
                f"{folded_batch_ids[folded_id]!r}, {batch_id!r}"
            )
        folded_batch_ids[folded_id] = batch_id
        if batch_records[batch_id]["batch"] != batch_id:
            raise FullBatchPublishError(
                f"public result summary[{index}] Batch/folder mapping differs"
            )
        if _result_item_references(
            item,
            label=f"public result summary[{index}]",
            require_touchstone=True,
            require_pcb_route=route_enabled,
        ) != batch_records[batch_id]["webFiles"]:
            raise FullBatchPublishError(
                f"public result summary[{index}] references differ from Batch folder {folder}"
            )
        summary_batches[batch_id] = (folder, item)

    for index, item in enumerate(detail_batches):
        if not isinstance(item, Mapping):
            raise FullBatchPublishError(f"public detail batch[{index}] is invalid")
        batch_id = item.get("Batch")
        summary_entry = summary_batches.get(str(batch_id))
        if summary_entry is None or item.get("PublicFolder") != summary_entry[0]:
            raise FullBatchPublishError(
                f"public detail batch[{index}] identity differs from result.json"
            )
        if _result_item_references(
            item,
            label=f"public detail batch[{index}]",
            require_touchstone=True,
            require_pcb_route=route_enabled,
        ) != batch_records[batch_id]["webFiles"]:
            raise FullBatchPublishError(
                f"public detail batch[{index}] references differ from its Batch folder"
            )

    for index, item in enumerate(detail.get("result") or []):
        if not isinstance(item, Mapping):
            raise FullBatchPublishError(f"public channel result[{index}] is invalid")
        batch_id = item.get("Batch")
        summary_entry = summary_batches.get(str(batch_id))
        if summary_entry is None or item.get("PublicFolder") != summary_entry[0]:
            raise FullBatchPublishError(
                f"public channel result[{index}] identity differs from its Batch"
            )
        if _result_item_references(
            item,
            label=f"public channel result[{index}]",
            require_touchstone=False,
            require_pcb_route=route_enabled,
        ) != batch_records[batch_id]["presentationFiles"]:
            raise FullBatchPublishError(
                f"public channel result[{index}] references differ from its Batch folder"
            )

    evaluated_rows = []
    for record in expected_public_batches:
        expected = _marker_evaluations(record.get("evaluationSources"), source_job_root)
        rows = [r for r in detail["result"] if r.get("Batch") == record["batch"]]
        if len(rows) != len(expected) or {r.get("ChannelId") for r in rows} != set(expected):
            raise FullBatchPublishError("public Marker channel set differs")
        for row in rows:
            if any(row.get(k) != v for k, v in expected[row["ChannelId"]].items()):
                raise FullBatchPublishError("public central Marker value/PASS-NG differs from waveform")
        verdict = aggregate_status(rows)
        for row in [*summaries, *detail_batches]:
            if row.get("Batch") == record["batch"] and row.get("evaluationStatus") != verdict:
                raise FullBatchPublishError("Batch PASS/NG must aggregate all channels")
        evaluated_rows.extend(rows)
    verdict = aggregate_status(evaluated_rows)
    if result.get("evaluationStatus") != verdict or detail.get("evaluationStatus") != verdict:
        raise FullBatchPublishError("overall PASS/NG must aggregate every channel")

    request_image = payloads["request.json"].get("Image") or {}
    expected_top = PUBLIC_PCB_TOP_IMAGE if overview_enabled else None
    expected_bottom = PUBLIC_PCB_BOTTOM_IMAGE if overview_enabled else None
    if (
        not isinstance(request_image, Mapping)
        or request_image.get("pcbTopImage") != expected_top
        or request_image.get("pcbBtmImage") != expected_bottom
    ):
        raise FullBatchPublishError(
            "public request.json whole-board Image references differ from the capture policy"
        )
    allowed_artifacts = allowed - expected_web
    web_reference_artifacts = {
        relative
        for record in batch_records.values()
        for relative in record["webFiles"]
    }
    if overview_enabled:
        web_reference_artifacts.update(PUBLIC_OVERVIEW_IMAGE_NAMES)
    customer_result_artifacts = {
        relative
        for record in batch_records.values()
        for relative in record["customerFiles"]
    }
    references = {
        _validate_public_reference(value, root=root, allowed_artifacts=allowed_artifacts)
        for value in _public_reference_values(payloads)
    }
    if references != web_reference_artifacts:
        raise FullBatchPublishError(
            "public Web artifact/reference sets differ: "
            f"unreferenced={sorted(web_reference_artifacts - references)}, "
            f"unknown={sorted(references - web_reference_artifacts)}"
        )
    return {
        "status": "validated",
        "contract": PROVISIONAL_PUBLISH_CONTRACT,
        "fileCount": len(actual),
        "batchCount": expected_batch_count,
        "references": sorted(references),
        "customerResultArtifacts": sorted(customer_result_artifacts),
        "reproductionArtifacts": sorted(
            allowed_artifacts
            - web_reference_artifacts
            - customer_result_artifacts
        ),
    }


def ensure_publish_target_available(outputs_dir: Path) -> None:
    if outputs_dir.exists():
        raise ExistingOutputsConflict(
            "customer outputs already exists; automatic overwrite/delete is forbidden: "
            f"{outputs_dir.resolve()}"
        )


def _same_volume_evidence(staging_tree: Path, outputs_dir: Path) -> dict[str, Any]:
    if _is_link_or_reparse(staging_tree) or not staging_tree.is_dir():
        raise FullBatchPublishError(
            f"validated staging tree is missing, symlinked, or reparse-backed: {staging_tree}"
        )
    source = staging_tree.resolve(strict=True)
    source_parent = source.parent
    destination_parent = outputs_dir.parent.resolve(strict=True)
    source_device = int(source.stat().st_dev)
    destination_device = int(destination_parent.stat().st_dev)
    source_volume = source.anchor.casefold()
    destination_volume = destination_parent.anchor.casefold()
    same_device = source_device == destination_device
    same_volume = same_device and source_volume == destination_volume
    return {
        "checkedAt": _attempt_timestamp(),
        "sourcePath": str(source),
        "sourceParentPath": str(source_parent),
        "sourceDevice": source_device,
        "sourceVolume": source_volume,
        "destinationParentPath": str(destination_parent),
        "destinationParentDevice": destination_device,
        "destinationVolume": destination_volume,
        "sameDevice": same_device,
        "sameVolume": same_volume,
    }


def _publish_tree(staging_tree: Path, outputs_dir: Path) -> dict[str, Any]:
    ensure_publish_target_available(outputs_dir)
    outputs_dir.parent.mkdir(parents=True, exist_ok=True)
    volume_evidence = _same_volume_evidence(staging_tree, outputs_dir)
    if not volume_evidence["sameDevice"] or not volume_evidence["sameVolume"]:
        raise FullBatchPublishError(
            "validated staging tree and outputs parent are not on the same device/volume: "
            f"sourceDevice={volume_evidence['sourceDevice']}, "
            f"destinationDevice={volume_evidence['destinationParentDevice']}, "
            f"sourceVolume={volume_evidence['sourceVolume']!r}, "
            f"destinationVolume={volume_evidence['destinationVolume']!r}"
        )
    # A target can appear after the initial caller preflight or while this attempt
    # was building.  Recheck immediately before the atomic directory rename.
    ensure_publish_target_available(outputs_dir)
    staging_tree.rename(outputs_dir)
    return volume_evidence


def _quarantine_failed_published_tree(
    outputs_dir: Path,
    *,
    attempt_dir: Path,
    job_root: Path,
) -> dict[str, Any]:
    quarantine = attempt_dir / "failed_published_tree"
    if quarantine.exists():
        raise FullBatchPublishError(
            f"failed publish quarantine target already exists: {quarantine}"
        )
    volume_evidence = _same_volume_evidence(outputs_dir, quarantine)
    if not volume_evidence["sameDevice"] or not volume_evidence["sameVolume"]:
        raise FullBatchPublishError(
            "failed published outputs cannot be quarantined across devices/volumes"
        )
    if quarantine.exists():
        raise FullBatchPublishError(
            f"failed publish quarantine target raced into existence: {quarantine}"
        )
    outputs_dir.rename(quarantine)
    if outputs_dir.exists() or not quarantine.is_dir():
        raise FullBatchPublishError(
            "failed published outputs quarantine rename did not move the exact tree"
        )
    tree_identity = _tree_identity(
        _directory_tree_evidence(
            quarantine,
            job_root=job_root,
            label="quarantined failed published outputs tree",
        )
    )
    return {
        "status": "quarantined",
        "path": str(quarantine),
        "treeIdentity": tree_identity,
        "sameVolumeEvidence": volume_evidence,
        "completedAt": _attempt_timestamp(),
    }


def publish_fullbatch_results(
    *,
    request_path: Path,
    job_root: Path,
    runtime_root: Path,
    batches: Sequence[BatchExecutionInput],
    started_at: datetime,
    completed_at: datetime,
    generation_manifest: Path | None = None,
) -> FullBatchPublishResult:
    job_root = job_root.resolve()
    request_path = _contained_path(
        request_path, root=job_root, label="external FullBatch request"
    )
    if not batches:
        raise FullBatchPublishError("strict FullBatch has no requested batches")
    outputs_dir = job_root / "outputs"
    ensure_publish_target_available(outputs_dir)
    started_at_ns = int(started_at.timestamp() * 1_000_000_000)

    # Batch source 검증도 attempt manifest 안에서 실행한다. 이 단계에서 실패해도
    # validation-failed 증거가 work/publish_attempts에 남아야 한다.
    attempt_dir = job_root / "work" / "publish_attempts" / uuid.uuid4().hex
    staging_tree = attempt_dir / "tree"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    attempt = _PublishAttemptManifest.create(
        attempt_dir=attempt_dir,
        request_path=request_path,
        outputs_dir=outputs_dir,
        staging_tree=staging_tree,
    )
    capture_policy = attempt.run_build_phase(
        "collecting-batch-sources",
        _fullbatch_capture_policy,
        batches,
        job_root=job_root,
    )
    route_enabled = bool(capture_policy["route"]["enabled"])
    overview_enabled = bool(capture_policy["overview"]["enabled"])
    prepared_batches: list[dict[str, Any]] = []
    internal_completion_evidence: list[dict[str, Any]] = []
    batch_directories: dict[str, str] = {}

    def _collect_batch_sources() -> None:
        for batch in batches:
            (
                batch_name,
                reports,
                waveform,
                pcb,
                reproduction,
                array_model_result,
                completion_evidence,
            ) = _strict_batch_sources(
                batch,
                job_root=job_root,
                started_at_ns=started_at_ns,
                capture_policy=capture_policy,
            )
            internal_completion_evidence.append(completion_evidence)
            folder = _safe_batch_directory(batch_name)
            folded_folder = folder.casefold()
            if folded_folder in batch_directories:
                raise FullBatchPublishError(
                    "public batch directory collision: "
                    f"{batch_directories[folded_folder]!r}, {batch_name!r}"
                )
            batch_directories[folded_folder] = batch_name
            # Human visual confirmation is appended later to the live record.
            # Preserve the exact automated Marker evidence at publish time.
            marker_snapshot = attempt_dir / "marker_evidence" / folder / "tdr_transient.json"
            marker_snapshot.parent.mkdir(parents=True, exist_ok=False)
            shutil.copy2(reports[0].parent.parent / "tdr_transient.json", marker_snapshot)
            prepared_batches.append(
                {
                    "batch": batch_name,
                    "folder": folder,
                    "evaluationSources": {
                        "config": str(batch.config_path.resolve()),
                        "transient": str(marker_snapshot),
                        "waveform": str(waveform),
                    },
                    "reports": reports,
                    "waveform": waveform,
                    "pcb": pcb,
                    "reproduction": reproduction,
                    "arrayModelResult": array_model_result,
                    "portCount": int(completion_evidence["syz"]["portCount"]),
                }
            )

    attempt.run_build_phase("collecting-batch-sources", _collect_batch_sources)

    prepared_overview: dict[str, Path] = {}

    def _collect_job_overview_sources() -> None:
        overview_dir = job_root / "work" / "fullbatch" / JOB_OVERVIEW_DIRNAME
        try:
            by_view = validate_job_overview_package(
                overview_dir, started_at_ns=started_at_ns
            )
        except (PcbCaptureContractError, OSError, RuntimeError, ValueError) as exc:
            raise FullBatchPublishError(
                f"Job PCB overview package validation failed: {exc}"
            ) from exc
        prepared_overview.update(by_view)

    if overview_enabled:
        attempt.run_build_phase(
            "collecting-batch-sources", _collect_job_overview_sources
        )

    prepared_reference: dict[str, Any] = {}

    def _collect_reference_sources() -> None:
        prepared_reference.update(_strict_reference_sources(job_root))

    attempt.run_build_phase("collecting-batch-sources", _collect_reference_sources)
    public_batch_artifacts: dict[str, dict[str, Any]] = {}
    allowed: set[str] = set(WEB_RESULT_FILENAMES)
    copy_evidence: list[dict[str, Any]] = []
    attempt.set_failure_context(
        lambda: {
            "capturePolicy": capture_policy,
            "allowlist": sorted(allowed),
            "copyEvidence": list(copy_evidence),
            "publicBatchArtifacts": dict(public_batch_artifacts),
        }
    )
    attempt.run_build_phase(
        "creating-staging-tree",
        staging_tree.mkdir,
        parents=True,
        exist_ok=False,
    )
    attempt.transition(
        "building",
        phase="copying-artifacts",
        updates={"stagingTreeState": "partial-present"},
    )
    for prepared in prepared_batches:
        batch_name = str(prepared["batch"])
        folder = str(prepared["folder"])
        reports = prepared["reports"]
        waveform = prepared["waveform"]
        pcb = prepared["pcb"]
        reproduction = prepared["reproduction"]
        array_model_result = prepared["arrayModelResult"]
        port_count = int(prepared["portCount"])
        base = PurePosixPath()
        if len(reports) != 1:
            raise FullBatchPublishError(
                f"public Batch requires exactly one native TDR report: {batch_name}"
            )
        report_relative = (
            base / f"{batch_name}{PUBLIC_TDR_IMAGE_SUFFIX}"
        ).as_posix()
        copy_evidence.append(
            attempt.run_build_phase(
                "copying-artifacts",
                _copy_public_file,
                reports[0],
                staging_tree / Path(*PurePosixPath(report_relative).parts),
                public_relative_path=report_relative,
            )
        )
        allowed.add(report_relative)
        report_refs = [report_relative]
        csv_relative = (base / f"{batch_name}{PUBLIC_TDR_CSV_SUFFIX}").as_posix()
        copy_evidence.append(
            attempt.run_build_phase(
                "copying-artifacts",
                _copy_public_file,
                waveform,
                staging_tree / Path(*PurePosixPath(csv_relative).parts),
                public_relative_path=csv_relative,
            )
        )
        allowed.add(csv_relative)
        pcb_refs: list[str] = []
        route_relative: str | None = None
        if route_enabled:
            route_relative = (
                base / f"{batch_name}{PUBLIC_PCB_IMAGE_SUFFIX}"
            ).as_posix()
            copy_evidence.append(
                attempt.run_build_phase(
                    "copying-artifacts",
                    _copy_public_jpeg,
                    pcb[STRICT_PCB_CAPTURE_VIEW],
                    staging_tree / Path(*PurePosixPath(route_relative).parts),
                    public_relative_path=route_relative,
                )
            )
            allowed.add(route_relative)
            pcb_refs.append(route_relative)
        customer_artifacts: dict[str, Any] = {}
        if array_model_result is not None:
            array_relative = (
                base / f"{batch_name}{ARRAY_MODEL_RESULT_SUFFIX}"
            ).as_posix()
            generated_source = (
                attempt_dir
                / "generated_customer_results"
                / folder
                / f"{batch_name}{ARRAY_MODEL_RESULT_SUFFIX}"
            )
            attempt.run_build_phase(
                "generating-customer-results",
                _write_json_atomic,
                generated_source,
                array_model_result["payload"],
            )
            generated_payload = _json_object(
                generated_source,
                label=f"batch {batch_name} generated Array model result",
            )
            component_manifest_path = Path(
                str(array_model_result["componentManifest"])
            )
            component_manifest = _json_object(
                component_manifest_path,
                label=f"batch {batch_name} component manifest",
            )
            try:
                validate_customer_array_model_result(
                    generated_payload,
                    component_manifest=component_manifest,
                    expected_batch_id=batch_name,
                )
            except ArrayModelResultError as exc:
                raise FullBatchPublishError(
                    f"batch {batch_name} generated Array model result is invalid: {exc}"
                ) from exc
            array_copy = attempt.run_build_phase(
                "copying-artifacts",
                _copy_public_file,
                generated_source,
                staging_tree / Path(*PurePosixPath(array_relative).parts),
                public_relative_path=array_relative,
            )
            copy_evidence.append(array_copy)
            allowed.add(array_relative)
            customer_artifacts["arrayModelResult"] = {
                "path": array_relative,
                "size": array_copy["size"],
                "sha256": array_copy["sha256"],
                "generatedSource": str(generated_source),
                "sourceComponentManifest": {
                    "path": str(component_manifest_path),
                    "size": int(array_model_result["componentManifestSize"]),
                    "sha256": str(
                        array_model_result["componentManifestSha256"]
                    ),
                },
            }
        expected_source_names = {
            "touchstone": f"{batch_name}.s{port_count}p",
            "siwz": f"{batch_name}.siwz",
            "aedt": f"{batch_name}.aedt",
            "aedtResults": f"{batch_name}.aedtresults",
            "aedtz": f"{batch_name}.aedtz",
        }
        for key, expected_name in expected_source_names.items():
            source = reproduction[key]
            if source.name != expected_name:
                raise FullBatchPublishError(
                    f"strict {key} source basename differs from the public Batch identity: "
                    f"{source.name!r} != {expected_name!r}"
                )

        reproduction_records: dict[str, Any] = {}
        source_mappings: dict[str, Any] = {}
        for key, suffix in (
            ("touchstone", f".s{port_count}p"),
            ("siwz", ".siwz"),
            ("aedtz", ".aedtz"),
        ):
            relative = (base / f"{batch_name}{suffix}").as_posix()
            copy_evidence.append(attempt.run_build_phase(
                "copying-artifacts", _copy_public_file, reproduction[key],
                staging_tree / Path(*PurePosixPath(relative).parts),
                public_relative_path=relative))
            allowed.add(relative)
            reproduction_records[key] = relative
            source_mappings[key] = {
                "source": str(reproduction[key]),
                "destinationRelativeToOutputs": relative,
                "namingPolicy": "source-basename-must-equal-public-batch-identity",
            }
        public_batch_artifacts[batch_name] = {
            "evaluationSources": {
                key: {"path": value, "size": Path(value).stat().st_size, "sha256": _sha256(Path(value))}
                for key, value in prepared["evaluationSources"].items()
            },
            "publicFolder": ".",
            "portCount": port_count,
            "tdrImages": report_refs,
            "tdrCsv": csv_relative,
            "pcbImages": pcb_refs,
            "routeImage": route_relative,
            "customerArtifacts": customer_artifacts,
            "reproductionArtifacts": reproduction_records,
            "sourceMappings": source_mappings,
        }

    reference_name = prepared_reference["siwz"].name
    reference_copy = attempt.run_build_phase(
        "copying-artifacts", _copy_public_file, prepared_reference["siwz"],
        staging_tree / reference_name, public_relative_path=reference_name)
    copy_evidence.append(reference_copy)
    allowed.add(reference_name)
    xml_source = job_root / "work" / "reference_archive" / "stackup.xml"
    copy_evidence.append(attempt.run_build_phase(
        "copying-artifacts", _copy_public_file, xml_source,
        staging_tree / "stackup.xml", public_relative_path="stackup.xml"))
    allowed.add("stackup.xml")
    expected_public_reference = {
        "siwz": {"path": reference_name, "size": reference_copy["size"],
                 "sha256": reference_copy["sha256"]},
        "sourceManifest": str(prepared_reference["manifest"]),
        "sourceManifestId": str(prepared_reference["manifestId"]),
    }

    if overview_enabled:
        for view, overview_name in (
            ("top", PUBLIC_PCB_TOP_IMAGE),
            ("bottom", PUBLIC_PCB_BOTTOM_IMAGE),
        ):
            copy_evidence.append(
                attempt.run_build_phase(
                    "copying-artifacts",
                    _copy_public_jpeg,
                    prepared_overview[view],
                    staging_tree / overview_name,
                    public_relative_path=overview_name,
                )
            )
            allowed.add(overview_name)

    attempt.run_build_phase(
        "exporting-web-results",
        export_eden_web_results,
        request_path=request_path,
        output_dir=staging_tree,
        runtime_root=runtime_root,
        batches=batches,
        exit_code=0,
        started_at=started_at,
        completed_at=completed_at,
        requested_operations={
            "generateConfig": True,
            "applyPorts": True,
            "setupSyz": True,
            "solveTouchstone": True,
            "runTdr": True,
            "tdrOnly": False,
            "planPcbCapture": False,
            "capturePcbRoutes": route_enabled,
            "capturePcbOverview": overview_enabled,
        },
        generation_manifest=generation_manifest,
        public_batch_artifacts=public_batch_artifacts,
        public_overview_artifacts=(
            {
                "top": PUBLIC_PCB_TOP_IMAGE,
                "bottom": PUBLIC_PCB_BOTTOM_IMAGE,
            }
            if overview_enabled
            else None
        ),
        include_internal_references=False,
    )
    expected_public_batches = [
        {
            "batch": batch_name,
            "publicFolder": artifacts["publicFolder"],
            "evaluationSources": artifacts["evaluationSources"],
            "portCount": artifacts["portCount"],
            "webArtifacts": {
                key: artifacts[key]
                for key in (
                    "tdrImages",
                    "tdrCsv",
                    "pcbImages",
                    "routeImage",
                )
            },
            "customerArtifacts": artifacts["customerArtifacts"],
            "reproductionArtifacts": artifacts["reproductionArtifacts"],
            "sourceMappings": artifacts["sourceMappings"],
        }
        for batch_name, artifacts in sorted(public_batch_artifacts.items())
    ]
    validation = attempt.run_build_phase(
        "validating-staging-tree",
        validate_staged_publish_tree,
        staging_tree,
        allowed_relative_paths=sorted(allowed),
        expected_batch_count=len(batches),
        expected_public_batches=expected_public_batches,
        expected_copy_evidence=copy_evidence,
        source_job_root=job_root,
        expected_public_reference=expected_public_reference,
        expected_capture_policy=capture_policy,
    )
    validated_tree_identity = attempt.run_build_phase(
        "validating-staging-tree",
        lambda: _tree_identity(
            _directory_tree_evidence(
                staging_tree,
                job_root=job_root,
                label="validated public staging tree",
            )
        ),
    )
    ready_at = _attempt_timestamp()
    attempt.transition(
        "validated-ready-to-publish",
        phase="validated-ready-to-publish",
        updates={
            "validatedAt": ready_at,
            "stagingTreeState": "validated-present",
            "allowlist": sorted(allowed),
            "batchDirectoryMappings": [
                {
                    "batch": batch_name,
                    "publicFolder": artifacts["publicFolder"],
                }
                for batch_name, artifacts in sorted(public_batch_artifacts.items())
            ],
            "publicBatchArtifacts": expected_public_batches,
            "capturePolicy": capture_policy,
            "publicReferenceArtifacts": expected_public_reference,
            "webReferencedArtifacts": validation["references"],
            "customerResultArtifacts": validation["customerResultArtifacts"],
            "reproductionArtifacts": validation["reproductionArtifacts"],
            "copyEvidence": copy_evidence,
            "internalCompletionEvidence": internal_completion_evidence,
            "validation": validation,
            "validatedTreeIdentity": validated_tree_identity,
        },
    )
    manifest_path = attempt.path
    volume_evidence: dict[str, Any] | None = None
    try:
        volume_evidence = _publish_tree(staging_tree, outputs_dir)
        attempt.transition(
            "validated-ready-to-publish",
            phase="validating-final-outputs",
            updates={
                "sameVolumeEvidence": volume_evidence,
                "stagingTreeState": "renamed-to-final-validation-pending",
            },
        )
        final_validation = validate_staged_publish_tree(
            outputs_dir,
            allowed_relative_paths=sorted(allowed),
            expected_batch_count=len(batches),
            expected_public_batches=expected_public_batches,
            expected_copy_evidence=copy_evidence,
            source_job_root=job_root,
            expected_public_reference=expected_public_reference,
            expected_capture_policy=capture_policy,
        )
        final_tree_identity = _tree_identity(
            _directory_tree_evidence(
                outputs_dir,
                job_root=job_root,
                label="published final outputs tree",
            )
        )
        if _canonical_json(final_validation) != _canonical_json(validation):
            raise FullBatchPublishError(
                "final outputs validation differs from the validated staging evidence"
            )
        if _canonical_json(final_tree_identity) != _canonical_json(
            validated_tree_identity
        ):
            raise FullBatchPublishError(
                "final outputs tree identity differs from the validated staging tree"
            )
        if staging_tree.exists():
            raise FullBatchPublishError(
                "atomic publish returned but the old staging tree still exists"
            )
        published_at = _attempt_timestamp()
        attempt.transition(
            "published",
            phase="published",
            updates={
                "publishedAt": published_at,
                "completedAt": published_at,
                "finalOutputsPath": str(outputs_dir),
                "finalOutputsValidation": final_validation,
                "finalOutputsTree": final_tree_identity,
                "stagingTreeState": "renamed-to-final-outputs; expected-absent",
            },
        )
    except Exception as exc:
        quarantine_evidence: dict[str, Any] | None = None
        quarantine_error: dict[str, str] | None = None
        attempt_created_outputs = volume_evidence is not None and outputs_dir.exists()
        if attempt_created_outputs and not staging_tree.exists():
            try:
                quarantine_evidence = _quarantine_failed_published_tree(
                    outputs_dir,
                    attempt_dir=attempt_dir,
                    job_root=job_root,
                )
            except Exception as quarantine_exc:
                quarantine_error = _safe_attempt_error(
                    quarantine_exc, phase="quarantining-failed-published-tree"
                )
                quarantine_path = attempt_dir / "failed_published_tree"
                if not outputs_dir.exists() and quarantine_path.is_dir():
                    quarantine_evidence = {
                        "status": "quarantined-evidence-failed",
                        "path": str(quarantine_path),
                        "error": quarantine_error,
                    }
        if volume_evidence is None and staging_tree.exists():
            try:
                volume_evidence = _same_volume_evidence(staging_tree, outputs_dir)
            except Exception:
                volume_evidence = None
        failure_updates: dict[str, Any] = {
            "sameVolumeEvidence": volume_evidence,
            "stagingTreeState": (
                "renamed-then-quarantined-after-final-validation-failure"
                if quarantine_evidence is not None
                else
                "preserved-after-publish-failure"
                if staging_tree.exists()
                else "renamed-or-missing-after-publish-failure"
            ),
            "finalOutputsPath": str(outputs_dir),
            "finalOutputsExists": outputs_dir.exists(),
            "unsafeExposedOutputs": bool(
                attempt_created_outputs and outputs_dir.exists()
            ),
            "quarantine": quarantine_evidence,
            "quarantineError": quarantine_error,
        }
        attempt.transition(
            "publish-failed",
            phase=(
                "validating-final-outputs"
                if outputs_dir.exists()
                else "publishing-atomic-rename"
            ),
            updates=failure_updates,
            error=exc,
        )
        if isinstance(exc, FullBatchPublishError):
            raise
        raise FullBatchPublishError(
            f"validated FullBatch publish failed; attempt evidence was preserved at "
            f"{attempt_dir}: {exc}"
        ) from exc
    published_manifest = _json_object(manifest_path, label="published attempt manifest")
    if published_manifest.get("status") != "published" or not outputs_dir.is_dir():
        raise FullBatchPublishError(
            "FullBatch publish API cannot return before a published manifest and final outputs exist"
        )
    return FullBatchPublishResult(
        contract=PROVISIONAL_PUBLISH_CONTRACT,
        output_dir=outputs_dir,
        manifest_path=manifest_path,
        public_files=tuple(sorted(allowed)),
    )


class DirectorySnapshotRecorder:
    """Record Job directory creation by orchestration stage outside outputs."""

    def __init__(self, job_root: Path, evidence_path: Path | None = None):
        self.job_root = job_root.resolve()
        self.evidence_path = (
            evidence_path.resolve()
            if evidence_path is not None
            else self.job_root
            / "work"
            / "fullbatch_orchestration"
            / "directory_snapshots.json"
        )
        self._previous: set[str] = set()
        self._entries: list[dict[str, Any]] = []

    def _directories(self) -> set[str]:
        directories = {"."}
        for path in self.job_root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                directories.add(path.relative_to(self.job_root).as_posix())
        return directories

    def capture(self, stage: str, *, creator: str) -> Path:
        if not stage.strip() or not creator.strip():
            raise FullBatchPublishError("directory snapshot requires stage and creator")
        current = self._directories()
        self._entries.append(
            {
                "stage": stage,
                "creator": creator,
                "capturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "directories": sorted(current),
                "createdSincePrevious": sorted(current - self._previous),
                "outputsExists": (self.job_root / "outputs").exists(),
            }
        )
        self._previous = current
        _write_json_atomic(
            self.evidence_path,
            {
                "schema": SNAPSHOT_SCHEMA,
                "jobRoot": str(self.job_root),
                "evidenceLocation": "work (not customer outputs)",
                "snapshots": self._entries,
            },
        )
        return self.evidence_path
