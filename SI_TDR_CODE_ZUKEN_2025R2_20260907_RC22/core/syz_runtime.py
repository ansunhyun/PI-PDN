"""Strict customer SIWave SYZ option, solve, and artifact contracts.

This module deliberately keeps the customer file-based workflow separate from
the legacy PoC numeric sweep workflow in ``main.py``.  Supported SIWave releases expose a
documented SWS import API but no documented SFSDF loader.  The production
default therefore parses the statically confirmed SFSDF frequency grammar and
applies it through documented sweep commands.  A native loader remains an
explicit extension point and must supply verified read-back evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from .channel.analysis_options import ANALYSIS_OPTION_FOLDER_PARTS
except ImportError:  # Support compact customer core imports.
    from channel.analysis_options import (  # type: ignore[no-redef]
        ANALYSIS_OPTION_FOLDER_PARTS,
    )

try:
    from .customer_pre_solve import (
        CustomerPreSolveError,
        validate_customer_pre_solve,
    )
except ImportError:  # Support compact customer core imports.
    from customer_pre_solve import (  # type: ignore[no-redef]
        CustomerPreSolveError,
        validate_customer_pre_solve,
    )

try:
    from .syz_options import SyzOptionError, normalize_syz_settings
    from .sfsdf_sweep import (
        PARSER_COMMAND_APPLY_MODE,
        PARSER_COMMAND_CAPABILITY_IDENTITY,
        SFSDF_APPLICATION_SCHEMA,
        SfsdfSweepError,
        apply_administrator_syz_options,
        apply_sfsdf_parser_commands,
        parse_sfsdf_frequency_definition,
        validate_administrator_syz_option_application,
        validate_sfsdf_command_application,
        frequency_grid_sha256,
    )
except ImportError:  # Support compact customer core imports.
    from syz_options import (  # type: ignore[no-redef]
        SyzOptionError,
        normalize_syz_settings,
    )
    from sfsdf_sweep import (  # type: ignore[no-redef]
        PARSER_COMMAND_APPLY_MODE,
        PARSER_COMMAND_CAPABILITY_IDENTITY,
        SFSDF_APPLICATION_SCHEMA,
        SfsdfSweepError,
        apply_administrator_syz_options,
        apply_sfsdf_parser_commands,
        parse_sfsdf_frequency_definition,
        validate_administrator_syz_option_application,
        validate_sfsdf_command_application,
        frequency_grid_sha256,
    )


SWS_IMPORT_API = "ScrImportSIwaveSimulationOptions"
SFSDF_ROUNDTRIP_SCHEMA = "si-tdr-sfsdf-roundtrip/1"
SFSDF_GRID_EQUIVALENCE_SCHEMA = "si-tdr-sfsdf-grid-equivalence/1"
SFSDF_APPLY_MODES = {"direct-loader-readback"}
SUPPORTED_AEDT_VERSIONS = frozenset({"2024.2", "2025.2"})
SFSDF_CAPABILITY_PENDING = (
    "SIWave 2025 R2 documents GUI Save/Load for .sfsdf but no SFSDF load/import "
    "command is present in the published scripting command set"
)
SELECTION_SOURCES = {"Spec.SYZ_Option", "administratorDefault"}
_PORT_HEADER = re.compile(r"^!\s*Port\[(\d+)\]\s*=\s*(.*?)\s*$", re.IGNORECASE)
_TOUCHSTONE_SUFFIX = re.compile(r"\.s(\d+)p$", re.IGNORECASE)
_FREQUENCY_SCALE = {
    "hz": 1.0,
    "khz": 1.0e3,
    "mhz": 1.0e6,
    "ghz": 1.0e9,
}


class StrictSyzError(RuntimeError):
    """Raised when a strict customer SYZ contract cannot be proven."""


@dataclass(frozen=True)
class SelectedOptionFile:
    kind: str
    name: str
    path: Path
    sha256: str

    def evidence(self) -> dict[str, Any]:
        return {
            "fileName": self.name,
            "resolvedPath": str(self.path),
            "sha256": self.sha256,
            "sizeBytes": self.path.stat().st_size,
        }


@dataclass(frozen=True)
class StrictSyzSelection:
    batch_id: str
    profile_id: str
    selection_source: str
    aedt_version: str
    job_root: Path
    sws: SelectedOptionFile
    sfsdf: SelectedOptionFile
    settings: dict[str, Any]


@dataclass(frozen=True)
class SfsdfApplyCapability:
    """A capability backed by verified SIWave 2025.2 behavior.

    ``apply`` must load the exact supplied SFSDF into the open SIWave project
    and return non-zero/truthy evidence. ``verify`` must independently return
    the strict round-trip contract after reading back the applied state. This
    extension point is reserved for a native direct-loader/read-back
    implementation. The production default uses the strict parser-to-documented
    command adapter and does not claim native loader read-back.
    """

    name: str
    apply: Callable[[Any, Path], Any]
    verify: Callable[[Any, Path], Mapping[str, Any]] | None = None


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def siwave_results_directory_evidence(path: Path) -> dict[str, Any]:
    """Record the uncompressed ``<Batch>.siwaveresults`` tree beside the SIW.

    9.4.6: 비압축 SIWave 세트(<Batch>.siw + <Batch>.siwaveresults/)가 공개
    대상이므로 publisher가 재검증할 recursive manifest를 남긴다.
    """

    resolved = path.resolve()
    if resolved.is_symlink() or not resolved.is_dir():
        raise StrictSyzError(
            f"SIWave results directory is missing or symlinked: {resolved}"
        )
    members = sorted(resolved.rglob("*"), key=lambda item: str(item))
    directories: list[str] = []
    manifest: list[dict[str, Any]] = []
    for item in members:
        if item.is_symlink():
            raise StrictSyzError(
                f"SIWave results directory contains a symlink: {item}"
            )
        relative = item.relative_to(resolved).as_posix()
        if item.is_dir():
            directories.append(relative)
            continue
        stat = item.stat()
        # SIWave는 자기 결과 트리에 0바이트 로그를 남긴다
        # (예: 0000.slog_geomproc_*.log, 2026-08-20 R19 실행 관측).
        # Ansys 생성 트리 안에서는 빈 파일을 거부하지 않고 그대로 기록한다.
        manifest.append(
            {
                "relativePath": relative,
                "size": stat.st_size,
                "sha256": _sha256(item),
            }
        )
    if not manifest:
        raise StrictSyzError(f"SIWave results directory is empty: {resolved}")
    directories.sort()
    manifest.sort(key=lambda item: str(item["relativePath"]))
    return {
        "path": str(resolved),
        "directories": directories,
        "fileManifest": manifest,
        "fileCount": len(manifest),
        "totalBytes": sum(int(item["size"]) for item in manifest),
        "treeSha256": hashlib.sha256(
            _canonical_json_bytes(
                {"directories": directories, "fileManifest": manifest}
            )
        ).hexdigest(),
    }


def _artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "sizeBytes": None,
            "sha256": None,
            "mtimeNs": None,
        }
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "exists": True,
        "sizeBytes": stat.st_size,
        "sha256": _sha256(path),
        "mtimeNs": stat.st_mtime_ns,
    }


def _require_success(result: Any, operation: str) -> dict[str, Any]:
    if result is None or result is False or result == 0:
        raise StrictSyzError(
            f"{operation} did not return positive success evidence: {result!r}"
        )
    return {"pythonType": type(result).__name__, "repr": repr(result)}


def _validate_grid_contract(raw: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StrictSyzError(f"{label} must be an object")
    count = raw.get("frequencyCount")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise StrictSyzError(f"{label}.frequencyCount must be a positive integer")
    frequencies: dict[str, float] = {}
    for field in ("firstFrequencyHz", "lastFrequencyHz"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrictSyzError(f"{label}.{field} must be a finite number")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise StrictSyzError(f"{label}.{field} must be a finite non-negative number")
        frequencies[field] = numeric
    if count == 1 and frequencies["firstFrequencyHz"] != frequencies["lastFrequencyHz"]:
        raise StrictSyzError(f"{label} single-point first/last frequencies must match")
    if count > 1 and frequencies["firstFrequencyHz"] >= frequencies["lastFrequencyHz"]:
        raise StrictSyzError(f"{label} multi-point first frequency must be below last frequency")
    grid_hash = str(raw.get("frequencyGridSha256") or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", grid_hash) is None:
        raise StrictSyzError(f"{label}.frequencyGridSha256 must be a SHA-256 value")
    return {
        "frequencyCount": count,
        **frequencies,
        "frequencyGridSha256": grid_hash,
    }


def _validate_sfsdf_round_trip(
    raw: Any,
    *,
    expected_source_hash: str,
    expected_capability_identity: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StrictSyzError("SFSDF round-trip verifier output must be an object")
    if raw.get("schema") != SFSDF_ROUNDTRIP_SCHEMA:
        raise StrictSyzError(
            "SFSDF round-trip verifier schema must be " + SFSDF_ROUNDTRIP_SCHEMA
        )
    if raw.get("status") != "verified":
        raise StrictSyzError("SFSDF round-trip verifier must return status=verified")
    apply_mode = str(raw.get("applyMode") or "").strip()
    if apply_mode not in SFSDF_APPLY_MODES:
        raise StrictSyzError(
            "SFSDF round-trip applyMode must be one of "
            f"{sorted(SFSDF_APPLY_MODES)}"
        )
    capability_identity = str(raw.get("capabilityIdentity") or "").strip()
    evidence_identity = str(raw.get("evidenceIdentity") or "").strip()
    if not capability_identity or not evidence_identity:
        raise StrictSyzError(
            "SFSDF round-trip capabilityIdentity and evidenceIdentity are required"
        )
    if (
        expected_capability_identity is not None
        and capability_identity != expected_capability_identity
    ):
        raise StrictSyzError(
            "SFSDF round-trip capability identity differs from the apply capability: "
            f"expected={expected_capability_identity!r}, actual={capability_identity!r}"
        )
    source_hash = str(raw.get("sourceSfsdfSha256") or "").strip().casefold()
    if source_hash != expected_source_hash.casefold():
        raise StrictSyzError(
            "SFSDF round-trip source hash differs from the selected file: "
            f"expected={expected_source_hash}, actual={source_hash or '<missing>'}"
        )
    verification = raw.get("verificationEvidence")
    if not isinstance(verification, Mapping):
        raise StrictSyzError("SFSDF round-trip verificationEvidence must be an object")
    method = str(verification.get("method") or "").strip()
    read_back = verification.get("readBack")
    if not method or not isinstance(read_back, Mapping) or not read_back:
        raise StrictSyzError(
            "SFSDF round-trip verificationEvidence requires method and non-empty readBack evidence"
        )
    expected_grid = _validate_grid_contract(
        raw.get("expectedSolutionGrid"),
        label="SFSDF round-trip expectedSolutionGrid",
    )
    normalized = dict(raw)
    normalized["sourceSfsdfSha256"] = source_hash
    normalized["expectedSolutionGrid"] = expected_grid
    normalized["verificationEvidence"] = dict(verification)
    return normalized


def _require_identical_grids(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    fields = (
        "frequencyCount",
        "firstFrequencyHz",
        "lastFrequencyHz",
        "frequencyGridSha256",
    )
    differences = {
        field: {"expected": expected.get(field), "actual": actual.get(field)}
        for field in fields
        if expected.get(field) != actual.get(field)
    }
    if differences:
        raise StrictSyzError(
            "exported Touchstone grid differs from verified SFSDF expected grid: "
            + json.dumps(differences, sort_keys=True)
        )


def _batch_id(context: Mapping[str, Any]) -> str:
    batch_id = str((context.get("segment") or {}).get("name") or "").strip()
    if (
        not batch_id
        or batch_id in {".", ".."}
        or Path(batch_id).name != batch_id
        or any(separator in batch_id for separator in ("/", "\\"))
    ):
        raise StrictSyzError(f"invalid strict SYZ batch ID: {batch_id!r}")
    configured_basename = str(
        (context.get("syz") or {}).get("touchstoneBaseName") or batch_id
    ).strip()
    if configured_basename != batch_id:
        raise StrictSyzError(
            "strict SYZ output basename must equal the current batch ID; "
            f"batch={batch_id!r}, touchstoneBaseName={configured_basename!r}"
        )
    return batch_id


def _validate_selected_file(
    raw: Any,
    *,
    kind: str,
    expected_folder_parts: tuple[str, ...],
    expected_suffix: str,
    job_root: Path,
) -> SelectedOptionFile:
    if not isinstance(raw, Mapping):
        raise StrictSyzError(f"analysisOptionSelection.syz.{kind} must be an object")
    required = ("fileName", "resolvedPath", "sha256")
    missing = [name for name in required if not str(raw.get(name) or "").strip()]
    if missing:
        raise StrictSyzError(
            f"analysisOptionSelection.syz.{kind} is missing {missing}"
        )
    file_name = str(raw["fileName"]).strip()
    if Path(file_name).name != file_name:
        raise StrictSyzError(f"{kind} fileName must be a basename: {file_name!r}")
    if Path(file_name).suffix.casefold() != expected_suffix:
        raise StrictSyzError(
            f"{kind} file must use {expected_suffix}: {file_name!r}"
        )
    path = Path(str(raw["resolvedPath"])).resolve()
    if not path.is_file():
        raise StrictSyzError(f"selected {kind} file is missing: {path}")
    if path.name != file_name:
        raise StrictSyzError(
            f"selected {kind} fileName/path mismatch: {file_name!r} != {path.name!r}"
        )
    folder_label = "/".join(expected_folder_parts)
    try:
        resolved_job_root = job_root.resolve(strict=True)
        expected_folder = resolved_job_root.joinpath(*expected_folder_parts).resolve(
            strict=True
        )
    except OSError as exc:
        raise StrictSyzError(
            f"selected {kind} required Job folder cannot be resolved: {folder_label}/"
        ) from exc
    if path.parent != expected_folder:
        raise StrictSyzError(
            f"selected {kind} must be directly under Job-local {folder_label}/: {path}"
        )
    if path.stat().st_size <= 0:
        raise StrictSyzError(f"selected {kind} file is empty: {path}")
    expected_hash = str(raw["sha256"]).strip().casefold()
    actual_hash = _sha256(path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise StrictSyzError(f"selected {kind} SHA-256 is invalid: {expected_hash!r}")
    if actual_hash != expected_hash:
        raise StrictSyzError(
            f"selected {kind} changed after resolution: expected={expected_hash}, "
            f"actual={actual_hash}, path={path}"
        )
    return SelectedOptionFile(kind, file_name, path, actual_hash)


def resolve_strict_syz_selection(context: Mapping[str, Any]) -> StrictSyzSelection:
    """Revalidate the immutable Job-local SYZ selection before SIWave starts."""

    component_contract = context.get("customerComponentHandling")
    if not isinstance(component_contract, Mapping) or not component_contract:
        raise StrictSyzError("strict SYZ selection requires customerComponentHandling")
    syz_context = context.get("syz") or {}
    if "frequencySweep" in syz_context:
        raise StrictSyzError(
            "customer-strict SYZ does not allow JSON syz.frequencySweep; use SWS/SFSDF"
        )
    batch_id = _batch_id(context)
    selection_root = context.get("analysisOptionSelection")
    if not isinstance(selection_root, Mapping):
        raise StrictSyzError("customer-strict SYZ requires analysisOptionSelection")
    if str(selection_root.get("batchId") or "") != batch_id:
        raise StrictSyzError(
            "analysisOptionSelection batch mismatch: "
            f"expected={batch_id!r}, actual={selection_root.get('batchId')!r}"
        )
    syz = selection_root.get("syz")
    if not isinstance(syz, Mapping):
        raise StrictSyzError("analysisOptionSelection.syz must be an object")
    profile_id = str(syz.get("profileId") or "").strip()
    if not profile_id:
        raise StrictSyzError("analysisOptionSelection.syz.profileId is required")
    selection_source = str(syz.get("selectionSource") or "").strip()
    if selection_source not in SELECTION_SOURCES:
        raise StrictSyzError(
            "analysisOptionSelection.syz.selectionSource must be one of "
            f"{sorted(SELECTION_SOURCES)}; actual={selection_source!r}"
        )
    declared_job_root = str(component_contract.get("jobRoot") or "").strip()
    if not declared_job_root:
        raise StrictSyzError(
            "customer-strict SYZ requires the external request Job root snapshot"
        )
    job_root = Path(declared_job_root).resolve()
    sws = _validate_selected_file(
        syz.get("sws"),
        kind="sws",
        expected_folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["sws"],
        expected_suffix=".sws",
        job_root=job_root,
    )
    sfsdf = _validate_selected_file(
        syz.get("sfsdf"),
        kind="sfsdf",
        expected_folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["sfsdf"],
        expected_suffix=".sfsdf",
        job_root=job_root,
    )
    try:
        settings = normalize_syz_settings(
            syz.get("settings"),
            where="analysisOptionSelection.syz.settings",
        )
    except SyzOptionError as exc:
        raise StrictSyzError(str(exc)) from exc
    version = str(context.get("aedtVersion") or "").strip()
    if version not in SUPPORTED_AEDT_VERSIONS:
        raise StrictSyzError(
            "customer-strict SYZ requires a supported aedtVersion; "
            f"supported={sorted(SUPPORTED_AEDT_VERSIONS)}, actual={version!r}"
        )
    return StrictSyzSelection(
        batch_id=batch_id,
        profile_id=profile_id,
        selection_source=selection_source,
        aedt_version=version,
        job_root=job_root,
        sws=sws,
        sfsdf=sfsdf,
        settings=settings,
    )


def _selection_evidence(selection: StrictSyzSelection) -> dict[str, Any]:
    return {
        "batchId": selection.batch_id,
        "profileId": selection.profile_id,
        "selectionSource": selection.selection_source,
        "aedtVersion": selection.aedt_version,
        "jobRoot": str(selection.job_root),
        "sws": selection.sws.evidence(),
        "sfsdf": selection.sfsdf.evidence(),
        "settings": dict(selection.settings),
    }


def _default_siwave_session(context: Mapping[str, Any], target_edb: Path) -> Any:
    from pyedb.siwave import Siwave

    session = Siwave(specified_version=str(context["aedtVersion"]))
    session.import_edb(str(target_edb))
    return session


def apply_strict_syz_options(
    context: Mapping[str, Any],
    *,
    target_edb: Path,
    session_factory: Callable[[Mapping[str, Any], Path], Any] | None = None,
    sfsdf_capability: SfsdfApplyCapability | None = None,
) -> Path:
    """Apply selected option files to an open SIWave project and save batch SIW.

    The default uses the strict parser-to-documented-command adapter.  A native
    capability may only be supplied after its actual SIWave loader and
    read-back behavior have been verified.
    """

    run_dir = Path(str((context.get("workspace") or {})["runDir"])).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    record_path = run_dir / "syz_setup.json"
    selection: StrictSyzSelection | None = None
    batch_siw: Path | None = None
    session: Any = None
    record: dict[str, Any] = {
        "schema": "si-tdr-strict-syz-setup/1",
        "status": "error",
        "setup": None,
        "selection": None,
        "operations": [],
        "sfsdfApplication": None,
        "roundTrip": {
            "status": "not-claimed",
            "reason": "parser-command mode proves source parsing and COM receipts; solve grid equivalence is required",
        },
    }
    parsed_sfsdf = None
    try:
        try:
            pre_solve = validate_customer_pre_solve(
                context, expected_target_edb=target_edb
            )
        except CustomerPreSolveError as exc:
            raise StrictSyzError(
                f"FB-06 customer component/Port pre-solve Gate failed: {exc}"
            ) from exc
        record["customerPreSolveValidation"] = pre_solve
        selection = resolve_strict_syz_selection(context)
        record["selection"] = _selection_evidence(selection)
        target_edb = target_edb.resolve()
        if not target_edb.is_dir():
            raise StrictSyzError(f"target AEDB is missing: {target_edb}")
        batch_siw = run_dir / f"{selection.batch_id}.siw"
        if batch_siw.exists():
            raise StrictSyzError(
                f"strict SYZ setup refuses to overwrite an existing batch SIW: {batch_siw}"
            )
        if sfsdf_capability is None:
            try:
                parsed_sfsdf = parse_sfsdf_frequency_definition(selection.sfsdf.path)
            except SfsdfSweepError as exc:
                raise StrictSyzError(f"strict SFSDF parse failed: {exc}") from exc
        else:
            if not sfsdf_capability.name.strip():
                raise StrictSyzError("SFSDF capability must have a non-empty evidence name")
            if not callable(sfsdf_capability.apply):
                raise StrictSyzError("SFSDF capability apply hook must be callable")
            if not callable(sfsdf_capability.verify):
                raise StrictSyzError(
                    "native SFSDF capability has no required direct-loader read-back verifier; "
                    + SFSDF_CAPABILITY_PENDING
                )

        factory = session_factory or _default_siwave_session
        session = factory(context, target_edb)
        project = getattr(session, "oproject", None)
        if project is None:
            raise StrictSyzError("SIWave session does not expose an open oproject")

        sws_loader = getattr(project, SWS_IMPORT_API, None)
        if not callable(sws_loader):
            raise StrictSyzError(f"SIWave project does not expose {SWS_IMPORT_API}")
        sws_result = sws_loader(str(selection.sws.path))
        record["operations"].append(
            {
                "kind": "sws",
                "api": SWS_IMPORT_API,
                "path": str(selection.sws.path),
                "returnEvidence": _require_success(sws_result, "SWS import"),
            }
        )

        if sfsdf_capability is None:
            assert parsed_sfsdf is not None
            try:
                application = apply_sfsdf_parser_commands(
                    project,
                    parsed=parsed_sfsdf,
                    syz_settings=selection.settings,
                )
                record["sfsdfApplication"] = validate_sfsdf_command_application(
                    application,
                    parsed=parsed_sfsdf,
                    syz_settings=selection.settings,
                )
            except SfsdfSweepError as exc:
                raise StrictSyzError(
                    f"strict SFSDF documented-command apply failed: {exc}"
                ) from exc
            record["operations"].append(
                {
                    "kind": "sfsdf",
                    "api": PARSER_COMMAND_CAPABILITY_IDENTITY,
                    "path": str(selection.sfsdf.path),
                    "applicationEvidenceIdentity": application["evidenceIdentity"],
                    "returnEvidence": {
                        "pythonType": "dict",
                        "repr": "status='applied'",
                    },
                }
            )
        else:
            sfsdf_result = sfsdf_capability.apply(project, selection.sfsdf.path)
            record["operations"].append(
                {
                    "kind": "sfsdf",
                    "api": sfsdf_capability.name,
                    "path": str(selection.sfsdf.path),
                    "returnEvidence": _require_success(sfsdf_result, "SFSDF apply"),
                }
            )
            try:
                manager_options = apply_administrator_syz_options(
                    project,
                    syz_settings=selection.settings,
                )
                manager_options = validate_administrator_syz_option_application(
                    manager_options,
                    syz_settings=selection.settings,
                )
            except SfsdfSweepError as exc:
                raise StrictSyzError(
                    f"administrator SYZ option apply failed: {exc}"
                ) from exc
            record["administratorSyzOptions"] = manager_options
            record["operations"].append(
                {
                    "kind": "syz-options",
                    "api": "documented-syz-option-com-commands",
                    "applicationEvidenceIdentity": manager_options[
                        "evidenceIdentity"
                    ],
                    "returnEvidence": {
                        "pythonType": "dict",
                        "repr": "status='applied'",
                    },
                }
            )

        saver = getattr(project, "ScrSaveProjectAs", None)
        if not callable(saver):
            raise StrictSyzError("SIWave project does not expose ScrSaveProjectAs")
        save_result = saver(str(batch_siw))
        record["operations"].append(
            {
                "kind": "save",
                "api": "ScrSaveProjectAs",
                "path": str(batch_siw),
                "returnEvidence": _require_success(save_result, "SIW save"),
            }
        )
        if not batch_siw.is_file() or batch_siw.stat().st_size <= 0:
            raise StrictSyzError(f"SIWave did not create a non-empty batch SIW: {batch_siw}")

        if sfsdf_capability is not None:
            verification = sfsdf_capability.verify(project, selection.sfsdf.path)
            record["roundTrip"] = _validate_sfsdf_round_trip(
                verification,
                expected_source_hash=selection.sfsdf.sha256,
                expected_capability_identity=sfsdf_capability.name,
            )
        record["status"] = "ok"
        record["setup"] = {
            "name": selection.batch_id,
            "type": "siwave-syz-file-profile",
            "applyMode": (
                PARSER_COMMAND_APPLY_MODE
                if sfsdf_capability is None
                else "direct-loader-readback"
            ),
        }
        record["targetEdb"] = str(target_edb)
        record["batchSiw"] = _artifact(batch_siw)
        _write_json(record_path, record)
        return record_path
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if batch_siw is not None:
            record["batchSiw"] = _artifact(batch_siw)
        _write_json(record_path, record)
        if isinstance(exc, StrictSyzError):
            raise
        raise StrictSyzError(f"strict SYZ option apply failed: {exc}") from exc
    finally:
        if session is not None:
            quitter = getattr(session, "quit_application", None)
            if callable(quitter):
                try:
                    quitter()
                except Exception as exc:
                    if record.get("status") == "ok":
                        record["status"] = "error"
                        record["error"] = {
                            "type": type(exc).__name__,
                            "message": f"SIWave session close failed: {exc}",
                        }
                        _write_json(record_path, record)
                        raise StrictSyzError(
                            f"SIWave session close failed after option apply: {exc}"
                        ) from exc


def _metadata_port_order(context: Mapping[str, Any]) -> list[str]:
    ports = context.get("ports") or {}
    raw_path = str(ports.get("metadataPath") or "").strip()
    if not raw_path:
        raise StrictSyzError("strict Touchstone validation requires ports.metadataPath")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(str((context.get("workspace") or {})["root"])) / path
    path = path.resolve()
    if not path.is_file():
        raise StrictSyzError(f"Port Role metadata is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata_ports = payload.get("ports") or []
    metadata_order = [str(item.get("name") or "") for item in metadata_ports]
    indices = [int(item.get("index") or 0) for item in metadata_ports]
    if indices != list(range(1, len(metadata_order) + 1)) or any(
        not name for name in metadata_order
    ):
        raise StrictSyzError("Port Role metadata port indices/names are invalid")
    role = payload.get("portRoleMetadata")
    if not isinstance(role, Mapping):
        raise StrictSyzError("Port Role metadata contract is missing")
    role_order = [
        str(item.get("name") or "")
        for item in role.get("portOrder") or []
        if isinstance(item, Mapping)
    ]
    if role_order != metadata_order:
        raise StrictSyzError(
            "Port Role metadata order does not match ports order: "
            f"ports={metadata_order}, role={role_order}"
        )
    return metadata_order


def validate_touchstone(
    path: Path,
    *,
    expected_port_order: list[str],
) -> dict[str, Any]:
    """Validate strict Touchstone header, matrix rows, and frequency grid."""

    path = path.resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise StrictSyzError(f"Touchstone is missing or empty: {path}")
    suffix_match = _TOUCHSTONE_SUFFIX.fullmatch(path.suffix)
    if suffix_match is None:
        raise StrictSyzError(f"invalid Touchstone extension: {path.name}")
    suffix_port_count = int(suffix_match.group(1))
    expected_port_count = len(expected_port_order)
    if suffix_port_count != expected_port_count:
        raise StrictSyzError(
            "Touchstone extension port count mismatch: "
            f"extension={suffix_port_count}, expected={expected_port_count}"
        )

    header_by_index: dict[int, str] = {}
    option_tokens: list[str] | None = None
    numeric_tokens: list[float] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped:
            continue
        header_match = _PORT_HEADER.match(stripped)
        if header_match:
            index = int(header_match.group(1))
            if index in header_by_index:
                raise StrictSyzError(f"duplicate Touchstone Port[{index}] header")
            header_by_index[index] = header_match.group(2).strip()
            continue
        if stripped.startswith("!"):
            continue
        if stripped.startswith("#"):
            if option_tokens is not None:
                raise StrictSyzError("Touchstone contains multiple option lines")
            option_tokens = stripped[1:].split()
            continue
        if stripped.startswith("["):
            continue
        data_text = stripped.split("!", 1)[0].strip()
        if not data_text:
            continue
        for token in data_text.split():
            try:
                value = float(token.replace("D", "E").replace("d", "e"))
            except ValueError as exc:
                raise StrictSyzError(
                    f"invalid Touchstone numeric token at line {line_number}: {token!r}"
                ) from exc
            if not math.isfinite(value):
                raise StrictSyzError(
                    f"non-finite Touchstone value at line {line_number}: {token!r}"
                )
            numeric_tokens.append(value)

    header_order = [header_by_index.get(index, "") for index in range(1, expected_port_count + 1)]
    if len(header_by_index) != expected_port_count or any(not name for name in header_order):
        raise StrictSyzError(
            "Touchstone must contain one named ! Port[index] header for every port"
        )
    if header_order != expected_port_order:
        raise StrictSyzError(
            "Touchstone header order mismatch: "
            f"expected={expected_port_order}, actual={header_order}"
        )
    if option_tokens is None or len(option_tokens) < 3:
        raise StrictSyzError("Touchstone option line is missing or incomplete")
    unit = option_tokens[0].casefold()
    if unit not in _FREQUENCY_SCALE:
        raise StrictSyzError(f"unsupported Touchstone frequency unit: {unit!r}")
    if option_tokens[1].casefold() != "s":
        raise StrictSyzError("strict SYZ output must contain S-parameters")
    if option_tokens[2].casefold() not in {"ri", "ma", "db"}:
        raise StrictSyzError(
            f"unsupported Touchstone data format: {option_tokens[2]!r}"
        )

    values_per_frequency = 1 + (2 * expected_port_count * expected_port_count)
    if not numeric_tokens or len(numeric_tokens) % values_per_frequency:
        raise StrictSyzError(
            "Touchstone network data does not contain complete matrix rows: "
            f"values={len(numeric_tokens)}, valuesPerFrequency={values_per_frequency}"
        )
    frequencies_hz = [
        numeric_tokens[index] * _FREQUENCY_SCALE[unit]
        for index in range(0, len(numeric_tokens), values_per_frequency)
    ]
    if not frequencies_hz or any(
        current <= previous
        for previous, current in zip(frequencies_hz, frequencies_hz[1:])
    ):
        raise StrictSyzError(
            "Touchstone frequency grid must be finite and strictly increasing"
        )
    grid_digest = frequency_grid_sha256(frequencies_hz)
    return {
        **_artifact(path),
        "portCount": expected_port_count,
        "portOrder": header_order,
        "frequencyUnitInFile": option_tokens[0],
        "frequencyCount": len(frequencies_hz),
        "firstFrequencyHz": frequencies_hz[0],
        "lastFrequencyHz": frequencies_hz[-1],
        "frequencyGridSha256": grid_digest,
        "networkDataValueCount": len(numeric_tokens),
    }


def build_syz_exec_text(
    *,
    touchstone: Path,
    batch_siw: Path,
    batch_siwz: Path,
    cpu_count: int = 4,
) -> str:
    if cpu_count < 1:
        raise StrictSyzError("SIWave CPU count must be positive")
    return "\n".join(
        [
            f"SetNumCpus {cpu_count}",
            "ExecSyzSim",
            f'ExportTouchstone "{touchstone.resolve()}"',
            f'SaveSiw "{batch_siw.resolve()}"',
            f'SaveArchive "{batch_siwz.resolve()}"',
            "",
        ]
    )


def resolve_siwave_executable(aedt_version: str) -> Path:
    clean_version = aedt_version.replace("20", "", 1).replace(".", "")
    env_name = f"ANSYSEM_ROOT{clean_version}"
    install_root = os.environ.get(env_name)
    if not install_root:
        raise StrictSyzError(f"environment variable {env_name} is not configured")
    executable = (Path(install_root) / "siwave_ng.exe").resolve()
    if not executable.is_file():
        raise StrictSyzError(f"SIWave executable is missing: {executable}")
    return executable


def solve_strict_syz(
    context: Mapping[str, Any],
    *,
    runner: Callable[..., Any] = subprocess.run,
    executable: Path | None = None,
    cpu_count: int = 4,
) -> Path:
    """Run a fresh exact-basename SYZ solve and validate all three artifacts."""

    run_dir = Path(str((context.get("workspace") or {})["runDir"])).resolve()
    touchstone_dir = Path(
        str((context.get("workspace") or {})["touchstoneDir"])
    ).resolve()
    touchstone_dir.mkdir(parents=True, exist_ok=True)
    record_path = run_dir / "channel_solve.json"
    selection: StrictSyzSelection | None = None
    batch_siw: Path | None = None
    batch_siwz: Path | None = None
    touchstone: Path | None = None
    record: dict[str, Any] = {
        "schema": "si-tdr-strict-syz-solve/1",
        "status": "error",
        "selection": None,
        "artifacts": {},
    }
    try:
        try:
            pre_solve = validate_customer_pre_solve(context)
        except CustomerPreSolveError as exc:
            raise StrictSyzError(
                f"FB-06 customer component/Port pre-solve Gate failed: {exc}"
            ) from exc
        record["customerPreSolveValidation"] = pre_solve
        selection = resolve_strict_syz_selection(context)
        record["selection"] = _selection_evidence(selection)
        setup_record_path = run_dir / "syz_setup.json"
        if not setup_record_path.is_file():
            raise StrictSyzError(f"strict SYZ setup record is missing: {setup_record_path}")
        setup_record = json.loads(setup_record_path.read_text(encoding="utf-8"))
        if setup_record.get("schema") != "si-tdr-strict-syz-setup/1":
            raise StrictSyzError("strict SYZ setup record schema is invalid")
        if setup_record.get("status") != "ok":
            raise StrictSyzError("strict SYZ setup did not complete successfully")
        setup_selection = setup_record.get("selection") or {}
        if (
            setup_selection.get("profileId") != selection.profile_id
            or (setup_selection.get("sws") or {}).get("sha256") != selection.sws.sha256
            or (setup_selection.get("sfsdf") or {}).get("sha256")
            != selection.sfsdf.sha256
            or setup_selection.get("settings") != selection.settings
        ):
            raise StrictSyzError("strict SYZ setup selection no longer matches Run Config")
        setup_operations = setup_record.get("operations")
        if not isinstance(setup_operations, list):
            raise StrictSyzError("strict SYZ setup operations evidence is missing")
        sfsdf_operations = [
            item
            for item in setup_operations
            if isinstance(item, Mapping) and item.get("kind") == "sfsdf"
        ]
        if len(sfsdf_operations) != 1:
            raise StrictSyzError(
                "strict SYZ setup must contain exactly one SFSDF apply operation"
            )
        sfsdf_operation = sfsdf_operations[0]
        apply_identity = str(sfsdf_operation.get("api") or "").strip()
        if not apply_identity:
            raise StrictSyzError("strict SYZ setup SFSDF apply identity is missing")
        application = setup_record.get("sfsdfApplication")
        round_trip: dict[str, Any] | None = None
        if application is not None:
            try:
                parsed_sfsdf = parse_sfsdf_frequency_definition(selection.sfsdf.path)
                verified_application = validate_sfsdf_command_application(
                    application,
                    parsed=parsed_sfsdf,
                    syz_settings=selection.settings,
                )
            except SfsdfSweepError as exc:
                raise StrictSyzError(
                    f"strict SFSDF command application revalidation failed: {exc}"
                ) from exc
            if apply_identity != PARSER_COMMAND_CAPABILITY_IDENTITY:
                raise StrictSyzError(
                    "strict SFSDF operation identity differs from parser-command application"
                )
            if (
                sfsdf_operation.get("applicationEvidenceIdentity")
                != verified_application["evidenceIdentity"]
            ):
                raise StrictSyzError(
                    "strict SFSDF operation/application evidence identity differs"
                )
            expected_solution_grid = verified_application["expectedSolutionGrid"]
            setup_evidence_schema = verified_application["schema"]
            setup_evidence_identity = verified_application["evidenceIdentity"]
            setup_apply_mode = verified_application["applyMode"]
            record["sfsdfApplicationEvidence"] = {
                "schema": setup_evidence_schema,
                "applyMode": setup_apply_mode,
                "capabilityIdentity": verified_application["capabilityIdentity"],
                "evidenceIdentity": setup_evidence_identity,
                "sourceSfsdfSha256": verified_application["sourceSfsdfSha256"],
                "expectedSolutionGrid": expected_solution_grid,
                "nativeLoaderReadBackClaimed": False,
            }
        else:
            try:
                manager_options = validate_administrator_syz_option_application(
                    setup_record.get("administratorSyzOptions"),
                    syz_settings=selection.settings,
                )
            except SfsdfSweepError as exc:
                raise StrictSyzError(
                    f"administrator SYZ option application revalidation failed: {exc}"
                ) from exc
            option_operations = [
                item
                for item in setup_operations
                if isinstance(item, Mapping) and item.get("kind") == "syz-options"
            ]
            if (
                len(option_operations) != 1
                or option_operations[0].get("api")
                != "documented-syz-option-com-commands"
                or option_operations[0].get("applicationEvidenceIdentity")
                != manager_options["evidenceIdentity"]
            ):
                raise StrictSyzError(
                    "administrator SYZ option operation/application evidence differs"
                )
            record["administratorSyzOptionsEvidence"] = {
                "schema": manager_options["schema"],
                "evidenceIdentity": manager_options["evidenceIdentity"],
                "settings": manager_options["administratorSyzOptions"],
            }
            round_trip = _validate_sfsdf_round_trip(
                setup_record.get("roundTrip"),
                expected_source_hash=selection.sfsdf.sha256,
                expected_capability_identity=apply_identity,
            )
            expected_solution_grid = round_trip["expectedSolutionGrid"]
            setup_evidence_schema = round_trip["schema"]
            setup_evidence_identity = round_trip["evidenceIdentity"]
            setup_apply_mode = round_trip["applyMode"]
            record["roundTripEvidence"] = {
                "schema": setup_evidence_schema,
                "applyMode": setup_apply_mode,
                "capabilityIdentity": round_trip["capabilityIdentity"],
                "evidenceIdentity": setup_evidence_identity,
                "sourceSfsdfSha256": round_trip["sourceSfsdfSha256"],
                "expectedSolutionGrid": expected_solution_grid,
            }

        batch_siw = run_dir / f"{selection.batch_id}.siw"
        batch_siwz = run_dir / f"{selection.batch_id}.siwz"
        expected_count = int((context.get("ports") or {}).get("touchstonePortCount") or 0)
        expected_order = [str(item) for item in (context.get("ports") or {}).get("portOrder") or []]
        metadata_order = _metadata_port_order(context)
        if expected_count <= 0 or len(expected_order) != expected_count:
            raise StrictSyzError(
                "strict SYZ requires ports.touchstonePortCount and a matching portOrder"
            )
        if metadata_order != expected_order:
            raise StrictSyzError(
                "Run Config portOrder does not match Port Role metadata: "
                f"config={expected_order}, metadata={metadata_order}"
            )
        touchstone = touchstone_dir / f"{selection.batch_id}.s{expected_count}p"
        if not batch_siw.is_file() or batch_siw.stat().st_size <= 0:
            raise StrictSyzError(f"strict SYZ batch SIW is missing or empty: {batch_siw}")
        if touchstone.exists():
            raise StrictSyzError(
                f"strict SYZ refuses to reuse a pre-existing Touchstone: {touchstone}"
            )
        if batch_siwz.exists():
            raise StrictSyzError(
                f"strict SYZ refuses to reuse a pre-existing SIWZ: {batch_siwz}"
            )
        before_siw = _artifact(batch_siw)

        exec_file = run_dir / f"{selection.batch_id}.exec"
        exec_file.write_text(
            build_syz_exec_text(
                touchstone=touchstone,
                batch_siw=batch_siw,
                batch_siwz=batch_siwz,
                cpu_count=cpu_count,
            ),
            encoding="utf-8",
            newline="\n",
        )
        siwave_exe = (executable or resolve_siwave_executable(selection.aedt_version)).resolve()
        if not siwave_exe.is_file():
            raise StrictSyzError(f"SIWave executable is missing: {siwave_exe}")
        command = [
            str(siwave_exe),
            str(batch_siw),
            str(exec_file),
            "-formatOutput",
            "-useSubdir",
        ]
        record.update(
            {
                "setupName": selection.batch_id,
                "execFile": str(exec_file),
                "command": command,
                "requestedTouchstone": str(touchstone),
                "requestedSiw": str(batch_siw),
                "requestedSiwz": str(batch_siwz),
                "exportedTouchstoneFiles": [],
                "beforeSolveSiw": before_siw,
            }
        )
        result = runner(
            command,
            cwd=run_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        return_code = getattr(result, "returncode", None)
        record["solver"] = {
            "returnCode": return_code,
            "stdout": str(getattr(result, "stdout", "") or ""),
            "stderr": str(getattr(result, "stderr", "") or ""),
        }
        if return_code != 0:
            raise StrictSyzError(
                f"SIWave SYZ command failed with return code {return_code!r}"
            )

        after_siw = _artifact(batch_siw)
        if not after_siw["exists"] or int(after_siw["sizeBytes"] or 0) <= 0:
            raise StrictSyzError(f"final batch SIW is missing or empty: {batch_siw}")
        if int(after_siw["mtimeNs"] or 0) <= int(before_siw["mtimeNs"] or 0):
            raise StrictSyzError(
                f"final batch SIW was not freshly saved by the solve: {batch_siw}"
            )
        siwz_evidence = _artifact(batch_siwz)
        if not siwz_evidence["exists"] or int(siwz_evidence["sizeBytes"] or 0) <= 0:
            raise StrictSyzError(f"batch SIWZ is missing or empty: {batch_siwz}")
        touchstone_evidence = validate_touchstone(
            touchstone,
            expected_port_order=expected_order,
        )
        record["exportedTouchstoneFiles"] = [str(touchstone)]
        # 9.4.6: 비압축 SIWave 결과 트리는 공개 대상이므로 evidence를 남긴다.
        siwave_results_dir = run_dir / f"{selection.batch_id}.siwaveresults"
        siwave_results_evidence = siwave_results_directory_evidence(
            siwave_results_dir
        )
        record["artifacts"] = {
            "touchstone": touchstone_evidence,
            "siw": after_siw,
            "siwz": siwz_evidence,
            "siwaveResults": siwave_results_evidence,
        }
        solution_grid = _validate_grid_contract(
            {
                key: touchstone_evidence[key]
                for key in (
                    "frequencyCount",
                    "firstFrequencyHz",
                    "lastFrequencyHz",
                    "frequencyGridSha256",
                )
            },
            label="exported Touchstone solutionGrid",
        )
        record["solutionGrid"] = solution_grid
        record["sfsdfGridEquivalence"] = {
            "schema": SFSDF_GRID_EQUIVALENCE_SCHEMA,
            "status": "error",
            "sourceSfsdfSha256": selection.sfsdf.sha256,
            "setupEvidenceSchema": setup_evidence_schema,
            "setupEvidenceIdentity": setup_evidence_identity,
            "applyMode": setup_apply_mode,
            "expectedSolutionGrid": expected_solution_grid,
            "solutionGrid": solution_grid,
        }
        if round_trip is not None:
            record["sfsdfGridEquivalence"]["roundTripEvidenceIdentity"] = (
                setup_evidence_identity
            )
        else:
            record["sfsdfGridEquivalence"]["applicationEvidenceIdentity"] = (
                setup_evidence_identity
            )
        _require_identical_grids(expected_solution_grid, solution_grid)
        record["sfsdfGridEquivalence"]["status"] = "verified"
        record["status"] = "ok"
        _write_json(record_path, record)
        return record_path
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if touchstone is not None:
            record["artifacts"]["touchstone"] = _artifact(touchstone)
        if batch_siw is not None:
            record["artifacts"]["siw"] = _artifact(batch_siw)
        if batch_siwz is not None:
            record["artifacts"]["siwz"] = _artifact(batch_siwz)
        _write_json(record_path, record)
        if isinstance(exc, StrictSyzError):
            raise
        raise StrictSyzError(f"strict SYZ solve failed: {exc}") from exc
