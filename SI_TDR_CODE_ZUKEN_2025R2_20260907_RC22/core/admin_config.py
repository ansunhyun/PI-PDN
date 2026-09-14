from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path, PurePath
from typing import Any, Mapping

try:
    from .syz_options import (
        SYZ_REQUIRED_SETTING_FIELDS,
        SYZ_SETTING_FIELDS,
        SyzOptionError,
        normalize_syz_settings,
    )
except ImportError:  # Support compact customer core imports.
    from syz_options import (  # type: ignore[no-redef]
        SYZ_REQUIRED_SETTING_FIELDS,
        SYZ_SETTING_FIELDS,
        SyzOptionError,
        normalize_syz_settings,
    )


ADMINISTRATOR_CONFIG_SCHEMA_VERSION = 1
SUPPORTED_DESIGN_INPUT_TYPES = frozenset({"zuken_design", "anf_cmp"})
DEFAULT_PCB_CAPTURE_POLICY = {
    "route": {"enabled": True},
    "overview": {"enabled": True},
}


def derive_design_input_type(is_zuken: bool) -> str:
    """관리자 Config는 isZuken만 선언하고 해석 입력 종류는 여기서 유도한다.

    기록·감사에는 계속 designInputType 이름으로 남는다.
    """

    return "zuken_design" if is_zuken else "anf_cmp"
SUPPORTED_AEDT_VERSIONS = frozenset({"2024.2", "2025.2"})

# FB-06 consumes this fixed policy.  It is deliberately not a customer option.
COMPONENT_HANDLING_CONTRACT = {
    "series": "short",
    "array": "bom-configured-source-column-authoritative",
}

# This is copied verbatim from DCIR/source/core/config.json (DCIR.dcShort).
DCIR_DC_SHORT = {
    "excludeNet": ["DUMMY", "ZUKEN_DUMMY", "OUTLINES"],
    "shortedComp": [
        "SHORT_1005",
        "SHORT_1005_BOT",
        "SHORT_3030",
        "SHORT_NARROW_1MM",
        "SHORT_ZEROFUSE_M10MM",
        "SHORT_ZEROFUSE_M10MM_0.12",
    ],
    "shortKey": "SIGN",
    "excludePrefixes": ["AR", "JK", "P", "IC", "X", "D"],
    "deleteCompTypes": ["IC", "IO", "Other"],
    "powerNetKeywords": ["+", "-", "V", "PWR"],
}

ZUKEN_BOOLEAN_FIELDS = (
    "isZuken",
    "exportANF",
    "exportCMP",
    "exportODB",
    "exportEDB",
)

LEGACY_IGNORED_ROOT_FIELDS = frozenset({"sws", "ARR"})
ALLOWED_ROOT_FIELDS = frozenset(
    {
        "schemaVersion",
        "aedtVersion",
        "isZuken",
        "DF_path",
        "exportANF",
        "exportCMP",
        "exportODB",
        "exportEDB",
        "preprocessing",
        "nets",
        "BOM",
        "ports",
        "tdrReport",
        "pcbCapture",
        "analysisOptions",
    }
) | LEGACY_IGNORED_ROOT_FIELDS
ALLOWED_TDR_REPORT_FIELDS = frozenset({"yAxisMarginOhm"})
ALLOWED_PREPROCESSING_FIELDS = frozenset(
    {"referencePolicy", "outputName", "dcShort", "BOM"}
)
# 고정 정책(mode, referenceNet, referenceNetPolicy, terminalLayerPolicy,
# portContractValidation)은 관리자가 고를 수 없으므로 Config에 두지 않는다.
# 생성 Run Config가 채우고 기록·감사에는 계속 남는다.
ALLOWED_PORT_FIELDS = frozenset({"singleEndedImpedanceOhm"})

# These customer-facing fields were removed by the FB-03 contract.  Legacy PoC
# fixtures may still use them through their dedicated internal entry points, but
# the EDEN/DCG administrator Config is fail-closed.
REMOVED_CUSTOMER_FIELDS = frozenset(
    {
        "preprocessingMode",
        "analysisTemplates",
        "frequencySweep",
        "stepPs",
        "stopPs",
        "useTsConvolution",
        "useToConvolution",
        "differentialBridgeOhm",
        "seriesTreatment",
        "dump_stage_outputs",
        "exportIPC",
        "arrayPinMap",
        "resistanceOhm",
    }
)


class AdministratorConfigError(ValueError):
    """Raised when the customer administrator Config violates the FB-03 schema."""


def _object(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdministratorConfigError(f"{where} must be an object")
    return value


def _non_empty_string(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdministratorConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _normalized_column_name(value: Any) -> str:
    return "".join(
        character for character in str(value).casefold() if character.isalnum()
    )


def _validate_normalized_column_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AdministratorConfigError(f"{where} must be a non-empty string array")
    normalized_seen: dict[str, str] = {}
    result: list[str] = []
    for index, raw_column in enumerate(value):
        column = _non_empty_string(raw_column, where=f"{where}[{index}]")
        if raw_column != column:
            raise AdministratorConfigError(
                f"{where}[{index}] must not have leading or trailing whitespace"
            )
        normalized = _normalized_column_name(column)
        if not normalized:
            raise AdministratorConfigError(
                f"{where}[{index}] must contain letters or numbers"
            )
        existing = normalized_seen.get(normalized)
        if existing is not None:
            raise AdministratorConfigError(
                f"{where} contains duplicate normalized columns: "
                f"{existing!r}, {column!r}"
            )
        normalized_seen[normalized] = column
        result.append(column)
    return result


def _walk_removed_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in REMOVED_CUSTOMER_FIELDS:
                raise AdministratorConfigError(
                    f"removed administrator Config field is not supported: {child_path}"
                )
            _walk_removed_fields(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_removed_fields(child, path=f"{path}[{index}]")


def _strip_legacy_arr(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove legacy ARR authoring before strict validation and normalization."""

    normalized = deepcopy(dict(value))
    normalized.pop("ARR", None)
    bom = normalized.get("BOM")
    if not isinstance(bom, Mapping):
        return normalized
    normalized_bom = deepcopy(dict(bom))
    comp_prop = normalized_bom.get("compProp")
    if isinstance(comp_prop, Mapping) and "ARR" in comp_prop:
        remaining = deepcopy(dict(comp_prop))
        remaining.pop("ARR", None)
        if remaining:
            normalized_bom["compProp"] = remaining
        else:
            normalized_bom.pop("compProp", None)
    normalized["BOM"] = normalized_bom
    return normalized


def _job_local_option_name(value: Any, *, where: str, extension: str) -> str:
    name = _non_empty_string(value, where=where)
    path = PurePath(name)
    if (
        path.name != name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise AdministratorConfigError(
            f"{where} must be a Job-local file name, not a path"
        )
    if Path(name).suffix.casefold() != extension:
        raise AdministratorConfigError(f"{where} must use the {extension} extension")
    return name


def _validate_dc_short(preprocessing: Mapping[str, Any]) -> None:
    if "version" in preprocessing:
        raise AdministratorConfigError(
            "preprocessing.version was removed; use top-level aedtVersion"
        )
    if "inputs" in preprocessing:
        raise AdministratorConfigError(
            "preprocessing.inputs was removed; input file names come from the external Heaven JSON"
        )
    unknown_fields = sorted(set(preprocessing) - ALLOWED_PREPROCESSING_FIELDS)
    if unknown_fields:
        raise AdministratorConfigError(
            f"preprocessing has unsupported fields: {unknown_fields}"
        )
    dc_short = _object(
        preprocessing.get("dcShort"),
        where="preprocessing.dcShort",
    )
    if dict(dc_short) != DCIR_DC_SHORT:
        raise AdministratorConfigError(
            "preprocessing.dcShort must match DCIR/source/core/config.json DCIR.dcShort exactly"
        )


def _validate_bom(config: Mapping[str, Any]) -> None:
    bom = _object(config.get("BOM"), where="BOM")
    if bom.get("schemaVersion") != 2:
        raise AdministratorConfigError(
            "BOM.schemaVersion must be 2; Array resistance is read from the "
            "first matching BOM.arrayResistanceSourceColumns header"
        )
    if "path" in bom:
        raise AdministratorConfigError(
            "BOM.path was removed; CAE.PCB.BOM supplies the Job-local BOM file name"
        )
    col_key = _validate_normalized_column_list(bom.get("colKey"), where="BOM.colKey")
    if bom.get("colKey") != ["Designator"]:
        raise AdministratorConfigError(
            "BOM.colKey must be exactly ['Designator']; legacy "
            "['Designator', 'Site Specification'] Configs must move the "
            "resistance column to BOM.arrayResistanceSourceColumns"
        )

    _validate_normalized_column_list(
        bom.get("arrayResistanceSourceColumns"),
        where="BOM.arrayResistanceSourceColumns",
    )

    if set(bom) != {
        "schemaVersion", "colKey", "arrayResistanceSourceColumns"
    }:
        raise AdministratorConfigError(
            "BOM permits only schemaVersion, colKey, and "
            "arrayResistanceSourceColumns; Array pin pairs and resistance values "
            "are derived from EDB pins and the selected BOM/PartList column"
        )


def _validate_ports(config: Mapping[str, Any]) -> None:
    ports = _object(config.get("ports"), where="ports")
    unknown = sorted(set(ports) - ALLOWED_PORT_FIELDS)
    if unknown:
        raise AdministratorConfigError(
            f"ports has unsupported fields: {unknown}; fixed referenceLayer is not allowed"
        )
    impedance = ports.get("singleEndedImpedanceOhm")
    if isinstance(impedance, bool) or not isinstance(impedance, (int, float)) or impedance <= 0:
        raise AdministratorConfigError(
            "ports.singleEndedImpedanceOhm must be a positive number"
        )


def _validate_analysis_options(config: Mapping[str, Any]) -> None:
    options = _object(config.get("analysisOptions"), where="analysisOptions")
    unknown_sections = sorted(set(options) - {"defaults", "syz", "tdr"})
    if unknown_sections:
        raise AdministratorConfigError(
            f"analysisOptions has unsupported sections: {unknown_sections}"
        )

    defaults = _object(options.get("defaults"), where="analysisOptions.defaults")
    if set(defaults) != {"syzProfileId", "tdrProfileId"}:
        raise AdministratorConfigError(
            "analysisOptions.defaults must contain only syzProfileId and tdrProfileId"
        )
    default_syz_profile_id = _non_empty_string(
        defaults.get("syzProfileId"),
        where="analysisOptions.defaults.syzProfileId",
    )
    default_tdr_profile_id = _non_empty_string(
        defaults.get("tdrProfileId"),
        where="analysisOptions.defaults.tdrProfileId",
    )

    syz_profiles = _object(options.get("syz"), where="analysisOptions.syz")
    tdr_profiles = _object(options.get("tdr"), where="analysisOptions.tdr")
    if not syz_profiles or not tdr_profiles:
        raise AdministratorConfigError(
            "analysisOptions.syz and analysisOptions.tdr must each contain at least one profile"
        )

    for profile_id, raw_profile in syz_profiles.items():
        profile_name = _non_empty_string(
            profile_id,
            where="analysisOptions.syz profile ID",
        )
        where = f"analysisOptions.syz.{profile_name}"
        profile = _object(raw_profile, where=where)
        expected_fields = {"sws", "sfsdf"} | set(SYZ_REQUIRED_SETTING_FIELDS)
        if profile.get("sweepMode") == "interpolating":
            expected_fields.add("interpolation")
        if set(profile) != expected_fields:
            raise AdministratorConfigError(
                f"{where} fields must be exactly {sorted(expected_fields)}"
            )
        _job_local_option_name(profile["sws"], where=f"{where}.sws", extension=".sws")
        _job_local_option_name(
            profile["sfsdf"],
            where=f"{where}.sfsdf",
            extension=".sfsdf",
        )
        try:
            normalize_syz_settings(
                {key: profile[key] for key in profile if key in SYZ_SETTING_FIELDS},
                where=where,
            )
        except SyzOptionError as exc:
            raise AdministratorConfigError(str(exc)) from exc

    for profile_id, raw_profile in tdr_profiles.items():
        profile_name = _non_empty_string(
            profile_id,
            where="analysisOptions.tdr profile ID",
        )
        where = f"analysisOptions.tdr.{profile_name}"
        profile = _object(raw_profile, where=where)
        if set(profile) != {"file"}:
            raise AdministratorConfigError(
                f"{where} must contain only the TDR option file name"
            )
        _job_local_option_name(
            profile["file"],
            where=f"{where}.file",
            extension=".json",
        )

    if default_syz_profile_id not in syz_profiles:
        raise AdministratorConfigError(
            "analysisOptions.defaults.syzProfileId references missing profile "
            f"{default_syz_profile_id!r}"
        )
    if default_tdr_profile_id not in tdr_profiles:
        raise AdministratorConfigError(
            "analysisOptions.defaults.tdrProfileId references missing profile "
            f"{default_tdr_profile_id!r}"
        )


def _validate_tdr_report(config: Mapping[str, Any]) -> None:
    report = _object(config.get("tdrReport", {}), where="tdrReport")
    unknown = sorted(set(report) - ALLOWED_TDR_REPORT_FIELDS)
    if unknown:
        raise AdministratorConfigError(
            f"tdrReport has unsupported fields: {unknown}"
        )
    margin = report.get("yAxisMarginOhm", 30)
    if (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(float(margin))
        or float(margin) <= 0
    ):
        raise AdministratorConfigError(
            "tdrReport.yAxisMarginOhm must be a positive number"
        )


def normalize_pcb_capture_policy(
    value: Any, *, allow_extra_fields: bool = False
) -> dict[str, dict[str, bool]]:
    """Return the two administrator-owned FullBatch capture switches.

    The root is optional for compatibility with administrator Configs created
    before the switches existed.  Once declared, both independent decisions
    must be explicit so a typo cannot silently change the public output set.
    """

    if value is None:
        return deepcopy(DEFAULT_PCB_CAPTURE_POLICY)
    policy = _object(value, where="pcbCapture")
    expected = {"route", "overview"}
    invalid_fields = (
        not expected.issubset(policy)
        if allow_extra_fields
        else set(policy) != expected
    )
    if invalid_fields:
        raise AdministratorConfigError(
            "pcbCapture must contain exactly route and overview"
        )
    normalized: dict[str, dict[str, bool]] = {}
    for kind in ("route", "overview"):
        option = _object(policy.get(kind), where=f"pcbCapture.{kind}")
        if set(option) != {"enabled"}:
            raise AdministratorConfigError(
                f"pcbCapture.{kind} must contain only enabled"
            )
        enabled = option.get("enabled")
        if not isinstance(enabled, bool):
            raise AdministratorConfigError(
                f"pcbCapture.{kind}.enabled must be a Boolean"
            )
        normalized[kind] = {"enabled": enabled}
    return normalized


def validate_administrator_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the strict customer administrator Config.

    File existence is intentionally not checked here.  FB-04 resolves the selected
    profile files from the staged Job folders before a solver starts.
    """

    raw_config = _object(payload, where="administrator Config root")
    config = _strip_legacy_arr(raw_config)
    _walk_removed_fields(config)

    unknown_root_fields = sorted(set(config) - ALLOWED_ROOT_FIELDS)
    if unknown_root_fields:
        raise AdministratorConfigError(
            f"administrator Config has unsupported root fields: {unknown_root_fields}"
        )

    if config.get("schemaVersion") != ADMINISTRATOR_CONFIG_SCHEMA_VERSION:
        raise AdministratorConfigError(
            f"schemaVersion must be {ADMINISTRATOR_CONFIG_SCHEMA_VERSION}"
        )
    if config.get("aedtVersion") not in SUPPORTED_AEDT_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_AEDT_VERSIONS))
        raise AdministratorConfigError(
            f"aedtVersion must be one of: {supported}"
        )

    for field in ZUKEN_BOOLEAN_FIELDS:
        if not isinstance(config.get(field), bool):
            raise AdministratorConfigError(f"{field} must be a Boolean")
    _non_empty_string(config.get("DF_path"), where="DF_path")

    is_zuken = bool(config["isZuken"])
    if is_zuken and not (config["exportANF"] and config["exportCMP"]):
        raise AdministratorConfigError(
            "isZuken=true requires exportANF=true and exportCMP=true "
            "because the common reference build consumes the ANF/CMP pair"
        )
    if not is_zuken:
        enabled_exports = [
            field
            for field in ("exportANF", "exportCMP", "exportODB", "exportEDB")
            if config[field]
        ]
        if enabled_exports:
            raise AdministratorConfigError(
                "Zuken export flags must be false when isZuken=false: "
                + ", ".join(enabled_exports)
            )

    preprocessing = _object(config.get("preprocessing"), where="preprocessing")
    _validate_dc_short(preprocessing)
    _validate_bom(config)
    _validate_ports(config)
    _validate_tdr_report(config)
    _validate_analysis_options(config)
    normalized = deepcopy(dict(config))
    normalized["pcbCapture"] = normalize_pcb_capture_policy(
        normalized.get("pcbCapture")
    )
    for field in LEGACY_IGNORED_ROOT_FIELDS:
        normalized.pop(field, None)
    return normalized
