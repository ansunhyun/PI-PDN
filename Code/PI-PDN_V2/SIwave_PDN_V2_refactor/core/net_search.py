from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


def _name(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return "".join(ch for ch in _name(value).upper() if ch.isalnum())


@dataclass
class PowerPathResult:
    target_net: str
    full_net_chain: list[str] = field(default_factory=list)
    component_chain: list[str] = field(default_factory=list)
    analysis_net_chain: list[str] = field(default_factory=list)
    selected_path_components: list[str] = field(default_factory=list)
    source_component: str = ""
    source_pin: str = ""
    boundary_component: str = ""
    boundary_type: str = ""
    boundary_soc_pin: str = ""
    boundary_soc_net: str = ""
    boundary_source_pin: str = ""
    boundary_source_net: str = ""
    status: str = "failed"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pin_name(comp: Any, pin: Any) -> str:
    for key, value in (getattr(comp, "pins", {}) or {}).items():
        if value is pin:
            return _name(key)
    return _name(getattr(pin, "name", ""))


def _component_map(edb: Any) -> dict[str, Any]:
    components = getattr(getattr(edb, "_components", None), "components", None)
    if components is None:
        components = getattr(getattr(edb, "components", None), "components", {})
    return dict(components or {})


def _component_nets(comp: Any) -> dict[str, Any]:
    result = {}
    for pin in (getattr(comp, "pins", {}) or {}).values():
        net = _name(getattr(pin, "net_name", ""))
        if net and net not in result:
            result[net] = pin
    return result


def _ordered_chain(target_net: str, discovered_chain: Iterable[str]) -> list[str]:
    target = _name(target_net)
    legacy = [_name(n) for n in (discovered_chain or []) if _name(n)]
    if legacy and legacy[-1] == target:
        legacy.reverse()
    elif target in legacy and legacy[0] != target:
        idx = legacy.index(target)
        forward = legacy[idx:]
        backward = list(reversed(legacy[: idx + 1]))
        legacy = forward if len(forward) >= len(backward) else backward
    raw = []
    for net in [target, *legacy]:
        if net and net not in raw:
            raw.append(net)
    return raw


def build_power_path_result(
    edb: Any,
    *,
    target_net: str,
    discovered_chain: Iterable[str],
    source_component: Any = None,
    source_pin: str = "",
    inductor_designators: Iterable[str] = (),
    switch_boundary_designators: Iterable[str] = (),
    inductor_prefixes: Iterable[str] = ("L", "B", "FB"),
) -> PowerPathResult:
    """Build a persisted SoC-to-source path and select its source boundary.

    FET/analog-switch components are hard PDN boundaries: terminate on the
    SoC-side pin and leave every other net connected to the device open.  When
    no such device is present, retain the legacy source-side inductor policy.
    """
    chain = _ordered_chain(target_net, discovered_chain)
    result = PowerPathResult(
        target_net=_name(target_net),
        full_net_chain=chain,
        source_component=_name(getattr(source_component, "name", source_component)),
        source_pin=_name(source_pin),
    )
    if not chain:
        result.reason = "empty net chain"
        return result

    designators = {_norm(v) for v in (inductor_designators or []) if _name(v)}
    switch_boundaries = {
        _norm(v) for v in (switch_boundary_designators or []) if _name(v)
    }
    source_nets = _component_nets(source_component) if source_component is not None else {}
    source_verified = bool(
        result.source_component
        and chain
        and chain[-1] in source_nets
    )
    prefixes = tuple(_name(v).upper() for v in (inductor_prefixes or []) if _name(v))

    def is_inductor(comp_name: str) -> bool:
        return _norm(comp_name) in designators or bool(prefixes and comp_name.upper().startswith(prefixes))

    bridges = []
    for left, right in zip(chain, chain[1:]):
        matches = []
        for comp_name, comp in _component_map(edb).items():
            nets = _component_nets(comp)
            if left in nets and right in nets:
                matches.append((_name(comp_name), comp, nets[left], nets[right]))
        if not matches:
            result.reason = f"no component bridge: {left} -> {right}"
            return result
        # Parallel decaps/resistors can touch the same pair.  Prefer the BOM or
        # designator-classified series inductor, then keep deterministic order.
        matches.sort(
            key=lambda row: (
                _norm(row[0]) not in switch_boundaries,
                not is_inductor(row[0]),
                row[0].upper(),
            )
        )
        bridge = matches[0]
        bridges.append(bridge)
        result.component_chain.append(bridge[0])

    boundary_index = None
    switch_index = next(
        (
            idx
            for idx, (comp_name, _comp, _soc_pin, _source_side_pin) in enumerate(bridges)
            if _norm(comp_name) in switch_boundaries
        ),
        None,
    )
    # A semiconductor power switch is a valid AC PDN boundary only after the
    # complete upstream path has been proven to end on the selected DCDC/LDO.
    if switch_index is not None and not source_verified:
        result.reason = "power switch found but path end is not a verified power IC"
        return result
    if switch_index is not None:
        comp_name, comp, soc_pin, source_side_pin = bridges[switch_index]
        boundary_index = switch_index
        result.boundary_component = comp_name
        result.boundary_type = "switch"
        result.boundary_soc_pin = _pin_name(comp, soc_pin)
        result.boundary_soc_net = _name(getattr(soc_pin, "net_name", ""))
        result.boundary_source_pin = _pin_name(comp, source_side_pin)
        result.boundary_source_net = _name(getattr(source_side_pin, "net_name", ""))

    # The boundary is the source-side inductor nearest the selected DCDC/LDO,
    # not the first branch inductor encountered from the SoC.
    if boundary_index is None:
        for idx in range(len(bridges) - 1, -1, -1):
            comp_name, comp, soc_pin, source_side_pin = bridges[idx]
            if not is_inductor(comp_name):
                continue
            boundary_index = idx
            result.boundary_component = comp_name
            result.boundary_type = "inductor"
            result.boundary_soc_pin = _pin_name(comp, soc_pin)
            result.boundary_soc_net = _name(getattr(soc_pin, "net_name", ""))
            result.boundary_source_pin = _pin_name(comp, source_side_pin)
            result.boundary_source_net = _name(getattr(source_side_pin, "net_name", ""))
            break

    if boundary_index is None:
        # A direct LDO output can legitimately have no series inductor.  The
        # source output net itself becomes the common termination plane.
        if result.source_component and len(chain) == 1:
            result.analysis_net_chain = list(chain)
            result.status = "ok"
            result.reason = "direct source output; terminate on target plane"
            return result
        result.reason = "no series inductor on discovered path"
        return result
    result.analysis_net_chain = chain[: boundary_index + 1]
    result.selected_path_components = [row[0] for row in bridges[:boundary_index]]
    result.status = "ok"
    if result.boundary_type == "switch":
        result.reason = "power switch boundary selected; other connected nets open"
    else:
        result.reason = "source-side power inductor selected; upstream source excluded"
    return result

