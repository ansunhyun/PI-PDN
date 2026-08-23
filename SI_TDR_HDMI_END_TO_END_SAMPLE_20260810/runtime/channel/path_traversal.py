from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .aedb_lookup import _component_pins_by_name, _ensure_pyedb_path, _pin_name, _pin_net
from .array_mapping import ArrayMappingLibrary, load_array_mapping_library
from .targets import ChannelTarget
from .time_range import annotate_resolved_path_routing_length


DEFAULT_ENDPOINT_PREFIXES = ("JK", "CN", "CON", "SW", "P")
SERIES_PASSIVE_TYPES = {
    "capacitor": "series_capacitor",
    "inductor": "series_inductor",
    "resistor": "series_resistor",
}


@dataclass(frozen=True)
class PathStep:
    kind: str
    net: str
    component: str | None = None
    component_type: str | None = None
    part_name: str | None = None
    action: str | None = None


@dataclass(frozen=True)
class PolarityPath:
    channel: str
    polarity: str
    start_component: str
    start_pin: str
    start_net: str | None
    start_port_name: str | None
    status: str
    end_net: str | None
    end_port_name: str | None
    endpoint_component: str | None
    endpoint_pin: str | None
    endpoint_type: str | None
    steps: list[PathStep]
    endpoint_candidates: list[dict[str, str | None]] | None = None
    error: str | None = None
    routing_length: dict[str, float | str] | None = None
    routing_length_evidence: dict[str, Any] | None = None
    measurement_direction: str | None = None
    measurement_direction_source: str | None = None


@dataclass(frozen=True)
class ChannelPathReport:
    aedb: str
    aedt_version: str
    paths: list[PolarityPath]

    @property
    def resolved_count(self) -> int:
        return sum(1 for path in self.paths if path.status.startswith("resolved"))

    @property
    def dropped_count(self) -> int:
        return sum(1 for path in self.paths if path.status.startswith("dropped"))

    @property
    def unresolved_count(self) -> int:
        return sum(
            1
            for path in self.paths
            if not path.status.startswith("resolved") and not path.status.startswith("dropped")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "aedb": self.aedb,
            "aedtVersion": self.aedt_version,
            "summary": {
                "total": len(self.paths),
                "resolved": self.resolved_count,
                "dropped": self.dropped_count,
                "unresolved": self.unresolved_count,
            },
            "paths": [asdict(path) for path in self.paths],
        }


def _component_type(component: object) -> str:
    return str(getattr(component, "type", "") or "")


def _component_part(component: object) -> str:
    return str(getattr(component, "component_def", "") or "")


def _component_nets(component: object) -> list[str]:
    return [str(net) for net in getattr(component, "nets", [])]


def _net_components(edb: object, net_name: str) -> dict[str, object]:
    try:
        net = edb._nets.nets[net_name]
    except Exception as exc:
        raise LookupError(f"net not found: {net_name}") from exc
    return dict(net.components.items())


def _component_by_name(edb: object, refdes: str) -> object | None:
    components = getattr(edb.components, "components", None)
    if isinstance(components, dict):
        component = components.get(refdes)
        if component is not None:
            return component
    try:
        return edb.components.get_component_by_name(refdes)
    except Exception:
        return None


def _all_component_names(edb: object) -> list[str]:
    components = getattr(edb.components, "components", None)
    if isinstance(components, dict):
        return sorted(str(refdes) for refdes in components)
    return []


def _is_endpoint_component(name: str, component: object, endpoint_prefixes: tuple[str, ...]) -> bool:
    upper_name = name.upper()
    if any(upper_name.startswith(prefix) for prefix in endpoint_prefixes):
        return True
    part_name = _component_part(component).upper()
    return any(token in part_name for token in ("CONNECTOR", "JACK", "SWITCH"))


def _is_auto_endpoint_component(name: str, component: object, endpoint_prefixes: tuple[str, ...]) -> bool:
    if _is_endpoint_component(name, component, endpoint_prefixes):
        return True
    return _component_type(component).casefold() in {"ic", "io"}


def _series_passive_kind(component: object) -> str | None:
    if len(_component_nets(component)) != 2:
        return None
    return SERIES_PASSIVE_TYPES.get(_component_type(component).casefold())


def _other_net(component: object, current_net: str) -> str | None:
    for net in _component_nets(component):
        if net != current_net:
            return net
    return None


def _is_reference_or_power_net(net_name: str) -> bool:
    upper_name = net_name.upper()
    return upper_name in {"GND", "PGND"} or upper_name.startswith(("+", "-"))


def _is_auto_excluded_branch_net(net_name: str) -> bool:
    upper_name = net_name.upper()
    return any(token in upper_name for token in ("DET", "PLUG", "HPD"))


def _can_auto_traverse_net(net_name: str) -> bool:
    return not _is_reference_or_power_net(net_name) and not _is_auto_excluded_branch_net(net_name)


def _start_net(edb: object, target: ChannelTarget, polarity: str) -> str | None:
    pin_name = target.pos_pin if polarity == "positive" else target.neg_pin
    pin = _component_pins_by_name(edb, target.ic_refdes).get(pin_name)
    return _pin_net(pin) if pin else None


def _endpoint_pin_name(target: ChannelTarget, polarity: str) -> str:
    return target.endpoint_pos_pin if polarity == "positive" else target.endpoint_neg_pin


def _explicit_endpoint_pin(edb: object, target: ChannelTarget, polarity: str) -> tuple[object, str] | None:
    if not target.endpoint_refdes:
        return None
    pin_name = _endpoint_pin_name(target, polarity)
    if not pin_name:
        return None
    pin = _component_pins_by_name(edb, target.endpoint_refdes).get(pin_name)
    if pin is None:
        raise LookupError(f"endpoint pin not found: {target.endpoint_refdes}.{pin_name}")
    return pin, pin_name


def _port_names_for_path(start_net: str, end_net: str, *, explicit_endpoint: bool) -> tuple[str, str]:
    start_port_name = start_net
    end_port_name = end_net
    if explicit_endpoint or start_net == end_net:
        end_port_name = f"{start_net}_0"
    return start_port_name, end_port_name


def _port_names_for_target(
    target: ChannelTarget,
    polarity: str,
    start_net: str,
    end_net: str,
    *,
    explicit_endpoint: bool,
) -> tuple[str, str]:
    start_port_name, end_port_name = _port_names_for_path(
        start_net, end_net, explicit_endpoint=explicit_endpoint
    )
    if polarity == "positive":
        near_name = target.near_pos_port
        far_name = target.far_pos_port
    else:
        near_name = target.near_neg_port
        far_name = target.far_neg_port

    if target.direction_resolution.near_path_side == "start":
        return near_name or start_port_name, far_name or end_port_name
    return far_name or start_port_name, near_name or end_port_name


def _component_pin_for_net(edb: object, refdes: str, net_name: str) -> str | None:
    for pin in _component_pins_by_name(edb, refdes).values():
        if _pin_net(pin) == net_name:
            return _pin_name(pin)
    return None


def _endpoint_candidate_record(
    *,
    component_name: str,
    component: object,
    pin_name: str | None,
    net_name: str,
    steps: list[PathStep],
) -> dict[str, str | None]:
    return {
        "component": component_name,
        "pin": pin_name,
        "net": net_name,
        "componentType": _component_type(component),
        "partName": _component_part(component),
        "depth": str(sum(1 for step in steps if step.kind.startswith("series_") or step.kind == "array_component")),
    }


def _same_net_pin_endpoint_candidates(
    edb: object,
    *,
    current_net: str,
    previous_component: str,
    endpoint_prefixes: tuple[str, ...],
    steps: list[PathStep],
) -> list[tuple[dict[str, str | None], str, str | None, object, list[PathStep]]]:
    candidates: list[tuple[dict[str, str | None], str, str | None, object, list[PathStep]]] = []
    for refdes in _all_component_names(edb):
        if refdes == previous_component:
            continue
        component = _component_by_name(edb, refdes)
        if component is None or not _is_endpoint_component(refdes, component, endpoint_prefixes):
            continue
        try:
            pins_by_name = _component_pins_by_name(edb, refdes)
        except LookupError:
            continue
        for pin_name, pin in sorted(pins_by_name.items(), key=lambda item: item[0]):
            if _pin_net(pin) != current_net:
                continue
            endpoint_steps = steps + [
                PathStep(
                    kind="same_net_endpoint",
                    net=current_net,
                    component=refdes,
                    component_type=_component_type(component),
                    part_name=_component_part(component),
                    action=f"stop at pin {pin_name}",
                )
            ]
            candidates.append(
                (
                    _endpoint_candidate_record(
                        component_name=refdes,
                        component=component,
                        pin_name=pin_name,
                        net_name=current_net,
                        steps=steps,
                    ),
                    refdes,
                    pin_name,
                    component,
                    endpoint_steps,
                )
            )
    return candidates


def _array_mapping_candidate(
    edb: object,
    refdes: str,
    component: object,
    current_net: str,
    array_mappings: ArrayMappingLibrary,
) -> tuple[str, str] | None:
    if array_mappings.is_empty:
        return None

    mapping = array_mappings.find(_component_part(component), refdes=refdes)
    if mapping is None:
        return None

    pins_by_name = _component_pins_by_name(edb, refdes)
    candidates: list[tuple[str, str]] = []
    for pin_name, pin in pins_by_name.items():
        if _pin_net(pin) != current_net:
            continue
        paired_pin_name = mapping.paired_pin(pin_name)
        if paired_pin_name is None:
            continue
        paired_pin = pins_by_name.get(paired_pin_name)
        if paired_pin is None:
            continue
        next_net = _pin_net(paired_pin)
        if next_net and next_net != current_net:
            candidates.append((paired_pin_name, next_net))

    if len(candidates) != 1:
        return None
    return candidates[0]


def _explicit_endpoint_path(
    edb: object,
    target: ChannelTarget,
    *,
    polarity: str,
    start_net: str,
    endpoint_pin: object,
    endpoint_pin_name: str,
    array_mappings: ArrayMappingLibrary,
    max_depth: int,
) -> PolarityPath | None:
    endpoint_net = _pin_net(endpoint_pin)
    if not endpoint_net:
        return None

    start_pin = target.pos_pin if polarity == "positive" else target.neg_pin
    start_step = PathStep(kind="start", net=start_net, component=target.ic_refdes, action=f"pin {start_pin}")
    queue: list[tuple[str, str, set[str], list[PathStep]]] = [
        (start_net, target.ic_refdes, {start_net}, [start_step])
    ]

    while queue:
        current_net, previous_component, visited_nets, steps = queue.pop(0)
        if current_net == endpoint_net:
            start_port_name, end_port_name = _port_names_for_target(
                target,
                polarity,
                start_net,
                current_net,
                explicit_endpoint=True,
            )
            return PolarityPath(
                channel=target.name,
                polarity=polarity,
                start_component=target.ic_refdes,
                start_pin=start_pin,
                start_net=start_net,
                start_port_name=start_port_name,
                status="resolved_by_explicit_endpoint",
                end_net=current_net,
                end_port_name=end_port_name,
                endpoint_component=target.endpoint_refdes,
                endpoint_pin=endpoint_pin_name,
                endpoint_type=None,
                steps=steps
                + [
                    PathStep(
                        kind="endpoint",
                        net=current_net,
                        component=target.endpoint_refdes,
                        action=f"stop at pin {endpoint_pin_name}",
                    )
                ],
            )

        traversal_count = sum(1 for step in steps if step.kind.startswith("series_") or step.kind == "array_component")
        if traversal_count >= max_depth:
            continue

        try:
            components = _net_components(edb, current_net)
        except LookupError:
            continue

        candidates: list[tuple[str, object, str, str, str]] = []
        for name, component in sorted(components.items(), key=lambda item: item[0]):
            if name == previous_component:
                continue

            passive_kind = _series_passive_kind(component)
            if passive_kind:
                next_net = _other_net(component, current_net)
                if next_net and next_net not in visited_nets:
                    if next_net == endpoint_net or not _is_reference_or_power_net(next_net):
                        candidates.append((name, component, next_net, passive_kind, f"short_to {next_net}"))
                continue

            array_candidate = _array_mapping_candidate(edb, name, component, current_net, array_mappings)
            if array_candidate is None:
                continue
            paired_pin, next_net = array_candidate
            if next_net not in visited_nets and (next_net == endpoint_net or not _is_reference_or_power_net(next_net)):
                candidates.append((name, component, next_net, "array_component", f"map_pin {paired_pin} short_to {next_net}"))

        for component_name, component, next_net, candidate_kind, action in candidates:
            queue.append(
                (
                    next_net,
                    component_name,
                    visited_nets | {next_net},
                    steps
                    + [
                        PathStep(
                            kind=candidate_kind,
                            net=current_net,
                            component=component_name,
                            component_type=_component_type(component),
                            part_name=_component_part(component),
                            action=action,
                        ),
                        PathStep(kind="net", net=next_net, action="arrived"),
                    ],
                )
            )

    return None


def _auto_endpoint_path(
    edb: object,
    target: ChannelTarget,
    *,
    polarity: str,
    start_net: str,
    endpoint_prefixes: tuple[str, ...],
    array_mappings: ArrayMappingLibrary,
    max_depth: int,
) -> PolarityPath:
    start_pin = target.pos_pin if polarity == "positive" else target.neg_pin
    start_step = PathStep(kind="start", net=start_net, component=target.ic_refdes, action=f"pin {start_pin}")
    queue: list[tuple[str, str, set[str], list[PathStep]]] = [
        (start_net, target.ic_refdes, {start_net}, [start_step])
    ]
    endpoint_candidates: list[tuple[dict[str, str | None], str, str | None, object, list[PathStep]]] = []
    endpoint_candidate_keys: set[tuple[str, str | None, str]] = set()

    def add_endpoint_candidate(
        candidate: tuple[dict[str, str | None], str, str | None, object, list[PathStep]]
    ) -> None:
        record, endpoint_name, endpoint_pin, _component, endpoint_steps = candidate
        key = (endpoint_name, endpoint_pin, str(endpoint_steps[-1].net))
        if key in endpoint_candidate_keys:
            return
        endpoint_candidate_keys.add(key)
        endpoint_candidates.append(candidate)

    while queue:
        current_net, previous_component, visited_nets, steps = queue.pop(0)
        traversal_count = sum(1 for step in steps if step.kind.startswith("series_") or step.kind == "array_component")

        try:
            components = _net_components(edb, current_net)
        except LookupError:
            continue

        for name, component in sorted(components.items(), key=lambda item: item[0]):
            if name == previous_component:
                continue
            if not _is_auto_endpoint_component(name, component, endpoint_prefixes):
                continue
            endpoint_pin = _component_pin_for_net(edb, name, current_net)
            endpoint_steps = steps + [
                PathStep(
                    kind="endpoint",
                    net=current_net,
                    component=name,
                    component_type=_component_type(component),
                    part_name=_component_part(component),
                    action=f"stop at pin {endpoint_pin}" if endpoint_pin else "stop",
                )
            ]
            add_endpoint_candidate(
                (
                    _endpoint_candidate_record(
                        component_name=name,
                        component=component,
                        pin_name=endpoint_pin,
                        net_name=current_net,
                        steps=steps,
                    ),
                    name,
                    endpoint_pin,
                    component,
                    endpoint_steps,
                )
            )

        for candidate in _same_net_pin_endpoint_candidates(
            edb,
            current_net=current_net,
            previous_component=previous_component,
            endpoint_prefixes=endpoint_prefixes,
            steps=steps,
        ):
            add_endpoint_candidate(candidate)

        if traversal_count >= max_depth:
            continue

        for name, component in sorted(components.items(), key=lambda item: item[0]):
            if name == previous_component:
                continue

            passive_kind = _series_passive_kind(component)
            if passive_kind:
                next_net = _other_net(component, current_net)
                if next_net and next_net not in visited_nets and _can_auto_traverse_net(next_net):
                    queue.append(
                        (
                            next_net,
                            name,
                            visited_nets | {next_net},
                            steps
                            + [
                                PathStep(
                                    kind=passive_kind,
                                    net=current_net,
                                    component=name,
                                    component_type=_component_type(component),
                                    part_name=_component_part(component),
                                    action=f"short_to {next_net}",
                                ),
                                PathStep(kind="net", net=next_net, action="arrived"),
                            ],
                        )
                    )
                continue

            array_candidate = _array_mapping_candidate(edb, name, component, current_net, array_mappings)
            if array_candidate is None:
                continue
            paired_pin, next_net = array_candidate
            if next_net not in visited_nets and _can_auto_traverse_net(next_net):
                queue.append(
                    (
                        next_net,
                        name,
                        visited_nets | {next_net},
                        steps
                        + [
                            PathStep(
                                kind="array_component",
                                net=current_net,
                                component=name,
                                component_type=_component_type(component),
                                part_name=_component_part(component),
                                action=f"map_pin {paired_pin} short_to {next_net}",
                            ),
                            PathStep(kind="net", net=next_net, action="arrived"),
                        ],
                    )
                )

    candidate_records = [candidate[0] for candidate in endpoint_candidates]
    if len(endpoint_candidates) != 1:
        reason = "dropped_no_endpoint_candidate" if not endpoint_candidates else "dropped_multiple_endpoint_candidates"
        return PolarityPath(
            channel=target.name,
            polarity=polarity,
            start_component=target.ic_refdes,
            start_pin=start_pin,
            start_net=start_net,
            start_port_name=start_net,
            status=reason,
            end_net=None,
            end_port_name=None,
            endpoint_component=None,
            endpoint_pin=None,
            endpoint_type=None,
            steps=[start_step],
            endpoint_candidates=candidate_records,
            error=f"auto endpoint discovery found {len(endpoint_candidates)} candidate(s)",
        )

    _record, endpoint_name, endpoint_pin, endpoint, endpoint_steps = endpoint_candidates[0]
    end_net = str(endpoint_steps[-1].net)
    start_port_name, end_port_name = _port_names_for_target(
        target,
        polarity,
        start_net,
        end_net,
        explicit_endpoint=False,
    )
    return PolarityPath(
        channel=target.name,
        polarity=polarity,
        start_component=target.ic_refdes,
        start_pin=start_pin,
        start_net=start_net,
        start_port_name=start_port_name,
        status="resolved_by_auto_candidate",
        end_net=end_net,
        end_port_name=end_port_name,
        endpoint_component=endpoint_name,
        endpoint_pin=endpoint_pin,
        endpoint_type=_component_type(endpoint),
        steps=endpoint_steps,
        endpoint_candidates=candidate_records,
    )


def _trace_polarity(
    edb: object,
    target: ChannelTarget,
    *,
    polarity: str,
    endpoint_prefixes: tuple[str, ...],
    array_mappings: ArrayMappingLibrary,
    max_depth: int,
) -> PolarityPath:
    start_pin = target.pos_pin if polarity == "positive" else target.neg_pin
    steps: list[PathStep] = []
    try:
        current_net = _start_net(edb, target, polarity)
    except Exception as exc:
        return PolarityPath(
            channel=target.name,
            polarity=polarity,
            start_component=target.ic_refdes,
            start_pin=start_pin,
            start_net=None,
            start_port_name=None,
            status="unresolved",
            end_net=None,
            end_port_name=None,
            endpoint_component=None,
            endpoint_pin=None,
            endpoint_type=None,
            steps=[],
            error=f"start pin lookup failed: {type(exc).__name__}: {exc}",
        )

    if current_net is None:
        return PolarityPath(
            channel=target.name,
            polarity=polarity,
            start_component=target.ic_refdes,
            start_pin=start_pin,
            start_net=None,
            start_port_name=None,
            status="unresolved",
            end_net=None,
            end_port_name=None,
            endpoint_component=None,
            endpoint_pin=None,
            endpoint_type=None,
            steps=[],
            error="start pin has no net",
        )

    steps.append(PathStep(kind="start", net=current_net, component=target.ic_refdes, action=f"pin {start_pin}"))

    try:
        explicit_endpoint = _explicit_endpoint_pin(edb, target, polarity)
    except Exception as exc:
        return PolarityPath(
            channel=target.name,
            polarity=polarity,
            start_component=target.ic_refdes,
            start_pin=start_pin,
            start_net=steps[0].net,
            start_port_name=steps[0].net,
            status="unresolved",
            end_net=current_net,
            end_port_name=None,
            endpoint_component=None,
            endpoint_pin=None,
            endpoint_type=None,
            steps=steps,
            error=f"endpoint pin lookup failed: {type(exc).__name__}: {exc}",
        )

    if explicit_endpoint is not None:
        endpoint_pin, endpoint_pin_name = explicit_endpoint
        explicit_path = _explicit_endpoint_path(
            edb,
            target,
            polarity=polarity,
            start_net=current_net,
            endpoint_pin=endpoint_pin,
            endpoint_pin_name=endpoint_pin_name,
            array_mappings=array_mappings,
            max_depth=max_depth,
        )
        if explicit_path is not None:
            return explicit_path
        return PolarityPath(
            channel=target.name,
            polarity=polarity,
            start_component=target.ic_refdes,
            start_pin=start_pin,
            start_net=current_net,
            start_port_name=current_net,
            status="dropped_explicit_endpoint_not_reached",
            end_net=None,
            end_port_name=None,
            endpoint_component=target.endpoint_refdes,
            endpoint_pin=endpoint_pin_name,
            endpoint_type=None,
            steps=steps,
            error="explicit endpoint pin was provided but no allowed series path reached it",
        )

    return _auto_endpoint_path(
        edb,
        target,
        polarity=polarity,
        start_net=current_net,
        endpoint_prefixes=endpoint_prefixes,
        array_mappings=array_mappings,
        max_depth=max_depth,
    )


def trace_channel_paths(
    aedb_path: Path,
    targets: list[ChannelTarget],
    *,
    aedt_version: str = "2024.2",
    endpoint_prefixes: tuple[str, ...] = DEFAULT_ENDPOINT_PREFIXES,
    array_mapping_path: Path | None = None,
    max_depth: int = 8,
) -> ChannelPathReport:
    # Validate an explicitly selected library before importing/opening PyEDB so
    # schema/version errors do not consume an Ansys license or start analysis.
    array_mappings = load_array_mapping_library(array_mapping_path)
    _ensure_pyedb_path()
    from pyedb import Edb  # noqa: PLC0415

    edb = Edb(edbpath=str(aedb_path), edbversion=aedt_version)
    paths: list[PolarityPath] = []
    try:
        for target in targets:
            direction = target.direction_resolution
            paths.append(
                annotate_resolved_path_routing_length(
                    edb,
                    replace(
                        _trace_polarity(
                            edb,
                            target,
                            polarity="positive",
                            endpoint_prefixes=endpoint_prefixes,
                            array_mappings=array_mappings,
                            max_depth=max_depth,
                        ),
                        measurement_direction=direction.value,
                        measurement_direction_source=direction.source,
                    ),
                )
            )
            paths.append(
                annotate_resolved_path_routing_length(
                    edb,
                    replace(
                        _trace_polarity(
                            edb,
                            target,
                            polarity="negative",
                            endpoint_prefixes=endpoint_prefixes,
                            array_mappings=array_mappings,
                            max_depth=max_depth,
                        ),
                        measurement_direction=direction.value,
                        measurement_direction_source=direction.source,
                    ),
                )
            )
    finally:
        edb.close_edb()

    return ChannelPathReport(
        aedb=str(aedb_path),
        aedt_version=aedt_version,
        paths=paths,
    )


def write_channel_path_report(
    aedb_path: Path,
    targets: list[ChannelTarget],
    output_path: Path,
    *,
    aedt_version: str = "2024.2",
    array_mapping_path: Path | None = None,
    max_depth: int = 8,
) -> ChannelPathReport:
    report = trace_channel_paths(
        aedb_path,
        targets,
        aedt_version=aedt_version,
        array_mapping_path=array_mapping_path,
        max_depth=max_depth,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(report.to_dict(), fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return report
