from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTRACT_NAME = "si_tdr.reference_preprocess"
CONTRACT_SCHEMA_VERSION = 1
PREPROCESSOR_IMPLEMENTATION_VERSION = 4
DEFAULT_MANIFEST_NAME = "reference_preprocess_manifest.json"
INPUT_PROVENANCE_KIND = "si_tdr_reference_preprocess"
SUPPORTED_PREPROCESSING_MODES = {
    "zuken_design",
    "anf_cmp",
    "reference_siw",
}
SUPPORTED_REFERENCE_POLICIES = {
    "dcir_reference_v1",
    "import_only",
}
SUPPORTED_BOM_EXPANDED_DESIGNATOR_POLICIES = {
    "exact_only",
    "parent_numeric_suffix",
}


class ReferencePreprocessError(RuntimeError):
    """Raised when the SI-TDR reference preprocessing contract is invalid."""


@dataclass(frozen=True)
class ReferencePreprocessRequest:
    config_path: Path
    mode: str
    mode_source: str
    work_dir: Path
    output_name: str
    aedt_version: str | None
    reference_policy: str | None
    settings: dict[str, Any]
    reference_siw: Path | None = None
    reference_aedb: Path | None = None
    design: Path | None = None
    anf: Path | None = None
    cmp: Path | None = None
    stackup: Path | None = None
    bom: Path | None = None
    pmap: Path | None = None
    sws: Path | None = None
    zuken_bin: Path | None = None


@dataclass(frozen=True)
class ReferencePreprocessResult:
    config_path: Path
    mode: str
    mode_source: str
    reference_siw: Path
    reference_aedb: Path
    manifest_path: Path
    manifest_id: str
    stages: tuple[dict[str, Any], ...]
    aedt_version: str | None = None
    implementation_version: int | None = PREPROCESSOR_IMPLEMENTATION_VERSION

    def as_layout_config(self) -> dict[str, str]:
        return {
            "referenceSiw": str(self.reference_siw.resolve()),
            "referenceEdb": str(self.reference_aedb.resolve()),
        }

    def as_input_provenance(self) -> dict[str, Any]:
        return {
            "kind": INPUT_PROVENANCE_KIND,
            "contract": CONTRACT_NAME,
            "schemaVersion": CONTRACT_SCHEMA_VERSION,
            "implementationVersion": self.implementation_version,
            "manifest": str(self.manifest_path.resolve()),
            "manifestId": self.manifest_id,
            "preprocessingMode": self.mode,
            "preprocessingModeSource": self.mode_source,
            "aedtVersion": self.aedt_version,
            "dcirRuntimeInvoked": False,
        }


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReferencePreprocessError(f"SI-TDR Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReferencePreprocessError(f"invalid SI-TDR Config JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReferencePreprocessError("SI-TDR Config root must be an object")
    return payload


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key) or {}
    if not isinstance(value, dict):
        raise ReferencePreprocessError(f"{key} must be an object")
    return value


def _path_value(
    value: Any,
    *,
    label: str,
    config_dir: Path,
    runtime_root: Path | None,
) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReferencePreprocessError(f"{label} must be a non-empty path string")
    path = Path(value.strip())
    if path.is_absolute():
        return path.resolve()
    config_relative = (config_dir / path).resolve()
    if config_relative.exists() or runtime_root is None:
        return config_relative
    return (runtime_root / path).resolve()


def _required_file(path: Path | None, label: str) -> Path:
    if path is None or not path.is_file():
        raise ReferencePreprocessError(f"{label} file not found: {path}")
    return path


def _optional_file(path: Path | None, label: str) -> Path | None:
    if path is not None and not path.is_file():
        raise ReferencePreprocessError(f"{label} file not found: {path}")
    return path


def _output_name(config: dict[str, Any], settings: dict[str, Any]) -> str:
    raw = settings.get("outputName")
    if raw is None:
        project = _object(config, "project")
        raw = project.get("name") or "si_tdr"
    if not isinstance(raw, str) or not raw.strip():
        raise ReferencePreprocessError("preprocessing.outputName must be a non-empty string")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.strip()).strip("._")
    if not name:
        raise ReferencePreprocessError("preprocessing.outputName has no usable characters")
    return name[:-4] if name.casefold().endswith("_ref") else name


def _same_optional_path(
    first: Path | None,
    second: Path | None,
    *,
    label: str,
) -> Path | None:
    if first is not None and second is not None and first != second:
        raise ReferencePreprocessError(f"conflicting {label} paths are configured")
    return first or second


def _validate_dcir_reference_policy(
    settings: dict[str, Any],
    *,
    bom: Path | None,
    stackup: Path | None,
    sws: Path | None,
) -> None:
    missing = [
        label
        for value, label in (
            (bom, "preprocessing.inputs.bom"),
            (stackup, "preprocessing.inputs.stackup"),
            (sws, "preprocessing.sws"),
        )
        if value is None
    ]
    if missing:
        raise ReferencePreprocessError(
            "dcir_reference_v1 requires explicit " + ", ".join(missing)
        )

    dc_short = settings.get("dcShort")
    if not isinstance(dc_short, dict):
        raise ReferencePreprocessError(
            "dcir_reference_v1 requires preprocessing.dcShort"
        )
    for key in (
        "excludeNet",
        "shortedComp",
        "excludePrefixes",
        "deleteCompTypes",
        "preserveCompTypes",
    ):
        value = dc_short.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ReferencePreprocessError(
                f"preprocessing.dcShort.{key} must be an explicit string array"
            )
    overlapping_types = set(dc_short["deleteCompTypes"]) & set(
        dc_short["preserveCompTypes"]
    )
    if overlapping_types:
        raise ReferencePreprocessError(
            "preprocessing.dcShort deleteCompTypes and preserveCompTypes overlap: "
            + ", ".join(sorted(overlapping_types))
        )
    short_key = dc_short.get("shortKey")
    if not isinstance(short_key, str) or not short_key:
        raise ReferencePreprocessError(
            "preprocessing.dcShort.shortKey must be an explicit non-empty string"
        )

    bom_settings = settings.get("BOM") or {}
    if not isinstance(bom_settings, dict):
        raise ReferencePreprocessError("preprocessing.BOM must be an object")
    configured_columns = bom_settings.get("designatorColumns", [])
    if not isinstance(configured_columns, list) or not all(
        isinstance(item, str) and item.strip() for item in configured_columns
    ):
        raise ReferencePreprocessError(
            "preprocessing.BOM.designatorColumns must be a string array"
        )
    expanded_policy = bom_settings.get("expandedDesignatorPolicy", "exact_only")
    if (
        not isinstance(expanded_policy, str)
        or expanded_policy not in SUPPORTED_BOM_EXPANDED_DESIGNATOR_POLICIES
    ):
        raise ReferencePreprocessError(
            "preprocessing.BOM.expandedDesignatorPolicy must be one of: "
            + ", ".join(sorted(SUPPORTED_BOM_EXPANDED_DESIGNATOR_POLICIES))
        )


def _validate_zuken_settings(
    settings: dict[str, Any],
    *,
    design: Path,
    zuken_bin: Path,
) -> None:
    if design.suffix.casefold() not in {".zip", ".pcb"}:
        raise ReferencePreprocessError(
            "zuken_design requires an explicit .zip or .pcb input"
        )
    for name in ("DFevolv.cr5.exe", "DFdsgn2anf.exe"):
        executable = zuken_bin / name
        if not executable.is_file():
            raise ReferencePreprocessError(f"Zuken executable not found: {executable}")
    zuken = settings.get("zuken") or {}
    if not isinstance(zuken, dict):
        raise ReferencePreprocessError("preprocessing.zuken must be an object")
    max_attempts = zuken.get("maxAttempts", 3)
    retry_delay = zuken.get("retryDelaySeconds", 120)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ReferencePreprocessError(
            "preprocessing.zuken.maxAttempts must be a positive integer"
        )
    if (
        isinstance(retry_delay, bool)
        or not isinstance(retry_delay, (int, float))
        or retry_delay < 0
    ):
        raise ReferencePreprocessError(
            "preprocessing.zuken.retryDelaySeconds must be a non-negative number"
        )


def resolve_reference_preprocess_request(
    config_path: Path,
    *,
    work_dir: Path,
    runtime_root: Path | None = None,
) -> ReferencePreprocessRequest:
    """Resolve the SI-TDR-owned preprocessing mode and its explicit inputs."""

    config_path = config_path.resolve()
    config = _load_json_object(config_path)
    settings = _object(config, "preprocessing")
    layout = _object(config, "layout")
    inputs = _object(settings, "inputs")

    raw_mode = config.get("preprocessingMode")
    if raw_mode is None:
        if layout.get("referenceSiw") and layout.get("referenceEdb"):
            mode = "reference_siw"
            mode_source = "legacy_layout_fallback"
        else:
            raise ReferencePreprocessError(
                "SI-TDR Config requires preprocessingMode for new preprocessing flows"
            )
    elif not isinstance(raw_mode, str) or raw_mode not in SUPPORTED_PREPROCESSING_MODES:
        raise ReferencePreprocessError(
            "preprocessingMode must be one of "
            + ", ".join(sorted(SUPPORTED_PREPROCESSING_MODES))
        )
    else:
        mode = raw_mode
        mode_source = "si_tdr_config"

    config_dir = config_path.parent
    resolve = lambda value, label: _path_value(  # noqa: E731
        value,
        label=label,
        config_dir=config_dir,
        runtime_root=runtime_root,
    )
    reference_siw = _same_optional_path(
        resolve(layout.get("referenceSiw"), "layout.referenceSiw"),
        resolve(inputs.get("referenceSiw"), "preprocessing.inputs.referenceSiw"),
        label="reference SIW",
    )
    reference_aedb = _same_optional_path(
        resolve(layout.get("referenceEdb"), "layout.referenceEdb"),
        resolve(inputs.get("referenceEdb"), "preprocessing.inputs.referenceEdb"),
        label="reference AEDB",
    )
    design = resolve(inputs.get("design"), "preprocessing.inputs.design")
    anf = resolve(inputs.get("anf"), "preprocessing.inputs.anf")
    cmp = resolve(inputs.get("cmp"), "preprocessing.inputs.cmp")
    stackup = resolve(inputs.get("stackup"), "preprocessing.inputs.stackup")
    bom = resolve(inputs.get("bom"), "preprocessing.inputs.bom")
    pmap = resolve(inputs.get("pmap"), "preprocessing.inputs.pmap")
    sws = resolve(settings.get("sws"), "preprocessing.sws")
    zuken_bin = resolve(settings.get("DF_path"), "preprocessing.DF_path")

    raw_version = (
        settings.get("version")
        if "version" in settings
        else config.get("aedtVersion")
    )
    if raw_version is not None and (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, (str, int, float))
    ):
        raise ReferencePreprocessError(
            "preprocessing.version/aedtVersion must be a non-empty scalar value"
        )
    aedt_version = str(raw_version).strip() if raw_version is not None else None
    if aedt_version == "":
        raise ReferencePreprocessError(
            "preprocessing.version/aedtVersion must be a non-empty scalar value"
        )

    raw_policy = settings.get("referencePolicy")
    reference_policy: str | None = None
    if mode != "reference_siw":
        if not isinstance(raw_policy, str) or raw_policy not in SUPPORTED_REFERENCE_POLICIES:
            raise ReferencePreprocessError(
                "preprocessing.referencePolicy must be explicit for generated references: "
                + ", ".join(sorted(SUPPORTED_REFERENCE_POLICIES))
            )
        reference_policy = raw_policy

    if mode == "reference_siw":
        reference_siw = _required_file(reference_siw, "reference SIW")
        if reference_aedb is not None:
            if not reference_aedb.is_dir() or not (reference_aedb / "edb.def").is_file():
                raise ReferencePreprocessError(
                    f"reference AEDB is incomplete: {reference_aedb}"
                )
        elif aedt_version is None:
            raise ReferencePreprocessError(
                "preprocessing.version/aedtVersion is required to export AEDB from reference SIW"
            )
    elif mode == "anf_cmp":
        anf = _required_file(anf, "ANF")
        cmp = _required_file(cmp, "CMP")
        stackup = _optional_file(stackup, "stackup")
        bom = _optional_file(bom, "BOM")
        pmap = _optional_file(pmap, "PMAP")
        sws = _optional_file(sws, "SWS")
        if aedt_version is None:
            raise ReferencePreprocessError("anf_cmp mode requires preprocessing.version")
    else:
        design = _required_file(design, "Zuken design")
        stackup = _optional_file(stackup, "stackup")
        bom = _optional_file(bom, "BOM")
        pmap = _optional_file(pmap, "PMAP")
        sws = _optional_file(sws, "SWS")
        if zuken_bin is None or not zuken_bin.is_dir():
            raise ReferencePreprocessError(f"Zuken DF_path directory not found: {zuken_bin}")
        _validate_zuken_settings(settings, design=design, zuken_bin=zuken_bin)
        if aedt_version is None:
            raise ReferencePreprocessError("zuken_design mode requires preprocessing.version")

    if reference_policy == "dcir_reference_v1":
        _validate_dcir_reference_policy(
            settings,
            bom=bom,
            stackup=stackup,
            sws=sws,
        )

    return ReferencePreprocessRequest(
        config_path=config_path,
        mode=mode,
        mode_source=mode_source,
        work_dir=work_dir.resolve(),
        output_name=_output_name(config, settings),
        aedt_version=aedt_version,
        reference_policy=reference_policy,
        settings=settings,
        reference_siw=reference_siw,
        reference_aedb=reference_aedb,
        design=design,
        anf=anf,
        cmp=cmp,
        stackup=stackup,
        bom=bom,
        pmap=pmap,
        sws=sws,
        zuken_bin=zuken_bin,
    )
