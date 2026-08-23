from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .ansys_backend import AcquiredAnfCmp, AnsysReferenceBackend, BuiltReference
from .contracts import (
    CONTRACT_NAME,
    CONTRACT_SCHEMA_VERSION,
    DEFAULT_MANIFEST_NAME,
    PREPROCESSOR_IMPLEMENTATION_VERSION,
    ReferencePreprocessError,
    ReferencePreprocessRequest,
    ReferencePreprocessResult,
    resolve_reference_preprocess_request,
)


class ReferencePreprocessBackend(Protocol):
    def acquire_zuken(
        self,
        request: ReferencePreprocessRequest,
        attempt_dir: Path,
    ) -> AcquiredAnfCmp: ...

    def build_from_anf_cmp(
        self,
        request: ReferencePreprocessRequest,
        anf: Path,
        cmp_path: Path,
        attempt_dir: Path,
    ) -> BuiltReference: ...

    def export_aedb_from_siw(
        self,
        request: ReferencePreprocessRequest,
        attempt_dir: Path,
    ) -> BuiltReference: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_directory(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    file_count = 0
    size_bytes = 0
    files = (
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file()
        and not (
            candidate.name.casefold() == "edb.def.tmp"
            and candidate.stat().st_size == 0
        )
    )
    for item in sorted(files):
        relative = item.relative_to(path).as_posix()
        size = item.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        size_bytes += size
    return digest.hexdigest(), file_count, size_bytes


def _portable_path(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:
        return str(path.resolve())


def _artifact(path: Path, *, base: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.is_file():
        return {
            "kind": "file",
            "path": _portable_path(path, base),
            "sizeBytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
    if path.is_dir():
        digest, file_count, size_bytes = _sha256_directory(path)
        return {
            "kind": "directory",
            "path": _portable_path(path, base),
            "fileCount": file_count,
            "sizeBytes": size_bytes,
            "sha256": digest,
        }
    raise ReferencePreprocessError(f"artifact not found: {path}")


def _resolve_artifact(record: Any, *, manifest_path: Path, label: str) -> Path:
    if not isinstance(record, dict):
        raise ReferencePreprocessError(f"manifest {label} must be an object")
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ReferencePreprocessError(f"manifest {label}.path is invalid")
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_path.parent / path
    actual = _artifact(path, base=manifest_path.parent)
    for key in ("kind", "sizeBytes", "sha256", "fileCount"):
        if key in actual and record.get(key) != actual[key]:
            raise ReferencePreprocessError(
                f"manifest {label}.{key} mismatch for {path.resolve()}"
            )
    return path.resolve()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_paths(request: ReferencePreprocessRequest) -> dict[str, Path]:
    candidates = {
        "referenceSiw": request.reference_siw,
        "referenceAedb": request.reference_aedb,
        "zukenDesign": request.design,
        "anf": request.anf,
        "cmp": request.cmp,
        "stackup": request.stackup,
        "bom": request.bom,
        "pmap": request.pmap,
        "sws": request.sws,
    }
    return {key: value for key, value in candidates.items() if value is not None}


def _request_identity(request: ReferencePreprocessRequest) -> dict[str, Any]:
    artifact_identities = {
        key: _artifact(path, base=request.config_path.parent)
        for key, path in _input_paths(request).items()
    }
    return {
        "contract": CONTRACT_NAME,
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "implementationVersion": PREPROCESSOR_IMPLEMENTATION_VERSION,
        "configSha256": _sha256_file(request.config_path),
        "mode": request.mode,
        "modeSource": request.mode_source,
        "referencePolicy": request.reference_policy,
        "outputName": request.output_name,
        "aedtVersion": request.aedt_version,
        "inputs": artifact_identities,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _result_from_manifest(payload: dict[str, Any], manifest_path: Path) -> ReferencePreprocessResult:
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        raise ReferencePreprocessError("manifest outputs must be an object")
    siw = _resolve_artifact(
        outputs.get("referenceSiw"),
        manifest_path=manifest_path,
        label="outputs.referenceSiw",
    )
    aedb = _resolve_artifact(
        outputs.get("referenceAedb"),
        manifest_path=manifest_path,
        label="outputs.referenceAedb",
    )
    if not (aedb / "edb.def").is_file():
        raise ReferencePreprocessError(f"manifest reference AEDB is incomplete: {aedb}")
    return ReferencePreprocessResult(
        config_path=_resolve_artifact(
            payload.get("config"),
            manifest_path=manifest_path,
            label="config",
        ),
        mode=str(payload["request"]["mode"]),
        mode_source=str(payload["request"]["modeSource"]),
        reference_siw=siw,
        reference_aedb=aedb,
        manifest_path=manifest_path.resolve(),
        manifest_id=str(payload["manifestId"]),
        stages=tuple(payload.get("stages") or []),
        aedt_version=(
            str(payload["request"]["aedtVersion"])
            if payload["request"].get("aedtVersion") is not None
            else None
        ),
        implementation_version=(
            int(payload["request"]["implementationVersion"])
            if payload["request"].get("implementationVersion") is not None
            else None
        ),
    )


def load_reference_preprocess_manifest(manifest_path: Path) -> ReferencePreprocessResult:
    manifest_path = manifest_path.resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReferencePreprocessError(f"preprocess manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ReferencePreprocessError(f"invalid preprocess manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReferencePreprocessError("preprocess manifest root must be an object")
    if payload.get("contract") != CONTRACT_NAME or payload.get("schemaVersion") != CONTRACT_SCHEMA_VERSION:
        raise ReferencePreprocessError("unsupported reference preprocess manifest contract")
    if payload.get("status") != "ready":
        raise ReferencePreprocessError(
            f"reference preprocess manifest is not ready: {payload.get('status')!r}"
        )
    manifest_id = payload.get("manifestId")
    unsigned = dict(payload)
    unsigned.pop("manifestId", None)
    if not isinstance(manifest_id, str) or manifest_id != _canonical_hash(unsigned):
        raise ReferencePreprocessError("reference preprocess manifestId mismatch")
    return _result_from_manifest(payload, manifest_path)


def _next_attempt(job_dir: Path) -> tuple[int, Path]:
    existing = [
        int(path.name.split("-", 1)[1])
        for path in job_dir.glob("attempt-*")
        if path.is_dir() and path.name.split("-", 1)[1].isdigit()
    ]
    number = max(existing, default=0) + 1
    attempt_dir = job_dir / f"attempt-{number:03d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    return number, attempt_dir


def _manifest_context(
    request: ReferencePreprocessRequest,
    *,
    job_dir: Path,
) -> dict[str, Any]:
    """Snapshot stable request evidence once, before live tools can fail."""

    return {
        "config": _artifact(request.config_path, base=job_dir),
        "request": {
            "mode": request.mode,
            "modeSource": request.mode_source,
            "referencePolicy": request.reference_policy,
            "outputName": request.output_name,
            "aedtVersion": request.aedt_version,
            "implementationVersion": PREPROCESSOR_IMPLEMENTATION_VERSION,
        },
        "inputs": {
            key: _artifact(path, base=job_dir)
            for key, path in _input_paths(request).items()
        },
    }


def _base_manifest(
    context: dict[str, Any],
    *,
    fingerprint: str,
    attempt_number: int,
    started: str,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT_NAME,
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "startedAt": started,
        "attempt": attempt_number,
        "fingerprint": fingerprint,
        **context,
    }


def _write_manifest_records(
    manifest_path: Path,
    *,
    attempt_number: int,
    payload: dict[str, Any],
) -> None:
    """Write an immutable attempt record, then update the current pointer."""

    attempt_manifest = manifest_path.with_name(
        f"reference_preprocess_manifest.attempt-{attempt_number:03d}.json"
    )
    _write_json_atomic(attempt_manifest, payload)
    _write_json_atomic(manifest_path, payload)


def _execute_reference_build(
    request: ReferencePreprocessRequest,
    backend: ReferencePreprocessBackend,
    attempt_dir: Path,
    stages: list[dict[str, Any]],
) -> BuiltReference:
    if request.mode == "zuken_design":
        acquired = backend.acquire_zuken(request, attempt_dir)
        stages.append(
            {
                "name": "zuken_design_to_anf_cmp",
                "status": "completed",
                "evidence": acquired.evidence,
            }
        )
        built = backend.build_from_anf_cmp(
            request,
            acquired.anf,
            acquired.cmp,
            attempt_dir,
        )
        stages.append(
            {
                "name": "reference_build",
                "status": "completed",
                "evidence": built.evidence,
            }
        )
        return built

    if request.mode == "anf_cmp":
        assert request.anf is not None and request.cmp is not None
        built = backend.build_from_anf_cmp(
            request,
            request.anf,
            request.cmp,
            attempt_dir,
        )
        stages.append(
            {
                "name": "reference_build",
                "status": "completed",
                "evidence": built.evidence,
            }
        )
        return built

    if request.reference_aedb is None:
        built = backend.export_aedb_from_siw(request, attempt_dir)
        stages.append(
            {
                "name": "reference_siw_export",
                "status": "completed",
                "evidence": built.evidence,
            }
        )
        return built

    assert request.reference_siw is not None
    built = BuiltReference(
        reference_siw=request.reference_siw,
        reference_aedb=request.reference_aedb,
        evidence={"action": "validated_existing_reference_pair"},
    )
    stages.append(
        {
            "name": "reference_pair_validation",
            "status": "completed",
            "evidence": built.evidence,
        }
    )
    return built


def run_reference_preprocessor(
    config_path: Path,
    *,
    work_dir: Path,
    runtime_root: Path | None = None,
    backend: ReferencePreprocessBackend | None = None,
) -> ReferencePreprocessResult:
    """Create or reuse the SI-TDR-owned reference pair for one Config.

    DCIR is never imported or executed.  The injected backend is the only
    license-dependent boundary, which keeps contract and manifest tests pure.
    """

    request = resolve_reference_preprocess_request(
        config_path,
        work_dir=work_dir,
        runtime_root=runtime_root,
    )
    identity = _request_identity(request)
    fingerprint = _canonical_hash(identity)
    job_dir = request.work_dir / fingerprint[:16]
    manifest_path = job_dir / DEFAULT_MANIFEST_NAME
    if manifest_path.is_file():
        try:
            return load_reference_preprocess_manifest(manifest_path)
        except ReferencePreprocessError:
            # A failed attempt or output drift is recoverable without deleting
            # prior evidence; a new numbered attempt is created below.
            pass

    job_dir.mkdir(parents=True, exist_ok=True)
    attempt_number, attempt_dir = _next_attempt(job_dir)
    backend = backend or AnsysReferenceBackend()
    stages: list[dict[str, Any]] = []
    started = _utc_now()
    base_manifest = _base_manifest(
        _manifest_context(request, job_dir=job_dir),
        fingerprint=fingerprint,
        attempt_number=attempt_number,
        started=started,
    )
    try:
        built = _execute_reference_build(request, backend, attempt_dir, stages)

        outputs = {
            "referenceSiw": _artifact(built.reference_siw, base=job_dir),
            "referenceAedb": _artifact(built.reference_aedb, base=job_dir),
        }
        payload: dict[str, Any] = {
            **base_manifest,
            "status": "ready",
            "createdAt": _utc_now(),
            "outputs": outputs,
            "stages": stages,
            "runtime": {
                "owner": "SI_TDR",
                "dcirRuntimeInvoked": False,
                "dcirPostInvoked": False,
                "dcirCaseSiwGenerated": False,
            },
            "unresolved": [
                {
                    "code": "product_family_preprocess_policy_not_inferred",
                    "disposition": "use explicit preprocessing.referencePolicy and settings",
                }
            ],
        }
        payload["manifestId"] = _canonical_hash(payload)
        _write_manifest_records(
            manifest_path,
            attempt_number=attempt_number,
            payload=payload,
        )
        return _result_from_manifest(payload, manifest_path)
    except Exception as exc:
        failure = {
            **base_manifest,
            "status": "failed",
            "createdAt": _utc_now(),
            "stages": stages,
            "runtime": {"owner": "SI_TDR", "dcirRuntimeInvoked": False},
            "failure": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        failure["manifestId"] = _canonical_hash(failure)
        _write_manifest_records(
            manifest_path,
            attempt_number=attempt_number,
            payload=failure,
        )
        if isinstance(exc, ReferencePreprocessError):
            raise
        raise ReferencePreprocessError(f"reference preprocessing failed: {exc}") from exc
