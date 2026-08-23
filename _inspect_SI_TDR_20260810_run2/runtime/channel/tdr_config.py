from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .direction import DirectionResolutionError
from .target_band import REFERENCE_IMPEDANCE_KEY, TARGET_RANGE_KEY
from .targets import ChannelTarget


ENDPOINT_ANNOTATION_SCHEMA = "si-tdr-endpoint-annotation/v1"


@dataclass(frozen=True)
class ResolvedChannelPolarity:
    channel: str
    polarity: str
    start_net: str
    start_port_name: str
    end_net: str
    end_port_name: str
    start_component: str
    start_pin: str
    endpoint_component: str
    endpoint_pin: str
    status: str
    measurement_direction: str
    measurement_direction_source: str

    @property
    def uses_explicit_endpoint_port_name(self) -> bool:
        return self.end_port_name != self.end_net


def _resolved_paths_by_key(path_report: dict[str, Any]) -> dict[tuple[str, str], ResolvedChannelPolarity]:
    records: dict[tuple[str, str], ResolvedChannelPolarity] = {}
    for item in path_report.get("paths") or []:
        channel = str(item.get("channel") or "")
        polarity = str(item.get("polarity") or "")
        if not str(item.get("status") or "").startswith("resolved"):
            continue
        start_net = item.get("start_net")
        end_net = item.get("end_net")
        if not channel or polarity not in {"positive", "negative"} or not start_net or not end_net:
            continue
        start_port_name = item.get("start_port_name") or start_net
        end_port_name = item.get("end_port_name") or end_net
        records[(channel, polarity)] = ResolvedChannelPolarity(
            channel=channel,
            polarity=polarity,
            start_net=str(start_net),
            start_port_name=str(start_port_name),
            end_net=str(end_net),
            end_port_name=str(end_port_name),
            start_component=str(item.get("start_component") or ""),
            start_pin=str(item.get("start_pin") or ""),
            endpoint_component=str(item.get("endpoint_component") or ""),
            endpoint_pin=str(item.get("endpoint_pin") or ""),
            status=str(item.get("status") or "resolved"),
            measurement_direction=str(item.get("measurement_direction") or ""),
            measurement_direction_source=str(
                item.get("measurement_direction_source") or ""
            ),
        )
    return records


def _load_port_order_template(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if isinstance(payload, list):
        return [str(item) for item in payload]
    port_order = payload.get("portOrder")
    if not isinstance(port_order, list):
        raise ValueError(f"port order template must contain a portOrder list: {path}")
    return [str(item) for item in port_order]


def _apply_port_order_template(
    generated_order: list[str],
    template_order: list[str] | None,
    *,
    strict_template: bool,
) -> tuple[list[str], dict[str, Any]]:
    if template_order is None:
        return generated_order, {
            "policy": "port-name-ordinal",
            "templatePortCount": None,
            "omittedTemplatePorts": [],
            "appendedGeneratedPorts": [],
        }

    generated_set = set(generated_order)
    template_set = set(template_order)
    ordered = [port for port in template_order if port in generated_set]
    appended = [port for port in generated_order if port not in template_set]
    omitted = [port for port in template_order if port not in generated_set]
    if strict_template and omitted:
        raise ValueError(f"port order template contains ports not produced by channel paths: {omitted}")
    return ordered + appended, {
        "policy": "template-filter",
        "templatePortCount": len(template_order),
        "omittedTemplatePorts": omitted,
        "appendedGeneratedPorts": appended,
    }


def _add_optional_float(target: ChannelTarget, source_key: str, channel: dict[str, Any], dest_key: str) -> None:
    raw = getattr(target, source_key)
    if raw == "":
        return
    channel[dest_key] = float(raw)


def _add_optional_string(target: ChannelTarget, source_key: str, channel: dict[str, Any], dest_key: str) -> None:
    raw = getattr(target, source_key)
    if raw == "":
        return
    channel[dest_key] = raw


def _path_component_evidence(
    *,
    channel: str,
    positive_record: dict[str, Any],
    negative_record: dict[str, Any],
    field: str,
) -> tuple[str | None, list[str], list[str]]:
    values = [
        str(record.get(field) or "").strip()
        for record in (positive_record, negative_record)
    ]
    source_fields = [
        f"channelPath.paths[channel={channel},polarity={polarity}].{field}"
        for polarity in ("positive", "negative")
    ]
    unique_values = sorted({value for value in values if value})
    resolved = unique_values[0] if len(unique_values) == 1 and all(values) else None
    return resolved, values, source_fields


def _measurement_endpoint_metadata(
    *,
    target: ChannelTarget,
    positive: ResolvedChannelPolarity,
    negative: ResolvedChannelPolarity,
    positive_record: dict[str, Any],
    negative_record: dict[str, Any],
    near_uses_path_start: bool,
) -> dict[str, Any]:
    start_refdes, start_values, start_fields = _path_component_evidence(
        channel=target.name,
        positive_record=positive_record,
        negative_record=negative_record,
        field="start_component",
    )
    endpoint_refdes, endpoint_values, endpoint_fields = _path_component_evidence(
        channel=target.name,
        positive_record=positive_record,
        negative_record=negative_record,
        field="endpoint_component",
    )
    issues: list[dict[str, Any]] = []
    if start_refdes is None:
        issues.append(
            {
                "code": "missing_or_conflicting_channel_path_start_refdes",
                "sourceFields": start_fields,
                "values": start_values,
            }
        )
    elif start_refdes != target.ic_refdes:
        issues.append(
            {
                "code": "target_and_channel_path_start_refdes_conflict",
                "sourceFields": ["ChannelTarget.ic_refdes", *start_fields],
                "values": [target.ic_refdes, *start_values],
            }
        )
    if endpoint_refdes is None:
        issues.append(
            {
                "code": "missing_or_conflicting_channel_path_endpoint_refdes",
                "sourceFields": endpoint_fields,
                "values": endpoint_values,
            }
        )

    positive_explicit = positive.uses_explicit_endpoint_port_name
    negative_explicit = negative.uses_explicit_endpoint_port_name
    if positive_explicit != negative_explicit:
        issues.append(
            {
                "code": "polarity_near_far_direction_conflict",
                "sourceFields": [
                    "channelPath.paths[polarity=positive].end_port_name/end_net",
                    "channelPath.paths[polarity=negative].end_port_name/end_net",
                ],
                "values": [positive_explicit, negative_explicit],
            }
        )

    mapping_reason = (
        "explicit_endpoint_port_name_maps_channel_path_start_to_near"
        if near_uses_path_start
        else "legacy_net_named_path_maps_channel_path_endpoint_to_near"
    )
    record: dict[str, Any] = {
        "schema": ENDPOINT_ANNOTATION_SCHEMA,
        "status": "unresolved" if issues else "resolved",
        "reason": (
            "endpoint annotation metadata failed closed; review issues"
            if issues
            else "measurement start/end follow the same near/far assignment used by Circuit wiring"
        ),
        "sourceArtifact": None,
        "direction": {
            "measurement": "near_to_far",
            "source": "generated_tdr_channel_near_far_assignment",
            "mappingReason": mapping_reason,
            "nearPorts": [
                positive.start_port_name if near_uses_path_start else positive.end_port_name,
                negative.start_port_name if near_uses_path_start else negative.end_port_name,
            ],
            "farPorts": [
                positive.end_port_name if near_uses_path_start else positive.start_port_name,
                negative.end_port_name if near_uses_path_start else negative.start_port_name,
            ],
        },
        "issues": issues,
    }
    if issues:
        return record

    path_start = {
        "refdes": start_refdes,
        "channelPathRole": "start",
        "sourceFields": start_fields,
    }
    path_endpoint = {
        "refdes": endpoint_refdes,
        "channelPathRole": "endpoint",
        "sourceFields": endpoint_fields,
    }
    if near_uses_path_start:
        measurement_start = {**path_start, "portRole": "near"}
        measurement_end = {**path_endpoint, "portRole": "far"}
    else:
        measurement_start = {**path_endpoint, "portRole": "near"}
        measurement_end = {**path_start, "portRole": "far"}
    record["start"] = measurement_start
    record["end"] = measurement_end
    return record
def _paired_value(
    positive_value: str,
    negative_value: str,
    *,
    fallback: str,
    where: str,
) -> str:
    present = {value for value in (positive_value, negative_value) if value}
    if len(present) > 1:
        raise ValueError(f"{where} differs between positive and negative paths: {sorted(present)}")
    return next(iter(present), fallback)


def _path_side_record(
    target: ChannelTarget,
    positive: ResolvedChannelPolarity,
    negative: ResolvedChannelPolarity,
    *,
    side: str,
) -> dict[str, Any]:
    if side == "start":
        component = _paired_value(
            positive.start_component,
            negative.start_component,
            fallback=target.ic_refdes,
            where=f"{target.name} path start component",
        )
        return {
            "pathSide": "start",
            "component": component or None,
            "pins": {
                "positive": positive.start_pin or target.pos_pin or None,
                "negative": negative.start_pin or target.neg_pin or None,
            },
            "nets": {
                "positive": positive.start_net,
                "negative": negative.start_net,
            },
            "ports": {
                "positive": {"name": positive.start_port_name},
                "negative": {"name": negative.start_port_name},
            },
        }
    if side != "endpoint":
        raise ValueError(f"unsupported path side: {side}")
    component = _paired_value(
        positive.endpoint_component,
        negative.endpoint_component,
        fallback=target.endpoint_refdes,
        where=f"{target.name} path endpoint component",
    )
    return {
        "pathSide": "endpoint",
        "component": component or None,
        "pins": {
            "positive": positive.endpoint_pin or target.endpoint_pos_pin or None,
            "negative": negative.endpoint_pin or target.endpoint_neg_pin or None,
        },
        "nets": {
            "positive": positive.end_net,
            "negative": negative.end_net,
        },
        "ports": {
            "positive": {"name": positive.end_port_name},
            "negative": {"name": negative.end_port_name},
        },
    }


def _with_port_indices(endpoint: dict[str, Any], index_by_name: dict[str, int]) -> dict[str, Any]:
    result = json.loads(json.dumps(endpoint))
    for polarity in ("positive", "negative"):
        port = result["ports"][polarity]
        port["index"] = index_by_name.get(str(port["name"]))
    return result


def build_tdr_config_fragment(
    targets: list[ChannelTarget],
    path_report: dict[str, Any],
    *,
    port_order_template: list[str] | None = None,
    strict_port_order_template: bool = False,
) -> dict[str, Any]:
    """Build the sNp/Circuit-facing config fragment from resolved Channel Paths."""
    paths = _resolved_paths_by_key(path_report)
    path_records = {
        (str(item.get("channel") or ""), str(item.get("polarity") or "")): item
        for item in path_report.get("paths") or []
    }
    channels: list[dict[str, Any]] = []
    role_channels: list[dict[str, Any]] = []
    report_group_map: dict[str, list[str]] = {}
    near_port_order: list[str] = []
    far_port_order: list[str] = []
    unresolved: list[dict[str, Any]] = []

    for target in targets:
        positive = paths.get((target.name, "positive"))
        negative = paths.get((target.name, "negative"))
        if positive is None or negative is None:
            positive_record = path_records.get((target.name, "positive")) or {}
            negative_record = path_records.get((target.name, "negative")) or {}
            unresolved.append(
                {
                    "channel": target.name,
                    "reason": "positive or negative path is unresolved",
                    "positiveStatus": str(positive_record.get("status") or "missing"),
                    "positiveError": str(positive_record.get("error") or ""),
                    "negativeStatus": str(negative_record.get("status") or "missing"),
                    "negativeError": str(negative_record.get("error") or ""),
                }
            )
            continue

        try:
            direction = target.direction_resolution
            if direction.status != "resolved" or direction.near_path_side is None:
                raise DirectionResolutionError(
                    f"measurement direction unresolved: {direction.issues}"
                )
            path_direction_values = {
                value
                for value in (
                    positive.measurement_direction,
                    negative.measurement_direction,
                )
                if value
            }
            if len(path_direction_values) > 1 or (
                path_direction_values
                and direction.value not in path_direction_values
            ):
                raise ValueError(
                    "Channel Path measurement direction conflicts with normalized target: "
                    f"{sorted(path_direction_values)} vs {direction.value!r}"
                )
            start_endpoint = _path_side_record(
                target, positive, negative, side="start"
            )
            path_endpoint = _path_side_record(
                target, positive, negative, side="endpoint"
            )
            if target.ic_refdes and start_endpoint["component"]:
                if (
                    str(target.ic_refdes).casefold()
                    != str(start_endpoint["component"]).casefold()
                ):
                    raise ValueError(
                        f"path start component {start_endpoint['component']!r} "
                        f"conflicts with target start {target.ic_refdes!r}"
                    )
            for polarity, expected_pin in (
                ("positive", target.pos_pin),
                ("negative", target.neg_pin),
            ):
                resolved_pin = start_endpoint["pins"][polarity]
                if expected_pin and resolved_pin and str(expected_pin) != str(resolved_pin):
                    raise ValueError(
                        f"path start {polarity} pin {resolved_pin!r} conflicts "
                        f"with target start pin {expected_pin!r}"
                    )
            if target.endpoint_refdes and path_endpoint["component"]:
                if (
                    str(target.endpoint_refdes).casefold()
                    != str(path_endpoint["component"]).casefold()
                ):
                    raise ValueError(
                        f"explicit endpoint {target.endpoint_refdes!r} conflicts with "
                        f"resolved endpoint {path_endpoint['component']!r}"
                    )
            for polarity, expected_pin in (
                ("positive", target.endpoint_pos_pin),
                ("negative", target.endpoint_neg_pin),
            ):
                resolved_pin = path_endpoint["pins"][polarity]
                if expected_pin and resolved_pin and str(expected_pin) != str(resolved_pin):
                    raise ValueError(
                        f"explicit endpoint {polarity} pin {expected_pin!r} conflicts "
                        f"with resolved pin {resolved_pin!r}"
                    )
        except (DirectionResolutionError, ValueError) as exc:
            unresolved.append(
                {
                    "channel": target.name,
                    "stage": "port_role_metadata",
                    "code": "direction_or_endpoint_conflict",
                    "reason": str(exc),
                }
            )
            continue

        near_endpoint = (
            start_endpoint
            if direction.near_path_side == "start"
            else path_endpoint
        )
        far_endpoint = (
            path_endpoint
            if direction.far_path_side == "endpoint"
            else start_endpoint
        )
        near_positive = str(near_endpoint["ports"]["positive"]["name"])
        near_negative = str(near_endpoint["ports"]["negative"]["name"])
        far_positive = str(far_endpoint["ports"]["positive"]["name"])
        far_negative = str(far_endpoint["ports"]["negative"]["name"])
        report_group = target.report_group or target.interface
        near_uses_path_start = direction.near_path_side == "start"

        channel_record: dict[str, Any] = {
            "name": target.name,
            "displayName": target.channel,
            "nearPositive": near_positive,
            "nearNegative": near_negative,
            "farPositive": far_positive,
            "farNegative": far_negative,
            "measurementEndpoints": _measurement_endpoint_metadata(
                target=target,
                positive=positive,
                negative=negative,
                positive_record=path_records[(target.name, "positive")],
                negative_record=path_records[(target.name, "negative")],
                near_uses_path_start=near_uses_path_start,
            ),
            "measurementDirection": direction.value,
            "directionProvenance": direction.to_dict(),
            "nearEndpoint": {
                "component": near_endpoint["component"],
                "pins": near_endpoint["pins"],
            },
            "farEndpoint": {
                "component": far_endpoint["component"],
                "pins": far_endpoint["pins"],
            },
            "reportGroup": report_group,
            "resultProvenance": {
                "startRefdes": near_endpoint["component"],
                "endRefdes": far_endpoint["component"],
                "measurementDirection": direction.value,
            },
        }
        reference_impedance = target.reference_impedance_ohm_value
        if reference_impedance is not None:
            channel_record[REFERENCE_IMPEDANCE_KEY] = reference_impedance
        target_range = target.target_range_ohm
        if target_range is not None:
            channel_record[TARGET_RANGE_KEY] = {
                "lower": target_range["lower"],
                "upper": target_range["upper"],
                "reason": target_range["reason"],
                "source": target_range["source"],
            }
        _add_optional_float(target, "rise_time_ps", channel_record, "riseTimePs")
        _add_optional_string(target, "pulse_repetition", channel_record, "pulseRepetition")
        _add_optional_string(target, "pulse_width", channel_record, "pulseWidth")
        _add_optional_string(target, "time_delay", channel_record, "timeDelay")
        if target.differential_bridge_ohm != "":
            channel_record["termination"] = {
                "differentialBridgeOhm": float(target.differential_bridge_ohm),
            }

        channels.append(channel_record)
        role_channels.append(
            {
                "name": target.name,
                "signalType": "differential",
                "measurementDirection": direction.to_dict(),
                "channelPath": {
                    "positive": {
                        "polarity": "positive",
                        "status": positive.status,
                        "start": {
                            "component": start_endpoint["component"],
                            "pin": start_endpoint["pins"]["positive"],
                            "net": positive.start_net,
                            "portName": positive.start_port_name,
                        },
                        "endpoint": {
                            "component": path_endpoint["component"],
                            "pin": path_endpoint["pins"]["positive"],
                            "net": positive.end_net,
                            "portName": positive.end_port_name,
                        },
                    },
                    "negative": {
                        "polarity": "negative",
                        "status": negative.status,
                        "start": {
                            "component": start_endpoint["component"],
                            "pin": start_endpoint["pins"]["negative"],
                            "net": negative.start_net,
                            "portName": negative.start_port_name,
                        },
                        "endpoint": {
                            "component": path_endpoint["component"],
                            "pin": path_endpoint["pins"]["negative"],
                            "net": negative.end_net,
                            "portName": negative.end_port_name,
                        },
                    },
                },
                "near": near_endpoint,
                "far": far_endpoint,
                "circuit": {
                    "sourceRole": "near",
                    "terminationRole": "far",
                    "sourcePorts": [near_positive, near_negative],
                    "terminationPorts": [far_positive, far_negative],
                },
                "report": {
                    "group": report_group,
                    "startRefdes": near_endpoint["component"],
                    "endRefdes": far_endpoint["component"],
                },
            }
        )
        near_port_order.extend([near_positive, near_negative])
        far_port_order.extend([far_positive, far_negative])
        report_group_map.setdefault(report_group, []).append(target.name)

    generated_port_names = near_port_order + far_port_order
    duplicate_port_names = sorted(
        name for name, count in Counter(generated_port_names).items() if count > 1
    )
    if duplicate_port_names:
        raise ValueError(
            f"generated port names must be unique: {duplicate_port_names}"
        )
    generated_port_order = sorted(generated_port_names)

    port_order, port_order_record = _apply_port_order_template(
        generated_port_order,
        port_order_template,
        strict_template=strict_port_order_template,
    )
    index_by_name = {
        name: index for index, name in enumerate(port_order, start=1)
    }
    indexed_role_channels: list[dict[str, Any]] = []
    for channel in role_channels:
        indexed = json.loads(json.dumps(channel))
        indexed["near"] = _with_port_indices(indexed["near"], index_by_name)
        indexed["far"] = _with_port_indices(indexed["far"], index_by_name)
        indexed_role_channels.append(indexed)

    role_metadata = {
        "schemaVersion": 2,
        "source": {
            "channelPathAedb": path_report.get("aedb"),
        },
        "portOrder": [
            {"index": index, "name": name}
            for index, name in enumerate(port_order, start=1)
        ],
        "channels": indexed_role_channels,
    }

    return {
        "ports": {
            "touchstonePortCount": len(port_order),
            "portOrder": port_order,
            "portOrderPolicy": port_order_record,
            "roleMetadataVersion": 2,
        },
        "tdr": {
            "channels": channels,
            "reportGroups": [
                {
                    "name": group_name,
                    "channels": channel_names,
                }
                for group_name, channel_names in report_group_map.items()
            ],
        },
        "portRoleMetadata": role_metadata,
        "unresolved": unresolved,
}


def write_tdr_config_fragment(
    csv_targets: list[ChannelTarget],
    path_report_path: Path,
    output_path: Path,
    *,
    port_order_template_path: Path | None = None,
    strict_port_order_template: bool = False,
) -> dict[str, Any]:
    with path_report_path.open("r", encoding="utf-8") as fp:
        path_report = json.load(fp)
    fragment = build_tdr_config_fragment(
        csv_targets,
        path_report,
        port_order_template=_load_port_order_template(port_order_template_path),
        strict_port_order_template=strict_port_order_template,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(fragment, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return fragment
