"""License-free FB-06 endpoint and Port Role contract validation."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Iterable, Mapping


PORT_CONTRACT_SCHEMA = "si-tdr-port-contract/1"
ENDPOINT_LAYER_POLICY = "endpoint-pin-same-layer"
REFERENCE_NET_POLICY = "edb-ground-auto-fail-closed"
REFERENCE_TERMINAL_POLICY = "nearest-same-component-reference-pin-on-endpoint-layer"


class ReferenceNetResolutionError(ValueError):
    def __init__(self, message: str, *, evidence: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.evidence = deepcopy(dict(evidence))


def _semantic_ground_name(name: str) -> bool:
    normalized = name.strip().upper()
    return (
        normalized in {"0", "GND", "GROUND", "PGND", "DGND", "AGND", "VSS"}
        or normalized.endswith("_GND")
        or normalized.startswith("GND_")
    )


def resolve_reference_net_candidate(
    candidates: Iterable[Mapping[str, Any]],
    *,
    preferred_name: str | None = None,
    signal_net: str | None = None,
) -> dict[str, Any]:
    """Choose one EDB-backed reference net, or fail on absent/ambiguous facts."""

    merged: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        name = str(raw.get("name") or "").strip()
        if not name or (signal_net and name.casefold() == signal_net.casefold()):
            continue
        key = name.casefold()
        item = merged.setdefault(
            key,
            {
                "name": name,
                "exists": False,
                "isGround": False,
                "isPowerGround": False,
                "evidence": [],
            },
        )
        item["exists"] = bool(item["exists"] or raw.get("exists", True))
        item["isGround"] = bool(item["isGround"] or raw.get("isGround", False))
        item["isPowerGround"] = bool(
            item["isPowerGround"] or raw.get("isPowerGround", False)
        )
        evidence = raw.get("evidence")
        values = evidence if isinstance(evidence, list) else [evidence]
        for value in values:
            if value and str(value) not in item["evidence"]:
                item["evidence"].append(str(value))

    available = [item for item in merged.values() if item["exists"]]
    preferred = preferred_name.strip() if preferred_name else None

    def failed(message: str, code: str) -> ReferenceNetResolutionError:
        return ReferenceNetResolutionError(
            message,
            evidence={
                "status": "error",
                "policy": REFERENCE_NET_POLICY,
                "preferredName": preferred,
                "signalNet": signal_net,
                "issue": code,
                "candidates": deepcopy(
                    sorted(available, key=lambda value: value["name"].casefold())
                ),
            },
        )

    def selected(item: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "policy": REFERENCE_NET_POLICY,
            "referenceNet": item["name"],
            "selectionReason": reason,
            "selectedEvidence": deepcopy(item),
            "candidates": deepcopy(sorted(available, key=lambda value: value["name"].casefold())),
        }

    if preferred:
        preferred_item = merged.get(preferred.casefold())
        if preferred_item and preferred_item["exists"] and (
            preferred_item["isGround"]
            or preferred_item["isPowerGround"]
            or _semantic_ground_name(preferred_item["name"])
        ):
            return selected(
                preferred_item,
                "configured name matched an existing EDB ground/reference candidate",
            )

    explicit_ground = [item for item in available if item["isGround"]]
    if len(explicit_ground) == 1:
        return selected(explicit_ground[0], "unique EDB ground-classified net")
    if len(explicit_ground) > 1:
        names = [item["name"] for item in explicit_ground]
        raise failed(f"ambiguous EDB ground nets: {names}", "multiple_edb_ground_nets")

    power_ground = [item for item in available if item["isPowerGround"]]
    semantic_power_ground = [
        item for item in power_ground if _semantic_ground_name(item["name"])
    ]
    if len(semantic_power_ground) == 1:
        return selected(
            semantic_power_ground[0],
            "unique ground-named EDB power/ground candidate",
        )
    if len(power_ground) == 1:
        return selected(
            power_ground[0],
            "unique EDB power/ground candidate (arbitrary CAD net name accepted)",
        )

    names = [item["name"] for item in available]
    if not names:
        raise failed(
            "no EDB-backed reference/GND net candidate was found",
            "reference_net_candidate_missing",
        )
    raise failed(
        "reference/GND net is ambiguous; explicit EDB ground classification is required: "
        f"candidates={names}",
        "reference_net_candidate_ambiguous",
    )


def build_endpoint_terminal_contract(
    *,
    component: str,
    pin: str,
    signal_net: str,
    pin_layer: str,
    pin_layer_evidence: Mapping[str, Any],
    reference_selection: Mapping[str, Any],
) -> dict[str, Any]:
    if not pin_layer.strip():
        raise ValueError(f"endpoint pin layer is empty: component={component}, pin={pin}")
    reference_net = str(reference_selection.get("referenceNet") or "").strip()
    if not reference_net:
        raise ValueError(f"reference net resolution is empty: component={component}, pin={pin}")
    return {
        "component": component,
        "pin": pin,
        "signalNet": signal_net,
        "positiveLayer": pin_layer,
        "referenceLayer": pin_layer,
        "layerPolicy": ENDPOINT_LAYER_POLICY,
        "layerEvidence": deepcopy(dict(pin_layer_evidence)),
        "referenceNet": reference_net,
        "referenceNetSelection": deepcopy(dict(reference_selection)),
    }


def validate_planned_port_contract(
    *,
    expected_order: list[str],
    touchstone_port_count: int,
    metadata_payload: Mapping[str, Any],
    tdr_channels: list[Mapping[str, Any]],
    require_role_metadata: bool,
) -> dict[str, Any]:
    """Validate intended export order and near/far roles before SYZ solve."""

    issues: list[dict[str, Any]] = []
    duplicate_expected = sorted(
        name for name, count in Counter(expected_order).items() if count > 1
    )
    if duplicate_expected:
        issues.append({"code": "duplicate_expected_port_names", "ports": duplicate_expected})
    if touchstone_port_count != len(expected_order):
        issues.append(
            {
                "code": "touchstone_port_count_mismatch",
                "configured": touchstone_port_count,
                "expectedOrderCount": len(expected_order),
            }
        )

    metadata_ports = metadata_payload.get("ports") or []
    metadata_names = [str(item.get("name") or "") for item in metadata_ports]
    metadata_indices = [int(item.get("index") or 0) for item in metadata_ports]
    if int(metadata_payload.get("portCount") or 0) != len(metadata_ports):
        issues.append(
            {
                "code": "port_metadata_count_mismatch",
                "declared": metadata_payload.get("portCount"),
                "actual": len(metadata_ports),
            }
        )
    if metadata_names != expected_order:
        issues.append(
            {
                "code": "port_metadata_order_mismatch",
                "expected": expected_order,
                "actual": metadata_names,
            }
        )
    if metadata_indices != list(range(1, len(metadata_ports) + 1)):
        issues.append(
            {
                "code": "port_metadata_indices_not_sequential",
                "indices": metadata_indices,
            }
        )

    role_metadata = metadata_payload.get("portRoleMetadata")
    if require_role_metadata and not isinstance(role_metadata, Mapping):
        issues.append({"code": "port_role_metadata_missing"})
    role_names: list[str] = []
    role_order_names: list[str] = []
    if isinstance(role_metadata, Mapping):
        if role_metadata.get("schemaVersion") != 2:
            issues.append(
                {
                    "code": "port_role_metadata_version_mismatch",
                    "actual": role_metadata.get("schemaVersion"),
                }
            )
        role_order = role_metadata.get("portOrder") or []
        role_order_names = [str(item.get("name") or "") for item in role_order]
        role_order_indices = [int(item.get("index") or 0) for item in role_order]
        if role_order_names != expected_order or role_order_indices != list(
            range(1, len(expected_order) + 1)
        ):
            issues.append(
                {
                    "code": "port_role_metadata_order_mismatch",
                    "expected": expected_order,
                    "actual": role_order_names,
                    "indices": role_order_indices,
                }
            )

        tdr_by_name = {
            str(channel.get("name") or ""): channel for channel in tdr_channels
        }
        role_channels = role_metadata.get("channels") or []
        if len(role_channels) != len(tdr_by_name):
            issues.append(
                {
                    "code": "port_role_metadata_channel_count_mismatch",
                    "roles": len(role_channels),
                    "tdr": len(tdr_by_name),
                }
            )
        for role_channel in role_channels:
            if not isinstance(role_channel, Mapping):
                issues.append({"code": "port_role_metadata_channel_not_object"})
                continue
            channel_name = str(role_channel.get("name") or "")
            tdr_channel = tdr_by_name.get(channel_name)
            if tdr_channel is None:
                issues.append(
                    {
                        "code": "port_role_metadata_channel_missing_from_tdr",
                        "channel": channel_name,
                    }
                )
                continue
            actual_endpoints: dict[str, list[str]] = {}
            endpoint_components: dict[str, Any] = {}
            endpoint_pins: dict[str, Any] = {}
            for endpoint_name in ("near", "far"):
                endpoint = role_channel.get(endpoint_name) or {}
                ports = endpoint.get("ports") or {}
                names = [
                    str((ports.get(polarity) or {}).get("name") or "")
                    for polarity in ("positive", "negative")
                ]
                indices = [
                    int((ports.get(polarity) or {}).get("index") or 0)
                    for polarity in ("positive", "negative")
                ]
                actual_endpoints[endpoint_name] = names
                endpoint_components[endpoint_name] = endpoint.get("component")
                endpoint_pins[endpoint_name] = endpoint.get("pins") or {}
                role_names.extend(names)
                if any(
                    not name
                    or name not in expected_order
                    or indices[index] != expected_order.index(name) + 1
                    for index, name in enumerate(names)
                ):
                    issues.append(
                        {
                            "code": "port_role_metadata_index_mismatch",
                            "channel": channel_name,
                            "endpoint": endpoint_name,
                            "ports": names,
                            "indices": indices,
                        }
                    )
            expected_near = [
                str(tdr_channel.get("nearPositive") or ""),
                str(tdr_channel.get("nearNegative") or ""),
            ]
            expected_far = [
                str(tdr_channel.get("farPositive") or ""),
                str(tdr_channel.get("farNegative") or ""),
            ]
            if actual_endpoints.get("near") != expected_near or actual_endpoints.get(
                "far"
            ) != expected_far:
                issues.append(
                    {
                        "code": "port_role_metadata_near_far_mismatch",
                        "channel": channel_name,
                        "expectedNear": expected_near,
                        "actualNear": actual_endpoints.get("near"),
                        "expectedFar": expected_far,
                        "actualFar": actual_endpoints.get("far"),
                    }
                )
            direction = role_channel.get("measurementDirection") or {}
            if (
                direction.get("value") != tdr_channel.get("measurementDirection")
                or endpoint_components.get("near")
                != (tdr_channel.get("nearEndpoint") or {}).get("component")
                or endpoint_components.get("far")
                != (tdr_channel.get("farEndpoint") or {}).get("component")
                or endpoint_pins.get("near")
                != (tdr_channel.get("nearEndpoint") or {}).get("pins", {})
                or endpoint_pins.get("far")
                != (tdr_channel.get("farEndpoint") or {}).get("pins", {})
            ):
                issues.append(
                    {
                        "code": "port_role_metadata_tdr_mismatch",
                        "channel": channel_name,
                    }
                )
        if Counter(role_names) != Counter(expected_order):
            issues.append(
                {
                    "code": "port_role_metadata_coverage_mismatch",
                    "expected": expected_order,
                    "actual": role_names,
                }
            )

    return {
        "schema": PORT_CONTRACT_SCHEMA,
        "status": "error" if issues else "ok",
        "validationStage": "before-syz-solve",
        "touchstonePortCount": touchstone_port_count,
        "plannedTouchstonePortOrder": list(expected_order),
        "metadataPortOrder": metadata_names,
        "roleMetadataRequired": require_role_metadata,
        "roleMetadataPortOrder": role_order_names,
        "roleMetadataCoverage": role_names,
        "issues": issues,
    }
