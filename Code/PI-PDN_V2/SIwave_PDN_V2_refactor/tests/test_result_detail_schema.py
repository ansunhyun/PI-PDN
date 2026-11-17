import ast
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from core.logger import LogLevel
from core.post_processing import PostProcessing
from core.post_stage import PostStageState, append_post_detail
from core.result_detail import build_pdn_tools, normalize_result_detail


def test_common_metadata_and_pdn_measurements_survive_normalization():
    raw = {"result": [{
        "IC": "U1", "Net": "SIGN001", "Display_Net": "+0.9V",
        "Impedance_CSV": r"C:\job\outputs\Z.csv",
        "FitView": Path("outputs/fit.jpg"), "Peak_Impedance_Ohm": 0.533462,
        "Peak_Frequency_Hz": 1e9, "setting": {"old": True},
        "Result_Folder": r"C:\job\case.siwaveresults",
    }], "postInfo": {"resultBasis": "latest_completed_local_run"}}
    schedule = {"startDate": "start", "endData": "end"}
    tools = build_pdn_tools({"PDN": {"version": "2025 R2"}}, code_version="1.4")
    normalize_result_detail(
        raw, setting={"stackup": r"C:\job\stackup.xml"},
        request={"Image": {"pcbTopImage": "top.png", "pcbBtmImage": "btm.png"}},
        result={"simSchedule": schedule}, tools=tools,
    )
    assert set(raw) == {"stackup", "tools", "Image", "simSchedule", "result", "postInfo"}
    case = raw["result"][0]
    assert case["Impedance_CSV"] == "Z.csv" and case["FitView"] == "fit.jpg"
    assert case["Net"] == "+0.9V" and case["EDB_Target_Net"] == "SIGN001"
    assert case["Peak_Impedance_Ohm"] == 0.533462 and case["Peak_Frequency_Hz"] == 1e9
    assert case["Result_Folder"] == r"C:\job\case.siwaveresults"
    assert "setting" not in case and raw["stackup"] == "stackup.xml"
    tools[0]["name"] = "changed"
    schedule["endData"] = "changed"
    assert raw["tools"][0]["name"] == "SIWave" and raw["simSchedule"]["endData"] == "end"
    before = copy.deepcopy(raw)
    normalize_result_detail(raw)
    assert raw == before


def test_legacy_post_detail_recovers_metadata_and_keeps_verified_file_names(tmp_path):
    (tmp_path / "result_detail.json").write_text(json.dumps({"result": [{"Net": "VDD"}]}))
    (tmp_path / "setting.json").write_text(json.dumps({"stackup": "stackup.xml"}))
    (tmp_path / "request.json").write_text(json.dumps({"Image": {"pcbTopImage": "top.png"}}))
    (tmp_path / "result.json").write_text(json.dumps({"simSchedule": {"startDate": "a", "endData": "b"}}))
    state = PostStageState(
        summary=[{"Status": "Complete", "Impedance_CSV": tmp_path / "Z.csv",
                  "Touchstone": tmp_path / "case.s1p", "Peak_Impedance_Ohm": 0.5}],
        change_history=[], gnd_net="GND", analysis_start="a", analysis_end="b",
    )
    append_post_detail(tmp_path, state)
    payload = json.loads((tmp_path / "result_detail.json").read_text())
    assert payload["simSchedule"] == {"startDate": "a", "endData": "b"}
    assert payload["stackup"] == "stackup.xml" and payload["tools"] == []
    assert payload["Image"] == {"pcbTopImage": "top.png", "pcbBtmImage": None}
    assert payload["result"][0]["Impedance_CSV"] == "Z.csv"
    assert payload["result"][0]["Touchstone"] == "case.s1p"
    assert payload["result"][0]["Peak_Impedance_Ohm"] == 0.5


def test_siwave_export_populates_common_detail_from_public_reports(tmp_path):
    processor = PostProcessing.__new__(PostProcessing)
    processor._outputFolder = tmp_path
    processor._logger = SimpleNamespace(log=lambda *a, **k: None)
    processor._setting = {
        "Request": {"Model": "T6", "Year": "2026", "Start_date": "a", "Target_date": "b", "Event": "test"},
        "CAE": {"PCB": {"Rev": "1", "PN": "PCB", "Stackup": Path("board.stk"), "BOM": Path("bom.csv")},
                "SOC": {"Name": "W26"}, "Purpose": "PDN"},
    }
    processor._conf = {"PDN": {"version": "2025.2"}}
    processor._summary = [{"IC": "U1", "Net": "VDD", "is_done": True, "Impedance_CSV": tmp_path / "Z.csv"}]
    processor._exportData = {
        "title": {}, "request": {"modelInfo": {}, "requestData": {}, "Image": {}},
        "result": {"simSchedule": {}}, "setting": {"tool": {}},
        "result_detail": {"tools": build_pdn_tools(processor._conf)},
    }
    processor.set_PDN_results("start", "end")
    detail = processor._exportData["result_detail"]
    assert detail["stackup"] == "stackup.xml"
    assert detail["Image"] == {"pcbTopImage": "top.png", "pcbBtmImage": "btm.png"}
    assert detail["simSchedule"] == {"startDate": "start", "endData": "end"}
    assert detail["result"][0]["Impedance_CSV"] == "Z.csv"


def test_aedt_export_uses_same_common_detail_contract(tmp_path):
    # Load only the exporter; importing the legacy runner launches the pipeline.
    source = Path(__file__).parents[1] / "legacy_pipeline.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "export_aedt_cutout_post_reports")
    namespace = {
        "Path": Path, "Logger": object, "json": json, "time": time, "datetime": datetime,
        "LogLevel": LogLevel, "VERSION": "1.4", "build_pdn_tools": build_pdn_tools,
        "normalize_result_detail": normalize_result_detail,
        "conf_manager": SimpleNamespace(data={"PDN": {"version": "2025.2"}}),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    (tmp_path / "preprocessing_result.json").write_text(json.dumps([{"Case_Index": 1, "Target_Net": "VDD"}]))
    (tmp_path / "aedt_cutout_result.json").write_text(json.dumps({"Records": [{"Case_Index": 1, "Status": "Done"}]}))
    namespace["export_aedt_cutout_post_reports"](tmp_path, SimpleNamespace(log=lambda *a, **k: None))
    detail = json.loads((tmp_path / "result_detail.json").read_text())
    assert {"stackup", "tools", "Image", "simSchedule", "result", "postInfo"} <= set(detail)
    assert detail["tools"][0]["name"] == "AEDT-HFSS3DLayout"
    assert detail["tools"][0]["requestedVersionDisplay"] == "2025.2"
    assert detail["result"][0]["Status"] == "Complete"


def test_pdn_details_include_absolute_files_without_changing_public_names(tmp_path):
    payload = {"result": [{
        "Net": "VDD", "Status": "Complete", "Impedance_CSV": "Z_Param_U1_VDD.csv",
        "Impedance_Plot": tmp_path / "Z_Param_U1_VDD.jpg",
        "Touchstone": r"C:\verified\case.s1p", "FitView": "fit.jpg", "ZoomView": "zoom.jpg",
        "Peak_Impedance_Ohm": 0.533462, "Peak_Frequency_Hz": 1e9,
        "Plot_Evidence": {"csvRows": 935},
    }]}
    normalize_result_detail(payload, output_dir=tmp_path)
    case = payload["result"][0]
    detail = case["PDN_Result"]
    assert detail["Files"]["Impedance_CSV"] == str(tmp_path / "Z_Param_U1_VDD.csv")
    assert detail["Files"]["Impedance_Plot"] == str(tmp_path / "Z_Param_U1_VDD.jpg")
    assert detail["Files"]["Touchstone"] == r"C:\verified\case.s1p"
    assert detail["Files"]["FitView"] == str(tmp_path / "fit.jpg")
    assert detail["Files"]["ZoomView"] == str(tmp_path / "zoom.jpg")
    assert case["Impedance_CSV"] == "Z_Param_U1_VDD.csv"
    assert detail["Peak_Impedance_Ohm"] == 0.533462 and detail["Plot_Evidence"]["csvRows"] == 935
    case["Impedance_CSV"] = "updated.csv"
    case["Peak_Impedance_Ohm"] = 0.7
    normalize_result_detail(payload, output_dir=tmp_path)
    assert case["PDN_Result"]["Files"]["Impedance_CSV"] == str(tmp_path / "updated.csv")
    assert case["PDN_Result"]["Peak_Impedance_Ohm"] == 0.7
    assert case["PDN_Result"]["Files"]["Touchstone"] == r"C:\verified\case.s1p"


def test_failed_pdn_case_does_not_invent_results(tmp_path):
    payload = {"result": [{"Net": "VDD", "Status": "Error", "Error": "solve failed"}]}
    normalize_result_detail(payload, output_dir=tmp_path)
    detail = payload["result"][0]["PDN_Result"]
    assert detail["Status"] == "Error" and detail["Error"] == "solve failed"
    assert all(value == "" for value in detail["Files"].values())
    assert detail["Peak_Impedance_Ohm"] is None and detail["Peak_Frequency_Hz"] is None
