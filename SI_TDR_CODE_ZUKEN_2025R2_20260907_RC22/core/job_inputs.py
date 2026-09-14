"""Resolve DCIR-compatible Heaven request files from an EDEN-staged Job."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .admin_config import derive_design_input_type, validate_administrator_config
except ImportError:  # Support direct execution from the SI_TDR folder.
    from admin_config import (  # type: ignore[no-redef]
        derive_design_input_type,
        validate_administrator_config,
    )

try:
    from .channel.analysis_options import resolve_analysis_option_catalog
except ImportError:  # Support direct execution from the SI_TDR folder.
    from channel.analysis_options import (  # type: ignore[no-redef]
        resolve_analysis_option_catalog,
    )


RESOLUTION_SCHEMA = "si-tdr-job-input-resolution/1"
GENERATED_CONFIG_SCHEMA = "si-tdr-generated-run-config/1"


class JobInputResolutionError(ValueError):
    """Raised when an external request cannot be resolved without guessing."""


@dataclass(frozen=True)
class JobInputField:
    key: str
    json_path: tuple[str, ...]
    subfolder: str
    allowed_extensions: frozenset[str]

    @property
    def json_path_text(self) -> str:
        return ".".join(self.json_path)


@dataclass(frozen=True)
class ResolvedJobInput:
    field: JobInputField
    requested_name: str
    path: Path
    selected_from: str

    def as_dict(self, *, job_root: Path) -> dict[str, Any]:
        return {
            "jsonPath": self.field.json_path_text,
            "requestedName": self.requested_name,
            "resolvedPath": str(self.path),
            "jobRelativePath": self.path.relative_to(job_root).as_posix(),
            "selectedFrom": self.selected_from,
            "extension": self.path.suffix.casefold(),
        }


@dataclass(frozen=True)
class JobInputResolution:
    request_path: Path
    job_root: Path
    request: dict[str, Any]
    inputs: tuple[ResolvedJobInput, ...]

    def input(self, key: str) -> ResolvedJobInput:
        try:
            return next(item for item in self.inputs if item.field.key == key)
        except StopIteration as exc:
            raise KeyError(key) from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RESOLUTION_SCHEMA,
            "requestPath": str(self.request_path),
            "jobRoot": str(self.job_root),
            "selectionPolicy": "job-root-first-then-key-subfolder",
            "inputs": {
                item.field.key: item.as_dict(job_root=self.job_root)
                for item in self.inputs
            },
        }


JOB_INPUT_FIELDS: tuple[JobInputField, ...] = (
    JobInputField(
        key="Spec",
        json_path=("CAE", "SOC", "Spec"),
        subfolder="Spec",
        allowed_extensions=frozenset({".csv"}),
    ),
    JobInputField(
        key="cadFile",
        json_path=("CAE", "PCB", "cadFile"),
        subfolder="cadFile",
        allowed_extensions=frozenset({".zip", ".pcb", ".anf"}),
    ),
    JobInputField(
        key="Stackup",
        json_path=("CAE", "PCB", "Stackup"),
        subfolder="Stackup",
        allowed_extensions=frozenset({".stk"}),
    ),
    JobInputField(
        key="BOM",
        json_path=("CAE", "PCB", "BOM"),
        subfolder="BOM",
        allowed_extensions=frozenset({".csv", ".xls", ".xlsx"}),
    ),
)


def is_external_heaven_request(payload: Mapping[str, Any]) -> bool:
    """Return true when a payload claims the preserved Heaven/DCIR contract."""

    return "Request" in payload or "CAE" in payload


def _read_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise JobInputResolutionError(f"external request JSON not found: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JobInputResolutionError(
            f"external request JSON is not readable JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise JobInputResolutionError("external request JSON root must be an object")
    return payload


def _object_at(payload: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any]:
    current: Any = payload
    walked: list[str] = []
    for key in path:
        walked.append(key)
        if not isinstance(current, Mapping):
            raise JobInputResolutionError(f"{'.'.join(walked[:-1])} must be an object")
        if key not in current:
            raise JobInputResolutionError(f"external request requires {'.'.join(walked)}")
        current = current[key]
    if not isinstance(current, Mapping):
        raise JobInputResolutionError(f"{'.'.join(path)} must be an object")
    return current


def _requested_file_name(payload: Mapping[str, Any], field: JobInputField) -> str:
    parent = _object_at(payload, field.json_path[:-1])
    leaf = field.json_path[-1]
    if leaf not in parent:
        raise JobInputResolutionError(
            f"external request requires {field.json_path_text}"
        )
    value = parent[leaf]
    if not isinstance(value, str) or not value.strip():
        raise JobInputResolutionError(
            f"external request {field.json_path_text} must be a non-empty file name"
        )
    name = value.strip()
    path = Path(name)
    if (
        path.is_absolute()
        or path.name != name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise JobInputResolutionError(
            f"external request {field.json_path_text} must be a Job-local file name, got {value!r}"
        )
    extension = path.suffix.casefold()
    if extension not in field.allowed_extensions:
        allowed = ", ".join(sorted(field.allowed_extensions))
        raise JobInputResolutionError(
            f"external request {field.json_path_text} has unsupported extension "
            f"{extension or '<none>'}; expected one of: {allowed}"
        )
    return name


def _matching_entries(folder: Path, requested_name: str) -> list[Path]:
    if not folder.is_dir():
        return []
    folded_name = requested_name.casefold()
    return sorted(
        (item for item in folder.iterdir() if item.name.casefold() == folded_name),
        key=lambda item: item.name,
    )


def _contained_file(path: Path, *, job_root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise JobInputResolutionError(f"{label} cannot be resolved: {path}") from exc
    if resolved != job_root and job_root not in resolved.parents:
        raise JobInputResolutionError(f"{label} resolves outside the Job root: {path}")
    if not resolved.is_file():
        raise JobInputResolutionError(f"{label} is not a file: {path}")
    return resolved


def _resolve_named_file(
    *,
    job_root: Path,
    field: JobInputField,
    requested_name: str,
) -> ResolvedJobInput:
    locations = ((job_root, "job-root"), (job_root / field.subfolder, field.subfolder))
    checked: list[Path] = []
    for folder, selected_from in locations:
        checked.append(folder / requested_name)
        matches = _matching_entries(folder, requested_name)
        if len(matches) > 1:
            candidates = ", ".join(str(item) for item in matches)
            raise JobInputResolutionError(
                f"external request {field.json_path_text} is ambiguous in {folder}: {candidates}"
            )
        if not matches:
            continue
        resolved = _contained_file(
            matches[0],
            job_root=job_root,
            label=f"external request {field.json_path_text}",
        )
        if resolved.suffix.casefold() not in field.allowed_extensions:
            raise JobInputResolutionError(
                f"resolved {field.json_path_text} has an unsupported extension: {resolved}"
            )
        return ResolvedJobInput(
            field=field,
            requested_name=requested_name,
            path=resolved,
            selected_from=selected_from,
        )
    checked_text = ", ".join(str(path) for path in checked)
    raise JobInputResolutionError(
        f"external request {field.json_path_text} file not found; checked: {checked_text}"
    )


def resolve_job_inputs(
    request_path: Path,
    *,
    payload: Mapping[str, Any] | None = None,
) -> JobInputResolution:
    """Resolve required DCIR request inputs without modifying the request payload."""

    resolved_request_path = request_path.resolve()
    if not resolved_request_path.is_file():
        raise JobInputResolutionError(
            f"external request JSON not found: {resolved_request_path}"
        )
    request = deepcopy(dict(payload)) if payload is not None else _read_request(resolved_request_path)
    if not is_external_heaven_request(request):
        raise JobInputResolutionError(
            "external request must preserve the Heaven/DCIR Request and CAE objects"
        )
    _object_at(request, ("Request",))
    _object_at(request, ("CAE",))
    job_root = resolved_request_path.parent.resolve()
    inputs = tuple(
        _resolve_named_file(
            job_root=job_root,
            field=field,
            requested_name=_requested_file_name(request, field),
        )
        for field in JOB_INPUT_FIELDS
    )
    return JobInputResolution(
        request_path=resolved_request_path,
        job_root=job_root,
        request=request,
        inputs=inputs,
    )


def build_generated_run_config(
    administrator_config: Mapping[str, Any],
    resolution: JobInputResolution,
    *,
    administrator_config_path: Path | None = None,
) -> dict[str, Any]:
    """Combine administrator policy and an immutable external-request snapshot."""

    generated = validate_administrator_config(administrator_config)
    resolved_analysis_options = resolve_analysis_option_catalog(
        generated,
        job_root=resolution.job_root,
    )
    try:
        from .preprocess.external_request import (
            build_external_reference_preprocess_plan,
        )
    except ImportError:  # Support direct execution from the SI_TDR folder.
        from preprocess.external_request import (  # type: ignore[no-redef]
            build_external_reference_preprocess_plan,
        )
    if administrator_config_path is None:
        raise JobInputResolutionError(
            "administrator_config_path is required for the generated reference contract"
        )
    reference_plan = build_external_reference_preprocess_plan(
        generated,
        resolution,
        resolved_analysis_options,
        administrator_config_path=administrator_config_path,
        work_dir=resolution.job_root / "work" / "reference_preprocess",
    )
    try:
        from .channel.customer_components import (
            build_customer_component_contract,
            resolve_customer_array_catalog,
        )
    except ImportError:  # Support direct execution from the SI_TDR folder.
        from channel.customer_components import (  # type: ignore[no-redef]
            build_customer_component_contract,
            resolve_customer_array_catalog,
        )
    bom_path = resolution.input("BOM").path
    array_resolution = resolve_customer_array_catalog(
        generated,
        bom_path=bom_path,
        administrator_config_path=administrator_config_path,
        job_root=resolution.job_root,
    )
    # 관리자 Config는 isZuken만 선언한다. 기록·감사가 사용하는 해석 입력 종류는
    # 모든 검증이 끝난 뒤 생성 Run Config에만 유도값으로 남긴다.
    generated["designInputType"] = derive_design_input_type(bool(generated["isZuken"]))
    generated["generatedRunConfig"] = {
        "schema": GENERATED_CONFIG_SCHEMA,
        "administratorConfigSchemaVersion": generated["schemaVersion"],
        "designInputType": generated["designInputType"],
        "administratorConfigPath": (
            str(administrator_config_path.resolve())
            if administrator_config_path is not None
            else None
        ),
        "arrayResistorBom": deepcopy(array_resolution["bom"]),
    }
    generated["externalRequest"] = deepcopy(resolution.request)
    generated["jobInputResolution"] = resolution.as_dict()
    generated["resolvedAnalysisOptionCatalog"] = resolved_analysis_options.to_dict()
    generated["referencePreprocessPlan"] = reference_plan.to_dict()
    generated["customerComponentHandling"] = build_customer_component_contract(
        array_catalog=array_resolution["arrayCatalog"],
        array_rule_resolution=array_resolution["arrayRuleResolution"],
        bom_snapshot=array_resolution["bom"],
        selector=array_resolution["selector"],
        resolved_columns=array_resolution["resolvedColumns"],
        bom_path=bom_path,
    )
    return generated
