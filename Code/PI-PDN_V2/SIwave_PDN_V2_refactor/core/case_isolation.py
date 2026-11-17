from __future__ import annotations

import re


def _token(value) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")


def _display_net(case) -> str:
    return str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))) or "")


def _common_plane(case) -> str:
    plane = str(case.get("Boundary_SOC_Net", "") or "").strip()
    chain = case.get("Analysis_Net_Chain", []) or []
    return plane or (str(chain[-1]).strip() if chain else str(case.get("Net", "") or "").strip())


def _passive_element_type(name: str):
    prefix = str(name or "").strip().upper()[:1]
    return {"C": "cap", "L": "ind", "R": "res"}.get(prefix)


def build_case_isolation_plans(cases, *, port_prefix="PORT_", vrm_prefix="Rvrm_"):
    """Return deterministic per-target topology plans for independent solves."""
    cases = list(cases or [])
    paths_by_plane = {}
    for case in cases:
        plane_paths = paths_by_plane.setdefault(_common_plane(case), set())
        plane_paths.update(
            str(name).strip()
            for name in (case.get("Selected_Path_Components", []) or [])
            if str(name).strip()
        )
    all_ports = {
        f"{port_prefix}{_token(case.get('IC', ''))}_{_token(_display_net(case))}" for case in cases
    }
    all_vrms = {f"{vrm_prefix}{_token(_common_plane(case))}" for case in cases}
    plans = []
    for index, case in enumerate(cases):
        selected = {str(name).strip() for name in (case.get("Selected_Path_Components", []) or []) if str(name).strip()}
        keep_port = f"{port_prefix}{_token(case.get('IC', ''))}_{_token(_display_net(case))}"
        keep_vrm = f"{vrm_prefix}{_token(_common_plane(case))}"
        source_boundary = str(case.get("Boundary_Component", "") or "").strip()
        boundary_type = str(case.get("Boundary_Type", "") or "").strip().lower()
        scope_open_boundary = boundary_type in {"switch", "fet", "tr", "analogswitch", "analog_switch"}
        # Only sibling branches fed by the same common plane are stubs for this
        # case.  Components on unrelated rails are excluded by selected nets.
        remove_branches = paths_by_plane.get(_common_plane(case), set()) - selected
        remove_elements = (
            remove_branches
            | (all_ports - {keep_port})
            | (all_vrms - {keep_vrm})
            | ({source_boundary} if source_boundary and not scope_open_boundary else set())
        )
        remove_element_types = {}
        for name in remove_elements:
            if name in all_ports:
                remove_element_types[name] = "port"
            elif name in all_vrms:
                remove_element_types[name] = "res"
            else:
                element_type = _passive_element_type(name)
                if element_type:
                    remove_element_types[name] = element_type
        plans.append({
            "index": index,
            "case": case,
            "display_net": _display_net(case),
            "common_plane_net": _common_plane(case),
            "selected_path_components": selected,
            "remove_branches": remove_branches,
            "keep_port": keep_port,
            "keep_vrm": keep_vrm,
            "source_boundary": source_boundary,
            "scope_open_boundaries": {source_boundary} if source_boundary and scope_open_boundary else set(),
            "remove_elements": remove_elements,
            "remove_element_types": remove_element_types,
        })
    return plans

