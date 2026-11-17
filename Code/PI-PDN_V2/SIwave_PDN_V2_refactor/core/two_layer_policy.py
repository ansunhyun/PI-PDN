from __future__ import annotations

from core.case_session import detect_system_cpu_counts, resolve_solver_cpu_count


def resolve_two_layer_3d_policy(layer_count: int, setup_config=None) -> dict:
    """Resolve the strict, case-local 2-layer HFSS 3D Layout policy."""
    if int(layer_count or 0) != 2:
        raise ValueError(f"HFSS 3D cutout requires exactly 2 conductor layers: {layer_count}")
    setup = dict(setup_config or {})
    cut = dict(setup.get("aedtCutout", {}) or {})
    ng = dict(setup.get("ngSolve", {}) or {})
    inventory = detect_system_cpu_counts()
    cores = resolve_solver_cpu_count(inventory["physical"], ng)
    extent = str(cut.get("extent_type", "Bounding") or "Bounding")
    if extent not in {"Bounding", "ConvexHull", "Conforming"}:
        raise ValueError(f"Unsupported 2-layer cutout extent_type: {extent}")
    return {
        "solver": "HFSS3DLayout",
        "conductorLayers": 2,
        "extentType": extent,
        "expansionSizeM": max(0.0, float(cut.get("expansion_size", 0.005) or 0.005)),
        "signalPolicy": "case-path-signals-only",
        "referencePolicy": "ground-only",
        "includePingroups": bool(cut.get("include_pingroups", False)),
        "checkTerminals": bool(cut.get("check_terminals", False)),
        "preserveComponentsWithModel": bool(cut.get("preserve_components_with_model", False)),
        "nonGraphical": bool(cut.get("non_graphical", True)),
        "cutoutThreads": 1,
        "solverPhysicalCores": cores,
        "physicalCores": inventory["physical"],
        "logicalProcessors": inventory["logical"],
    }
