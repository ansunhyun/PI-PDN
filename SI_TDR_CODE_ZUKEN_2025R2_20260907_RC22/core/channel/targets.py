from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .direction import (
    PATH_ENDPOINT_TO_START,
    PATH_START_TO_ENDPOINT,
    DirectionResolution,
    DirectionResolutionError,
    direction_candidate,
    require_resolved_direction,
    resolve_measurement_direction,
)
from .target_band import (
    LEGACY_REFERENCE_IMPEDANCE_KEY,
    REFERENCE_IMPEDANCE_KEY,
    TARGET_RANGE_KEY,
    resolve_reference_impedance,
    target_range_from_bounds,
)


REQUIRED_COLUMNS = [
    "interface",
    "channel",
    "signal_type",
    "ic_refdes",
    "pos_pin",
    "neg_pin",
]

OPTIONAL_COLUMNS = [
    "report_group",
    "interface_version",
    "channel_group",
    "direction",
    "endpoint_refdes",
    "endpoint_pos_pin",
    "endpoint_neg_pin",
    "near_pos_port",
    "near_neg_port",
    "far_pos_port",
    "far_neg_port",
    "measurement_direction",
    "measurement_direction_source",
    "measurement_direction_reason",
    "reference_impedance_ohm",
    "target_lower_ohm",
    "target_upper_ohm",
    "target_band_status",
    "target_band_reason",
    "target_band_source",
    "target_impedance_ohm",
    "rise_time_ps",
    "differential_bridge_ohm",
    "pulse_repetition",
    "pulse_width",
    "time_delay",
    "notes",
]


class ChannelTargetValidationError(ValueError):
    pass


def _clean(value: str | None) -> str:
    return (value or "").strip()


@dataclass(frozen=True)
class ChannelTarget:
    interface: str
    channel: str
    signal_type: str
    ic_refdes: str
    pos_pin: str
    neg_pin: str
    report_group: str = ""
    interface_version: str = ""
    channel_group: str = ""
    direction: str = ""
    endpoint_refdes: str = ""
    endpoint_pos_pin: str = ""
    endpoint_neg_pin: str = ""
    near_pos_port: str = ""
    near_neg_port: str = ""
    far_pos_port: str = ""
    far_neg_port: str = ""
    measurement_direction: str = ""
    measurement_direction_source: str = ""
    measurement_direction_reason: str = ""
    reference_impedance_ohm: str = ""
    target_lower_ohm: str = ""
    target_upper_ohm: str = ""
    target_band_status: str = ""
    target_band_reason: str = ""
    target_band_source: str = ""
    target_impedance_ohm: str = ""
    rise_time_ps: str = ""
    differential_bridge_ohm: str = ""
    pulse_repetition: str = ""
    pulse_width: str = ""
    time_delay: str = ""
    notes: str = ""

    @property
    def logical_group(self) -> str:
        parts = [self.interface]
        if self.interface_version:
            parts.append(self.interface_version)
        if self.channel_group:
            parts.append(self.channel_group)
        return "_".join(parts)

    @property
    def name(self) -> str:
        return f"{self.logical_group}_{self.channel}"

    @property
    def normalized_signal_type(self) -> str:
        return self.signal_type.casefold()

    @property
    def port_role_names(self) -> tuple[str, str, str, str]:
        return (
            self.near_pos_port,
            self.near_neg_port,
            self.far_pos_port,
            self.far_neg_port,
        )

    @property
    def has_complete_port_role_names(self) -> bool:
        return all(self.port_role_names)

    @property
    def has_explicit_endpoint(self) -> bool:
        return bool(
            self.endpoint_refdes
            and self.endpoint_pos_pin
            and self.endpoint_neg_pin
        )

    def legacy_direction_candidate(self):
        if any(self.port_role_names):
            return direction_candidate(
                PATH_START_TO_ENDPOINT,
                source=(
                    "legacy_explicit_port_roles_adapter"
                    if self.has_complete_port_role_names
                    else "legacy_partial_port_roles_adapter"
                ),
                priority=100,
                reason=(
                    "legacy near/far port columns were historically assigned to "
                    "path start/endpoint"
                ),
            )
        if self.has_explicit_endpoint:
            return direction_candidate(
                PATH_START_TO_ENDPOINT,
                source="legacy_explicit_endpoint_adapter",
                priority=100,
                reason=(
                    "legacy explicit endpoint rows historically measured from path start"
                ),
            )
        return direction_candidate(
            PATH_ENDPOINT_TO_START,
            source="legacy_auto_endpoint_adapter",
            priority=100,
            reason=(
                "legacy auto-endpoint rows historically measured from the discovered endpoint"
            ),
        )

    @property
    def direction_resolution(self) -> DirectionResolution:
        explicit = direction_candidate(
            self.measurement_direction,
            source=self.measurement_direction_source or "explicit_input",
            priority=300,
            reason=self.measurement_direction_reason or None,
        )
        return resolve_measurement_direction(
            [explicit, self.legacy_direction_candidate() if explicit is None else None]
        )

    @property
    def resolved_measurement_direction(self) -> str:
        return require_resolved_direction(
            self.direction_resolution,
            where=f"ChannelTarget[{self.name}]",
        )

    @property
    def reference_impedance_ohm_value(self) -> float | None:
        settings: dict[str, Any] = {}
        if self.reference_impedance_ohm:
            settings[REFERENCE_IMPEDANCE_KEY] = self.reference_impedance_ohm
        if self.target_impedance_ohm:
            settings[LEGACY_REFERENCE_IMPEDANCE_KEY] = self.target_impedance_ohm
        return resolve_reference_impedance(
            settings,
            where=f"ChannelTarget[{self.name}]",
        ).value_ohm

    @property
    def target_range_ohm(self) -> dict[str, Any] | None:
        normalized_status = self.target_band_status.casefold()
        if normalized_status not in {"", "configured", "not-configured"}:
            raise ChannelTargetValidationError(
                f"ChannelTarget[{self.name}].target_band_status must be "
                "'configured', 'not-configured', or empty"
            )
        if not self.target_lower_ohm and not self.target_upper_ohm:
            if normalized_status == "configured":
                raise ChannelTargetValidationError(
                    f"ChannelTarget[{self.name}] has configured target band status without bounds"
                )
            if normalized_status == "not-configured" or self.target_band_reason or self.target_band_source:
                return {
                    "status": "not-configured",
                    "lower": None,
                    "upper": None,
                    "source": self.target_band_source or None,
                    "reason": self.target_band_reason or "explicitly left unconfigured",
                }
            return None
        if normalized_status == "not-configured":
            raise ChannelTargetValidationError(
                f"ChannelTarget[{self.name}] has target bounds with not-configured status"
            )
        target_range = target_range_from_bounds(
            self.target_lower_ohm,
            self.target_upper_ohm,
            where=f"ChannelTarget[{self.name}]",
        ).to_dict()
        target_range["source"] = self.target_band_source or None
        target_range["reason"] = self.target_band_reason or target_range["reason"]
        return target_range

    @classmethod
    def from_row(cls, row: dict[str, str], *, line_number: int) -> "ChannelTarget":
        missing = [column for column in REQUIRED_COLUMNS if not _clean(row.get(column))]
        if missing:
            raise ChannelTargetValidationError(f"CSV line {line_number}: missing required columns: {missing}")

        target = cls(
            interface=_clean(row.get("interface")),
            channel=_clean(row.get("channel")),
            signal_type=_clean(row.get("signal_type")),
            ic_refdes=_clean(row.get("ic_refdes")),
            pos_pin=_clean(row.get("pos_pin")),
            neg_pin=_clean(row.get("neg_pin")),
            report_group=_clean(row.get("report_group")),
            interface_version=_clean(row.get("interface_version")),
            channel_group=_clean(row.get("channel_group")),
            direction=_clean(row.get("direction")).upper(),
            endpoint_refdes=_clean(row.get("endpoint_refdes")),
            endpoint_pos_pin=_clean(row.get("endpoint_pos_pin")),
            endpoint_neg_pin=_clean(row.get("endpoint_neg_pin")),
            near_pos_port=_clean(row.get("near_pos_port")),
            near_neg_port=_clean(row.get("near_neg_port")),
            far_pos_port=_clean(row.get("far_pos_port")),
            far_neg_port=_clean(row.get("far_neg_port")),
            measurement_direction=_clean(row.get("measurement_direction")),
            measurement_direction_source=_clean(
                row.get("measurement_direction_source")
            ),
            measurement_direction_reason=_clean(
                row.get("measurement_direction_reason")
            ),
            reference_impedance_ohm=_clean(row.get("reference_impedance_ohm")),
            target_lower_ohm=_clean(row.get("target_lower_ohm")),
            target_upper_ohm=_clean(row.get("target_upper_ohm")),
            target_band_status=_clean(row.get("target_band_status")),
            target_band_reason=_clean(row.get("target_band_reason")),
            target_band_source=_clean(row.get("target_band_source")),
            target_impedance_ohm=_clean(row.get("target_impedance_ohm")),
            rise_time_ps=_clean(row.get("rise_time_ps")),
            differential_bridge_ohm=_clean(row.get("differential_bridge_ohm")),
            pulse_repetition=_clean(row.get("pulse_repetition")),
            pulse_width=_clean(row.get("pulse_width")),
            time_delay=_clean(row.get("time_delay")),
            notes=_clean(row.get("notes")),
        )
        if target.normalized_signal_type not in {"differential", "diff"}:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: unsupported signal_type={target.signal_type!r}; "
                "only differential is supported in the EPIC 02 baseline"
            )
        endpoint_values = (
            target.endpoint_refdes,
            target.endpoint_pos_pin,
            target.endpoint_neg_pin,
        )
        if any(endpoint_values) and not all(endpoint_values):
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: endpoint_refdes/endpoint_pos_pin/"
                "endpoint_neg_pin must be provided together"
            )
        if any(target.port_role_names) and not target.has_complete_port_role_names:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: near/far P/N port role names must be provided together"
            )
        if target.measurement_direction_source and not target.measurement_direction:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: measurement_direction_source requires measurement_direction"
            )
        if target.measurement_direction_reason and not target.measurement_direction:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: measurement_direction_reason requires measurement_direction"
            )
        try:
            resolution = target.direction_resolution
        except DirectionResolutionError as exc:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: {exc}"
            ) from exc
        if target.measurement_direction:
            target = replace(
                target,
                measurement_direction=target.resolved_measurement_direction,
                measurement_direction_source=(
                    target.measurement_direction_source or "explicit_input"
                ),
            )
        else:
            target = replace(
                target,
                measurement_direction=require_resolved_direction(
                    resolution,
                    where=f"CSV line {line_number}",
                ),
                measurement_direction_source=resolution.source or "",
                measurement_direction_reason=resolution.reason or "",
            )
        if target.direction not in {"", "TX", "RX"}:
            raise ChannelTargetValidationError(
                f"CSV line {line_number}: unsupported direction={target.direction!r}; "
                "expected TX, RX, or empty"
            )
        _ = target.reference_impedance_ohm_value
        _ = target.target_range_ohm
        return target


@dataclass(frozen=True)
class PortRoleMetadata:
    version: int
    source_csv: str
    channels: list[dict[str, Any]]
    report_groups: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_channel_targets_csv(path: Path) -> list[ChannelTarget]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None:
            raise ChannelTargetValidationError(f"CSV has no header: {path}")

        missing_header = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing_header:
            raise ChannelTargetValidationError(f"CSV header missing required columns: {missing_header}")
        if "signal_direction" in reader.fieldnames:
            raise ChannelTargetValidationError(
                "CSV signal_direction is not part of the core model; keep RX/TX "
                "naming in interface, channel, or report_group"
            )

        targets = [ChannelTarget.from_row(row, line_number=reader.line_num) for row in reader]

    seen: set[str] = set()
    duplicates: list[str] = []
    for target in targets:
        key = target.name
        if key in seen:
            duplicates.append(target.name)
        seen.add(key)
    if duplicates:
        raise ChannelTargetValidationError(f"duplicate interface/channel targets: {duplicates}")
    return targets


def _role_port_name(target: ChannelTarget, role: str) -> str:
    return f"{target.name}_{role}".upper()


def _target_tdr_options(target: ChannelTarget, report_group: str) -> dict[str, Any]:
    tdr: dict[str, Any] = {
        "probeType": "differential",
        "reportGroup": report_group,
    }
    reference_impedance = target.reference_impedance_ohm_value
    if reference_impedance is not None:
        tdr[REFERENCE_IMPEDANCE_KEY] = reference_impedance
    target_range = target.target_range_ohm
    if target_range is not None:
        tdr[TARGET_RANGE_KEY] = {
            "lower": target_range["lower"],
            "upper": target_range["upper"],
            "reason": target_range["reason"],
            "source": target_range["source"],
        }
    if target.rise_time_ps:
        tdr["riseTimePs"] = float(target.rise_time_ps)
    if target.pulse_repetition:
        tdr["pulseRepetition"] = target.pulse_repetition
    if target.pulse_width:
        tdr["pulseWidth"] = target.pulse_width
    if target.time_delay:
        tdr["timeDelay"] = target.time_delay
    if target.differential_bridge_ohm:
        tdr["termination"] = {
            "differentialBridgeOhm": float(target.differential_bridge_ohm),
        }
    return tdr


def build_port_role_metadata(targets: list[ChannelTarget], *, source_csv: Path) -> PortRoleMetadata:
    channels: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    report_group_map: dict[str, list[str]] = {}

    for target in targets:
        group_name = target.report_group or target.interface
        report_group_map.setdefault(group_name, []).append(target.name)
        direction = target.direction_resolution

        has_endpoint = target.has_explicit_endpoint
        if not has_endpoint:
            unresolved.append(
                {
                    "channel": target.name,
                    "reason": "endpoint requires channel path traversal",
                    "start": {
                        "component": target.ic_refdes,
                        "pins": {
                            "positive": target.pos_pin,
                            "negative": target.neg_pin,
                        },
                    },
                }
            )

        start_endpoint = {
            "pathSide": "start",
            "component": target.ic_refdes,
            "pins": {
                "positive": target.pos_pin,
                "negative": target.neg_pin,
            },
        }
        path_endpoint = {
            "pathSide": "endpoint",
            "component": target.endpoint_refdes or None,
            "pins": {
                "positive": target.endpoint_pos_pin or None,
                "negative": target.endpoint_neg_pin or None,
            },
            "resolution": "provided" if has_endpoint else "pending_path_traversal",
        }
        near_endpoint = (
            start_endpoint if direction.near_path_side == "start" else path_endpoint
        )
        far_endpoint = (
            path_endpoint if direction.far_path_side == "endpoint" else start_endpoint
        )
        channels.append(
            {
                "name": target.name,
                "displayName": target.channel,
                "interface": target.interface,
                "channel": target.channel,
                "type": "differential",
                "source": {
                    "component": target.ic_refdes,
                    "pins": {
                        "positive": target.pos_pin,
                        "negative": target.neg_pin,
                    },
                },
                "endpoint": {
                    "component": target.endpoint_refdes or None,
                    "pins": {
                        "positive": target.endpoint_pos_pin or None,
                        "negative": target.endpoint_neg_pin or None,
                    },
                    "resolution": "provided" if has_endpoint else "pending_path_traversal",
                },
                "ports": {
                    "nearPositive": target.near_pos_port
                    or _role_port_name(target, "NEAR_POS"),
                    "nearNegative": target.near_neg_port
                    or _role_port_name(target, "NEAR_NEG"),
                    "farPositive": (
                        target.far_pos_port or _role_port_name(target, "FAR_POS")
                        if has_endpoint
                        else None
                    ),
                    "farNegative": (
                        target.far_neg_port or _role_port_name(target, "FAR_NEG")
                        if has_endpoint
                        else None
                    ),
                },
                "measurementDirection": direction.to_dict(),
                "near": near_endpoint,
                "far": far_endpoint,
                "circuit": {
                    "sourceRole": "near",
                    "terminationRole": "far",
                },
                "resultProvenance": {
                    "reportGroup": group_name,
                    "startRefdes": near_endpoint["component"],
                    "endRefdes": far_endpoint["component"],
                },
                "tdr": _target_tdr_options(target, group_name),
                "notes": target.notes,
            }
        )

    report_groups = [
        {
            "name": name,
            "channels": channel_names,
        }
        for name, channel_names in report_group_map.items()
    ]
    return PortRoleMetadata(
        version=2,
        source_csv=str(source_csv),
        channels=channels,
        report_groups=report_groups,
        unresolved=unresolved,
    )


def write_port_role_metadata(csv_path: Path, output_path: Path) -> PortRoleMetadata:
    targets = load_channel_targets_csv(csv_path)
    metadata = build_port_role_metadata(targets, source_csv=csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(metadata.to_dict(), fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return metadata
