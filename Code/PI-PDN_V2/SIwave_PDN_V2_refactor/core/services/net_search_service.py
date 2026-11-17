from __future__ import annotations

import csv
import json
from pathlib import Path

from core.net_search import PowerPathResult, build_power_path_result


def resolve_case_power_path(*, edb, case, discovered_chain, source_component, source_pin, bom_info, net_search_conf=None):
    conf = dict(net_search_conf or {})
    result = build_power_path_result(
        edb,
        target_net=case.get("Net", ""),
        discovered_chain=discovered_chain,
        source_component=source_component,
        source_pin=source_pin,
        inductor_designators=list(bom_info.get("bulkInd", [])) + list(bom_info.get("beadInd", [])),
        switch_boundary_designators=(
            list(bom_info.get("FET", []))
            + list(bom_info.get("TR", []))
            + list(bom_info.get("analogSwitch", []))
        ),
        inductor_prefixes=conf.get("inductorPrefixes", ["L", "B", "FB"]),
    )
    case.update({
        "Full_Net_Chain": result.full_net_chain,
        "Component_Chain": result.component_chain,
        "Analysis_Net_Chain": result.analysis_net_chain,
        "Selected_Path_Components": result.selected_path_components,
        "Boundary_Component": result.boundary_component,
        "Boundary_Type": result.boundary_type,
        "Boundary_SOC_Pin": result.boundary_soc_pin,
        "Boundary_SOC_Net": result.boundary_soc_net,
        "Boundary_Source_Pin": result.boundary_source_pin,
        "Boundary_Source_Net": result.boundary_source_net,
        "Net_Search_Status": result.status,
        "Net_Search_Reason": result.reason,
    })
    return result


def export_net_search_results(output_dir: Path, cases) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = [{
        "target_net": case.get("Net", ""),
        "full_net_chain": case.get("Full_Net_Chain", []),
        "component_chain": case.get("Component_Chain", []),
        "analysis_net_chain": case.get("Analysis_Net_Chain", []),
        "selected_path_components": case.get("Selected_Path_Components", []),
        "source_component": case.get("Source_name", ""),
        "source_pin": case.get("Source_pin", ""),
        "boundary_component": case.get("Boundary_Component", ""),
        "boundary_type": case.get("Boundary_Type", ""),
        "boundary_soc_pin": case.get("Boundary_SOC_Pin", ""),
        "boundary_soc_net": case.get("Boundary_SOC_Net", ""),
        "boundary_source_pin": case.get("Boundary_Source_Pin", ""),
        "boundary_source_net": case.get("Boundary_Source_Net", ""),
        "status": case.get("Net_Search_Status", "failed"),
        "reason": case.get("Net_Search_Reason", ""),
    } for case in cases]
    json_path = output_dir / "net_search_result.json"
    csv_path = output_dir / "net_search_result.csv"
    with open(json_path, "w", encoding="utf-8") as stream:
        json.dump({"records": records}, stream, indent=2, ensure_ascii=False)
    fields = list(records[0].keys()) if records else list(PowerPathResult("").to_dict().keys())
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({
                **record,
                "full_net_chain": " -> ".join(record["full_net_chain"]),
                "component_chain": " -> ".join(record["component_chain"]),
                "analysis_net_chain": " -> ".join(record["analysis_net_chain"]),
                "selected_path_components": " -> ".join(record["selected_path_components"]),
            })
    return json_path, csv_path

