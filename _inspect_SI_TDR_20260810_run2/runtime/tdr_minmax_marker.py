from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "si-tdr-minmax-marker/v1"
SPEED_OF_LIGHT_MM_PER_PS = 0.299792458
TIE_BREAK_POLICY = "earliest_time_then_source_index"

_TIME_TO_PS = {
    "fs": 0.001,
    "ps": 1.0,
    "ns": 1_000.0,
    "us": 1_000_000.0,
    "ms": 1_000_000_000.0,
    "s": 1_000_000_000_000.0,
}
_DISTANCE_TO_MM = {
    "um": 0.001,
    "µm": 0.001,
    "mm": 1.0,
    "cm": 10.0,
    "m": 1_000.0,
}
_VELOCITY_TO_MM_PER_PS = {
    "mm/ps": 1.0,
    "m/s": 1.0e-9,
}
_OHM_UNITS = {"ohm", "ohms", "ω", "Ω"}
_HEADER_WITH_UNIT = re.compile(r"^\s*(.*?)\s*\[\s*([^\]]+)\s*\]\s*$")


class MarkerConfigurationError(ValueError):
    """Raised when a marker window cannot be interpreted without guessing."""


class TdrCsvError(ValueError):
    """Raised when a TDR CSV schema or unit is invalid."""


def _clean_unit(value: object) -> str:
    return str(value or "").strip().casefold().replace("μ", "µ")


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise MarkerConfigurationError(f"{field} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MarkerConfigurationError(f"{field} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise MarkerConfigurationError(f"{field} must be a finite number")
    return parsed


def _quantity(
    raw: object,
    *,
    field: str,
    expected_dimension: str | None = None,
) -> tuple[float, str, str]:
    if not isinstance(raw, Mapping):
        raise MarkerConfigurationError(f"{field} must be an object with value and unit")
    value = _finite_float(raw.get("value"), f"{field}.value")
    unit = _clean_unit(raw.get("unit"))
    if unit in _TIME_TO_PS:
        dimension = "time"
    elif unit in _DISTANCE_TO_MM:
        dimension = "distance"
    else:
        supported = ", ".join(sorted([*_TIME_TO_PS, *_DISTANCE_TO_MM]))
        raise MarkerConfigurationError(
            f"{field}.unit={raw.get('unit')!r} is unsupported; supported units: {supported}"
        )
    if expected_dimension is not None and dimension != expected_dimension:
        raise MarkerConfigurationError(
            f"{field} must use a {expected_dimension} unit, got {raw.get('unit')!r}"
        )
    return value, unit, dimension


def _distance_model(
    raw: object,
    *,
    required: bool,
) -> dict[str, Any] | None:
    if raw is None:
        if required:
            raise MarkerConfigurationError(
                "distanceModel is required for distance windows or exclusions"
            )
        return None
    if not isinstance(raw, Mapping):
        raise MarkerConfigurationError("distanceModel must be an object")

    velocity_object = raw.get("propagationVelocity")
    velocity_scalar = raw.get("propagationVelocityMmPerPs")
    effective_er = raw.get("effectiveEr")
    sources = [
        velocity_object is not None,
        velocity_scalar is not None,
        effective_er is not None,
    ]
    if sum(sources) != 1:
        raise MarkerConfigurationError(
            "distanceModel must define exactly one of propagationVelocity, "
            "propagationVelocityMmPerPs, or effectiveEr"
        )

    source: str
    er_value: float | None = None
    if velocity_object is not None:
        if not isinstance(velocity_object, Mapping):
            raise MarkerConfigurationError(
                "distanceModel.propagationVelocity must be an object with value and unit"
            )
        velocity_value = _finite_float(
            velocity_object.get("value"),
            "distanceModel.propagationVelocity.value",
        )
        velocity_unit = _clean_unit(velocity_object.get("unit"))
        if velocity_unit not in _VELOCITY_TO_MM_PER_PS:
            supported = ", ".join(sorted(_VELOCITY_TO_MM_PER_PS))
            raise MarkerConfigurationError(
                "distanceModel.propagationVelocity.unit="
                f"{velocity_object.get('unit')!r} is unsupported; supported units: {supported}"
            )
        velocity_mm_per_ps = velocity_value * _VELOCITY_TO_MM_PER_PS[velocity_unit]
        source = "propagation_velocity"
    elif velocity_scalar is not None:
        velocity_mm_per_ps = _finite_float(
            velocity_scalar,
            "distanceModel.propagationVelocityMmPerPs",
        )
        source = "propagation_velocity_mm_per_ps"
    else:
        er_value = _finite_float(effective_er, "distanceModel.effectiveEr")
        if er_value <= 0:
            raise MarkerConfigurationError("distanceModel.effectiveEr must be greater than zero")
        velocity_mm_per_ps = SPEED_OF_LIGHT_MM_PER_PS / math.sqrt(er_value)
        source = "effective_er"

    if velocity_mm_per_ps <= 0:
        raise MarkerConfigurationError("distanceModel propagation velocity must be greater than zero")

    if "roundTripFactor" not in raw:
        raise MarkerConfigurationError(
            "distanceModel.roundTripFactor must be explicitly configured"
        )
    round_trip_factor = _finite_float(
        raw.get("roundTripFactor"),
        "distanceModel.roundTripFactor",
    )
    if round_trip_factor <= 0:
        raise MarkerConfigurationError("distanceModel.roundTripFactor must be greater than zero")

    if "timeOrigin" not in raw:
        raise MarkerConfigurationError(
            "distanceModel.timeOrigin must be explicitly configured"
        )
    time_origin_raw = raw.get("timeOrigin")
    origin_value, origin_unit, _ = _quantity(
        time_origin_raw,
        field="distanceModel.timeOrigin",
        expected_dimension="time",
    )
    time_origin_ps = origin_value * _TIME_TO_PS[origin_unit]
    return {
        "source": source,
        "propagationVelocityMmPerPs": velocity_mm_per_ps,
        "effectiveEr": er_value,
        "roundTripFactor": round_trip_factor,
        "timeOriginPs": time_origin_ps,
    }


def _distance_mm_to_time_ps(distance_mm: float, model: Mapping[str, Any]) -> float:
    return float(model["timeOriginPs"]) + (
        distance_mm
        * float(model["roundTripFactor"])
        / float(model["propagationVelocityMmPerPs"])
    )


def _distance_width_mm_to_time_ps(distance_mm: float, model: Mapping[str, Any]) -> float:
    return (
        distance_mm
        * float(model["roundTripFactor"])
        / float(model["propagationVelocityMmPerPs"])
    )


def _time_ps_to_distance_mm(time_ps: float, model: Mapping[str, Any] | None) -> float | None:
    if model is None:
        return None
    return (
        (time_ps - float(model["timeOriginPs"]))
        * float(model["propagationVelocityMmPerPs"])
        / float(model["roundTripFactor"])
    )


def _config_needs_distance_model(config: Mapping[str, Any]) -> bool:
    interest = config.get("interestWindow")
    if isinstance(interest, Mapping) and str(interest.get("domain") or "").casefold() == "distance":
        return True
    exclusions = config.get("endpointExclusions") or []
    if not isinstance(exclusions, Sequence) or isinstance(exclusions, (str, bytes)):
        return False
    for exclusion in exclusions:
        if not isinstance(exclusion, Mapping):
            continue
        width = exclusion.get("width")
        if isinstance(width, Mapping) and _clean_unit(width.get("unit")) in _DISTANCE_TO_MM:
            return True
    return False


def _resolve_marker_config(config: object) -> dict[str, Any]:
    if config is None:
        return {"state": "not_configured"}
    if not isinstance(config, Mapping):
        raise MarkerConfigurationError("minMaxMarkers must be an object")
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MarkerConfigurationError("minMaxMarkers.enabled must be true or false")
    if not enabled:
        return {"state": "disabled"}

    interest = config.get("interestWindow")
    if interest is None:
        return {"state": "not_configured"}
    if not isinstance(interest, Mapping):
        raise MarkerConfigurationError("interestWindow must be an object")

    domain = str(interest.get("domain") or "").strip().casefold()
    if domain not in {"time", "distance"}:
        raise MarkerConfigurationError("interestWindow.domain must be 'time' or 'distance'")

    requires_distance = _config_needs_distance_model(config)
    model = _distance_model(config.get("distanceModel"), required=requires_distance)
    start_value, start_unit, _ = _quantity(
        interest.get("start"),
        field="interestWindow.start",
        expected_dimension=domain,
    )
    end_value, end_unit, _ = _quantity(
        interest.get("end"),
        field="interestWindow.end",
        expected_dimension=domain,
    )

    if domain == "time":
        start_ps = start_value * _TIME_TO_PS[start_unit]
        end_ps = end_value * _TIME_TO_PS[end_unit]
    else:
        start_mm = start_value * _DISTANCE_TO_MM[start_unit]
        end_mm = end_value * _DISTANCE_TO_MM[end_unit]
        if start_mm < 0 or end_mm < 0:
            raise MarkerConfigurationError("distance interestWindow boundaries cannot be negative")
        assert model is not None
        start_ps = _distance_mm_to_time_ps(start_mm, model)
        end_ps = _distance_mm_to_time_ps(end_mm, model)

    if end_ps <= start_ps:
        raise MarkerConfigurationError("interestWindow.end must be greater than interestWindow.start")

    exclusions_raw = config.get("endpointExclusions") or []
    if not isinstance(exclusions_raw, Sequence) or isinstance(exclusions_raw, (str, bytes)):
        raise MarkerConfigurationError("endpointExclusions must be a list")

    resolved_exclusions: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    near_width_ps = 0.0
    far_width_ps = 0.0
    for index, exclusion in enumerate(exclusions_raw):
        field = f"endpointExclusions[{index}]"
        if not isinstance(exclusion, Mapping):
            raise MarkerConfigurationError(f"{field} must be an object")
        endpoint = str(exclusion.get("endpoint") or "").strip().casefold()
        if endpoint not in {"near", "far"}:
            raise MarkerConfigurationError(f"{field}.endpoint must be 'near' or 'far'")
        if endpoint in seen_endpoints:
            raise MarkerConfigurationError(f"endpointExclusions contains duplicate {endpoint!r}")
        seen_endpoints.add(endpoint)
        width_value, width_unit, width_dimension = _quantity(
            exclusion.get("width"),
            field=f"{field}.width",
        )
        if width_value < 0:
            raise MarkerConfigurationError(f"{field}.width cannot be negative")
        if width_dimension == "time":
            width_ps = width_value * _TIME_TO_PS[width_unit]
            width_mm = _time_ps_to_distance_mm(
                float(model["timeOriginPs"]) + width_ps,
                model,
            ) if model is not None else None
        else:
            assert model is not None
            width_mm = width_value * _DISTANCE_TO_MM[width_unit]
            width_ps = _distance_width_mm_to_time_ps(width_mm, model)
        if endpoint == "near":
            near_width_ps = width_ps
        else:
            far_width_ps = width_ps
        resolved_exclusions.append(
            {
                "endpoint": endpoint,
                "requested": {"value": width_value, "unit": width_unit},
                "resolvedWidthPs": width_ps,
                "resolvedWidthMm": width_mm,
            }
        )

    effective_start_ps = start_ps + near_width_ps
    effective_end_ps = end_ps - far_width_ps
    if effective_end_ps <= effective_start_ps:
        raise MarkerConfigurationError(
            "endpoint exclusions consume the complete interest window"
        )

    return {
        "state": "configured",
        "boundaryPolicy": "closed",
        "domain": domain,
        "requested": {
            "start": {"value": start_value, "unit": start_unit},
            "end": {"value": end_value, "unit": end_unit},
        },
        "resolvedTimeRangePs": {"start": start_ps, "end": end_ps},
        "resolvedDistanceRangeMm": (
            {
                "start": _time_ps_to_distance_mm(start_ps, model),
                "end": _time_ps_to_distance_mm(end_ps, model),
            }
            if model is not None
            else None
        ),
        "endpointExclusions": resolved_exclusions,
        "effectiveTimeRangePs": {
            "start": effective_start_ps,
            "end": effective_end_ps,
        },
        "effectiveDistanceRangeMm": (
            {
                "start": _time_ps_to_distance_mm(effective_start_ps, model),
                "end": _time_ps_to_distance_mm(effective_end_ps, model),
            }
            if model is not None
            else None
        ),
        "distanceModel": model,
    }


def _sample_float_with_reason(value: object) -> tuple[float | None, str | None]:
    if value is None:
        return None, "missing"
    if isinstance(value, str) and not value.strip():
        return None, "blank"
    if isinstance(value, bool):
        return None, "invalid_numeric"
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, "invalid_numeric"
    if not math.isfinite(parsed):
        return None, "non_finite"
    return parsed, None


def _sample_float(value: object) -> float | None:
    parsed, _ = _sample_float_with_reason(value)
    return parsed


def _normalize_samples(samples: object) -> tuple[list[dict[str, float | int]], dict[str, Any]]:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        samples = []
    valid: list[dict[str, float | int]] = []
    invalid_reasons: Counter[str] = Counter()
    for source_index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            invalid_reasons["invalid_sample"] += 1
            continue
        raw_time = sample.get("time_ps")
        raw_value = sample.get("impedance_ohm")
        time_ps, time_reason = _sample_float_with_reason(raw_time)
        impedance_ohm, impedance_reason = _sample_float_with_reason(raw_value)
        if time_ps is None:
            invalid_reasons[f"{time_reason}_time"] += 1
            continue
        if impedance_ohm is None:
            invalid_reasons[f"{impedance_reason}_impedance"] += 1
            continue
        valid.append(
            {
                "timePs": time_ps,
                "impedanceOhm": impedance_ohm,
                "sourceIndex": source_index,
            }
        )
    valid.sort(key=lambda item: (float(item["timePs"]), int(item["sourceIndex"])))
    total_count = len(samples)
    invalid_count = total_count - len(valid)
    if total_count == 0:
        quality_status = "empty"
    elif not valid:
        quality_status = "invalid"
    elif invalid_count:
        quality_status = "partial"
    else:
        quality_status = "complete"
    return valid, {
        "status": quality_status,
        "totalSampleCount": total_count,
        "validSampleCount": len(valid),
        "invalidSampleCount": invalid_count,
        "invalidReasons": dict(sorted(invalid_reasons.items())),
    }


def finite_trace_points(samples: object) -> tuple[list[float], list[float]]:
    """Return time-sorted finite plot points using the same rules as marker analysis."""
    valid, _ = _normalize_samples(samples)
    return (
        [float(item["timePs"]) for item in valid],
        [float(item["impedanceOhm"]) for item in valid],
    )


def _waveform_record(
    valid_samples: Sequence[Mapping[str, float | int]],
    quality: Mapping[str, Any],
    model: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not valid_samples:
        return {
            "sampleCounts": dict(quality),
            "timeRangePs": None,
            "distanceRangeMm": None,
        }
    start_ps = float(valid_samples[0]["timePs"])
    end_ps = float(valid_samples[-1]["timePs"])
    return {
        "sampleCounts": dict(quality),
        "timeRangePs": {"start": start_ps, "end": end_ps},
        "distanceRangeMm": (
            {
                "start": _time_ps_to_distance_mm(start_ps, model),
                "end": _time_ps_to_distance_mm(end_ps, model),
            }
            if model is not None
            else None
        ),
    }


def _marker(
    candidates: Sequence[Mapping[str, float | int]],
    *,
    kind: str,
    model: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if kind == "min":
        extreme_value = min(float(item["impedanceOhm"]) for item in candidates)
    elif kind == "max":
        extreme_value = max(float(item["impedanceOhm"]) for item in candidates)
    else:
        raise ValueError(f"unsupported marker kind: {kind}")
    ties = [
        item
        for item in candidates
        if float(item["impedanceOhm"]) == extreme_value
    ]
    selected = min(
        ties,
        key=lambda item: (float(item["timePs"]), int(item["sourceIndex"])),
    )
    time_ps = float(selected["timePs"])
    return {
        "kind": kind,
        "impedanceOhm": extreme_value,
        "timePs": time_ps,
        "distanceMm": _time_ps_to_distance_mm(time_ps, model),
        "sourceSampleIndex": int(selected["sourceIndex"]),
        "tieCount": len(ties),
        "tieBreakPolicy": TIE_BREAK_POLICY,
    }


def _state_channel_record(
    *,
    trace_name: str,
    channel_name: str,
    status: str,
    reason: str,
    full_waveform: Mapping[str, Any],
    interest_window: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "channel": channel_name,
        "trace": trace_name,
        "status": status,
        "reason": reason,
        "fullWaveform": dict(full_waveform),
        "interestWindow": dict(interest_window) if interest_window is not None else None,
        "selectedSampleCount": 0,
        "minMarker": None,
        "maxMarker": None,
        "evaluation": {
            "status": "not_evaluated",
            "reason": "pass_fail_rule_out_of_scope",
        },
    }


def transient_unit_errors(
    transient: Mapping[str, Any],
    trace_names: Sequence[str],
) -> dict[str, str]:
    """Validate the declared units of normalized `time_ps`/`impedance_ohm` samples."""
    errors: dict[str, str] = {}
    time_unit = _clean_unit(transient.get("timeUnit", "ps"))
    if time_unit != "ps":
        for trace_name in trace_names:
            errors[str(trace_name)] = (
                f"tdr_transient timeUnit must be 'ps' for time_ps samples, "
                f"got {transient.get('timeUnit')!r}"
            )
        return errors

    accepted_ohm_units = {_clean_unit(item) for item in _OHM_UNITS}
    if "traceUnits" not in transient:
        declared = transient.get("traceUnit", "ohm")
        if _clean_unit(declared) not in accepted_ohm_units:
            return {
                str(trace_name): (
                    "trace unit must be impedance [ohm] for impedance_ohm samples, "
                    f"got {declared!r}"
                )
                for trace_name in trace_names
            }
        return errors

    trace_units = transient.get("traceUnits")
    if not isinstance(trace_units, Mapping):
        return {
            str(trace_name): "tdr_transient traceUnits must be an object"
            for trace_name in trace_names
        }
    for trace_name in trace_names:
        if str(trace_name) not in trace_units:
            errors[str(trace_name)] = (
                f"tdr_transient traceUnits is missing trace {str(trace_name)!r}"
            )
            continue
        declared = trace_units.get(str(trace_name))
        if _clean_unit(declared) not in accepted_ohm_units:
            errors[str(trace_name)] = (
                f"trace unit must be impedance [ohm] for impedance_ohm samples, "
                f"got {declared!r}"
            )
    return errors


def _analyze_channel(
    *,
    trace_name: str,
    channel_name: str,
    samples: object,
    marker_config: object,
    unit_error: str | None,
) -> dict[str, Any]:
    valid_samples, quality = _normalize_samples(samples)
    if unit_error is not None:
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="invalid_data_unit",
            reason=unit_error,
            full_waveform={
                "sampleCounts": dict(quality),
                "timeRangePs": None,
                "distanceRangeMm": None,
            },
            interest_window=None,
        )

    try:
        resolved = _resolve_marker_config(marker_config)
    except MarkerConfigurationError as exc:
        full_waveform = _waveform_record(valid_samples, quality, None)
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="invalid_configuration",
            reason=str(exc),
            full_waveform=full_waveform,
            interest_window=None,
        )

    model = resolved.get("distanceModel")
    full_waveform = _waveform_record(valid_samples, quality, model)
    if resolved["state"] == "disabled":
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="disabled",
            reason="min/max marker calculation is disabled",
            full_waveform=full_waveform,
            interest_window=None,
        )
    if resolved["state"] == "not_configured":
        record = _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="not_configured",
            reason="interestWindow is not configured",
            full_waveform=full_waveform,
            interest_window=None,
        )
        record["fallback"] = {
            "status": "full_waveform_only",
            "markersGenerated": False,
            "reason": "implicit full-waveform extrema are not used",
        }
        return record
    if len(valid_samples) < 2:
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="incomplete_trace",
            reason="at least two finite time/impedance samples are required",
            full_waveform=full_waveform,
            interest_window=resolved,
        )

    effective = resolved["effectiveTimeRangePs"]
    effective_start = float(effective["start"])
    effective_end = float(effective["end"])
    trace_start = float(valid_samples[0]["timePs"])
    trace_end = float(valid_samples[-1]["timePs"])
    if trace_start > effective_start or trace_end < effective_end:
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="incomplete_window_coverage",
            reason=(
                f"trace range [{trace_start}, {trace_end}] ps does not cover "
                f"effective interest range [{effective_start}, {effective_end}] ps"
            ),
            full_waveform=full_waveform,
            interest_window=resolved,
        )

    selected = [
        sample
        for sample in valid_samples
        if effective_start <= float(sample["timePs"]) <= effective_end
    ]
    if len(selected) < 2:
        return _state_channel_record(
            trace_name=trace_name,
            channel_name=channel_name,
            status="insufficient_interest_samples",
            reason="effective interest window contains fewer than two finite samples",
            full_waveform=full_waveform,
            interest_window=resolved,
        )

    status = "ok_with_warnings" if quality["invalidSampleCount"] else "ok"
    return {
        "channel": channel_name,
        "trace": trace_name,
        "status": status,
        "reason": (
            "non-finite or incomplete samples were excluded"
            if status == "ok_with_warnings"
            else None
        ),
        "fullWaveform": full_waveform,
        "interestWindow": resolved,
        "selectedSampleCount": len(selected),
        "minMarker": _marker(selected, kind="min", model=model),
        "maxMarker": _marker(selected, kind="max", model=model),
        "evaluation": {
            "status": "not_evaluated",
            "reason": "pass_fail_rule_out_of_scope",
        },
    }


def _aggregate_status(channels: Sequence[Mapping[str, Any]]) -> str:
    if not channels:
        return "no_traces"
    statuses = {str(item.get("status")) for item in channels}
    if len(statuses) == 1:
        return next(iter(statuses))
    if statuses <= {"ok", "ok_with_warnings"}:
        return "ok_with_warnings" if "ok_with_warnings" in statuses else "ok"
    return "partial"


def analyze_tdr_minmax(
    samples_by_trace: Mapping[str, object],
    marker_config: object,
    *,
    trace_to_channel: Mapping[str, str] | None = None,
    marker_config_by_channel: Mapping[str, object] | None = None,
    unit_errors: Mapping[str, str] | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze finite samples inside an explicit interest window for each trace."""
    trace_to_channel = trace_to_channel or {}
    marker_config_by_channel = marker_config_by_channel or {}
    unit_errors = unit_errors or {}
    ordered = sorted(
        (
            (
                str(trace),
                str(trace_to_channel.get(str(trace), str(trace))),
                samples,
            )
            for trace, samples in samples_by_trace.items()
        ),
        key=lambda item: (item[1].casefold(), item[0]),
    )
    channels = [
        _analyze_channel(
            trace_name=trace_name,
            channel_name=channel_name,
            samples=samples,
            marker_config=marker_config_by_channel.get(channel_name, marker_config),
            unit_error=unit_errors.get(trace_name),
        )
        for trace_name, channel_name, samples in ordered
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": _aggregate_status(channels),
        "source": dict(source or {}),
        "channelCount": len(channels),
        "markerChannelCount": sum(
            item.get("minMarker") is not None and item.get("maxMarker") is not None
            for item in channels
        ),
        "evaluation": {
            "status": "not_evaluated",
            "reason": "pass_fail_rule_out_of_scope",
        },
        "channels": channels,
    }


def extract_marker_config(payload: Mapping[str, Any]) -> object:
    """Extract minMaxMarkers from a full run config or a marker-only config."""
    if "tdr" in payload:
        tdr = payload.get("tdr") or {}
        processing = tdr.get("resultProcessing") or {}
        return processing.get("minMaxMarkers")
    if "resultProcessing" in payload:
        processing = payload.get("resultProcessing") or {}
        return processing.get("minMaxMarkers")
    if "minMaxMarkers" in payload:
        return payload.get("minMaxMarkers")
    if any(
        key in payload
        for key in ("enabled", "interestWindow", "endpointExclusions", "distanceModel")
    ):
        return payload
    return None


def channel_marker_overrides(tdr_config: Mapping[str, Any]) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for channel in tdr_config.get("channels") or []:
        if not isinstance(channel, Mapping) or not channel.get("name"):
            continue
        processing = channel.get("resultProcessing") or {}
        if isinstance(processing, Mapping) and "minMaxMarkers" in processing:
            overrides[str(channel["name"])] = processing.get("minMaxMarkers")
    return overrides


def read_tdr_csv(path: Path) -> tuple[dict[str, list[dict[str, object]]], dict[str, Any]]:
    """Read a wide TDR CSV with explicit `Time [unit]` and `Channel [ohm]` headers."""
    with path.open("r", newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        headers = reader.fieldnames or []
        if not headers:
            raise TdrCsvError(f"TDR CSV has no header: {path}")
        if len(headers) != len(set(headers)):
            raise TdrCsvError("TDR CSV contains duplicate header names")

        time_columns: list[tuple[str, str]] = []
        channel_columns: list[tuple[str, str]] = []
        ignored_columns: list[str] = []
        seen_channels: set[str] = set()
        for header in headers:
            match = _HEADER_WITH_UNIT.match(header)
            if not match:
                if str(header).strip().casefold() == "time":
                    raise TdrCsvError("time column must declare a unit, for example Time [ns]")
                ignored_columns.append(header)
                continue
            label = match.group(1).strip()
            raw_unit = match.group(2).strip()
            unit = _clean_unit(raw_unit)
            if label.casefold() == "time":
                if unit not in _TIME_TO_PS:
                    supported = ", ".join(sorted(_TIME_TO_PS))
                    raise TdrCsvError(
                        f"unsupported time unit {raw_unit!r}; supported units: {supported}"
                    )
                time_columns.append((header, unit))
                continue
            if unit not in {_clean_unit(item) for item in _OHM_UNITS}:
                raise TdrCsvError(
                    f"unsupported TDR value unit {raw_unit!r} in column {header!r}; "
                    "impedance columns must use [ohm] or [Ω]"
                )
            if not label:
                raise TdrCsvError(f"empty channel name in column {header!r}")
            folded = label.casefold()
            if folded in seen_channels:
                raise TdrCsvError(f"duplicate channel label {label!r}")
            seen_channels.add(folded)
            channel_columns.append((header, label))

        if len(time_columns) != 1:
            raise TdrCsvError(
                f"TDR CSV must contain exactly one explicit Time [unit] column; found {len(time_columns)}"
            )
        if not channel_columns:
            raise TdrCsvError("TDR CSV contains no [ohm] channel columns")

        time_header, time_unit = time_columns[0]
        samples_by_channel = {label: [] for _, label in channel_columns}
        row_count = 0
        for row_count, row in enumerate(reader, start=1):
            if None in row:
                raise TdrCsvError(
                    f"TDR CSV row {row_count + 1} contains more fields than the header"
                )
            raw_time = row.get(time_header)
            parsed_time = _sample_float(raw_time)
            time_ps: object = (
                parsed_time * _TIME_TO_PS[time_unit]
                if parsed_time is not None
                else raw_time
            )
            for header, label in channel_columns:
                samples_by_channel[label].append(
                    {
                        "time_ps": time_ps,
                        "impedance_ohm": row.get(header),
                    }
                )

    return samples_by_channel, {
        "kind": "tdr_csv",
        "path": str(path),
        "timeColumn": time_header,
        "timeUnit": time_unit,
        "channelColumns": {
            label: header
            for header, label in channel_columns
        },
        "ignoredColumns": ignored_columns,
        "rowCount": row_count,
    }


def _csv_row(channel: Mapping[str, Any]) -> dict[str, Any]:
    waveform = channel.get("fullWaveform") or {}
    counts = waveform.get("sampleCounts") or {}
    time_range = waveform.get("timeRangePs") or {}
    interest = channel.get("interestWindow") or {}
    requested_time = interest.get("resolvedTimeRangePs") or {}
    effective_time = interest.get("effectiveTimeRangePs") or {}
    effective_distance = interest.get("effectiveDistanceRangeMm") or {}
    min_marker = channel.get("minMarker") or {}
    max_marker = channel.get("maxMarker") or {}
    evaluation = channel.get("evaluation") or {}
    return {
        "channel": channel.get("channel"),
        "trace": channel.get("trace"),
        "analysis_status": channel.get("status"),
        "analysis_reason": channel.get("reason"),
        "evaluation_status": evaluation.get("status"),
        "evaluation_reason": evaluation.get("reason"),
        "total_samples": counts.get("totalSampleCount"),
        "valid_samples": counts.get("validSampleCount"),
        "invalid_samples": counts.get("invalidSampleCount"),
        "selected_samples": channel.get("selectedSampleCount"),
        "waveform_start_ps": time_range.get("start"),
        "waveform_end_ps": time_range.get("end"),
        "interest_start_ps": requested_time.get("start"),
        "interest_end_ps": requested_time.get("end"),
        "effective_start_ps": effective_time.get("start"),
        "effective_end_ps": effective_time.get("end"),
        "effective_start_mm": effective_distance.get("start"),
        "effective_end_mm": effective_distance.get("end"),
        "min_impedance_ohm": min_marker.get("impedanceOhm"),
        "min_time_ps": min_marker.get("timePs"),
        "min_distance_mm": min_marker.get("distanceMm"),
        "min_source_sample_index": min_marker.get("sourceSampleIndex"),
        "min_tie_count": min_marker.get("tieCount"),
        "max_impedance_ohm": max_marker.get("impedanceOhm"),
        "max_time_ps": max_marker.get("timePs"),
        "max_distance_mm": max_marker.get("distanceMm"),
        "max_source_sample_index": max_marker.get("sourceSampleIndex"),
        "max_tie_count": max_marker.get("tieCount"),
    }


MARKER_CSV_FIELDS = [
    "channel",
    "trace",
    "analysis_status",
    "analysis_reason",
    "evaluation_status",
    "evaluation_reason",
    "total_samples",
    "valid_samples",
    "invalid_samples",
    "selected_samples",
    "waveform_start_ps",
    "waveform_end_ps",
    "interest_start_ps",
    "interest_end_ps",
    "effective_start_ps",
    "effective_end_ps",
    "effective_start_mm",
    "effective_end_mm",
    "min_impedance_ohm",
    "min_time_ps",
    "min_distance_mm",
    "min_source_sample_index",
    "min_tie_count",
    "max_impedance_ohm",
    "max_time_ps",
    "max_distance_mm",
    "max_source_sample_index",
    "max_tie_count",
]


def write_marker_results(
    result: Mapping[str, Any],
    *,
    json_path: Path,
    csv_path: Path,
) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as fp:
        json.dump(result, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=MARKER_CSV_FIELDS)
        writer.writeheader()
        for channel in result.get("channels") or []:
            writer.writerow(_csv_row(channel))
    return json_path, csv_path


def add_marker_overlays(
    axes: Any,
    result: Mapping[str, Any],
    *,
    line_by_trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Draw resolved interest spans and min/max points on an existing chart."""
    spans: set[tuple[float, float]] = set()
    marker_count = 0
    min_label_used = False
    max_label_used = False
    for channel in result.get("channels") or []:
        interest = channel.get("interestWindow") or {}
        effective = interest.get("effectiveTimeRangePs") or {}
        if effective.get("start") is not None and effective.get("end") is not None:
            spans.add((float(effective["start"]), float(effective["end"])))

        trace = str(channel.get("trace") or "")
        line = line_by_trace.get(trace)
        color = line.get_color() if line is not None and hasattr(line, "get_color") else None
        for key, marker_symbol, label_text in [
            ("minMarker", "v", "Interest min"),
            ("maxMarker", "^", "Interest max"),
        ]:
            marker = channel.get(key)
            if not marker:
                continue
            label = "_nolegend_"
            if key == "minMarker" and not min_label_used:
                label = label_text
                min_label_used = True
            elif key == "maxMarker" and not max_label_used:
                label = label_text
                max_label_used = True
            axes.scatter(
                [float(marker["timePs"])],
                [float(marker["impedanceOhm"])],
                marker=marker_symbol,
                s=38,
                color=color,
                edgecolors="black",
                linewidths=0.45,
                zorder=5,
                label=label,
            )
            marker_count += 1

    for start_ps, end_ps in sorted(spans):
        axes.axvspan(
            start_ps,
            end_ps,
            color="#6C757D",
            alpha=0.06,
            zorder=0,
        )
    return {
        "markerCount": marker_count,
        "interestSpanCount": len(spans),
    }
