from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REFERENCE_IMPEDANCE_KEY = "referenceImpedanceOhm"
LEGACY_REFERENCE_IMPEDANCE_KEY = "targetImpedanceOhm"
TARGET_RANGE_KEY = "targetRangeOhm"
TARGET_RANGE_FIELDS = {
    "lower",
    "upper",
    "source",
    "reason",
    "status",
    "implementationStatus",
}


class TdrImpedanceSchemaError(ValueError):
    """Raised when reference impedance or acceptance-band settings are ambiguous."""


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _positive_number(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise TdrImpedanceSchemaError(f"{where} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TdrImpedanceSchemaError(f"{where} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise TdrImpedanceSchemaError(f"{where} must be a finite positive number")
    return number


@dataclass(frozen=True)
class ReferenceImpedance:
    value_ohm: float | None
    source: str | None
    legacy_alias_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "configured" if self.value_ohm is not None else "not-configured",
            "valueOhm": self.value_ohm,
            "source": self.source,
            "legacyAliasUsed": self.legacy_alias_used,
        }


@dataclass(frozen=True)
class TargetBand:
    lower_ohm: float | None
    upper_ohm: float | None
    source: str | None
    reason: str | None = None

    @property
    def configured(self) -> bool:
        return self.lower_ohm is not None and self.upper_ohm is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "configured" if self.configured else "not-configured",
            "lower": self.lower_ohm,
            "upper": self.upper_ohm,
            "source": self.source,
            "reason": self.reason,
        }


def resolve_reference_impedance(
    settings: dict[str, Any],
    *,
    fallback: ReferenceImpedance | None = None,
    where: str = "tdr",
) -> ReferenceImpedance:
    canonical_key_present = REFERENCE_IMPEDANCE_KEY in settings
    legacy_key_present = LEGACY_REFERENCE_IMPEDANCE_KEY in settings
    if canonical_key_present and _is_missing(settings.get(REFERENCE_IMPEDANCE_KEY)):
        raise TdrImpedanceSchemaError(
            f"{where}.{REFERENCE_IMPEDANCE_KEY} cannot be null or blank"
        )
    if legacy_key_present and _is_missing(settings.get(LEGACY_REFERENCE_IMPEDANCE_KEY)):
        raise TdrImpedanceSchemaError(
            f"{where}.{LEGACY_REFERENCE_IMPEDANCE_KEY} cannot be null or blank"
        )

    canonical = (
        _positive_number(
            settings[REFERENCE_IMPEDANCE_KEY],
            where=f"{where}.{REFERENCE_IMPEDANCE_KEY}",
        )
        if canonical_key_present
        else None
    )
    legacy = (
        _positive_number(
            settings[LEGACY_REFERENCE_IMPEDANCE_KEY],
            where=f"{where}.{LEGACY_REFERENCE_IMPEDANCE_KEY}",
        )
        if legacy_key_present
        else None
    )
    if canonical is not None and legacy is not None and not math.isclose(
        canonical,
        legacy,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise TdrImpedanceSchemaError(
            f"{where}: {REFERENCE_IMPEDANCE_KEY}={canonical:g} conflicts with legacy "
            f"{LEGACY_REFERENCE_IMPEDANCE_KEY}={legacy:g}"
        )
    if canonical is not None:
        return ReferenceImpedance(
            value_ohm=canonical,
            source=f"{where}.{REFERENCE_IMPEDANCE_KEY}",
            legacy_alias_used=legacy_key_present,
        )
    if legacy is not None:
        return ReferenceImpedance(
            value_ohm=legacy,
            source=f"{where}.{LEGACY_REFERENCE_IMPEDANCE_KEY}",
            legacy_alias_used=True,
        )
    if fallback is not None:
        return fallback
    return ReferenceImpedance(value_ohm=None, source=None)


def merge_tdr_setting_overrides(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """Apply a shallow TDR override while treating both reference names as one field."""
    merged = dict(base)
    canonical_override = REFERENCE_IMPEDANCE_KEY in override
    legacy_override = LEGACY_REFERENCE_IMPEDANCE_KEY in override
    if canonical_override and not legacy_override:
        merged.pop(LEGACY_REFERENCE_IMPEDANCE_KEY, None)
    elif legacy_override and not canonical_override:
        merged.pop(REFERENCE_IMPEDANCE_KEY, None)
    merged.update(override)
    return merged


def _pending_reason(raw: dict[str, Any]) -> str:
    for key in ("reason", "implementationStatus"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "customer acceptance policy is not configured"


def resolve_target_band(
    settings: dict[str, Any],
    *,
    fallback: TargetBand | None = None,
    where: str = "tdr",
) -> TargetBand:
    if TARGET_RANGE_KEY not in settings:
        if fallback is not None:
            return fallback
        return TargetBand(
            lower_ohm=None,
            upper_ohm=None,
            source=None,
            reason="customer acceptance policy is not configured",
        )

    raw = settings.get(TARGET_RANGE_KEY)
    source = f"{where}.{TARGET_RANGE_KEY}"
    if raw is None:
        return TargetBand(
            lower_ohm=None,
            upper_ohm=None,
            source=source,
            reason="explicitly left unconfigured",
        )
    if not isinstance(raw, dict):
        raise TdrImpedanceSchemaError(f"{source} must be an object or null")
    unknown = sorted(set(raw) - TARGET_RANGE_FIELDS)
    if unknown:
        raise TdrImpedanceSchemaError(
            f"{source} contains unsupported fields: {unknown}"
        )

    status = raw.get("status")
    if "status" in raw:
        if _is_missing(status) or str(status).casefold() not in {
            "configured",
            "not-configured",
        }:
            raise TdrImpedanceSchemaError(
                f"{source}.status must be 'configured' or 'not-configured'"
            )
        status = str(status).casefold()

    lower_present = "lower" in raw and not _is_missing(raw.get("lower"))
    upper_present = "upper" in raw and not _is_missing(raw.get("upper"))
    if lower_present != upper_present:
        raise TdrImpedanceSchemaError(
            f"{source} must provide both lower and upper, or leave both unconfigured"
        )
    if not lower_present:
        if status == "configured":
            raise TdrImpedanceSchemaError(
                f"{source} has configured status without lower and upper"
            )
        return TargetBand(
            lower_ohm=None,
            upper_ohm=None,
            source=source,
            reason=_pending_reason(raw),
        )
    if status == "not-configured":
        raise TdrImpedanceSchemaError(
            f"{source} has lower and upper with not-configured status"
        )

    lower = _positive_number(raw["lower"], where=f"{source}.lower")
    upper = _positive_number(raw["upper"], where=f"{source}.upper")
    if lower >= upper:
        raise TdrImpedanceSchemaError(
            f"{source}.lower must be less than {source}.upper; got {lower:g} and {upper:g}"
        )
    return TargetBand(
        lower_ohm=lower,
        upper_ohm=upper,
        source=str(raw.get("source") or source),
        reason=str(raw.get("reason")).strip() if raw.get("reason") else None,
    )


def target_range_from_bounds(
    lower: Any,
    upper: Any,
    *,
    where: str,
) -> TargetBand:
    settings = {
        TARGET_RANGE_KEY: {
            "lower": None if _is_missing(lower) else lower,
            "upper": None if _is_missing(upper) else upper,
        }
    }
    return resolve_target_band(settings, where=where)


def validate_tdr_impedance_config(tdr: dict[str, Any]) -> None:
    if not isinstance(tdr, dict):
        raise TdrImpedanceSchemaError("tdr must be an object")
    global_reference = resolve_reference_impedance(tdr)
    if global_reference.value_ohm is None:
        raise TdrImpedanceSchemaError(
            f"tdr requires {REFERENCE_IMPEDANCE_KEY}; legacy "
            f"{LEGACY_REFERENCE_IMPEDANCE_KEY} remains accepted as a reference-only alias"
        )
    global_band = resolve_target_band(tdr)
    channels = tdr.get("channels") or []
    if not isinstance(channels, list) or not all(isinstance(item, dict) for item in channels):
        raise TdrImpedanceSchemaError("tdr.channels must be a list of objects")
    channel_names: set[str] = set()
    for index, channel in enumerate(channels):
        name = str(channel.get("name") or "").strip()
        if not name:
            raise TdrImpedanceSchemaError(
                f"tdr.channels[{index}].name must be a non-empty string"
            )
        if name in channel_names:
            raise TdrImpedanceSchemaError(
                f"tdr.channels contains duplicate name: {name}"
            )
        channel_names.add(name)
        resolve_reference_impedance(
            channel,
            fallback=global_reference,
            where=f"tdr.channels[{name}]",
        )
        resolve_target_band(
            channel,
            fallback=global_band,
            where=f"tdr.channels[{name}]",
        )


def _channel_settings(tdr: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(channel.get("name")): channel
        for channel in (tdr.get("channels") or [])
        if channel.get("name")
    }


def _trace_names(transient: dict[str, Any]) -> list[str]:
    samples_by_trace = transient.get("samplesByTrace") or {}
    if samples_by_trace:
        return [str(name) for name in samples_by_trace]
    return [str(name) for name in (transient.get("traceNames") or [])]


def build_tdr_impedance_metadata(
    tdr: dict[str, Any],
    transient: dict[str, Any],
) -> dict[str, Any]:
    validate_tdr_impedance_config(tdr)
    global_reference = resolve_reference_impedance(tdr)
    global_band = resolve_target_band(tdr)
    configured_channels = _channel_settings(tdr)
    probes = (transient.get("manualTopology") or {}).get("tdrProbes") or []
    if not isinstance(probes, list) or not all(isinstance(probe, dict) for probe in probes):
        raise TdrImpedanceSchemaError(
            "tdr_transient.manualTopology.tdrProbes must be a list of objects"
        )
    trace_to_probe: dict[str, dict[str, Any]] = {}
    for probe in probes:
        trace_name = str(probe.get("traceName") or "")
        if not trace_name:
            continue
        if trace_name in trace_to_probe:
            raise TdrImpedanceSchemaError(
                f"tdr_transient contains duplicate TDR probe traceName: {trace_name}"
            )
        trace_to_probe[trace_name] = probe

    trace_names = _trace_names(transient)
    if not trace_names and transient.get("samples"):
        trace_names = ["TDR"]

    channels: list[dict[str, Any]] = []
    legacy_used = global_reference.legacy_alias_used
    for trace_name in trace_names:
        probe = trace_to_probe.get(trace_name) or {}
        channel_name = str(probe.get("channel") or "")
        if configured_channels and not probe:
            raise TdrImpedanceSchemaError(
                f"tdr_transient trace {trace_name!r} has no TDR probe/channel mapping"
            )
        if configured_channels and channel_name not in configured_channels:
            raise TdrImpedanceSchemaError(
                f"tdr_transient trace {trace_name!r} references unknown channel "
                f"{channel_name!r}"
            )
        settings = dict(configured_channels.get(channel_name) or {})
        reference = resolve_reference_impedance(
            settings,
            fallback=global_reference,
            where=f"tdr.channels[{channel_name or trace_name}]",
        )
        if any(
            key in probe
            for key in (REFERENCE_IMPEDANCE_KEY, LEGACY_REFERENCE_IMPEDANCE_KEY)
        ):
            probe_reference = resolve_reference_impedance(
                probe,
                where=f"tdr_transient.tdrProbes[{trace_name}]",
            )
            if (
                probe_reference.value_ohm is not None
                and reference.value_ohm is not None
                and not math.isclose(
                    probe_reference.value_ohm,
                    reference.value_ohm,
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
            ):
                raise TdrImpedanceSchemaError(
                    f"tdr_transient trace {trace_name!r} used reference impedance "
                    f"{probe_reference.value_ohm:g} ohm but config resolves to "
                    f"{reference.value_ohm:g} ohm"
                )
        band = resolve_target_band(
            settings,
            fallback=global_band,
            where=f"tdr.channels[{channel_name or trace_name}]",
        )
        legacy_used = legacy_used or reference.legacy_alias_used
        channels.append(
            {
                "channel": channel_name or None,
                "traceName": trace_name,
                REFERENCE_IMPEDANCE_KEY: reference.value_ohm,
                "referenceSource": reference.source,
                TARGET_RANGE_KEY: band.to_dict(),
            }
        )

    return {
        "schemaVersion": 1,
        REFERENCE_IMPEDANCE_KEY: global_reference.value_ohm,
        "referenceImpedanceSource": global_reference.source,
        TARGET_RANGE_KEY: global_band.to_dict(),
        "channels": channels,
        "migration": {
            "legacySingleTargetField": LEGACY_REFERENCE_IMPEDANCE_KEY,
            "legacyFieldUsed": legacy_used,
            "policy": "reference-only-no-implicit-acceptance-band",
            "targetBandDerivedFromReference": False,
        },
    }


def _trace_impedance_map(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(channel["traceName"]): channel
        for channel in metadata.get("channels") or []
        if channel.get("traceName")
    }


def write_tdr_waveform_csv(
    transient: dict[str, Any],
    metadata: dict[str, Any],
    output_path: Path,
) -> Path:
    fieldnames = [
        "trace_name",
        "channel",
        "sample_index",
        "time_ps",
        "impedance_ohm",
        "reference_impedance_ohm",
        "target_lower_ohm",
        "target_upper_ohm",
        "target_band_status",
        "target_band_source",
        "target_band_reason",
    ]
    trace_metadata = _trace_impedance_map(metadata)
    samples_by_trace = transient.get("samplesByTrace") or {}
    if samples_by_trace:
        traces = [(str(name), samples) for name, samples in samples_by_trace.items()]
    else:
        names = _trace_names(transient)
        traces = [(names[0] if names else "TDR", transient.get("samples") or [])]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for trace_name, samples in traces:
            trace = trace_metadata.get(trace_name) or {}
            band = trace.get(TARGET_RANGE_KEY) or metadata.get(TARGET_RANGE_KEY) or {}
            for index, sample in enumerate(samples):
                writer.writerow(
                    {
                        "trace_name": trace_name,
                        "channel": trace.get("channel") or "",
                        "sample_index": sample.get("index", index),
                        "time_ps": sample.get("time_ps"),
                        "impedance_ohm": sample.get("impedance_ohm"),
                        "reference_impedance_ohm": trace.get(
                            REFERENCE_IMPEDANCE_KEY,
                            metadata.get(REFERENCE_IMPEDANCE_KEY),
                        ),
                        "target_lower_ohm": band.get("lower"),
                        "target_upper_ohm": band.get("upper"),
                        "target_band_status": band.get("status") or "not-configured",
                        "target_band_source": band.get("source") or "",
                        "target_band_reason": band.get("reason") or "",
                    }
                )
    return output_path


def _chart_scope_label(channel_names: list[str], trace_names: list[str]) -> str:
    names = channel_names or trace_names
    kind = "channels" if channel_names else "traces"
    if len(names) <= 3:
        return ", ".join(names)
    return f"{len(names)} {kind}"


def build_tdr_impedance_overlay_records(metadata: dict[str, Any]) -> dict[str, Any]:
    """Group reference lines and target bands by their channel/trace scope."""

    channels = metadata.get("channels") or []
    reference_scopes: dict[float, dict[str, set[str]]] = {}
    target_band_scopes: dict[tuple[float, float], dict[str, set[str]]] = {}
    for item in channels:
        channel_name = str(item.get("channel") or "")
        trace_name = str(item.get("traceName") or "")
        reference = item.get(REFERENCE_IMPEDANCE_KEY)
        if reference is not None:
            scope = reference_scopes.setdefault(
                float(reference),
                {"channels": set(), "traceNames": set()},
            )
            if channel_name:
                scope["channels"].add(channel_name)
            if trace_name:
                scope["traceNames"].add(trace_name)
        band = item.get(TARGET_RANGE_KEY) or {}
        if band.get("status") == "configured":
            band_key = (float(band["lower"]), float(band["upper"]))
            scope = target_band_scopes.setdefault(
                band_key,
                {"channels": set(), "traceNames": set()},
            )
            if channel_name:
                scope["channels"].add(channel_name)
            if trace_name:
                scope["traceNames"].add(trace_name)

    if not reference_scopes and metadata.get(REFERENCE_IMPEDANCE_KEY) is not None:
        reference_scopes[float(metadata[REFERENCE_IMPEDANCE_KEY])] = {
            "channels": set(),
            "traceNames": set(),
        }
    if not target_band_scopes:
        global_band = metadata.get(TARGET_RANGE_KEY) or {}
        if global_band.get("status") == "configured":
            target_band_scopes[
                (float(global_band["lower"]), float(global_band["upper"]))
            ] = {"channels": set(), "traceNames": set()}

    target_band_records: list[dict[str, Any]] = []
    for (lower, upper), scope in sorted(target_band_scopes.items()):
        channel_names = sorted(scope["channels"])
        trace_names = sorted(scope["traceNames"])
        target_band_records.append(
            {
                "lower": lower,
                "upper": upper,
                "channels": channel_names,
                "traceNames": trace_names,
            }
        )

    reference_records: list[dict[str, Any]] = []
    for reference, scope in sorted(reference_scopes.items()):
        channel_names = sorted(scope["channels"])
        trace_names = sorted(scope["traceNames"])
        reference_records.append(
            {
                "value": reference,
                "channels": channel_names,
                "traceNames": trace_names,
            }
        )

    return {
        "referenceLinesOhm": reference_records,
        "targetBandsOhm": target_band_records,
    }


def add_tdr_impedance_chart_overlays(ax: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    overlays = build_tdr_impedance_overlay_records(metadata)
    band_colors = ["#E09F3E", "#2A9D8F", "#9B5DE5", "#F15BB5"]
    for index, band in enumerate(overlays["targetBandsOhm"]):
        scope_label = _chart_scope_label(band["channels"], band["traceNames"])
        ax.axhspan(
            band["lower"],
            band["upper"],
            color=band_colors[index % len(band_colors)],
            alpha=0.16,
            label=(
                f"Target band {band['lower']:g}-{band['upper']:g} ohm"
                + (f" ({scope_label})" if scope_label else "")
            ),
        )

    for reference in overlays["referenceLinesOhm"]:
        scope_label = _chart_scope_label(
            reference["channels"],
            reference["traceNames"],
        )
        ax.axhline(
            reference["value"],
            color="#C1121F",
            linestyle="--",
            linewidth=1.25,
            label=(
                f"Reference {reference['value']:g} ohm"
                + (f" ({scope_label})" if scope_label else "")
            ),
        )

    return overlays
