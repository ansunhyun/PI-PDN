"""Common web report structure shared with the DCIR result-detail contract."""
from __future__ import annotations

import copy
from pathlib import Path, PureWindowsPath

from core.aedt_version import normalize_aedt_version


ARTIFACT_FIELDS = (
    "Impedance_Plot", "Impedance_CSV", "Z_Parameter_Plot",
    "Z_Parameter_CSV", "Touchstone", "FitView", "ZoomView", "zPlot",
)


def artifact_filename(value):
    return PureWindowsPath(str(value)).name if value else ""


def _absolute_artifact_path(value, output_dir):
    if not value:
        return ""
    path = Path(str(value))
    if path.is_absolute():
        return str(path)
    windows_path = PureWindowsPath(str(value))
    if windows_path.is_absolute():
        return str(windows_path)
    return str((Path(output_dir) / path).resolve())


def _pdn_result_details(case, output_dir):
    output_directory = str(Path(output_dir).resolve())
    previous = case.get("PDN_Result", {})
    previous_files = previous.get("Files", {})
    files = {}
    for key in ARTIFACT_FIELDS:
        value = case.get(key)
        previous_path = previous_files.get(key, "")
        if (value and previous_path and previous.get("Output_Directory") == output_directory
                and str(value) == artifact_filename(previous_path)):
            files[key] = previous_path
        else:
            files[key] = _absolute_artifact_path(value, output_dir)
    return {
        "Output_Directory": output_directory,
        "Status": case.get("Status", "Pending"),
        "Error": case.get("Error", ""),
        "Report_Type": case.get("Report_Type", "Z-Parameter Plot"),
        "Z_Parameter": case.get("Z_Parameter", "Z(1,1)"),
        "Files": files,
        "Peak_Impedance_Ohm": case.get("Peak_Impedance_Ohm"),
        "Peak_Frequency_Hz": case.get("Peak_Frequency_Hz"),
        "Plot_Evidence": copy.deepcopy(case.get("Plot_Evidence", {})),
    }


def build_pdn_tools(conf, *, stage="POST", code_version="", backend=None):
    pdn = conf.get("PDN", {})
    backend = backend or pdn.get("runtime", {}).get("solver_backend", "siwave")
    return [{
        "comp": "ANSYS",
        "name": "AEDT-HFSS3DLayout" if backend == "aedt_cutout" else "SIWave",
        "stage": stage,
        "requestedVersionDisplay": normalize_aedt_version(pdn["version"]) if pdn.get("version") else "-",
        # A configured version does not establish the installed patch version.
        "actualVersionDisplay": "-",
        "codeVersions": code_version,
    }]


def normalize_result_detail(payload, *, setting=None, request=None, result=None, tools=None, output_dir=None):
    """Keep PDN measurements and diagnostics while aligning public metadata."""
    setting, request, result = setting or {}, request or {}, result or {}
    payload.setdefault("stackup", setting.get("stackup"))
    payload["stackup"] = artifact_filename(payload["stackup"]) if payload["stackup"] else None
    if tools is not None:
        payload["tools"] = copy.deepcopy(tools)
    else:
        payload.setdefault("tools", [])
    if not isinstance(payload.get("Image"), dict):
        payload["Image"] = copy.deepcopy(request.get("Image") or {})
    for key in ("pcbTopImage", "pcbBtmImage"):
        value = payload["Image"].get(key)
        payload["Image"][key] = artifact_filename(value) if value else None
    if not isinstance(payload.get("simSchedule"), dict):
        payload["simSchedule"] = copy.deepcopy(result.get("simSchedule") or {})
    for key in ("startDate", "endData"):
        payload["simSchedule"].setdefault(key, None)
    payload.setdefault("result", [])
    for case in payload["result"]:
        if not isinstance(case, dict):
            continue
        case.pop("setting", None)
        if output_dir is not None:
            case["PDN_Result"] = _pdn_result_details(case, output_dir)
        for key in ARTIFACT_FIELDS:
            if key in case:
                case[key] = artifact_filename(case[key])
        edb_net = str(case.get("EDB_Target_Net") or case.get("Net") or "").strip()
        case["EDB_Target_Net"] = edb_net
        if edb_net.upper().startswith("SIGN") and case.get("Display_Net"):
            case["Net"] = case["Display_Net"]
    return payload
