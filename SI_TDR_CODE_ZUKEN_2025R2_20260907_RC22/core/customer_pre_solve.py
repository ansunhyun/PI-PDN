"""Fail-closed FB-06 component and Port evidence contract.

This module has no dependency on ``main`` or ``syz_runtime``.  The runtime,
publisher, and tests can therefore use the same production validator without a
circular import.  The FB-11 live auditor deliberately implements a separate
reader and does not call this validator.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .channel.array_resistor_bom import (
        ARRAY_BOM_POLICY,
        ArrayResistorBomError,
        automatic_array_pin_map,
        canonical_resistance_edb_input,
        resistance_edb_value_string_matches,
        resolve_array_resistor_bom,
        validate_array_bom_snapshot,
    )
    from .channel.customer_components import (
        CUSTOMER_ARRAY_ACTION,
        CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
        CUSTOMER_ARRAY_MODEL_STATE_SCHEMA,
        CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
        CUSTOMER_COMPONENT_READBACK_CAPABILITY,
        CUSTOMER_COMPONENT_READBACK_DETECTION,
        CUSTOMER_COMPONENT_READBACK_SCHEMA,
        CUSTOMER_OFF_PATH_EVIDENCE_SCOPE,
        CUSTOMER_OFF_PATH_POLICY,
        CUSTOMER_SERIES_ACTION,
        plan_customer_path_components,
    )
    from .channel.port_contract import (
        ENDPOINT_LAYER_POLICY,
        PORT_CONTRACT_SCHEMA,
        REFERENCE_NET_POLICY,
        REFERENCE_TERMINAL_POLICY,
        validate_planned_port_contract,
    )
    from .channel.series_models import ARRAY_STEP_KIND, SERIES_STEP_TYPES
except ImportError:  # Support compact customer core imports.
    from channel.array_resistor_bom import (  # type: ignore[no-redef]
        ARRAY_BOM_POLICY,
        ArrayResistorBomError,
        automatic_array_pin_map,
        canonical_resistance_edb_input,
        resistance_edb_value_string_matches,
        resolve_array_resistor_bom,
        validate_array_bom_snapshot,
    )
    from channel.customer_components import (  # type: ignore[no-redef]
        CUSTOMER_ARRAY_ACTION,
        CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
        CUSTOMER_ARRAY_MODEL_STATE_SCHEMA,
        CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
        CUSTOMER_COMPONENT_READBACK_CAPABILITY,
        CUSTOMER_COMPONENT_READBACK_DETECTION,
        CUSTOMER_COMPONENT_READBACK_SCHEMA,
        CUSTOMER_OFF_PATH_EVIDENCE_SCOPE,
        CUSTOMER_OFF_PATH_POLICY,
        CUSTOMER_SERIES_ACTION,
        plan_customer_path_components,
    )
    from channel.port_contract import (  # type: ignore[no-redef]
        ENDPOINT_LAYER_POLICY,
        PORT_CONTRACT_SCHEMA,
        REFERENCE_NET_POLICY,
        REFERENCE_TERMINAL_POLICY,
        validate_planned_port_contract,
    )
    from channel.series_models import (  # type: ignore[no-redef]
        ARRAY_STEP_KIND,
        SERIES_STEP_TYPES,
    )


CUSTOMER_PRE_SOLVE_SCHEMA = "si-tdr-customer-pre-solve-validation/4"
CUSTOMER_PRE_SOLVE_RECORD = "customer_pre_solve_validation.json"
COMPONENT_RECORD = "customer_component_manifest.json"
PORTS_APPLY_RECORD = "ports_apply.json"
PORT_CONTRACT_RECORD = "pre_solve_port_contract.json"
COMBINED_STAGE = "after-port-creation-before-siwave"
FILESYSTEM_TIMESTAMP_TOLERANCE_NS = 2_000_000_000
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


class CustomerPreSolveError(RuntimeError):
    """Raised when strict component/Port application cannot be proven."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CustomerPreSolveError(f"{label} must be an object")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CustomerPreSolveError(f"{label} must be a finite JSON number")
    result = float(value)
    if not math.isfinite(result):
        raise CustomerPreSolveError(f"{label} must be a finite JSON number")
    return result


def _parse_aware_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CustomerPreSolveError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CustomerPreSolveError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CustomerPreSolveError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _validate_combined_time(
    combined: Mapping[str, Any],
    *,
    combined_path: Path,
    record_evidence: Mapping[str, Mapping[str, Any]],
    target_evidence: Mapping[str, Any],
    started_at_ns: int | None,
) -> dict[str, Any]:
    created = _parse_aware_time(combined.get("createdAt"), "combined.createdAt")
    created_ns = _time_ns(created)
    latest_source_ns = max(
        [int(item["mtimeNs"]) for item in record_evidence.values()]
        + [int(target_evidence["newestMtimeNs"])]
    )
    if created_ns + FILESYSTEM_TIMESTAMP_TOLERANCE_NS < latest_source_ns:
        raise CustomerPreSolveError(
            "combined.createdAt predates authoritative record/target AEDB evidence"
        )
    if started_at_ns is not None and (
        created_ns + FILESYSTEM_TIMESTAMP_TOLERANCE_NS < started_at_ns
    ):
        raise CustomerPreSolveError("combined.createdAt predates FullBatch start")
    now = datetime.now(timezone.utc)
    if created > now + MAX_FUTURE_CLOCK_SKEW:
        raise CustomerPreSolveError("combined.createdAt is unreasonably in the future")
    combined_mtime_ns = combined_path.stat().st_mtime_ns
    if combined_mtime_ns + FILESYSTEM_TIMESTAMP_TOLERANCE_NS < created_ns:
        raise CustomerPreSolveError("combined record mtime predates combined.createdAt")
    if combined_mtime_ns > _time_ns(now + MAX_FUTURE_CLOCK_SKEW):
        raise CustomerPreSolveError("combined record mtime is unreasonably in the future")
    return {
        "createdAt": created.isoformat(),
        "filesystemTimestampToleranceNs": FILESYSTEM_TIMESTAMP_TOLERANCE_NS,
        "latestAuthoritativeSourceMtimeNs": latest_source_ns,
    }


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CustomerPreSolveError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise CustomerPreSolveError(f"{label} root must be an object: {path}")
    return payload


def _contained(path: Path, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CustomerPreSolveError(f"{label} is missing: {path}") from exc
    root = root.resolve()
    if resolved != root and root not in resolved.parents:
        raise CustomerPreSolveError(f"{label} is outside the Job root: {resolved}")
    return resolved


def _resolve_path(value: Any, *, base: Path, root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CustomerPreSolveError(f"{label} path is missing")
    raw = Path(value.strip())
    return _contained(raw if raw.is_absolute() else base / raw, root, label)


def file_evidence(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise CustomerPreSolveError(f"evidence file is missing or empty: {resolved}")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "sizeBytes": stat.st_size,
        "sha256": _sha256(resolved),
        "mtimeNs": stat.st_mtime_ns,
    }


def directory_evidence(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise CustomerPreSolveError(f"target AEDB is not a directory: {resolved}")
    digest = hashlib.sha256()
    files = sorted(
        (
            item
            for item in resolved.rglob("*")
            if item.is_file()
            and not (
                item.name.casefold() == "edb.def.tmp"
                and item.stat().st_size == 0
            )
        ),
        key=lambda item: item.relative_to(resolved).as_posix(),
    )
    if not files:
        raise CustomerPreSolveError(f"target AEDB is empty: {resolved}")
    total = 0
    newest = 0
    for item in files:
        relative = item.relative_to(resolved).as_posix()
        stat = item.stat()
        item_hash = _sha256(item)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item_hash.encode("ascii"))
        digest.update(b"\n")
        total += stat.st_size
        newest = max(newest, stat.st_mtime_ns)
    return {
        "path": str(resolved),
        "fileCount": len(files),
        "sizeBytes": total,
        "sha256": digest.hexdigest(),
        "newestMtimeNs": newest,
    }


def _require_file_evidence(
    record: Any, *, expected: Path, root: Path, label: str
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise CustomerPreSolveError(f"{label} evidence is not an object")
    actual_path = _resolve_path(
        record.get("path"), base=root, root=root, label=label
    )
    if actual_path != expected.resolve():
        raise CustomerPreSolveError(f"{label} path differs: {actual_path} != {expected.resolve()}")
    actual = file_evidence(actual_path)
    if _canonical(record) != _canonical(actual):
        raise CustomerPreSolveError(f"{label} size/hash/mtime evidence drift")
    return actual


def _require_directory_evidence(
    record: Any, *, expected: Path, root: Path, label: str
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise CustomerPreSolveError(f"{label} evidence is not an object")
    actual_path = _resolve_path(
        record.get("path"), base=root, root=root, label=label
    )
    if actual_path != expected.resolve():
        raise CustomerPreSolveError(f"{label} path differs")
    actual = directory_evidence(actual_path)
    if _canonical(record) != _canonical(actual):
        raise CustomerPreSolveError(f"{label} tree evidence drift")
    return actual


def _paths(context: Mapping[str, Any]) -> tuple[Path, Path, Path, str]:
    handling = context.get("customerComponentHandling")
    if not isinstance(handling, Mapping):
        raise CustomerPreSolveError("strict customerComponentHandling is missing")
    if (
        handling.get("series") != "short"
        or handling.get("array") != "bom-configured-source-column-authoritative"
    ):
        raise CustomerPreSolveError("strict Series/Array policy differs")
    root = Path(str(handling.get("jobRoot") or "")).resolve()
    if not root.is_dir():
        raise CustomerPreSolveError("customer Job root is missing")
    run_dir = _contained(
        Path(str((context.get("workspace") or {}).get("runDir") or "")),
        root,
        "strict run directory",
    )
    config = _contained(
        Path(str(context.get("configPath") or "")), root, "Generated Run Config"
    )
    if not config.is_file():
        raise CustomerPreSolveError("Generated Run Config is not a file")
    batch = str((context.get("segment") or {}).get("name") or "").strip()
    if not batch:
        raise CustomerPreSolveError("strict batch ID is missing")
    return root, run_dir, config, batch


def is_strict_customer_context(context: Mapping[str, Any]) -> bool:
    handling = context.get("customerComponentHandling")
    return bool(
        isinstance(handling, Mapping)
        and handling.get("series") == "short"
        and handling.get("array") == "bom-configured-source-column-authoritative"
    )


def _config_and_sources(
    context: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], Path, dict[str, Any], Path, dict[str, Any], Path, str]:
    root, run_dir, config_path, batch = _paths(context)
    config = _read_object(config_path, "Generated Run Config")
    config_batch = str((config.get("segment") or {}).get("name") or "")
    if config_batch != batch:
        raise CustomerPreSolveError("Generated Run Config batch ID differs")
    handling = config.get("customerComponentHandling")
    if not isinstance(handling, Mapping):
        raise CustomerPreSolveError("Generated Run Config customerComponentHandling is missing")
    if any(key in config for key in ("seriesModels", "seriesTreatment")):
        raise CustomerPreSolveError("legacy Series fields are forbidden in strict Run Config")
    if any(
        key in handling
        for key in ("arrayPinMap", "resistanceOhm", "value", "seriesTreatment")
    ):
        raise CustomerPreSolveError("legacy/value fields are forbidden in strict component policy")
    if handling.get("arrayPolicy") != ARRAY_BOM_POLICY:
        raise CustomerPreSolveError("authoritative Array BOM policy differs")
    snapshot = handling.get("bomSnapshot")
    try:
        validate_array_bom_snapshot(snapshot, job_root=root)
        bom_path = _resolve_path(
            handling.get("bom"),
            base=config_path.parent,
            root=root,
            label="Job BOM/PartList",
        )
        current_resolution = resolve_array_resistor_bom(
            config,
            job_root=root,
            bom_path=bom_path,
        )
    except ArrayResistorBomError as exc:
        raise CustomerPreSolveError(str(exc)) from exc
    for handling_key, resolution_key in (
        ("bomSnapshot", "bom"),
        ("selector", "selector"),
        ("resolvedColumns", "resolvedColumns"),
        ("arrayCatalog", "arrayCatalog"),
        ("arrayRuleResolution", "arrayRuleResolution"),
    ):
        if _canonical(handling.get(handling_key)) != _canonical(
            current_resolution.get(resolution_key)
        ):
            raise CustomerPreSolveError(
                "authoritative Array BOM/PartList resolution differs: "
                f"{handling_key}"
            )
    report = _resolve_path(
        handling.get("channelPathReport"),
        base=config_path.parent,
        root=root,
        label="Channel Path report",
    )
    ports = _mapping(config.get("ports"), "Generated Run Config ports")
    metadata = _resolve_path(
        ports.get("metadataPath"),
        base=config_path.parent,
        root=root,
        label="Port Role metadata",
    )
    return (
        config_path,
        config,
        report,
        _read_object(report, "Channel Path report"),
        metadata,
        _read_object(metadata, "Port Role metadata"),
        run_dir,
        batch,
    )


def _reference_edb_from_config(
    config: Mapping[str, Any], *, config_path: Path, root: Path
) -> Path:
    layout = _mapping(config.get("layout"), "Generated Run Config layout")
    reference = _resolve_path(
        layout.get("referenceEdb"),
        base=config_path.parent,
        root=root,
        label="Generated Run Config layout.referenceEdb",
    )
    if not reference.is_dir():
        raise CustomerPreSolveError("layout.referenceEdb is not an AEDB directory")
    return reference


def component_source_evidence(context: Mapping[str, Any]) -> dict[str, Any]:
    config, payload, report, _, _, _, _, _ = _config_and_sources(context)
    handling = _mapping(
        payload.get("customerComponentHandling"),
        "Generated Run Config customerComponentHandling",
    )
    root = Path(str(handling.get("jobRoot") or "")).resolve()
    snapshot = _mapping(handling.get("bomSnapshot"), "Array BOM snapshot")
    try:
        validate_array_bom_snapshot(snapshot, job_root=root)
    except ArrayResistorBomError as exc:
        raise CustomerPreSolveError(str(exc)) from exc
    bom_path = _resolve_path(
        handling.get("bom"), base=config.parent, root=root, label="Job BOM/PartList"
    )
    return {
        "runConfig": file_evidence(config),
        "channelPathReport": file_evidence(report),
        "bom": file_evidence(bom_path),
    }


def port_source_evidence(context: Mapping[str, Any]) -> dict[str, Any]:
    config, _, _, _, metadata, _, _, _ = _config_and_sources(context)
    return {
        "runConfig": file_evidence(config),
        "metadata": file_evidence(metadata),
    }


def _forbidden_keys(value: Any, *, path: str = "$") -> None:
    # ``resistanceOhm`` is forbidden as a customer override but is required in
    # saved-EDB read-back evidence.  Keep the input/plan boundary strict while
    # allowing the verifier to record the actual measured zero-ohm state.
    forbidden = {"seriesTreatment"}
    component_suffix = path[len("$.components[") :] if path.startswith("$.components[") else ""
    at_component_entry = bool(component_suffix) and "." not in component_suffix
    if path == "$" or at_component_entry:
        forbidden.update({"resistanceOhm", "value"})
    if isinstance(value, Mapping):
        found = forbidden & set(value)
        if found:
            raise CustomerPreSolveError(
                f"strict component evidence contains forbidden fields at {path}: {sorted(found)}"
            )
        for key, item in value.items():
            _forbidden_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbidden_keys(item, path=f"{path}[{index}]")


def _validate_array_model_state(value: Any, label: str) -> dict[str, Any]:
    state = _mapping(value, label)
    if (
        state.get("schema") != CUSTOMER_ARRAY_MODEL_STATE_SCHEMA
        or state.get("capabilityIdentity") != CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY
        or state.get("capabilityDetection") != CUSTOMER_COMPONENT_READBACK_DETECTION
        or state.get("fullImportedModelReadBack") != "not-claimed"
        or not str(state.get("claimScope") or "").strip()
        or not str(state.get("modelType") or "").strip()
    ):
        raise CustomerPreSolveError(f"{label} identity/scope differs")
    values = _mapping(state.get("componentValues"), f"{label}.componentValues")
    if set(values) != {"resistance", "capacitance", "inductance"}:
        raise CustomerPreSolveError(f"{label}.componentValues coverage differs")
    for name, raw in values.items():
        item = _mapping(raw, f"{label}.componentValues.{name}")
        if set(item) != {"available", "value"} or not isinstance(
            item.get("available"), bool
        ):
            raise CustomerPreSolveError(f"{label}.componentValues.{name} differs")
        if item["available"] and item.get("value") is not None:
            _finite_number(item.get("value"), f"{label}.componentValues.{name}.value")
        elif not item["available"] and item.get("value") is not None:
            raise CustomerPreSolveError(
                f"{label}.componentValues.{name}.value must be null when unavailable"
            )
    pin_nets = _mapping(state.get("pinNets"), f"{label}.pinNets")
    if not pin_nets or any(not str(name).strip() for name in pin_nets):
        raise CustomerPreSolveError(f"{label}.pinNets is incomplete")
    pairs = state.get("pinPairs")
    if not isinstance(pairs, list) or not pairs:
        raise CustomerPreSolveError(f"{label}.pinPairs must be a non-empty array")
    keys: list[tuple[str, str]] = []
    for index, raw in enumerate(pairs):
        pair = _mapping(raw, f"{label}.pinPairs[{index}]")
        first = str(pair.get("firstPin") or "").strip()
        second = str(pair.get("secondPin") or "").strip()
        if not first or not second or first == second:
            raise CustomerPreSolveError(f"{label}.pinPairs[{index}] endpoints differ")
        enabled = _mapping(
            pair.get("rlcEnable"), f"{label}.pinPairs[{index}].rlcEnable"
        )
        rlc = _mapping(
            pair.get("rlcValues"), f"{label}.pinPairs[{index}].rlcValues"
        )
        resistance_string = _mapping(
            pair.get("resistanceValueString"),
            f"{label}.pinPairs[{index}].resistanceValueString",
        )
        expected_fields = {"resistance", "inductance", "capacitance"}
        if set(enabled) != expected_fields or set(rlc) != expected_fields:
            raise CustomerPreSolveError(f"{label}.pinPairs[{index}] RLC coverage differs")
        if any(not isinstance(enabled[field], bool) for field in expected_fields):
            raise CustomerPreSolveError(f"{label}.pinPairs[{index}] enables differ")
        if (
            set(resistance_string) != {"value", "source"}
            or not str(resistance_string.get("value") or "").strip()
            or not str(resistance_string.get("source") or "").endswith(
                ".ToString()"
            )
        ):
            raise CustomerPreSolveError(
                f"{label}.pinPairs[{index}] resistance Value.ToString evidence differs"
            )
        for field in expected_fields:
            if rlc[field] is not None:
                _finite_number(
                    rlc[field], f"{label}.pinPairs[{index}].rlcValues.{field}"
                )
        keys.append((first.casefold(), second.casefold()))
    if len(set(keys)) != len(keys):
        raise CustomerPreSolveError(f"{label}.pinPairs contains duplicates")
    return dict(state)


def _require_configured_array_state(
    state: Mapping[str, Any],
    *,
    pin_pairs: list[list[str]],
    resistance_ohm: float,
    edb_resistance_input: str,
    label: str,
) -> None:
    if str(state.get("modelType") or "").casefold() != "rlc" or str(
        state.get("componentType") or ""
    ).casefold() != "resistor":
        raise CustomerPreSolveError(f"{label} must be an RLC Resistor")
    expected = {
        tuple(sorted((str(left), str(right)), key=str.casefold))
        for left, right in pin_pairs
    }
    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw in enumerate(state.get("pinPairs") or []):
        pair = _mapping(raw, f"{label}.pinPairs[{index}]")
        key = tuple(
            sorted(
                (str(pair.get("firstPin") or ""), str(pair.get("secondPin") or "")),
                key=str.casefold,
            )
        )
        actual[key] = pair
    if set(actual) != expected:
        raise CustomerPreSolveError(f"{label} configured pin-pair coverage differs")
    for key, pair in actual.items():
        enabled = _mapping(pair.get("rlcEnable"), f"{label}.{key}.rlcEnable")
        values = _mapping(pair.get("rlcValues"), f"{label}.{key}.rlcValues")
        resistance_string = _mapping(
            pair.get("resistanceValueString"),
            f"{label}.{key}.resistanceValueString",
        )
        if (
            enabled != {"resistance": True, "inductance": False, "capacitance": False}
            or values != {
                "resistance": resistance_ohm,
                "inductance": 0.0,
                "capacitance": 0.0,
            }
            or not resistance_edb_value_string_matches(
                edb_resistance_input, resistance_string.get("value")
            )
            or not str(resistance_string.get("source") or "").endswith(
                ".ToString()"
            )
        ):
            raise CustomerPreSolveError(f"{label} RLC values differ from Config")


def _validate_component_manifest(
    context: Mapping[str, Any], *, target_edb: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    (
        config_path,
        config,
        report_path,
        report,
        _,
        _,
        run_dir,
        batch,
    ) = _config_and_sources(context)
    root = Path(str((config.get("customerComponentHandling") or {}).get("jobRoot"))).resolve()
    manifest_path = run_dir / COMPONENT_RECORD
    manifest = _read_object(manifest_path, COMPONENT_RECORD)
    _forbidden_keys(manifest)
    handling = config["customerComponentHandling"]
    snapshot = _mapping(handling.get("bomSnapshot"), "Array BOM snapshot")
    if _canonical(manifest.get("bomSnapshot")) != _canonical(snapshot):
        raise CustomerPreSolveError("component manifest Array BOM snapshot differs")
    if _canonical(manifest.get("selector")) != _canonical(handling.get("selector")):
        raise CustomerPreSolveError("component manifest Array source priority differs")
    if _canonical(manifest.get("resolvedColumns")) != _canonical(
        handling.get("resolvedColumns")
    ):
        raise CustomerPreSolveError("component manifest selected Array source differs")
    try:
        validate_array_bom_snapshot(snapshot, job_root=root)
    except ArrayResistorBomError as exc:
        raise CustomerPreSolveError(str(exc)) from exc
    expected_plan = plan_customer_path_components(
        report,
        array_catalog=list(handling.get("arrayCatalog") or []),
        batch_id=batch,
    )
    if (
        manifest.get("schema") != CUSTOMER_COMPONENT_CONTRACT_SCHEMA
        or manifest.get("status") != "ok"
        or manifest.get("mode") != "customer-strict"
        or manifest.get("batchId") != batch
        or manifest.get("offPathPolicy") != CUSTOMER_OFF_PATH_POLICY
        or manifest.get("offPathEvidenceScope")
        != CUSTOMER_OFF_PATH_EVIDENCE_SCOPE
    ):
        raise CustomerPreSolveError("customer component manifest identity/status differs")
    recorded_target = _resolve_path(
        manifest.get("targetEdb"), base=run_dir, root=root, label="component target AEDB"
    )
    if recorded_target != target_edb.resolve():
        raise CustomerPreSolveError("component target AEDB differs")
    recorded_report = _resolve_path(
        manifest.get("channelPathReport"),
        base=run_dir,
        root=root,
        label="component Channel Path report",
    )
    if recorded_report != report_path.resolve():
        raise CustomerPreSolveError("component Channel Path report differs")
    source = manifest.get("sourceEvidence") or {}
    if not isinstance(source, Mapping):
        raise CustomerPreSolveError("component source evidence is missing")
    _require_file_evidence(source.get("runConfig"), expected=config_path, root=root, label="component Run Config")
    _require_file_evidence(source.get("channelPathReport"), expected=report_path, root=root, label="component Channel Path report")
    bom_path = _resolve_path(
        handling.get("bom"),
        base=config_path.parent,
        root=root,
        label="Job BOM/PartList",
    )
    _require_file_evidence(
        source.get("bom"),
        expected=bom_path,
        root=root,
        label="component Job BOM/PartList",
    )

    expected_components = expected_plan["components"]
    actual_components = manifest.get("components")
    if (
        manifest.get("componentCount") != len(expected_components)
        or not isinstance(actual_components, list)
        or len(actual_components) != len(expected_components)
    ):
        raise CustomerPreSolveError("component count differs from resolved Channel Paths")
    verification_by_component: dict[str, dict[str, Any]] = {}
    modified: list[str] = []
    modeled_arrays: list[str] = []
    array_resistance_bindings: list[dict[str, Any]] = []
    for expected, actual in zip(expected_components, actual_components):
        if not isinstance(actual, Mapping):
            raise CustomerPreSolveError("component manifest contains a non-object entry")
        for field in (
            "component",
            "pathIncluded",
            "pathEvidence",
            "stepKinds",
            "isArray",
            "arrayGroup",
            "bomSelection",
            "arrayCatalogDesignator",
            "arrayDesignatorMatch",
            "pinMapPolicy",
            "arrayModel",
            "action",
        ):
            if _canonical(actual.get(field)) != _canonical(expected.get(field)):
                raise CustomerPreSolveError(
                    f"component plan/evidence differs: {expected.get('component')}/{field}"
                )
        before = actual.get("before")
        after = actual.get("after")
        verification = actual.get("verification")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping) or not isinstance(verification, Mapping):
            raise CustomerPreSolveError(f"component apply evidence is incomplete: {expected['component']}")
        pin_nets = before.get("pinNets")
        if not isinstance(pin_nets, Mapping):
            raise CustomerPreSolveError(f"component pin evidence is missing: {expected['component']}")
        if expected["isArray"]:
            if expected["action"] != CUSTOMER_ARRAY_ACTION:
                raise CustomerPreSolveError("Array action differs")
            pin_count = actual.get("edbPinCount")
            if isinstance(pin_count, bool) or pin_count not in {4, 8}:
                raise CustomerPreSolveError(
                    f"Array EDB pin count must be 4 or 8: {expected['component']}"
                )
            try:
                pairs = automatic_array_pin_map(
                    list(pin_nets), refdes=str(expected["component"])
                )
            except ArrayResistorBomError as exc:
                raise CustomerPreSolveError(str(exc)) from exc
            if actual.get("arrayPinMap") != pairs or pin_count != len(pin_nets):
                raise CustomerPreSolveError(
                    f"Array automatic pin map differs: {expected['component']}"
                )
            expected["edbPinCount"] = pin_count
            expected["arrayPinMap"] = pairs
            if any(pin not in pin_nets for pair in pairs for pin in pair):
                raise CustomerPreSolveError(f"Array pin map references missing pins: {expected['component']}")
            model = _mapping(expected.get("arrayModel"), f"Array {expected['component']} model")
            resistance = _finite_number(
                model.get("r_ohm"), f"Array {expected['component']} resistance"
            )
            edb_resistance_input = model.get("r_edb_input")
            if (
                model.get("type") != "resistor"
                or resistance <= 0
                or not isinstance(edb_resistance_input, str)
                or edb_resistance_input
                != canonical_resistance_edb_input(resistance)
            ):
                raise CustomerPreSolveError(f"Array Config model differs: {expected['component']}")
            if (
                str(after.get("componentType") or "").casefold() != "resistor"
                or _canonical(after.get("pinNets")) != _canonical(pin_nets)
            ):
                raise CustomerPreSolveError(f"Array post-apply identity/pins differ: {expected['component']}")
            visible_before = _mapping(
                before.get("wrapperVisibleModelState"),
                f"Array {expected['component']} before wrapper state",
            )
            if visible_before.get("status") not in {
                "captured",
                "unavailable-or-incomplete",
            }:
                raise CustomerPreSolveError(
                    f"Array before wrapper state differs: {expected['component']}"
                )
            after_state = _validate_array_model_state(
                after.get("arrayModelState"),
                f"Array {expected['component']} immediate post-apply model state",
            )
            _require_configured_array_state(
                after_state,
                pin_pairs=list(pairs),
                resistance_ohm=resistance,
                edb_resistance_input=edb_resistance_input,
                label=f"Array {expected['component']} immediate post-apply model state",
            )
            operation = _mapping(
                verification.get("operationReceipt"),
                f"Array {expected['component']} operation receipt",
            )
            cmp_case = verification.get("cmpCase")
            cmp_resistance = verification.get("cmpResistanceOhm")
            differences = verification.get("differences")
            if (
                set(verification)
                != {
                    "result", "method", "arrayPinMapPinsExist", "pinPairs",
                    "bomResistanceTokens", "resistanceOhm",
                    "edbResistanceInput", "resistanceSemanticSha256",
                    "modelSource", "bomSelection",
                    "edbPinCount", "cmpCase", "cmpResistanceOhm",
                    "differences", "conflictPolicy", "action",
                    "operationReceipt", "edbReadBack",
                }
                or verification.get("result")
                != "configured-source-column-model-reapplied"
                or verification.get("method")
                != "bom-configured-source-column-fixed-pin-pair-rlc"
                or verification.get("arrayPinMapPinsExist") is not True
                or verification.get("pinPairs") != pairs
                or _canonical(verification.get("bomResistanceTokens"))
                != _canonical(expected.get("bomSelection", {}).get("sourceTokens"))
                or verification.get("resistanceOhm") != resistance
                or verification.get("edbResistanceInput")
                != edb_resistance_input
                or verification.get("resistanceSemanticSha256")
                != expected.get("bomSelection", {}).get(
                    "resistanceSemanticSha256"
                )
                or verification.get("modelSource")
                != "bom-configured-source-column"
                or _canonical(verification.get("bomSelection"))
                != _canonical(expected.get("bomSelection"))
                or verification.get("edbPinCount") != pin_count
                or cmp_case not in {"A", "B", "C"}
                or (cmp_case == "A" and cmp_resistance != resistance)
                or (cmp_case == "B" and cmp_resistance is not None)
                or (
                    cmp_resistance is not None
                    and (
                        isinstance(cmp_resistance, bool)
                        or not isinstance(cmp_resistance, (int, float))
                        or not math.isfinite(float(cmp_resistance))
                    )
                )
                or (
                    cmp_case == "B"
                    and visible_before.get("status") != "unavailable-or-incomplete"
                )
                or (
                    cmp_case in {"A", "C"}
                    and visible_before.get("status") != "captured"
                )
                or not isinstance(differences, list)
                or (cmp_case == "A" and differences)
                or (cmp_case in {"B", "C"} and not differences)
                or verification.get("conflictPolicy")
                != "record-and-bom-reapply"
                or verification.get("action") != CUSTOMER_ARRAY_ACTION
                or operation
                != {
                    "operation": "assign-pin-pair-rlc",
                    "target": expected["component"],
                    "pinPairs": pairs,
                    "bomResistanceTokens": expected.get("bomSelection", {}).get(
                        "sourceTokens"
                    ),
                    "resistanceOhm": resistance,
                    "edbResistanceInput": edb_resistance_input,
                    "resistanceSemanticSha256": expected.get(
                        "bomSelection", {}
                    ).get("resistanceSemanticSha256"),
                    "wrapperPostApplyState": "captured",
                    "result": "completed",
                }
                or verification.get("edbReadBack") != "pending-save-reopen"
            ):
                raise CustomerPreSolveError(f"Array model apply evidence differs: {expected['component']}")
            modeled_arrays.append(str(expected["component"]))
        else:
            if expected["action"] != CUSTOMER_SERIES_ACTION or ARRAY_STEP_KIND in expected.get("stepKinds", []):
                raise CustomerPreSolveError("Series action/type differs")
            if not set(expected.get("stepKinds") or []).issubset(SERIES_STEP_TYPES):
                raise CustomerPreSolveError(f"non-Series path component was shorted: {expected['component']}")
            if len(pin_nets) != 2:
                raise CustomerPreSolveError(f"Series component is not two-pin: {expected['component']}")
            expected_pair = [sorted((str(value) for value in pin_nets), key=str.casefold)]
            if (
                verification.get("result") != "short-applied"
                or verification.get("method") != "zero-ohm-pin-pair-rlc"
                or verification.get("rOhm") != 0.0
                or verification.get("pinPairs") != expected_pair
                or set(verification) != {"result", "method", "pinPairs", "rOhm"}
            ):
                raise CustomerPreSolveError(f"Series zero-ohm apply evidence differs: {expected['component']}")
            modified.append(str(expected["component"]))
        verification_by_component[str(expected["component"])] = dict(verification)

    if manifest.get("modifiedComponents") != modified or manifest.get("modeledArrayComponents") != modeled_arrays:
        raise CustomerPreSolveError("modified/modeled component partition differs")
    expected_channels: dict[str, list[dict[str, Any]]] = {}
    for channel, items in expected_plan["channels"].items():
        expected_channels[channel] = [
            {**item, "verification": verification_by_component[item["component"]]}
            for item in items
        ]
    if _canonical(manifest.get("channels")) != _canonical(expected_channels):
        raise CustomerPreSolveError("component channel/path partition differs")
    read_back = manifest.get("readBack")
    if (
        not isinstance(read_back, Mapping)
        or read_back.get("schema") != CUSTOMER_COMPONENT_READBACK_SCHEMA
        or read_back.get("status") != "verified"
        or read_back.get("capabilityIdentity")
        != CUSTOMER_COMPONENT_READBACK_CAPABILITY
        or read_back.get("capabilityDetection")
        != CUSTOMER_COMPONENT_READBACK_DETECTION
        or read_back.get("saveReopenVerified") is not True
        or read_back.get("componentCount") != len(expected_components)
        or not isinstance(read_back.get("components"), list)
        or len(read_back["components"]) != len(expected_components)
    ):
        raise CustomerPreSolveError("saved-EDB component read-back evidence is not verified")
    expected_readback_order = [str(item["component"]) for item in expected_components]
    if [str(item.get("component") or "") for item in read_back["components"] if isinstance(item, Mapping)] != expected_readback_order:
        raise CustomerPreSolveError("component read-back order/coverage differs")
    for expected, evidence in zip(expected_components, read_back["components"]):
        if not isinstance(evidence, Mapping) or evidence.get("status") != "verified":
            raise CustomerPreSolveError("component read-back contains an unverified entry")
        if evidence.get("component") != expected.get("component") or evidence.get("action") != expected.get("action"):
            raise CustomerPreSolveError("component read-back target/action differs")
        if expected["isArray"]:
            read_back_state = _validate_array_model_state(
                evidence.get("readBackState"),
                f"Array {expected['component']} read-back model state",
            )
            model = _mapping(expected.get("arrayModel"), f"Array {expected['component']} model")
            resistance = _finite_number(
                model.get("r_ohm"), f"Array {expected['component']} resistance"
            )
            edb_resistance_input = model.get("r_edb_input")
            if (
                not isinstance(edb_resistance_input, str)
                or edb_resistance_input
                != canonical_resistance_edb_input(resistance)
            ):
                raise CustomerPreSolveError(
                    f"Array {expected['component']} EDB input differs"
                )
            _require_configured_array_state(
                read_back_state,
                pin_pairs=list(expected.get("arrayPinMap") or []),
                resistance_ohm=resistance,
                edb_resistance_input=edb_resistance_input,
                label=f"Array {expected['component']} read-back model state",
            )
            expected_state_hash = hashlib.sha256(
                _canonical(read_back_state).encode("utf-8")
            ).hexdigest()
            if (
                set(evidence)
                != {
                    "component",
                    "action",
                    "status",
                    "arrayPinMap",
                    "modelSource",
                    "bomSelection",
                    "bomResistanceTokens",
                    "edbPinCount",
                    "modelInstalled",
                    "resistanceOhm",
                    "edbResistanceInput",
                    "resistanceSemanticSha256",
                    "afterEqualsReadBack",
                    "modelStateScope",
                    "modelStateSha256",
                    "readBackState",
                    "fullImportedModelReadBack",
                }
                or
                _canonical(evidence.get("arrayPinMap"))
                != _canonical(expected.get("arrayPinMap"))
                or evidence.get("modelSource") != "bom-configured-source-column"
                or _canonical(evidence.get("bomSelection"))
                != _canonical(expected.get("bomSelection"))
                or _canonical(evidence.get("bomResistanceTokens"))
                != _canonical(expected.get("bomSelection", {}).get("sourceTokens"))
                or evidence.get("edbPinCount") != expected.get("edbPinCount")
                or evidence.get("modelInstalled") is not True
                or evidence.get("resistanceOhm") != resistance
                or evidence.get("edbResistanceInput")
                != edb_resistance_input
                or evidence.get("resistanceSemanticSha256")
                != expected.get("bomSelection", {}).get(
                    "resistanceSemanticSha256"
                )
                or evidence.get("afterEqualsReadBack") is not True
                or evidence.get("modelStateScope")
                != CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY
                or evidence.get("modelStateSha256") != expected_state_hash
                or evidence.get("fullImportedModelReadBack") != "not-claimed"
            ):
                raise CustomerPreSolveError(
                    f"Array saved-EDB Config model read-back differs: {expected['component']}"
                )
            array_resistance_bindings.append(
                {
                    "component": expected["component"],
                    "bomResistanceTokens": deepcopy(
                        evidence.get("bomResistanceTokens")
                    ),
                    "resistanceOhm": resistance,
                    "edbResistanceInput": edb_resistance_input,
                    "resistanceSemanticSha256": evidence.get(
                        "resistanceSemanticSha256"
                    ),
                    "readBackModelStateSha256": evidence.get(
                        "modelStateSha256"
                    ),
                }
            )
        else:
            pin_nets = actual_components[expected_readback_order.index(str(expected["component"]))].get("before", {}).get("pinNets", {})
            expected_pair = sorted((str(value) for value in pin_nets), key=str.casefold)
            if (
                evidence.get("modelType") != "RLC"
                or evidence.get("pinPair") != expected_pair
                or evidence.get("resistanceOhm") != 0.0
                or evidence.get("componentResistanceOhm") != 0.0
                or evidence.get("rEnabled") is not True
                or evidence.get("lEnabled") is not False
                or evidence.get("cEnabled") is not False
            ):
                raise CustomerPreSolveError(
                    f"Series saved-EDB RLC read-back differs: {expected['component']}"
                )
    apply_evidence = manifest.get("applyEvidence")
    if (
        not isinstance(apply_evidence, Mapping)
        or apply_evidence.get("modelSetOperationCount") != len(modified) + len(modeled_arrays)
        or apply_evidence.get("edbReadBack") != "verified-after-save-reopen"
        or apply_evidence.get("readBackCapabilityIdentity")
        != read_back.get("capabilityIdentity")
    ):
        raise CustomerPreSolveError("component apply/read-back boundary evidence differs")
    return manifest, {
        "record": file_evidence(manifest_path),
        "componentCount": len(expected_components),
        "modifiedComponents": modified,
        "modeledArrayComponents": modeled_arrays,
        "channels": sorted(expected_channels),
        "offPathPolicy": CUSTOMER_OFF_PATH_POLICY,
        "offPathEvidenceScope": CUSTOMER_OFF_PATH_EVIDENCE_SCOPE,
        "edbReadBack": "verified-after-save-reopen",
        "readBackCapabilityIdentity": read_back.get("capabilityIdentity"),
        "arrayBom": deepcopy(dict(snapshot)),
        "arrayResistanceBindings": array_resistance_bindings,
    }


def _validate_reference_selection(
    selection_value: Any,
    *,
    contract: Mapping[str, Any],
    applied: Mapping[str, Any],
    expected_component: str,
    expected_pin: str,
    expected_net: str,
) -> dict[str, Any]:
    selection = _mapping(selection_value, "Port referenceNetSelection")
    reason = selection.get("selectionReason")
    candidates = selection.get("candidates")
    selected = _mapping(
        selection.get("selectedEvidence"), "Port selected reference evidence"
    )
    if (
        selection.get("policy") != REFERENCE_NET_POLICY
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(candidates, list)
        or not candidates
    ):
        raise CustomerPreSolveError("Port reference selection policy/evidence differs")
    candidate_objects = [
        _mapping(item, f"Port reference candidate[{index}]")
        for index, item in enumerate(candidates)
    ]
    matching = [
        item for item in candidate_objects if _canonical(item) == _canonical(selected)
    ]
    reference_net = str(applied.get("referenceNet") or "")
    evidence_values = selected.get("evidence")
    if not isinstance(evidence_values, list):
        evidence_values = [evidence_values]
    if (
        len(matching) != 1
        or selected.get("exists") is not True
        or str(selected.get("name") or "") != reference_net
        or not any(str(value).strip() for value in evidence_values if value is not None)
    ):
        raise CustomerPreSolveError(
            "selected Reference Net must exactly match one EDB-backed candidate"
        )
    layer_evidence = _mapping(
        contract.get("layerEvidence"), "Port terminal layerEvidence"
    )
    pin_range = _mapping(
        layer_evidence.get("pinLayerRange"), "Port terminal pinLayerRange"
    )
    layer = str(applied.get("layer") or "")
    if (
        contract.get("component") != expected_component
        or contract.get("pin") != expected_pin
        or contract.get("signalNet") != expected_net
        or layer_evidence.get("source") != "pin_layer_range_start"
        or pin_range.get("start") != layer
        or _canonical(pin_range) != _canonical(applied.get("pinLayerRange"))
    ):
        raise CustomerPreSolveError(
            "Port endpoint/layer EDB-backed contract differs"
        )
    return dict(selection)


def _validate_ports_apply(
    context: Mapping[str, Any], *, target_edb: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path, config, _, _, metadata_path, metadata, run_dir, batch = _config_and_sources(context)
    root = Path(str((config.get("customerComponentHandling") or {}).get("jobRoot"))).resolve()
    ports_config = _mapping(config.get("ports"), "Generated Run Config ports")
    if (
        ports_config.get("mode") != "metadata-snp"
        or ports_config.get("terminalLayerPolicy") != ENDPOINT_LAYER_POLICY
        or "referenceLayer" in ports_config
        or ports_config.get("portContractValidation") != "strict"
    ):
        raise CustomerPreSolveError("strict metadata Port policy differs or fixed referenceLayer is present")
    record_path = run_dir / PORTS_APPLY_RECORD
    record = _read_object(record_path, PORTS_APPLY_RECORD)
    if (
        record.get("schema") != PORT_CONTRACT_SCHEMA
        or record.get("status") != "ok"
        or record.get("mode") != "metadata-snp"
        or record.get("batchId") != batch
    ):
        raise CustomerPreSolveError("ports_apply identity/status differs")
    recorded_target = _resolve_path(record.get("targetEdb"), base=run_dir, root=root, label="Port target AEDB")
    if recorded_target != target_edb.resolve():
        raise CustomerPreSolveError("Port target AEDB differs")
    source_edb = _reference_edb_from_config(
        config, config_path=config_path, root=root
    )
    recorded_source = _resolve_path(
        record.get("sourceEdb"),
        base=run_dir,
        root=root,
        label="Port source AEDB",
    )
    if recorded_source != source_edb or recorded_source == recorded_target:
        raise CustomerPreSolveError(
            "ports_apply sourceEdb differs from layout.referenceEdb or equals targetEdb"
        )
    configured_impedance = _finite_number(
        ports_config.get("singleEndedImpedanceOhm"),
        "ports.singleEndedImpedanceOhm",
    )
    recorded_impedance = _finite_number(
        record.get("singleEndedImpedanceOhm"),
        "ports_apply.singleEndedImpedanceOhm",
    )
    if configured_impedance <= 0 or recorded_impedance != configured_impedance:
        raise CustomerPreSolveError("Port single-ended impedance differs")
    recorded_metadata = _resolve_path(record.get("metadataPath"), base=run_dir, root=root, label="Port metadata")
    if recorded_metadata != metadata_path.resolve():
        raise CustomerPreSolveError("ports_apply metadata path differs")
    source = record.get("sourceEvidence") or {}
    if not isinstance(source, Mapping):
        raise CustomerPreSolveError("ports_apply source evidence is missing")
    _require_file_evidence(source.get("runConfig"), expected=config_path, root=root, label="Port Run Config")
    _require_file_evidence(source.get("metadata"), expected=metadata_path, root=root, label="Port metadata")

    expected_order = [str(value) for value in ports_config.get("portOrder") or []]
    metadata_ports = metadata.get("ports") or []
    metadata_order = [str(item.get("name") or "") for item in metadata_ports if isinstance(item, Mapping)]
    expected_count = int(ports_config.get("touchstonePortCount") or 0)
    created = record.get("createdPorts")
    if (
        expected_count <= 0
        or expected_order != metadata_order
        or len(expected_order) != expected_count
        or len(set(expected_order)) != len(expected_order)
        or record.get("portCount") != expected_count
        or record.get("portOrder") != expected_order
        or not isinstance(created, list)
        or len(created) != expected_count
    ):
        raise CustomerPreSolveError("Port count/order/metadata coverage differs")
    if any(not isinstance(item, Mapping) for item in created):
        raise CustomerPreSolveError("createdPorts contains a non-object entry")
    created_names = [str(item.get("name") or "") for item in created]
    if created_names != expected_order or len(set(created_names)) != len(created_names):
        raise CustomerPreSolveError("created Port order has missing/extra/duplicate names")
    references: list[dict[str, Any]] = []
    for expected_index, (metadata_port, applied) in enumerate(zip(metadata_ports, created), start=1):
        if not isinstance(metadata_port, Mapping):
            raise CustomerPreSolveError("metadata ports contains a non-object entry")
        positive = metadata_port.get("positive") or {}
        parameters = positive.get("parameters") if isinstance(positive, Mapping) else None
        padstack = parameters.get("padstack") if isinstance(parameters, Mapping) else None
        if (
            metadata_port.get("index") != expected_index
            or applied.get("index") != expected_index
            or applied.get("applyMode") != "created"
            or applied.get("terminalType") != "PadstackInstanceTerminal"
            or not isinstance(padstack, Mapping)
        ):
            raise CustomerPreSolveError(f"Port creation method/index differs: {expected_order[expected_index - 1]}")
        expected_component = str(padstack.get("component") or "")
        expected_pin = str(padstack.get("pin") or "")
        expected_net = str(positive.get("net") or padstack.get("net") or "")
        if (
            not expected_component
            or not expected_pin
            or not expected_net
            or applied.get("component") != expected_component
            or applied.get("pin") != expected_pin
            or applied.get("net") != expected_net
            or applied.get("layerSource") != "pin_layer_range_start"
        ):
            raise CustomerPreSolveError(f"Port component/pin/net/layer source differs: {applied.get('name')}")
        contract = applied.get("terminalLayerContract")
        selection = applied.get("referenceNetSelection")
        if not isinstance(contract, Mapping) or not isinstance(selection, Mapping):
            raise CustomerPreSolveError(f"Port terminal/reference contract is missing: {applied.get('name')}")
        if (
            contract.get("component") != expected_component
            or contract.get("pin") != expected_pin
            or contract.get("signalNet") != expected_net
            or contract.get("layerPolicy") != ENDPOINT_LAYER_POLICY
            or not str(applied.get("layer") or "")
            or applied.get("layer") != applied.get("referenceLayer")
            or applied.get("layer") != contract.get("positiveLayer")
            or applied.get("referenceLayer") != contract.get("referenceLayer")
            or applied.get("referenceNet") != contract.get("referenceNet")
            or _canonical(selection) != _canonical(contract.get("referenceNetSelection"))
        ):
            raise CustomerPreSolveError(f"same-layer/reference contract differs: {applied.get('name')}")
        selection = _validate_reference_selection(
            selection,
            contract=contract,
            applied=applied,
            expected_component=expected_component,
            expected_pin=expected_pin,
            expected_net=expected_net,
        )
        reference_pin_selection = _mapping(
            applied.get("referencePinSelection"),
            f"Port referencePinSelection {applied.get('name')}",
        )
        reference_pin_range = _mapping(
            reference_pin_selection.get("pinLayerRange"),
            f"Port reference pinLayerRange {applied.get('name')}",
        )
        reference_position = _mapping(
            reference_pin_selection.get("position"),
            f"Port reference position {applied.get('name')}",
        )
        candidate_count = reference_pin_selection.get("candidateCount")
        distance = _finite_number(
            reference_pin_selection.get("distanceMeter"),
            f"Port reference distance {applied.get('name')}",
        )
        if (
            applied.get("referenceTerminalType") != "PadstackInstanceTerminal"
            or applied.get("referenceComponent") != expected_component
            or not str(applied.get("referencePin") or "").strip()
            or reference_pin_selection.get("policy") != REFERENCE_TERMINAL_POLICY
            or isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count <= 0
            or reference_pin_selection.get("component") != expected_component
            or reference_pin_selection.get("pin") != applied.get("referencePin")
            or reference_pin_selection.get("net") != applied.get("referenceNet")
            or reference_pin_selection.get("layer") != applied.get("referenceLayer")
            or reference_pin_range.get("start") != applied.get("referenceLayer")
            or not str(reference_pin_range.get("stop") or "").strip()
            or set(reference_position) != {"x", "y"}
            or any(not str(reference_position.get(axis) or "").strip() for axis in ("x", "y"))
            or _canonical(reference_position) != _canonical(applied.get("referencePoint"))
            or distance < 0
        ):
            raise CustomerPreSolveError(
                f"same-component reference pin evidence differs: {applied.get('name')}"
            )
        references.append(
            {
                "name": applied["name"],
                "component": expected_component,
                "pin": expected_pin,
                "net": expected_net,
                "layer": applied["layer"],
                "referenceNet": applied["referenceNet"],
                "referenceComponent": applied["referenceComponent"],
                "referencePin": applied["referencePin"],
                "selectionReason": selection.get("selectionReason"),
            }
        )
    return record, {
        "record": file_evidence(record_path),
        "portCount": expected_count,
        "portOrder": expected_order,
        "sourceEdb": directory_evidence(source_edb),
        "singleEndedImpedanceOhm": configured_impedance,
        "terminalLayerPolicy": ENDPOINT_LAYER_POLICY,
        "ports": references,
        "edbReadBack": "not-claimed-live-pending",
    }


def _validate_planned_contract(
    context: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path, config, _, _, metadata_path, metadata, run_dir, batch = _config_and_sources(context)
    root = Path(str((config.get("customerComponentHandling") or {}).get("jobRoot"))).resolve()
    ports_config = _mapping(config.get("ports"), "Generated Run Config ports")
    tdr_config = _mapping(config.get("tdr"), "Generated Run Config tdr")
    expected_order = [str(value) for value in ports_config.get("portOrder") or []]
    expected = validate_planned_port_contract(
        expected_order=expected_order,
        touchstone_port_count=int(ports_config.get("touchstonePortCount") or 0),
        metadata_payload=metadata,
        tdr_channels=[
            item for item in tdr_config.get("channels") or []
            if isinstance(item, Mapping)
        ],
        require_role_metadata=True,
    )
    record_path = run_dir / PORT_CONTRACT_RECORD
    record = _read_object(record_path, PORT_CONTRACT_RECORD)
    expected.update(
        {
            "batchId": batch,
            "metadataPath": str(metadata_path),
            "metadataEvidence": file_evidence(metadata_path),
            "portOrderPolicy": ports_config.get("portOrderPolicy"),
        }
    )
    if _canonical(record) != _canonical(expected):
        raise CustomerPreSolveError("pre-solve Port contract differs from current metadata/TDR roles")
    if (
        record.get("schema") != PORT_CONTRACT_SCHEMA
        or record.get("status") != "ok"
        or record.get("validationStage") != "before-syz-solve"
        or record.get("batchId") != batch
        or record.get("issues") != []
        or record.get("plannedTouchstonePortOrder") != expected_order
        or record.get("roleMetadataPortOrder") != expected_order
        or len(record.get("roleMetadataCoverage") or []) != len(expected_order)
        or set(record.get("roleMetadataCoverage") or []) != set(expected_order)
    ):
        raise CustomerPreSolveError("pre-solve Port status/order/role coverage differs")
    _require_file_evidence(record.get("metadataEvidence"), expected=metadata_path, root=root, label="pre-solve metadata")
    return record, {
        "record": file_evidence(record_path),
        "portCount": len(expected_order),
        "portOrder": expected_order,
        "roleMetadataCoverage": list(record.get("roleMetadataCoverage") or []),
        "issueCount": 0,
    }


def _validate_sources(
    context: Mapping[str, Any], *, target_edb: Path
) -> dict[str, Any]:
    root, run_dir, config_path, batch = _paths(context)
    target_edb = _contained(target_edb, root, "target AEDB")
    if target_edb.parent != run_dir:
        raise CustomerPreSolveError("target AEDB is not the current batch run copy")
    _, component = _validate_component_manifest(context, target_edb=target_edb)
    _, ports = _validate_ports_apply(context, target_edb=target_edb)
    _, planned = _validate_planned_contract(context)
    if ports["portOrder"] != planned["portOrder"]:
        raise CustomerPreSolveError("created/planned Port orders differ")
    return {
        "batchId": batch,
        "runConfig": file_evidence(config_path),
        "targetEdb": directory_evidence(target_edb),
        "records": {
            "component": component["record"],
            "portsApply": ports["record"],
            "preSolvePortContract": planned["record"],
        },
        "summary": {
            "components": {key: component[key] for key in (
                "componentCount", "modifiedComponents", "modeledArrayComponents",
                "channels", "offPathPolicy", "offPathEvidenceScope", "edbReadBack",
                "readBackCapabilityIdentity",
                "arrayBom",
                "arrayResistanceBindings",
            )},
            "ports": {key: ports[key] for key in (
                "portCount", "portOrder", "sourceEdb", "singleEndedImpedanceOhm",
                "terminalLayerPolicy", "ports", "edbReadBack",
            )},
            "plannedPortContract": {key: planned[key] for key in (
                "portCount", "portOrder", "roleMetadataCoverage", "issueCount",
            )},
        },
    }


def create_customer_pre_solve_validation(
    context: Mapping[str, Any], *, target_edb: Path
) -> Path:
    """Validate all three records, then write and re-open the combined Gate."""

    root, run_dir, _, batch = _paths(context)
    evidence = _validate_sources(context, target_edb=target_edb)
    path = run_dir / CUSTOMER_PRE_SOLVE_RECORD
    payload = {
        "schema": CUSTOMER_PRE_SOLVE_SCHEMA,
        "status": "verified",
        "validationStage": COMBINED_STAGE,
        "batchId": batch,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        **evidence,
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    validate_customer_pre_solve(context, expected_target_edb=target_edb)
    return _contained(path, root, CUSTOMER_PRE_SOLVE_RECORD)


def validate_customer_pre_solve(
    context: Mapping[str, Any],
    *,
    expected_target_edb: Path | None = None,
    started_at_ns: int | None = None,
) -> dict[str, Any]:
    """Recompute source records and require an exact, unmodified combined Gate."""

    root, run_dir, _, batch = _paths(context)
    path = run_dir / CUSTOMER_PRE_SOLVE_RECORD
    combined = _read_object(path, CUSTOMER_PRE_SOLVE_RECORD)
    combined_target = _mapping(combined.get("targetEdb"), "combined.targetEdb")
    target_value = combined_target.get("path")
    target = _resolve_path(
        target_value, base=run_dir, root=root, label="combined target AEDB"
    )
    if expected_target_edb is not None and target != expected_target_edb.resolve():
        raise CustomerPreSolveError("combined target AEDB differs from the requested target")
    current = _validate_sources(context, target_edb=target)
    if (
        combined.get("schema") != CUSTOMER_PRE_SOLVE_SCHEMA
        or combined.get("status") != "verified"
        or combined.get("validationStage") != COMBINED_STAGE
        or combined.get("batchId") != batch
    ):
        raise CustomerPreSolveError("combined customer pre-solve schema/status/batch differs")
    for field in ("runConfig", "targetEdb", "records", "summary"):
        if _canonical(combined.get(field)) != _canonical(current.get(field)):
            raise CustomerPreSolveError(f"combined customer pre-solve {field} evidence drift")
    stat = path.stat()
    newest_source = max(
        int(item["mtimeNs"]) for item in current["records"].values()
    )
    if stat.st_mtime_ns < newest_source:
        raise CustomerPreSolveError("combined customer pre-solve record is stale")
    if started_at_ns is not None:
        if stat.st_mtime_ns < started_at_ns:
            raise CustomerPreSolveError(
                "combined customer pre-solve record predates FullBatch start"
            )
        if any(
            int(item["mtimeNs"]) < started_at_ns
            for item in current["records"].values()
        ):
            raise CustomerPreSolveError(
                "customer component/Port record predates FullBatch start"
            )
    timing = _validate_combined_time(
        combined,
        combined_path=path,
        record_evidence=current["records"],
        target_evidence=current["targetEdb"],
        started_at_ns=started_at_ns,
    )
    return {
        "schema": CUSTOMER_PRE_SOLVE_SCHEMA,
        "status": "verified",
        "record": file_evidence(path),
        "batchId": batch,
        "runConfig": current["runConfig"],
        "targetEdb": current["targetEdb"],
        "records": current["records"],
        "summary": current["summary"],
        "timing": timing,
    }
