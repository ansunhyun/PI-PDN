from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any


SPEED_OF_LIGHT_MM_PER_PS = 0.299792458

_LENGTH_TO_MM = {
    "m": 1000.0,
    "meter": 1000.0,
    "meters": 1000.0,
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "um": 0.001,
    "micrometer": 0.001,
    "micrometers": 0.001,
    "mil": 0.0254,
    "mils": 0.0254,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
}

_TIME_TO_PS = {
    "s": 1.0e12,
    "sec": 1.0e12,
    "second": 1.0e12,
    "seconds": 1.0e12,
    "ns": 1000.0,
    "nanosecond": 1000.0,
    "nanoseconds": 1000.0,
    "ps": 1.0,
    "picosecond": 1.0,
    "picoseconds": 1.0,
}

_VELOCITY_TO_MM_PER_PS = {
    "mm/ps": 1.0,
    "mmperps": 1.0,
    "m/s": 1.0e-9,
    "mpersec": 1.0e-9,
    "m/second": 1.0e-9,
    "in/ns": 0.0254,
    "inch/ns": 0.0254,
}

_DIFFERENTIAL_LENGTH_POLICIES = {
    "max_positive_negative",
    "min_positive_negative",
    "average_positive_negative",
    "positive",
    "negative",
}


class TimeRangeResolutionError(ValueError):
    """Raised when a route-length time range is not safe to apply."""


def _numeric_policy() -> dict[str, str]:
    return {
        "calculation": "Python IEEE-754 binary64",
        "rounding": "none",
        "aedtSerialization": "shortest round-trip-safe decimal",
    }


def format_tdr_time_ps(raw: object) -> str:
    """Serialize a validated time without the six-digit rounding of ``:g``."""
    if isinstance(raw, bool):
        raise ValueError("boolean is not a TDR time")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"TDR time must be finite, got {raw!r}")
    token = repr(value)
    if token.endswith(".0"):
        token = token[:-2]
    return f"{token}ps"


def _evidence_scalar(raw: object) -> object:
    if isinstance(raw, float) and not math.isfinite(raw):
        return repr(raw)
    if isinstance(raw, dict):
        return {
            str(key): _evidence_scalar(value)
            for key, value in raw.items()
        }
    if isinstance(raw, (list, tuple)):
        return [_evidence_scalar(value) for value in raw]
    return raw


def _normalize_unit(unit: object) -> str:
    return (
        str(unit or "")
        .strip()
        .casefold()
        .replace(" ", "")
        .replace("μ", "u")
        .replace("µ", "u")
    )


def _add_issue(
    issues: list[dict[str, str]],
    code: str,
    path: str,
    message: str,
) -> None:
    issues.append(
        {
            "code": code,
            "path": path,
            "message": message,
        }
    )


def _finite_number(
    raw: object,
    *,
    path: str,
    issues: list[dict[str, str]],
    minimum: float | None = None,
    minimum_inclusive: bool = False,
) -> float | None:
    if raw is None:
        _add_issue(issues, "missing_value", path, "a numeric value is required")
        return None
    if isinstance(raw, bool):
        _add_issue(issues, "invalid_number", path, "boolean is not a numeric value")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        _add_issue(issues, "invalid_number", path, f"expected a finite number, got {raw!r}")
        return None
    if not math.isfinite(value):
        _add_issue(issues, "invalid_number", path, f"expected a finite number, got {raw!r}")
        return None
    if minimum is not None:
        valid = value >= minimum if minimum_inclusive else value > minimum
        if not valid:
            operator = ">=" if minimum_inclusive else ">"
            _add_issue(
                issues,
                "out_of_range",
                path,
                f"value must be {operator} {minimum:g}, got {value:g}",
            )
            return None
    return value


def _quantity(
    raw: object,
    *,
    path: str,
    unit_factors: dict[str, float],
    canonical_unit: str,
    issues: list[dict[str, str]],
    minimum: float = 0.0,
    minimum_inclusive: bool = False,
    implicit_unit: str | None = None,
) -> tuple[float, dict[str, Any]] | None:
    if isinstance(raw, dict):
        raw_value = raw.get("value")
        raw_unit = raw.get("unit")
    elif implicit_unit is not None:
        raw_value = raw
        raw_unit = implicit_unit
    else:
        _add_issue(
            issues,
            "quantity_object_required",
            path,
            f"expected {{value, unit}} quantity in {canonical_unit}",
        )
        return None

    value = _finite_number(
        raw_value,
        path=f"{path}.value" if isinstance(raw, dict) else path,
        issues=issues,
        minimum=minimum,
        minimum_inclusive=minimum_inclusive,
    )
    normalized_unit = _normalize_unit(raw_unit)
    factor = unit_factors.get(normalized_unit)
    if factor is None:
        supported = ", ".join(sorted(unit_factors))
        _add_issue(
            issues,
            "unsupported_unit",
            f"{path}.unit" if isinstance(raw, dict) else path,
            f"unsupported unit {raw_unit!r}; supported units: {supported}",
        )
        return None
    if value is None:
        return None
    canonical_value = value * factor
    if not math.isfinite(canonical_value):
        _add_issue(
            issues,
            "non_finite_converted_value",
            path,
            f"conversion to {canonical_unit} produced a non-finite value",
        )
        return None
    return canonical_value, {
        "value": value,
        "unit": str(raw_unit),
        "canonicalValue": canonical_value,
        "canonicalUnit": canonical_unit,
    }


def _fixed_resolution(tdr: dict[str, Any], *, explicit: bool) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    transient = tdr.get("transient") or {}
    view = tdr.get("view") or {}
    x_axis = view.get("xAxisPs") or {}

    stop_raw = transient.get("stopPs", 30000)
    stop_ps = _finite_number(
        stop_raw,
        path="tdr.transient.stopPs",
        issues=issues,
        minimum=0.0,
    )
    view_min_raw = x_axis.get("min")
    view_max_raw = x_axis.get("max")
    view_min_ps = (
        _finite_number(
            view_min_raw,
            path="tdr.view.xAxisPs.min",
            issues=issues,
        )
        if view_min_raw is not None
        else None
    )
    view_max_ps = (
        _finite_number(
            view_max_raw,
            path="tdr.view.xAxisPs.max",
            issues=issues,
            minimum=0.0,
        )
        if view_max_raw is not None
        else None
    )
    if (
        stop_ps is not None
        and view_max_ps is not None
        and stop_ps < view_max_ps
    ):
        _add_issue(
            issues,
            "stop_before_view_end",
            "tdr.transient.stopPs",
            "transient stop must be greater than or equal to view max",
        )

    mode = "golden_fixed" if explicit else "golden_fixed_fallback"
    return {
        "schemaVersion": 1,
        "status": "unresolved" if issues else "resolved",
        "mode": mode,
        "source": (
            "explicit_fixed_policy"
            if explicit
            else "legacy_tdr_fields_timeRangePolicy_absent"
        ),
        "policy": {
            "mode": mode,
            "routeLengthUsed": False,
        },
        "inputs": {
            "viewMinPs": _evidence_scalar(view_min_raw),
            "viewMaxPs": _evidence_scalar(view_max_raw),
            "stopPs": _evidence_scalar(stop_raw),
        },
        "numericPolicy": _numeric_policy(),
        "calculation": None,
        "manualOverride": {
            "requested": False,
            "valid": True,
            "applied": False,
            "reason": None,
            "values": {},
        },
        "precedence": [
            "existing tdr.view.xAxisPs and tdr.transient.stopPs",
            "existing runtime stop default when stopPs is absent",
        ],
        "effective": {
            "viewMinPs": view_min_ps,
            "viewMaxPs": view_max_ps,
            "stopPs": stop_ps,
        },
        "issues": issues,
    }


def _path_length_mm(
    item: dict[str, Any],
    *,
    path: str,
    issues: list[dict[str, str]],
) -> tuple[float, dict[str, Any]] | None:
    evidence = item.get("routing_length_evidence") or item.get("routingLengthEvidence")
    if isinstance(evidence, dict) and str(evidence.get("status") or "") == "unresolved":
        _add_issue(
            issues,
            "routing_length_measurement_unresolved",
            path,
            "Channel Path routing-length measurement is unresolved",
        )
        return None

    if "routing_length" in item:
        raw = item.get("routing_length")
        return _quantity(
            raw,
            path=f"{path}.routing_length",
            unit_factors=_LENGTH_TO_MM,
            canonical_unit="mm",
            issues=issues,
        )
    if "routingLength" in item:
        raw = item.get("routingLength")
        return _quantity(
            raw,
            path=f"{path}.routingLength",
            unit_factors=_LENGTH_TO_MM,
            canonical_unit="mm",
            issues=issues,
        )
    for key in ("routing_length_mm", "routingLengthMm"):
        if key in item:
            return _quantity(
                item.get(key),
                path=f"{path}.{key}",
                unit_factors=_LENGTH_TO_MM,
                canonical_unit="mm",
                issues=issues,
                implicit_unit="mm",
            )

    _add_issue(
        issues,
        "missing_routing_length",
        path,
        "resolved Channel Path does not contain routing_length {value, unit}",
    )
    return None


def _select_differential_length(
    positive_mm: float,
    negative_mm: float,
    policy: str,
) -> tuple[float, str]:
    if policy == "max_positive_negative":
        if positive_mm >= negative_mm:
            return positive_mm, "positive"
        return negative_mm, "negative"
    if policy == "min_positive_negative":
        if positive_mm <= negative_mm:
            return positive_mm, "positive"
        return negative_mm, "negative"
    if policy == "average_positive_negative":
        return (positive_mm + negative_mm) / 2.0, "average"
    if policy == "positive":
        return positive_mm, "positive"
    return negative_mm, "negative"


def _channel_length_records(
    tdr: dict[str, Any],
    path_report: dict[str, Any] | None,
    *,
    differential_policy: str,
    issues: list[dict[str, str]],
) -> list[dict[str, Any]]:
    channels = tdr.get("channels") or []
    channel_names = [
        str(channel.get("name") or "").strip()
        for channel in channels
        if isinstance(channel, dict)
    ]
    channel_names = list(dict.fromkeys(name for name in channel_names if name))
    if not channel_names:
        _add_issue(
            issues,
            "missing_tdr_channels",
            "tdr.channels",
            "route_length mode requires at least one named TDR channel",
        )
        return []
    if not isinstance(path_report, dict):
        _add_issue(
            issues,
            "missing_channel_path_report",
            "channelPath.report",
            "route_length mode requires a resolved Channel Path report",
        )
        return []

    resolved_by_key: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(path_report.get("paths") or []):
        if not isinstance(item, dict):
            continue
        if not str(item.get("status") or "").startswith("resolved"):
            continue
        key = (str(item.get("channel") or ""), str(item.get("polarity") or ""))
        resolved_by_key.setdefault(key, []).append((index, item))

    records: list[dict[str, Any]] = []
    for channel_name in channel_names:
        lengths: dict[str, tuple[float, dict[str, Any]]] = {}
        source_paths: dict[str, int] = {}
        for polarity in ("positive", "negative"):
            matches = resolved_by_key.get((channel_name, polarity), [])
            if len(matches) != 1:
                _add_issue(
                    issues,
                    "resolved_path_cardinality",
                    f"channelPath.paths[{channel_name}.{polarity}]",
                    f"expected exactly one resolved path, found {len(matches)}",
                )
                continue
            index, item = matches[0]
            result = _path_length_mm(
                item,
                path=f"channelPath.paths[{index}]",
                issues=issues,
            )
            if result is not None:
                lengths[polarity] = result
                source_paths[polarity] = index

        if set(lengths) != {"positive", "negative"}:
            continue
        positive_mm = lengths["positive"][0]
        negative_mm = lengths["negative"][0]
        selected_mm, selected_from = _select_differential_length(
            positive_mm,
            negative_mm,
            differential_policy,
        )
        if not math.isfinite(selected_mm):
            _add_issue(
                issues,
                "non_finite_derived_length",
                f"channelPath.paths[{channel_name}]",
                "the selected P/N routing length is non-finite",
            )
            continue
        records.append(
            {
                "channel": channel_name,
                "positiveLengthMm": positive_mm,
                "negativeLengthMm": negative_mm,
                "differentialLengthPolicy": differential_policy,
                "selectedLengthMm": selected_mm,
                "selectedFrom": selected_from,
                "sourcePathIndexes": source_paths,
                "sourceQuantities": {
                    "positive": lengths["positive"][1],
                    "negative": lengths["negative"][1],
                },
            }
        )
    return records


def _policy_quantity(
    policy: dict[str, Any],
    *,
    object_key: str,
    scalar_key: str,
    unit_factors: dict[str, float],
    canonical_unit: str,
    issues: list[dict[str, str]],
    minimum_inclusive: bool = False,
) -> tuple[float, dict[str, Any]] | None:
    if policy.get(object_key) is not None:
        return _quantity(
            policy.get(object_key),
            path=f"tdr.timeRangePolicy.{object_key}",
            unit_factors=unit_factors,
            canonical_unit=canonical_unit,
            issues=issues,
            minimum_inclusive=minimum_inclusive,
        )
    if policy.get(scalar_key) is not None:
        implicit_unit = canonical_unit
        return _quantity(
            policy.get(scalar_key),
            path=f"tdr.timeRangePolicy.{scalar_key}",
            unit_factors=unit_factors,
            canonical_unit=canonical_unit,
            issues=issues,
            minimum_inclusive=minimum_inclusive,
            implicit_unit=implicit_unit,
        )
    _add_issue(
        issues,
        "missing_policy_value",
        f"tdr.timeRangePolicy.{object_key}",
        f"{object_key} is required in route_length mode",
    )
    return None


def _propagation_velocity(
    policy: dict[str, Any],
    *,
    issues: list[dict[str, str]],
) -> tuple[float, dict[str, Any]] | None:
    propagation = policy.get("propagation")
    if propagation is None:
        propagation = {}
    if not isinstance(propagation, dict):
        _add_issue(
            issues,
            "invalid_object",
            "tdr.timeRangePolicy.propagation",
            "propagation must be an object",
        )
        propagation = {}

    velocity_raw = policy.get("propagationVelocity")
    velocity_path = "tdr.timeRangePolicy.propagationVelocity"
    if velocity_raw is None:
        velocity_raw = propagation.get("velocity")
        velocity_path = "tdr.timeRangePolicy.propagation.velocity"
    velocity_legacy = policy.get("propagationVelocityMmPerPs")
    if velocity_raw is None and velocity_legacy is not None:
        velocity_raw = velocity_legacy
        velocity_path = "tdr.timeRangePolicy.propagationVelocityMmPerPs"

    effective_raw = policy.get("effectiveDielectricConstant")
    effective_path = "tdr.timeRangePolicy.effectiveDielectricConstant"
    if effective_raw is None:
        effective_raw = propagation.get("effectiveDielectricConstant")
        effective_path = "tdr.timeRangePolicy.propagation.effectiveDielectricConstant"

    if velocity_raw is not None and effective_raw is not None:
        _add_issue(
            issues,
            "ambiguous_propagation_model",
            "tdr.timeRangePolicy.propagation",
            "provide exactly one of propagation velocity or effective dielectric constant",
        )
        return None
    if velocity_raw is None and effective_raw is None:
        _add_issue(
            issues,
            "missing_propagation_model",
            "tdr.timeRangePolicy.propagation",
            "provide propagation velocity or effective dielectric constant; no generic default is used",
        )
        return None

    if velocity_raw is not None:
        if velocity_path.endswith("MmPerPs"):
            result = _quantity(
                velocity_raw,
                path=velocity_path,
                unit_factors=_VELOCITY_TO_MM_PER_PS,
                canonical_unit="mm/ps",
                issues=issues,
                implicit_unit="mm/ps",
            )
        else:
            result = _quantity(
                velocity_raw,
                path=velocity_path,
                unit_factors=_VELOCITY_TO_MM_PER_PS,
                canonical_unit="mm/ps",
                issues=issues,
            )
        if result is None:
            return None
        return result[0], {
            "model": "explicit_velocity",
            "input": result[1],
            "velocityMmPerPs": result[0],
        }

    effective = _finite_number(
        effective_raw,
        path=effective_path,
        issues=issues,
        minimum=1.0,
        minimum_inclusive=True,
    )
    if effective is None:
        return None
    velocity = SPEED_OF_LIGHT_MM_PER_PS / math.sqrt(effective)
    return velocity, {
        "model": "effective_dielectric_constant",
        "effectiveDielectricConstant": effective,
        "vacuumSpeedMmPerPs": SPEED_OF_LIGHT_MM_PER_PS,
        "velocityMmPerPs": velocity,
        "formula": "velocityMmPerPs = vacuumSpeedMmPerPs / sqrt(effectiveDielectricConstant)",
    }


def _manual_override(
    policy: dict[str, Any],
    *,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    issue_count_before = len(issues)
    manual = policy.get("manualOverride") or {}
    if not isinstance(manual, dict):
        _add_issue(
            issues,
            "invalid_object",
            "tdr.timeRangePolicy.manualOverride",
            "manualOverride must be an object",
        )
        return {
            "requested": True,
            "valid": False,
            "applied": False,
            "reason": None,
            "values": {},
        }

    view_raw = manual.get("viewMax")
    view_implicit = False
    if view_raw is None and manual.get("viewMaxPs") is not None:
        view_raw = manual.get("viewMaxPs")
        view_implicit = True
    stop_raw = manual.get("transientStop")
    stop_implicit = False
    if stop_raw is None and manual.get("stopPs") is not None:
        stop_raw = manual.get("stopPs")
        stop_implicit = True
    present = view_raw is not None or stop_raw is not None
    reason = str(manual.get("reason") or "").strip()
    values: dict[str, float] = {}
    input_values: dict[str, Any] = {}

    if not present:
        return {
            "requested": False,
            "valid": True,
            "applied": False,
            "reason": reason or None,
            "values": values,
            "inputs": input_values,
        }
    if not bool(policy.get("manualOverrideAllowed", False)):
        _add_issue(
            issues,
            "manual_override_not_allowed",
            "tdr.timeRangePolicy.manualOverride",
            "manual override values were supplied but manualOverrideAllowed is not true",
        )
    if not reason:
        _add_issue(
            issues,
            "manual_override_reason_required",
            "tdr.timeRangePolicy.manualOverride.reason",
            "an explicit non-empty reason is required for a manual override",
        )

    if view_raw is not None:
        result = _quantity(
            view_raw,
            path="tdr.timeRangePolicy.manualOverride.viewMax",
            unit_factors=_TIME_TO_PS,
            canonical_unit="ps",
            issues=issues,
            implicit_unit="ps" if view_implicit else None,
        )
        if result is not None:
            values["viewMaxPs"] = result[0]
            input_values["viewMax"] = result[1]
    if stop_raw is not None:
        result = _quantity(
            stop_raw,
            path="tdr.timeRangePolicy.manualOverride.transientStop",
            unit_factors=_TIME_TO_PS,
            canonical_unit="ps",
            issues=issues,
            implicit_unit="ps" if stop_implicit else None,
        )
        if result is not None:
            values["stopPs"] = result[0]
            input_values["transientStop"] = result[1]
    return {
        "requested": True,
        "valid": len(issues) == issue_count_before and bool(values),
        "applied": False,
        "reason": reason or None,
        "values": values,
        "inputs": input_values,
    }


def _explicit_transient_stop(
    tdr: dict[str, Any],
    *,
    issues: list[dict[str, str]],
) -> tuple[float, dict[str, Any]] | None:
    transient = tdr.get("transient") or {}
    if "stopPs" not in transient:
        _add_issue(
            issues,
            "missing_analysis_stop",
            "tdr.transient.stopPs",
            (
                "report_view_only observation policy requires an explicit "
                "transient solve stop; no runtime default is used"
            ),
        )
        return None
    return _quantity(
        transient.get("stopPs"),
        path="tdr.transient.stopPs",
        unit_factors=_TIME_TO_PS,
        canonical_unit="ps",
        issues=issues,
        implicit_unit="ps",
    )


def _resolve_observation_window(
    policy: dict[str, Any],
    tdr: dict[str, Any],
    channel_lengths: list[dict[str, Any]],
    *,
    round_trip_factor: float | None,
    velocity_mm_per_ps: float | None,
    issues: list[dict[str, str]],
) -> dict[str, Any] | None:
    raw = policy.get("observationWindow")
    if raw is None:
        return None
    issue_count_before = len(issues)
    if not isinstance(raw, dict):
        _add_issue(
            issues,
            "invalid_object",
            "tdr.timeRangePolicy.observationWindow",
            "observationWindow must be an object",
        )
        return {
            "status": "unresolved",
            "scope": None,
            "channels": [],
            "reportGroups": [],
            "commonViewMaxPs": None,
        }

    scope = str(raw.get("scope") or "")
    if scope != "report_view_only":
        _add_issue(
            issues,
            "unsupported_observation_scope",
            "tdr.timeRangePolicy.observationWindow.scope",
            "observationWindow.scope must be 'report_view_only'",
        )

    reflection_round_trip_count = _finite_number(
        raw.get("reflectionRoundTripCount"),
        path=(
            "tdr.timeRangePolicy.observationWindow."
            "reflectionRoundTripCount"
        ),
        issues=issues,
        minimum=0.0,
    )
    rise_time_guard_multiplier = _finite_number(
        raw.get("riseTimeGuardMultiplier"),
        path=(
            "tdr.timeRangePolicy.observationWindow."
            "riseTimeGuardMultiplier"
        ),
        issues=issues,
        minimum=0.0,
        minimum_inclusive=True,
    )
    rise_time_result = _quantity(
        raw.get("riseTime"),
        path="tdr.timeRangePolicy.observationWindow.riseTime",
        unit_factors=_TIME_TO_PS,
        canonical_unit="ps",
        issues=issues,
    )

    quantization = raw.get("quantization")
    if not isinstance(quantization, dict):
        _add_issue(
            issues,
            "invalid_object",
            "tdr.timeRangePolicy.observationWindow.quantization",
            "quantization must be an object",
        )
        quantization = {}
    quantization_mode = str(quantization.get("mode") or "")
    if quantization_mode != "ceil":
        _add_issue(
            issues,
            "unsupported_quantization_mode",
            "tdr.timeRangePolicy.observationWindow.quantization.mode",
            "quantization.mode must be 'ceil'",
        )
    quantization_step_result = _quantity(
        quantization.get("step"),
        path="tdr.timeRangePolicy.observationWindow.quantization.step",
        unit_factors=_TIME_TO_PS,
        canonical_unit="ps",
        issues=issues,
    )

    report_group_aggregation = str(
        raw.get("reportGroupAggregationPolicy") or ""
    )
    if report_group_aggregation != "max_channel_view":
        _add_issue(
            issues,
            "unsupported_report_group_aggregation",
            (
                "tdr.timeRangePolicy.observationWindow."
                "reportGroupAggregationPolicy"
            ),
            (
                "reportGroupAggregationPolicy must be "
                "'max_channel_view'"
            ),
        )
    common_grouped_report_policy = str(
        raw.get("commonGroupedReportPolicy") or ""
    )
    if common_grouped_report_policy not in {
        "max_report_group_view",
        "per_report_group_view",
    }:
        _add_issue(
            issues,
            "unsupported_common_grouped_report_policy",
            (
                "tdr.timeRangePolicy.observationWindow."
                "commonGroupedReportPolicy"
            ),
            (
                "commonGroupedReportPolicy must be "
                "'max_report_group_view' or 'per_report_group_view'"
            ),
        )

    provenance = {
        "implementationStatus": raw.get("implementationStatus"),
        "evaluationStatus": raw.get("evaluationStatus"),
        "customerApproval": raw.get("customerApproval"),
    }
    observation = {
        "status": "unresolved",
        "scope": scope or None,
        "provenance": _evidence_scalar(provenance),
        "reflectionOrder": {
            "reflectionRoundTripCount": reflection_round_trip_count,
            "formulaTerm": (
                "reflectionRoundTripCount * firstRoundTripPs"
            ),
        },
        "riseTimeGuard": {
            "multiplier": rise_time_guard_multiplier,
            "riseTime": rise_time_result[1] if rise_time_result else None,
            "guardPs": None,
            "formulaTerm": "riseTimeGuardMultiplier * riseTimePs",
        },
        "presentation": {
            "quantization": {
                "mode": quantization_mode or None,
                "step": (
                    quantization_step_result[1]
                    if quantization_step_result
                    else None
                ),
                "formula": (
                    "ceil(rawObservationPs / quantizationStepPs) "
                    "* quantizationStepPs"
                ),
            },
            "reportGroupAggregationPolicy": (
                report_group_aggregation or None
            ),
            "commonGroupedReportPolicy": (
                common_grouped_report_policy or None
            ),
        },
        "channels": [],
        "reportGroups": [],
        "commonViewMaxPs": None,
    }

    if (
        len(issues) != issue_count_before
        or round_trip_factor is None
        or velocity_mm_per_ps is None
        or reflection_round_trip_count is None
        or rise_time_guard_multiplier is None
        or rise_time_result is None
        or quantization_step_result is None
        or not channel_lengths
    ):
        return observation

    rise_time_ps = rise_time_result[0]
    rise_time_guard_ps = rise_time_guard_multiplier * rise_time_ps
    quantization_step_ps = quantization_step_result[0]
    if not math.isfinite(rise_time_guard_ps):
        _add_issue(
            issues,
            "non_finite_derived_time",
            (
                "tdr.timeRangeResolution.calculation.observation."
                "riseTimeGuard.guardPs"
            ),
            "rise-time guard calculation produced a non-finite value",
        )
        return observation
    observation["riseTimeGuard"]["guardPs"] = rise_time_guard_ps

    channel_windows: list[dict[str, Any]] = []
    for channel in channel_lengths:
        selected_length_mm = float(channel["selectedLengthMm"])
        first_round_trip_ps = (
            round_trip_factor * selected_length_mm / velocity_mm_per_ps
        )
        raw_observation_ps = (
            reflection_round_trip_count * first_round_trip_ps
            + rise_time_guard_ps
        )
        quotient = raw_observation_ps / quantization_step_ps
        display_view_max_ps = (
            math.ceil(quotient) * quantization_step_ps
        )
        derived = (
            first_round_trip_ps,
            raw_observation_ps,
            quotient,
            display_view_max_ps,
        )
        if not all(math.isfinite(value) for value in derived):
            _add_issue(
                issues,
                "non_finite_derived_time",
                (
                    "tdr.timeRangeResolution.calculation.observation."
                    f"channels[{channel['channel']}]"
                ),
                "observation-window calculation produced a non-finite value",
            )
            continue
        channel_windows.append(
            {
                "channel": channel["channel"],
                "selectedLengthMm": selected_length_mm,
                "firstRoundTripPs": first_round_trip_ps,
                "reflectionHorizonPs": (
                    reflection_round_trip_count * first_round_trip_ps
                ),
                "riseTimeGuardPs": rise_time_guard_ps,
                "rawObservationPs": raw_observation_ps,
                "quantizationStepPs": quantization_step_ps,
                "displayViewMaxPs": display_view_max_ps,
            }
        )
    observation["channels"] = channel_windows
    if len(channel_windows) != len(channel_lengths):
        return observation

    configured_groups = tdr.get("reportGroups") or []
    if configured_groups:
        group_specs = configured_groups
    else:
        group_specs = [
            {
                "name": "all_channels",
                "channels": [
                    str(item["channel"]) for item in channel_windows
                ],
            }
        ]

    group_windows: list[dict[str, Any]] = []
    for index, group in enumerate(group_specs):
        if not isinstance(group, dict):
            _add_issue(
                issues,
                "invalid_object",
                f"tdr.reportGroups[{index}]",
                "report group must be an object",
            )
            continue
        group_name = str(group.get("name") or "").strip()
        if not group_name:
            _add_issue(
                issues,
                "missing_report_group_name",
                f"tdr.reportGroups[{index}].name",
                "report group name is required",
            )
            continue
        channel_names = {
            str(item) for item in group.get("channels") or []
        }
        channel_prefixes = [
            str(item) for item in group.get("channelPrefixes") or []
        ]
        matched = [
            item
            for item in channel_windows
            if str(item["channel"]) in channel_names
            or any(
                str(item["channel"]).startswith(prefix)
                for prefix in channel_prefixes
            )
        ]
        if not matched:
            _add_issue(
                issues,
                "empty_observation_report_group",
                f"tdr.reportGroups[{index}]",
                (
                    "report group does not match any resolved TDR "
                    "channel length"
                ),
            )
            continue
        governing = max(
            matched,
            key=lambda item: (
                float(item["displayViewMaxPs"]),
                float(item["rawObservationPs"]),
            ),
        )
        group_windows.append(
            {
                "name": group_name,
                "channels": [str(item["channel"]) for item in matched],
                "governingChannel": governing["channel"],
                "rawObservationPs": max(
                    float(item["rawObservationPs"]) for item in matched
                ),
                "displayViewMaxPs": max(
                    float(item["displayViewMaxPs"]) for item in matched
                ),
            }
        )
    observation["reportGroups"] = group_windows
    if len(issues) != issue_count_before or not group_windows:
        return observation

    governing_group = max(
        group_windows,
        key=lambda item: (
            float(item["displayViewMaxPs"]),
            float(item["rawObservationPs"]),
        ),
    )
    observation["commonViewMaxPs"] = float(
        governing_group["displayViewMaxPs"]
    )
    observation["governingReportGroup"] = governing_group["name"]
    observation["status"] = "resolved"
    return observation


def resolve_tdr_time_range(
    tdr: dict[str, Any],
    path_report: dict[str, Any] | None = None,
    *,
    path_report_source: str | None = None,
) -> dict[str, Any]:
    """Resolve effective TDR view/stop values and return reviewable JSON evidence."""
    policy = tdr.get("timeRangePolicy")
    if policy is None:
        return _fixed_resolution(tdr, explicit=False)
    if not isinstance(policy, dict):
        return {
            "schemaVersion": 1,
            "status": "unresolved",
            "mode": "invalid",
            "source": "tdr.timeRangePolicy",
            "policy": None,
            "inputs": {},
            "calculation": None,
            "manualOverride": None,
            "precedence": [],
            "effective": None,
            "issues": [
                {
                    "code": "invalid_object",
                    "path": "tdr.timeRangePolicy",
                    "message": "timeRangePolicy must be an object",
                }
            ],
        }

    mode = str(policy.get("mode") or "").strip().casefold()
    if mode in {"golden_fixed", "fixed"}:
        return _fixed_resolution(tdr, explicit=True)

    issues: list[dict[str, str]] = []
    if mode != "route_length":
        _add_issue(
            issues,
            "unsupported_mode",
            "tdr.timeRangePolicy.mode",
            f"unsupported time-range mode {policy.get('mode')!r}",
        )

    route_source = str(policy.get("routeLengthSource") or "")
    if route_source != "resolved_channel_path":
        _add_issue(
            issues,
            "unsupported_route_length_source",
            "tdr.timeRangePolicy.routeLengthSource",
            "routeLengthSource must be 'resolved_channel_path'",
        )

    differential_policy = str(policy.get("differentialLengthPolicy") or "")
    if differential_policy not in _DIFFERENTIAL_LENGTH_POLICIES:
        _add_issue(
            issues,
            "unsupported_differential_length_policy",
            "tdr.timeRangePolicy.differentialLengthPolicy",
            "select an explicit supported P/N length policy: "
            + ", ".join(sorted(_DIFFERENTIAL_LENGTH_POLICIES)),
        )

    channel_aggregation_policy = str(
        policy.get("channelAggregationPolicy") or "max_selected_channel"
    )
    if channel_aggregation_policy != "max_selected_channel":
        _add_issue(
            issues,
            "unsupported_channel_aggregation_policy",
            "tdr.timeRangePolicy.channelAggregationPolicy",
            "channelAggregationPolicy must be 'max_selected_channel'",
        )

    round_trip_factor = _finite_number(
        policy.get("roundTripFactor"),
        path="tdr.timeRangePolicy.roundTripFactor",
        issues=issues,
        minimum=0.0,
    )
    velocity_result = _propagation_velocity(policy, issues=issues)
    observation_configured = policy.get("observationWindow") is not None
    if observation_configured:
        for object_key, scalar_key in (
            ("viewMargin", "viewMarginPs"),
            ("stopMargin", "stopMarginPs"),
        ):
            if policy.get(object_key) is not None or policy.get(scalar_key) is not None:
                _add_issue(
                    issues,
                    "conflicting_legacy_margin",
                    f"tdr.timeRangePolicy.{object_key}",
                    (
                        f"{object_key} cannot be combined with "
                        "observationWindow; reflection/rise-time/display "
                        "and solve policies must remain independent"
                    ),
                )
        view_margin_result = None
        stop_margin_result = None
        analysis_stop_result = _explicit_transient_stop(tdr, issues=issues)
    else:
        view_margin_result = _policy_quantity(
            policy,
            object_key="viewMargin",
            scalar_key="viewMarginPs",
            unit_factors=_TIME_TO_PS,
            canonical_unit="ps",
            issues=issues,
            minimum_inclusive=True,
        )
        stop_margin_result = _policy_quantity(
            policy,
            object_key="stopMargin",
            scalar_key="stopMarginPs",
            unit_factors=_TIME_TO_PS,
            canonical_unit="ps",
            issues=issues,
            minimum_inclusive=True,
        )
        analysis_stop_result = None
    manual = _manual_override(policy, issues=issues)

    channel_lengths = (
        _channel_length_records(
            tdr,
            path_report,
            differential_policy=differential_policy,
            issues=issues,
        )
        if differential_policy in _DIFFERENTIAL_LENGTH_POLICIES
        else []
    )
    observation = _resolve_observation_window(
        policy,
        tdr,
        channel_lengths,
        round_trip_factor=round_trip_factor,
        velocity_mm_per_ps=(
            velocity_result[0] if velocity_result is not None else None
        ),
        issues=issues,
    )

    calculation: dict[str, Any] | None = None
    effective: dict[str, float] | None = None
    if (
        not issues
        and round_trip_factor is not None
        and velocity_result is not None
        and channel_lengths
        and (
            (
                observation_configured
                and observation is not None
                and observation.get("status") == "resolved"
                and analysis_stop_result is not None
            )
            or (
                not observation_configured
                and view_margin_result is not None
                and stop_margin_result is not None
            )
        )
    ):
        governing = max(channel_lengths, key=lambda item: float(item["selectedLengthMm"]))
        governing_length_mm = float(governing["selectedLengthMm"])
        velocity_mm_per_ps = velocity_result[0]
        round_trip_ps = (
            round_trip_factor * governing_length_mm / velocity_mm_per_ps
        )
        if observation_configured:
            view_margin_ps = None
            stop_margin_ps = None
            automatic_view_max_ps = float(
                observation["commonViewMaxPs"]
            )
            automatic_stop_ps = None
            configured_analysis_stop_ps = analysis_stop_result[0]
            formula = {
                "firstRoundTripPs": (
                    "roundTripFactor * governingRoutingLengthMm / "
                    "propagationVelocityMmPerPs"
                ),
                "rawObservationPs": (
                    "reflectionRoundTripCount * firstRoundTripPs + "
                    "riseTimeGuardMultiplier * riseTimePs"
                ),
                "viewMaxPs": (
                    "ceil(rawObservationPs / quantizationStepPs) * "
                    "quantizationStepPs; grouped reports use max group"
                ),
                "stopPs": (
                    "independent explicit tdr.transient.stopPs; "
                    "display range does not truncate solve/data"
                ),
            }
        else:
            view_margin_ps = view_margin_result[0]
            stop_margin_ps = stop_margin_result[0]
            automatic_view_max_ps = round_trip_ps + view_margin_ps
            automatic_stop_ps = automatic_view_max_ps + stop_margin_ps
            configured_analysis_stop_ps = automatic_stop_ps
            formula = {
                "roundTripPs": (
                    "roundTripFactor * governingRoutingLengthMm / "
                    "propagationVelocityMmPerPs"
                ),
                "viewMaxPs": "roundTripPs + viewMarginPs",
                "stopPs": "viewMaxPs + stopMarginPs",
            }
        derived_times = {
            "roundTripPs": round_trip_ps,
            "viewMaxPs": automatic_view_max_ps,
            "stopPs": configured_analysis_stop_ps,
        }
        for name, value in derived_times.items():
            if not math.isfinite(value):
                _add_issue(
                    issues,
                    "non_finite_derived_time",
                    f"tdr.timeRangeResolution.calculation.{name}",
                    f"{name} calculation produced a non-finite value",
                )
        calculation = {
            "formula": formula,
            "channelAggregationPolicy": channel_aggregation_policy,
            "channels": channel_lengths,
            "governingChannel": governing["channel"],
            "governingRoutingLengthMm": governing_length_mm,
            "propagation": velocity_result[1],
            "roundTripFactor": round_trip_factor,
            "roundTripPs": round_trip_ps if math.isfinite(round_trip_ps) else None,
            "viewMarginPs": view_margin_ps,
            "stopMarginPs": stop_margin_ps,
            "automatic": {
                "viewMaxPs": (
                    automatic_view_max_ps
                    if math.isfinite(automatic_view_max_ps)
                    else None
                ),
                "stopPs": (
                    automatic_stop_ps
                    if automatic_stop_ps is not None
                    and math.isfinite(automatic_stop_ps)
                    else None
                ),
            },
        }
        if observation_configured:
            calculation["physical"] = {
                "lengthDefinition": (
                    "selected resolved Channel Path centerline routing length"
                ),
                "formula": (
                    "firstRoundTripPs = roundTripFactor * "
                    "routingLengthMm / propagationVelocityMmPerPs"
                ),
                "roundTripFactor": round_trip_factor,
                "governingChannel": governing["channel"],
                "governingRoutingLengthMm": governing_length_mm,
                "firstRoundTripPs": (
                    round_trip_ps if math.isfinite(round_trip_ps) else None
                ),
                "propagation": velocity_result[1],
            }
            calculation["observation"] = observation
            calculation["analysisWindow"] = {
                "scope": "solve_and_full_data",
                "source": "tdr.transient.stopPs",
                "input": analysis_stop_result[1],
                "stopPs": configured_analysis_stop_ps,
                "independentFromDisplayView": True,
            }
        if not issues:
            effective_view_max_ps = float(
                manual["values"].get("viewMaxPs", automatic_view_max_ps)
            )
            effective_stop_ps = float(
                manual["values"].get("stopPs", configured_analysis_stop_ps)
            )
            required_stop_ps = (
                effective_view_max_ps
                if observation_configured
                else effective_view_max_ps + stop_margin_ps
            )
            if not math.isfinite(required_stop_ps):
                _add_issue(
                    issues,
                    "non_finite_derived_time",
                    "tdr.timeRangeResolution.effective.requiredStopPs",
                    "required analysis stop calculation produced a non-finite value",
                )
            elif effective_stop_ps < required_stop_ps:
                _add_issue(
                    issues,
                    (
                        "analysis_stop_before_view_end"
                        if observation_configured
                        else "stop_margin_violation"
                    ),
                    (
                        "tdr.transient.stopPs"
                        if observation_configured
                        else "tdr.timeRangePolicy.manualOverride.transientStop"
                    ),
                    (
                        "explicit transient stop must be >= report view max"
                        if observation_configured
                        else (
                            "effective transient stop must be >= "
                            "effective view max + stop margin"
                        )
                    ),
                )
            step_raw = (tdr.get("transient") or {}).get("stepPs")
            if step_raw is not None:
                step_ps = _finite_number(
                    step_raw,
                    path="tdr.transient.stepPs",
                    issues=issues,
                    minimum=0.0,
                )
                if step_ps is not None and effective_stop_ps <= step_ps:
                    _add_issue(
                        issues,
                        "stop_not_greater_than_step",
                        "tdr.transient.stopPs",
                        "effective transient stop must be greater than transient step",
                    )
        if not issues:
            existing_min = ((tdr.get("view") or {}).get("xAxisPs") or {}).get("min")
            view_min_ps = (
                0.0
                if existing_min is None
                else _finite_number(
                    existing_min,
                    path="tdr.view.xAxisPs.min",
                    issues=issues,
                )
            )
            if (
                view_min_ps is not None
                and view_min_ps >= effective_view_max_ps
            ):
                _add_issue(
                    issues,
                    "invalid_view_range",
                    "tdr.view.xAxisPs",
                    "view min must be less than effective view max",
                )
            if not issues and view_min_ps is not None:
                effective = {
                    "viewMinPs": view_min_ps,
                    "viewMaxPs": effective_view_max_ps,
                    "stopPs": effective_stop_ps,
                }
                manual["applied"] = bool(manual["values"])

    return {
        "schemaVersion": 1,
        "status": "unresolved" if issues else "resolved",
        "mode": "route_length",
        "source": "resolved_channel_path",
        "pathReportSource": path_report_source,
        "policy": {
            "routeLengthSource": route_source or None,
            "differentialLengthPolicy": differential_policy or None,
            "channelAggregationPolicy": channel_aggregation_policy,
            "manualOverrideAllowed": bool(policy.get("manualOverrideAllowed", False)),
            "noGenericPhysicalDefaults": True,
            "observationWindow": (
                {
                    "configured": True,
                    "scope": observation.get("scope") if observation else None,
                    "displayOnly": True,
                    "solveWindowIndependent": True,
                    "provenance": (
                        observation.get("provenance")
                        if observation
                        else None
                    ),
                }
                if observation_configured
                else None
            ),
        },
        "inputs": {
            "pathReportSource": path_report_source,
            "channelCount": len(tdr.get("channels") or []),
            "resolvedChannelLengthCount": len(channel_lengths),
            "viewMargin": view_margin_result[1] if view_margin_result else None,
            "stopMargin": stop_margin_result[1] if stop_margin_result else None,
            "analysisStop": (
                analysis_stop_result[1] if analysis_stop_result else None
            ),
        },
        "numericPolicy": {
            **_numeric_policy(),
            "presentationQuantization": (
                (observation or {}).get("presentation", {}).get(
                    "quantization"
                )
                if observation_configured
                else None
            ),
        },
        "calculation": calculation,
        "manualOverride": manual,
        "precedence": (
            [
                "explicit manualOverride values with non-empty reason (highest)",
                "configured report_view_only observation calculation for view",
                "explicit tdr.transient.stopPs for independent solve/data range",
                "no implicit Golden/default fallback inside route_length mode",
            ]
            if observation_configured
            else [
                "explicit manualOverride values with non-empty reason (highest)",
                "validated route-length automatic calculation",
                "no implicit Golden fallback inside route_length mode (lowest/disabled)",
            ]
        ),
        "effective": effective,
        "issues": issues,
    }


def apply_tdr_time_range_resolution(
    tdr: dict[str, Any],
    resolution: dict[str, Any],
) -> None:
    if resolution.get("status") != "resolved":
        issue_codes = [
            str(issue.get("code"))
            for issue in resolution.get("issues") or []
            if isinstance(issue, dict)
        ]
        raise TimeRangeResolutionError(
            "TDR time range is unresolved: " + ", ".join(issue_codes)
        )
    effective = resolution.get("effective") or {}
    if resolution.get("mode") != "route_length":
        tdr["timeRangeResolution"] = resolution
        return
    stop_ps = effective.get("stopPs")
    if stop_ps is not None:
        tdr.setdefault("transient", {})["stopPs"] = float(stop_ps)
    view_min_ps = effective.get("viewMinPs")
    view_max_ps = effective.get("viewMaxPs")
    if view_min_ps is not None or view_max_ps is not None:
        x_axis = tdr.setdefault("view", {}).setdefault("xAxisPs", {})
        if view_min_ps is not None:
            x_axis["min"] = float(view_min_ps)
        if view_max_ps is not None:
            x_axis["max"] = float(view_max_ps)
        observation = (
            ((resolution.get("calculation") or {}).get("observation") or {})
        )
        manual_view_override = (
            ((resolution.get("manualOverride") or {}).get("values") or {}).get(
                "viewMaxPs"
            )
            is not None
        )
        per_report_group_view = (
            ((observation.get("presentation") or {}).get(
                "commonGroupedReportPolicy"
            ))
            == "per_report_group_view"
            and not manual_view_override
        )
        report_group_view_max_ps = {
            str(item.get("name")): float(item["displayViewMaxPs"])
            for item in observation.get("reportGroups") or []
            if isinstance(item, dict)
            and item.get("name")
            and item.get("displayViewMaxPs") is not None
        }
        for group in tdr.get("reportGroups") or []:
            if not isinstance(group, dict):
                continue
            group_x_axis = group.setdefault("view", {}).setdefault("xAxisPs", {})
            if view_min_ps is not None:
                group_x_axis["min"] = float(view_min_ps)
            if view_max_ps is not None:
                group_name = str(group.get("name") or "")
                group_x_axis["max"] = (
                    report_group_view_max_ps[group_name]
                    if per_report_group_view
                    and group_name in report_group_view_max_ps
                    else float(view_max_ps)
                )
    tdr["timeRangeResolution"] = resolution


def _channel_path_report_reference(config: dict[str, Any]) -> str | None:
    policy = (config.get("tdr") or {}).get("timeRangePolicy") or {}
    if isinstance(policy, dict) and policy.get("channelPathReport"):
        return str(policy["channelPathReport"])
    channel_path = config.get("channelPath") or {}
    if isinstance(channel_path, dict) and channel_path.get("report"):
        return str(channel_path["report"])
    series_models = config.get("seriesModels") or {}
    if isinstance(series_models, dict) and series_models.get("channelPathReport"):
        return str(series_models["channelPathReport"])
    return None


def _resolve_reference_path(
    raw_path: str,
    *,
    config_path: Path,
    project_root: Path,
) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    candidates = [
        project_root / path,
        config_path.parent / path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def resolve_run_config_time_range(
    config: dict[str, Any],
    *,
    config_path: Path,
    project_root: Path,
) -> dict[str, Any]:
    tdr = config.get("tdr")
    if not isinstance(tdr, dict):
        raise TimeRangeResolutionError("config.tdr must be an object")

    policy = tdr.get("timeRangePolicy")
    mode = (
        str(policy.get("mode") or "").strip().casefold()
        if isinstance(policy, dict)
        else ""
    )
    path_report = None
    path_source = None
    load_issue: dict[str, str] | None = None
    if mode == "route_length":
        raw_path = _channel_path_report_reference(config)
        if raw_path:
            resolved_path = _resolve_reference_path(
                raw_path,
                config_path=config_path,
                project_root=project_root,
            )
            path_source = str(resolved_path.resolve())
            try:
                with resolved_path.open("r", encoding="utf-8") as fp:
                    payload = json.load(fp)
                if not isinstance(payload, dict):
                    raise ValueError("JSON root must be an object")
                path_report = payload
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                load_issue = {
                    "code": "channel_path_report_load_failed",
                    "path": "channelPath.report",
                    "message": f"{type(exc).__name__}: {exc}",
                }

    resolution = resolve_tdr_time_range(
        tdr,
        path_report,
        path_report_source=path_source,
    )
    if load_issue is not None:
        resolution["status"] = "unresolved"
        resolution.setdefault("issues", []).append(load_issue)
        resolution["effective"] = None
    if resolution.get("status") == "resolved":
        apply_tdr_time_range_resolution(tdr, resolution)
    else:
        tdr["timeRangeResolution"] = resolution
    return resolution


def write_time_range_resolution(
    path: Path,
    resolution: dict[str, Any],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(resolution, fp, indent=2, ensure_ascii=False, allow_nan=False)
        fp.write("\n")
    return path


def _primitive_kind(primitive: object) -> str:
    for name in ("primitive_type", "type"):
        try:
            value = getattr(primitive, name)
            value = value() if callable(value) else value
        except Exception:
            continue
        if value is not None:
            return str(value).casefold()
    return ""


def _positive_finite_attribute(
    owner: object,
    name: str,
) -> float | None:
    try:
        value = getattr(owner, name)
        value = value() if callable(value) else value
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) and number > 0 else None


def _arc_length_m(arc: object) -> float | None:
    length = _positive_finite_attribute(arc, "length")
    if length is not None:
        return length
    try:
        getter = getattr(arc, "GetLength")
        value = float(getter())
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _finite_mm_or_none(value_m: float | None) -> float | None:
    if value_m is None:
        return None
    value_mm = value_m * 1000.0
    return value_mm if math.isfinite(value_mm) else None


def _primitive_path_centerline_length_m(primitive: object) -> float | None:
    if "path" not in _primitive_kind(primitive):
        return None

    arcs: object | None = None
    try:
        getter = getattr(primitive, "get_center_line_polygon_data")
        center_line = getter()
        arcs = getattr(center_line, "arcs")
        arcs = arcs() if callable(arcs) else arcs
    except Exception:
        arcs = None

    if arcs is None:
        try:
            edb_object = getattr(primitive, "_edb_object")
            center_line = edb_object.GetCenterLine()
            arcs = list(center_line.GetArcData())
        except Exception:
            arcs = None

    if arcs is None:
        try:
            edb_object = getattr(primitive, "_edb_object")
            center_line = edb_object.cast().center_line
            arcs = list(center_line.arc_data)
        except Exception:
            arcs = None

    total = 0.0
    count = 0
    for arc in arcs or []:
        value = _arc_length_m(arc)
        if value is None:
            continue
        total += value
        count += 1
    return total if count and math.isfinite(total) and total > 0 else None


def _edb_net(edb: object, net_name: str) -> object:
    candidates: list[object] = []
    for owner_name in ("nets", "_nets"):
        try:
            owner = getattr(edb, owner_name)
        except Exception:
            continue
        candidates.append(owner)
        try:
            nested = getattr(owner, "nets")
        except Exception:
            nested = None
        if nested is not None:
            candidates.append(nested)
    for candidate in candidates:
        try:
            return candidate[net_name]  # type: ignore[index]
        except Exception:
            continue
    raise LookupError(f"net not found: {net_name}")


def annotate_resolved_path_routing_length(edb: object, path: Any) -> Any:
    """Attach path-net trace length and evidence to a resolved PolarityPath."""
    if not str(getattr(path, "status", "") or "").startswith("resolved"):
        return path

    unique_nets = list(
        dict.fromkeys(
            str(getattr(step, "net"))
            for step in getattr(path, "steps", []) or []
            if getattr(step, "net", None)
        )
    )
    issues: list[dict[str, str]] = []
    net_records: list[dict[str, Any]] = []
    total_m = 0.0
    for net_name in unique_nets:
        try:
            net = _edb_net(edb, net_name)
            primitives = getattr(net, "primitives")
            primitives = primitives() if callable(primitives) else primitives
        except Exception as exc:
            _add_issue(
                issues,
                "net_primitive_lookup_failed",
                f"net[{net_name}]",
                f"{type(exc).__name__}: {exc}",
            )
            continue

        centerline_lengths_m: list[float] = []
        reported_lengths_m: list[float] = []
        widths_m: list[float] = []
        path_primitive_count = 0
        unreadable_path_count = 0
        for primitive in primitives or []:
            if "path" not in _primitive_kind(primitive):
                continue
            path_primitive_count += 1
            centerline_length_m = _primitive_path_centerline_length_m(primitive)
            if centerline_length_m is None:
                unreadable_path_count += 1
            else:
                centerline_lengths_m.append(centerline_length_m)
            reported_length_m = _positive_finite_attribute(primitive, "length")
            if reported_length_m is not None:
                reported_lengths_m.append(reported_length_m)
            width_m = _positive_finite_attribute(primitive, "width")
            if width_m is not None:
                widths_m.append(width_m)
        net_total_m = sum(centerline_lengths_m)
        net_length_mm = net_total_m * 1000.0
        reported_total_m = (
            sum(reported_lengths_m)
            if len(reported_lengths_m) == path_primitive_count
            else None
        )
        end_cap_extension_m = (
            reported_total_m - net_total_m
            if reported_total_m is not None
            and len(centerline_lengths_m) == path_primitive_count
            else None
        )
        if not math.isfinite(net_total_m) or not math.isfinite(net_length_mm):
            _add_issue(
                issues,
                "non_finite_routing_length",
                f"net[{net_name}]",
                "Path primitive length sum or conversion to mm is non-finite",
            )
            net_length_mm = None
        net_records.append(
            {
                "net": net_name,
                "pathPrimitiveCount": path_primitive_count,
                "measuredPathPrimitiveCount": len(centerline_lengths_m),
                "unreadablePathPrimitiveCount": unreadable_path_count,
                "lengthMm": net_length_mm,
                "centerlineLengthMm": net_length_mm,
                "reportedPathLengthMm": _finite_mm_or_none(reported_total_m),
                "excludedEndCapExtensionMm": _finite_mm_or_none(
                    end_cap_extension_m
                ),
                "pathWidthSumMm": _finite_mm_or_none(
                    sum(widths_m)
                    if len(widths_m) == path_primitive_count
                    else None
                ),
            }
        )
        if path_primitive_count == 0:
            _add_issue(
                issues,
                "no_path_primitives",
                f"net[{net_name}]",
                "resolved path net contains no measurable Path primitives",
            )
        if unreadable_path_count:
            _add_issue(
                issues,
                "unreadable_path_primitive_length",
                f"net[{net_name}]",
                f"{unreadable_path_count} Path primitive centerline length(s) could not be read",
            )
        if math.isfinite(net_total_m):
            total_m += net_total_m
            if not math.isfinite(total_m):
                _add_issue(
                    issues,
                    "non_finite_routing_length",
                    "path.routing_length",
                    "routing-length accumulation is non-finite",
                )

    if not unique_nets:
        _add_issue(
            issues,
            "missing_path_nets",
            "path.steps",
            "resolved Channel Path contains no step nets",
        )
    total_mm = total_m * 1000.0
    if not math.isfinite(total_mm):
        _add_issue(
            issues,
            "non_finite_routing_length",
            "path.routing_length",
            "routing-length conversion to mm is non-finite",
        )
    if total_m <= 0:
        _add_issue(
            issues,
            "non_positive_routing_length",
            "path.routing_length",
            "measured routing length must be greater than zero",
        )

    evidence = {
        "status": "unresolved" if issues else "resolved",
        "method": "sum_path_centerline_arcs_on_unique_resolved_path_nets",
        "lengthDefinition": (
            "EDB Path centerline arc length; PyEDB Path.length end-cap "
            "extensions are excluded"
        ),
        "sourceUnit": "m",
        "outputUnit": "mm",
        "nets": net_records,
        "excludedGeometry": [
            "Path end-cap extension beyond the centerline",
            "non-Path copper polygons",
            "padstack vertical barrel length",
            "component/package internal length",
        ],
        "issues": issues,
    }
    routing_length = (
        None
        if issues
        else {
            "value": total_mm,
            "unit": "mm",
        }
    )
    return replace(
        path,
        routing_length=routing_length,
        routing_length_evidence=evidence,
    )
