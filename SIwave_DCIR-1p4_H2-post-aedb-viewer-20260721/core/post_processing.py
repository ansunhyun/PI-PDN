# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import traceback
import time
import os
import vtk
import pyvista as pv
import numpy as np
import ctypes
import copy
import gc  
# import ansys.aedt.core

from core.database import DCIRSessionException, ErrorCode
from core.post_stage import export_post_edb, remove_artifact_path
from datetime import datetime
from pyaedt import Edb, Hfss3dLayout
# from core.SettingsManager import SettingsManager
from ansys.aedt.core.generic.settings import Settings
from EBU_lib.SIwave import SIwave
from core.logger import LogLevel
from pathlib import Path
from tqdm import tqdm


def set_layer_visibility(oEditor, layer, VisFlag=0, pattern=1):
    """Set the visibility and pattern of a specific layer."""
    try:
        oEditor.ChangeLayer(
            [
                "NAME:SLayer",
                "Name:=", layer.name,
                "ID:=", layer.id,
                "Type:=", layer.type,
                "Top Bottom:=", layer.top_bottom,
                "Color:=", (layer.color[0] << 16) | (layer.color[1] << 8) | layer.color[2],
                "Transparency:=", 60,
                "Pattern:=", pattern,
                "VisFlag:=", VisFlag,
                "Locked:=", False,
                "DrawOverride:=", 0,
                "Zones:=", []
            ])
        return True
    except:
        print(traceback.format_exc())
        return False


def set_dcir_sim(h3dl, setup_name, sources, compute_inductance=False, UseDCCustomSettings=False, DCSliderPos=0):
    """Set up the DCIR simulation parameters."""
    try:
        oModule = h3dl.odesign.GetModule("SolveSetups")
        oModule.Edit(setup_name,
                     [
                         "NAME:" + setup_name,
                         [
                             "NAME:Properties",
                             "Enable:=", "true"
                         ],
                         "SolveSetupType:=", "SIwaveDCIR",
                         "SimSetupType:=", "kSIwaveDCIR",
                         [
                             "NAME:SimulationSettings",
                             "Enabled:=", True,
                             "UseSISettings:=", True,
                             "UseCustomSettings:=", False,
                             "SISliderPos:=", 1,
                             "PISliderPos:=", 1,
                             [
                                 "NAME:SIWAdvancedSettings",
                                 "IncludeCoPlaneCoupling:=", True,
                                 "IncludeInterPlaneCoupling:=", False,
                                 "IncludeSplitPlaneCoupling:=", True,
                                 "IncludeFringeCoupling:=", True,
                                 "IncludeTraceCoupling:=", True,
                                 "XtalkThreshold:=", "-34.000000",
                                 "MaxCoupledLines:=", 12,
                                 "MinVoidArea:=", "2.000000mm2",
                                 "MinPadAreaToMesh:=", "84.108700mm2",
                                 "MinPlaneAreaToMesh:=", "0.000148mm2",
                                 "SnapLengthThreshold:=", "0.001000mm",
                                 "MeshAutoMatic:=", True,
                                 "MeshFrequency:=", "4GHz",
                                 "AcDcMergeMode:=", 0,
                                 "ReturnCurrentDistribution:=", False,
                                 "IncludeVISources:=", False,
                                 "IncludeInfGnd:=", False,
                                 "InfGndLocation:=", "0.000000mm",
                                 "PerformERC:=", False,
                                 "IgnoreNonFunctionalPads:=", True
                             ],
                             [
                                 "NAME:SIWDCSettings",
                                 "UseDCCustomSettings:=", UseDCCustomSettings,
                                 "PlotJV:=", True,
                                 "ComputeInductance:=", compute_inductance,
                                 "ContactRadius:=", "0.001mm",
                                 "DCSliderPos:=", DCSliderPos
                             ],
                             [
                                 "NAME:SIWDCAdvancedSettings",
                                 "DcMinPlaneAreaToMesh:=", "4.205430mm2",
                                 "DcMinVoidAreaToMesh:=", "0.002370mm2",
                                 "MaxInitMeshEdgeLength:=", "5.000000mm",
                                 "PerformAdaptiveRefinement:=", False,
                                 "MaxNumPasses:=", 5,
                                 "MinNumPasses:=", 1,
                                 "PercentLocalRefinement:=", 20,
                                 "EnergyError:=", 2,
                                 "MeshBws:=", False,
                                 "RefineBws:=", False,
                                 "MeshVias:=", False,
                                 "RefineVias:=", False,
                                 "NumBwSides:=", 8,
                                 "NumViaSides:=", 8
                             ],
                             [
                                 "NAME:SIWDCIRSettings",
                                 "IcepakTempFile:=", "",
                                 "SourceTermsToGround:=", [f"{sources[0]}:=", 0, f"{sources[1]}:=", 1],
                                 "ExportDCThermalData:=", False,
                                 "ImportThermalData:=", False,
                                 "FullDCReportPath:=", "",
                                 "ViaReportPath:=", "",
                                 "PerPinResPath:=", "",
                                 "DCReportConfigFile:=", "",
                                 "DCReportShowActiveDevices:=", False,
                                 "PerPinUsePinFormat:=", False,
                                 "UseLoopResForPerPin:=", False,
                                 "UseExternalSources:=", False,
                                 "ExternalSourceFilePath:=", ""
                             ]
                         ],
                         [
                             "NAME:SweepDataList"
                         ]
                     ])
        return True
    except Exception as e:
        print(f"Error setting up DCIR simulation: {e}")
        return False


def create_mesh_field(h3dl, setup_name, face_list, case_file):
    try:
        version = int(h3dl._aedt_version.replace('.', ''))
        if version < 20251:
            field_type = "DC Fields"
        else:
            field_type = "DCIR Fields"
        oModule = h3dl.odesign.GetModule("FieldsReporter")
        oModule.CreateFieldPlot(
            [
                "NAME:Mesh1",
                "SolutionName:=", setup_name,
                "UserSpecifyName:=", 0,
                "UserSpecifyFolder:=", 0,
                "QuantityName:=", "Mesh",
                "PlotFolder:=", "MeshPlots",
                "FieldType:="	, field_type,  
                "StreamlinePlot:="	, False,
                "AdjacentSidePlot:="	, False,
                "FullModelPlot:="	, False,
                "IntrinsicVar:="	, "",
                "PlotGeomInfo:="	, [1 ,"Surface", "FacesList", len(face_list)] + face_list,
                "FilterBoxes:="	, [0],
                "Real time mode:="	, True,
                [
                    "NAME:MeshSettings",
                    "Scale factor:="	, 100,
                    "Transparency:="	, 0,
                    "Mesh type:="		, "Shaded",
                    "Surface only:="	, True,
                    "Add grid:="		, True,
                    "Refinement:="		, 0,
                    "Use geometry color:="	, True,
                    "Mesh line color:="	, [0 ,0, 255],
                    "Filled color:=", [255, 255, 255]
                ],
                "EnableGaussianSmoothing:=", False,
                "SurfaceOnly:="	, False
            ], "Field")
        oModule.ExportFieldPlot("Mesh1", False, case_file)
        return True
    except:
        return False


def save_multiblock_case(plotter, output_case_file):
    blocks = pv.MultiBlock()
    for actor in plotter.renderer.actors.values():
        if hasattr(actor, 'mapper') and hasattr(actor.mapper, 'dataset'):
            dataset = actor.mapper.dataset
            if dataset is not None:
                if hasattr(dataset, 'copy'):
                    blocks.append(pv.wrap(dataset))

    if len(blocks) > 0:
        blocks.save(output_case_file)
        return True
    else:
        return False


def set_parallel_camera_to_bounds(plotter, bounds, margin=0.05):
    x_min, x_max, y_min, y_max, z_min, z_max = bounds

    center_x = (x_min + x_max) / 2
    center_y = (y_min + y_max) / 2
    center_z = (z_min + z_max) / 2

    width = x_max - x_min
    height = y_max - y_min

    plotter.camera_position = [
        (center_x, center_y, center_z + 100),  
        (center_x, center_y, center_z),
        (0, 1, 0)
    ]

    plotter.enable_parallel_projection()

    window_size = plotter.window_size
    window_aspect = window_size[0] / window_size[1]
    bounds_aspect = width / height

    if bounds_aspect > window_aspect:
        parallel_scale = width * (1 + margin * 2) / 2
    else:
        parallel_scale = height * (1 + margin * 2) / 2

    plotter.camera.parallel_scale = parallel_scale

    return parallel_scale


class PostProcessing:
    """Class to extract results from SIwave simulation."""

    def __init__(self, conf, setting, outputFolder, summary, GND_NET, logger, siwave=None):
        self._siwave = siwave
        self._conf = conf
        self._setting = setting
        self._outputFolder = outputFolder
        self._summary = summary
        self._gnd_net = GND_NET
        self._logger = logger
        self._h3dl = None
        user32 = ctypes.windll.user32
        self._screen_size = list((user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))

        if self._setting['Request']['CAE_type'] == 'PI-DCIR':
            self._exportData = {
                "title": {
                    "model": None,
                    "revision": None,
                    "date": None
                },
                "request": {
                    "modelInfo": {
                        "name": None,
                        "year": None,
                        "requestDate": None,
                        "targetDate": None,
                        "event": None
                    },
                    "requestData": {
                        "socName": None,
                        "pcbPartNo": None,
                        "pcbRevision": None,
                        "Stackup": None,
                        "bom": None,
                        "purpose": None
                    },
                    "Image": {
                        "pcbTopImage": None,
                        "pcbBtmImage": None
                    }
                },
                "result": {
                    "simSchedule": {
                        "startDate": None,
                        "endData": None
                    },
                    "summary": []
                },
                "setting": {
                    "tool": {
                        "comp": "ANSYS",
                        "name": "SIwave",
                        "version": None
                    },
                    "stackup": None,
                    "setting": []
                },
                "result_detail": {
                    "result": []
                }
            }
        else:
            # 💡 [추가] PI-DCIR이 아닐 경우를 대비해 빈 딕셔너리로 초기화하여 AttributeError 방지
            self._exportData = {}

    def set_DCIR_results(self, startTime, endTime):
        self._logger.log(f"Title", level=LogLevel.DETAIL2)
        self._exportData["title"]["model"] = self._setting["Request"]["Model"]
        self._exportData["title"]["revision"] = self._setting["CAE"]["PCB"]["Rev"]
        self._exportData["title"]["date"] = datetime.now().strftime("%Y-%m-%d")

        self._logger.log(f"Simulation Request Info", level=LogLevel.DETAIL2)
        self._exportData["request"]["modelInfo"]["name"] = self._setting["Request"]["Model"]
        self._exportData["request"]["modelInfo"]["year"] = self._setting["Request"]["Year"]
        self._exportData["request"]["modelInfo"]["requestDate"] = self._setting["Request"]["Start_date"]
        self._exportData["request"]["modelInfo"]["targetDate"] = self._setting["Request"]["Target_date"]
        self._exportData["request"]["modelInfo"]["event"] = self._setting["Request"]["Event"]
        self._exportData["request"]["requestData"]["socName"] = self._setting["CAE"]["SOC"]["Name"]
        self._exportData["request"]["requestData"]["pcbPartNo"] = self._setting["CAE"]["PCB"]["PN"]
        self._exportData["request"]["requestData"]["pcbRevision"] = self._setting["CAE"]["PCB"]["Rev"]
        self._exportData["request"]["requestData"]["Stackup"] = self._setting["CAE"]["PCB"]["Stackup"].name
        self._exportData["request"]["requestData"]["bom"] = self._setting["CAE"]["PCB"]["BOM"].name
        self._exportData["request"]["requestData"]["purpose"] = self._setting["CAE"]["Purpose"]
        self._exportData["request"]["Image"]["pcbTopImage"] = 'top.png'
        self._exportData["request"]["Image"]["pcbBtmImage"] = 'btm.png'

        self._logger.log(f"Simulation Result", level=LogLevel.DETAIL2)
        self._exportData["result"]["simSchedule"]["startDate"] = startTime
        self._exportData["result"]["simSchedule"]["endData"] = endTime
        for case in self._summary:
            case.pop('DCDC', None)
        public_summary = [
            {key: value for key, value in case.items() if not key.startswith('_')}
            for case in self._summary
        ]
        self._exportData["result"]["summary"] = copy.deepcopy(public_summary)
        for case in self._exportData["result"]["summary"]:
            if case['is_done']:
                case['FitView'] = case['FitView'].name
                case['ZoomView'] = case['ZoomView'].name
                case['Field_Case'] = case['Field_Case'].name
                case['Mesh_Case'] = case['Mesh_Case'].name

        self._logger.log(f"Simulation Setting Info.", level=LogLevel.DETAIL2)
        self._exportData["setting"]['tool']["version"] = self._conf['DCIR']['version'].replace('.', ' R')
        self._exportData["setting"]["stackup"] = "stackup.xml"
        self._exportData["setting"]["setting"] = copy.deepcopy(public_summary)
        for case in self._exportData["setting"]["setting"]:
            if case['is_done']:
                case['FitView'] = case['FitView'].name
                case['ZoomView'] = case['ZoomView'].name
                case['Field_Case'] = case['Field_Case'].name
                case['Mesh_Case'] = case['Mesh_Case'].name

        self._logger.log(f"Simulation Result - Detail", level=LogLevel.DETAIL2)
        self._exportData["result_detail"]["result"] = copy.deepcopy(public_summary)
        for case in self._exportData["result_detail"]["result"]:
            if case['is_done']:
                case['FitView'] = case['FitView'].name
                case['ZoomView'] = case['ZoomView'].name
                case['Field_Case'] = case['Field_Case'].name
                case['Mesh_Case'] = case['Mesh_Case'].name


    def export_edb(self, siw_file, edb_path, version):
        self._logger.log("Export fresh Post EDB", level=LogLevel.DETAIL4)
        self._logger.log(f"Latest completed SIW : {siw_file}", level=LogLevel.DETAIL5)
        self._logger.log(f"Post EDB : {edb_path}", level=LogLevel.DETAIL5)
        return export_post_edb(
            Path(siw_file),
            Path(edb_path),
            version,
            siwave_factory=SIwave,
        )


    def import_edb(self, edb_path, version):
        prj_name = Path(edb_path).with_suffix('.aedt')
        layer_list = []
        sources = []
        edb = None
        try:
            self._logger.log("Open EDB", level=LogLevel.DETAIL4)
            self._logger.log(f"{edb_path}", level=LogLevel.DETAIL5)
            edb = Edb(str(edb_path), edbversion=version)

            excitations_nets = edb.excitations_nets
            self._logger.log(f"Excitations Nets : {excitations_nets}", level=LogLevel.DETAIL5)

            layer_list = list(edb.stackup.signal_layers.keys())
            self._logger.log(f"Layer List : {layer_list}", level=LogLevel.DETAIL5)

            current_source = None
            voltage_source = None
            for name, source in edb.sources.items():
                if source.boundary_type == 'kCurrentSource':
                    current_source = name
                elif source.boundary_type == 'kVoltageSource':
                    voltage_source = name
            if not current_source or not voltage_source:
                raise RuntimeError(
                    f"Post AEDB must contain one current and one voltage source: {edb_path}"
                )
            sources = [current_source, voltage_source]
            self._logger.log(f"Current & Voltage Source : {sources}", level=LogLevel.DETAIL5)

            # PyAEDT must release the read-only EDB session before AEDT imports
            # the same database into HFSS 3D Layout.
            edb.close()
            edb = None
            self._logger.log(f"Close EDB : {edb_path}", level=LogLevel.DETAIL5)

            remove_artifact_path(prj_name)
            remove_artifact_path(prj_name.with_suffix('.aedtresults'))

            import_result = self._h3dl.import_edb(str(edb_path))
            if import_result is False or self._h3dl.odesign is None:
                raise RuntimeError(f"AEDT failed to import Post EDB: {edb_path}")
            self._logger.log(f"Import EDB file : {edb_path / 'edb.def'}", level=LogLevel.DETAIL5)

            self._h3dl.save_project(file_name=str(prj_name))
        finally:
            if edb is not None:
                edb.close()
                self._logger.log(f"Close EDB : {edb_path}", level=LogLevel.DETAIL5)

        self._logger.log(f"Saved AEDT file : {prj_name}", level=LogLevel.DETAIL5)
        return prj_name, layer_list, sources


    def extract_case_img(self, prj_name, layer_list, case, gnd_net, sources, fit_file, zoom_file, field_case_file, mesh_case_file, version):
        try:
            logger = self._logger
            logger.log("Extract Case & Images", level=LogLevel.DETAIL4)
            
            # [수정] None 값이 들어올 경우를 대비해 확실하게 빈 리스트로 초기화
            target_net_list = case.get('Full_Net_Chain') or []
            
            # 만약 Full_Net_Chain이 비어있을 경우를 대비한 안전장치 (Fallback)
            if not target_net_list:
                for key in ['Net', 'DCDC_net']:
                    val = case.get(key)
                    if isinstance(val, str) and val:
                        target_net_list.append(val)
                    elif isinstance(val, list):
                        target_net_list.extend(val)

            # 중복된 Net 이름 제거 및 최종 Target Net 확정
            target_net = list(set(target_net_list))
            
            if not target_net:
                logger.log("Warning: No target net found. Skipping plot.", level=LogLevel.WARNING)
                return
                
            logger.log(f"Final Target Nets to Plot: {target_net}", level=LogLevel.DETAIL5)

            # region 2. Export Layer Stackup XML
            stackup_xml = Path(self._outputFolder / "stackup.xml")
            if not stackup_xml.exists():
                logger.log("Save Stackup XML", level=LogLevel.DETAIL5)
                self._h3dl.oeditor.ExportStackupXML(stackup_xml)
                logger.log(f"{stackup_xml}", level=LogLevel.DETAIL6)
            # endregion

            # region 3. Set active setup
            # 💡 [수정] 객체 초기화 지연에 대비한 방어 코드 적용
            try:
                setup_name = self._h3dl.active_setup
            except AttributeError:
                logger.log("[WARNING] AEDT 객체 초기화 지연 감지. 5초 대기 후 재시도합니다...", level=LogLevel.WARNING)
                import time
                time.sleep(5.0)
                try:
                    # 재시도 시 setups 리스트에서 직접 가져오기 시도
                    setup_name = self._h3dl.setups[0].name if self._h3dl.setups else None
                except Exception as e:
                    logger.log(f"[ERROR] Setup 이름 추출 최종 실패: {e}", level=LogLevel.ERROR)
                    setup_name = None
            logger.log(f"Setup name : {setup_name}", level=LogLevel.DETAIL5)
            # endregion

            # region 4. Check Intrinsics
            intrinsics = self._h3dl.post._check_intrinsics(None, setup_name)
            logger.log(f"Intrinsics : {intrinsics}", level=LogLevel.DETAIL5)
            # endregion

            # region 5. Set DCIR Simulation
            self._h3dl.post.available_report_quantities(context='Sources', is_siwave_dc=True, quantities_category='Voltage')
            self._h3dl.post.available_report_quantities(context='Sources', is_siwave_dc=True, quantities_category='Current')
            set_dcir_sim(self._h3dl, setup_name, sources)
            logger.log(f"DCIR Simulation Setting", level=LogLevel.DETAIL5)
            # endregion

            # region 6. Analyze
            logger.log(f"Analyze : {setup_name}", level=LogLevel.DETAIL5)
            self._h3dl.analyze(setup=setup_name)

            # Save project with retry logic
            max_save_attempts = 5
            for attempt in range(max_save_attempts):
                try:
                    self._h3dl.save_project()
                    logger.log(f"Project saved successfully (attempt {attempt + 1})", level=LogLevel.DETAIL6)
                    break
                except Exception as e:
                    if attempt < max_save_attempts - 1:
                        logger.log(f"Save attempt {attempt + 1} failed: {str(e)}, retrying...", level=LogLevel.WARNING)
                        time.sleep(2)
                    else:
                        logger.log(f"All save attempts failed, continuing without save: {str(e)}", level=LogLevel.WARNING)
            
            # region 6.5 Validate Target Nets with AEDT internal names
            logger.log("Validate Target Nets with AEDT internal names", level=LogLevel.DETAIL6)
            existing_nets = list(self._h3dl.modeler.nets.keys())
            valid_target_nets = []
            
            for net in target_net:
                if net in existing_nets:
                    valid_target_nets.append(net)
                else:
                    alt_net = net.replace('+', '_')
                    if alt_net in existing_nets:
                        valid_target_nets.append(alt_net)
                        logger.log(f"Net name mapped: {net} -> {alt_net}", level=LogLevel.DETAIL6)
                    else:
                        logger.log(f"Warning: Net '{net}' not found in 3D Layout.", level=LogLevel.WARNING)
            
            valid_target_nets = list(set(valid_target_nets))
            logger.log(f"Validated Target Nets: {valid_target_nets}", level=LogLevel.DETAIL5)

            siw_base_name = prj_name.stem 
            ic_name = case.get('IC', '')
            
            display_name = siw_base_name
            if ic_name and f"_{ic_name}_" in siw_base_name:
                net_part = siw_base_name.split(f"_{ic_name}_")[-1]
                display_name = f"{ic_name}_{net_part}"
            
            out_dir = Path(fit_file).parent
            
            new_fit_file = out_dir / f"{display_name}_FitView.jpg"
            new_zoom_file = out_dir / f"{display_name}_ZoomView.jpg"
            new_field_case = out_dir / f"Field_{display_name}.case"
            new_mesh_case = out_dir / f"Mesh_{display_name}.case"
            
            case['FitView'] = new_fit_file
            case['ZoomView'] = new_zoom_file
            case['Field_Case'] = new_field_case
            case['Mesh_Case'] = new_mesh_case
            
            fit_file = str(new_fit_file)
            zoom_file = str(new_zoom_file)
            field_case_file = str(new_field_case)
            mesh_case_file = str(new_mesh_case)
            
            logger.log(f"Updated output filenames based on SIW name: {display_name}", level=LogLevel.DETAIL5)
            # endregion

            # region 7. Create Field Plot & Export Field Case File
            logger.log("Create Field Plot", level=LogLevel.DETAIL5)
            plot_name = f"DCIR_{display_name}"
            logger.log(f"Field Plot Name : {plot_name}", level=LogLevel.DETAIL6)
            
            face_list = []
            for layer in layer_list:
                for net in valid_target_nets: 
                    try:
                        faces = self._h3dl.odesign.GetGeometryIdsForNetLayerCombination(net, layer, setup_name)
                        if faces and len(faces) > 2:
                            face_list.extend(faces[2:])
                    except Exception as e:
                        # 💡 [수정 2] 빈 레이어 경고(Warning) 예외 처리 (Bypass) 적용
                        error_msg = str(e)
                        if "-2147352567" in error_msg or "-2147024344" in error_msg:
                            pass  # 해당 레이어에 네트가 없는 정상이므로 조용히 넘어감
                        else:
                            logger.log(f"Warning: Failed to get field faces for Net '{net}' on Layer '{layer}': {e}", level=LogLevel.WARNING)
            
            face_list = list(set(face_list))
            
            if not face_list:
                raise RuntimeError("No faces extracted for Field Plot")
            
            logger.log("Export Case File", level=LogLevel.DETAIL6)
            self._h3dl.post._create_fieldplot(face_list, "Voltage", setup_name, intrinsics, "FacesList", plot_name)
            field_case_file_path = Path(field_case_file)
            logger.log(f"{field_case_file}", level=LogLevel.DETAIL6)
            self._h3dl.post.export_field_plot(plot_name, str(field_case_file_path.parent), file_name=str(field_case_file_path.stem), file_format='case')
            self._h3dl.save_project()
            # endregion

            # region 8. Create Mesh Plot & Export Field Case File
            logger.log("Create Mesh Plot", level=LogLevel.DETAIL6)
            plot_name = f"MESH_{display_name}"
            logger.log(f"Mesh Plot Name : {plot_name}", level=LogLevel.DETAIL6)
            
            mesh_face_list = []
            for layer in layer_list:
                for net in valid_target_nets + [gnd_net]:
                    try:
                        faces = self._h3dl.odesign.GetGeometryIdsForNetLayerCombination(net, layer, setup_name)
                        if faces and len(faces) > 2:
                            mesh_face_list.extend(faces[2:])
                    except Exception as e:
                        # 💡 [수정 2] Mesh 추출 시에도 동일하게 빈 레이어 경고 예외 처리 적용
                        error_msg = str(e)
                        if "-2147352567" in error_msg or "-2147024344" in error_msg:
                            pass
                        else:
                            logger.log(f"Warning: Failed to get mesh faces for Net '{net}' on Layer '{layer}': {e}", level=LogLevel.WARNING)
            
            mesh_face_list = list(set(mesh_face_list))

            logger.log(f"Target Net: {valid_target_nets + [gnd_net]}", level=LogLevel.DETAIL6)
            logger.log(f"Target Layer: {layer_list}", level=LogLevel.DETAIL6)
            logger.log(f"Face List : {mesh_face_list}", level=LogLevel.DETAIL6)

            if not create_mesh_field(self._h3dl, setup_name, mesh_face_list, mesh_case_file):
                raise DCIRSessionException(ErrorCode.MESH_FIELD_PLOT_FAIL, plot_name)
            self._h3dl.save_project()
            # endregion

            # region 9. Load Case files
            logger.log('Load Case files', level=LogLevel.DETAIL5)

            logger.log(f'Load Mesh Case file : {mesh_case_file}', level=LogLevel.DETAIL6)
            pv.set_plot_theme("document")
            reader = vtk.vtkGenericEnSightReader()
            reader.SetCaseFileName(mesh_case_file)
            reader.ReadAllVariablesOn()
            reader.Update()
            Mesh_out = reader.GetOutput()
            Mesh_block = pv.wrap(Mesh_out)
            Mesh_grid = Mesh_block[0]

            logger.log(f'Load Field Case file : {field_case_file}', level=LogLevel.DETAIL6)
            pv.set_plot_theme("document")
            reader = vtk.vtkGenericEnSightReader()
            reader.SetCaseFileName(field_case_file)
            reader.ReadAllVariablesOn()
            reader.Update()
            Field_out = reader.GetOutput()
            Field_block = pv.wrap(Field_out)
            Field_grid = Field_block[0]
            # endregion

            # region 10. Create Mesh and Field Plot
            logger.log(f'Create Plot', level=LogLevel.DETAIL5)
            
            # 💡 [수정] TypeError를 추가하여 None 값이 들어올 경우도 안전하게 방어합니다.
            plot_conf = (self._conf or {}).get('DCIR', {}).get('plot', {})
            bg_color = plot_conf.get('bgColor', 'whitesmoke')
            try:
                bg_opacity = float(plot_conf.get('bgOpacity', 0.2))
            except (ValueError, TypeError):
                bg_opacity = 0.2  # 변환 실패 시 기본값 적용
            nan_color = plot_conf.get('nanColor', 'darkgray')

            plotter = pv.Plotter(off_screen=True, title="FitView")
            logger.log(f'Add Mesh Grid', level=LogLevel.DETAIL6)
            
            # 💡 [수정] 하드코딩된 값을 변수로 교체
            plotter.add_mesh(Mesh_grid, show_edges=False, scalars=None, color=bg_color, opacity=bg_opacity, show_scalar_bar=False)
            
            logger.log(f'Add Field Grid', level=LogLevel.DETAIL6)
            plotter.add_mesh(Field_grid,
                             show_edges=False,
                             scalars=Field_grid.active_scalars,
                             cmap="rainbow",
                             nan_color=nan_color,  # 💡 [수정] 하드코딩된 값을 변수로 교체
                             show_scalar_bar=True,
                             scalar_bar_args={
                                 "title": "Voltage [V]",
                                 "vertical": True,
                                 "position_x": 0.03,
                                 "position_y": 0.5,
                                 "width": 0.1,
                                 "title_font_size": 48,
                                 "label_font_size": 40,
                                 "fmt": "%.3f",
                                 "font_family": "arial",
                                 "bold": True
                             }
                             )
            # endregion

            # region 11. Draw All Components
            font_scale = 10000
            min_font_size = 6
            max_font_size = 30

            logger.log(f'Draw All Components', level=LogLevel.DETAIL5)
            EDB_FILE_PATH = prj_name.with_suffix('.aedb')
            edb = Edb(EDB_FILE_PATH, edbversion=version)

            logger.log(f'Get Target Components', level=LogLevel.DETAIL6)
            target_comp = {}
            if case.get('IC') in edb.components.components:
                target_comp[case['IC']] = edb.components.components[case['IC']]
            if case.get('DCDC_name') in edb.components.components:
                target_comp[case['DCDC_name']] = edb.components.components[case['DCDC_name']]

            total_comps = len(target_comp)
            for comp_name, comp_inst in tqdm(target_comp.items(), total=total_comps, desc="Generating FitView"):
                x1, y1, x2, y2 = comp_inst.bounding_box
                width = x2 - x1
                height = y2 - y1

                corners = np.array([
                    [x1, y1, 0],
                    [x2, y1, 0],
                    [x2, y2, 0],
                    [x1, y2, 0],
                    [x1, y1, 0]
                ])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))

                plotter.add_mesh(rect, color="black", line_width=3)

                font_size = int(font_scale * min(width, height))
                font_size = max(min_font_size, min(font_size, max_font_size))

                text_center = [(x1 + x2) / 2, (y1 + y2) / 2, 0]
                plotter.add_point_labels([text_center], [comp_name],
                                         font_size=font_size,
                                         text_color="blue",
                                         shadow=False,
                                         always_visible=True,
                                         shape=None)
            # endregion

            # region 12. Draw Zoom Area
            logger.log(f'Draw Zoom Area', level=LogLevel.DETAIL5)
            min_X, min_Y = float('inf'), float('inf')
            max_X, max_Y = float('-inf'), float('-inf')

            for net in target_net:
                if net in edb.nets.nets: 
                    for prim in edb.nets.nets[net].primitives:
                        x1, y1, x2, y2 = prim.bbox
                        min_X = min(min_X, x1)
                        min_Y = min(min_Y, y1)
                        max_X = max(max_X, x2)
                        max_Y = max(max_Y, y2)

            for comp_inst in target_comp.values():
                x1, y1, x2, y2 = comp_inst.bounding_box
                min_X = min(min_X, x1)
                min_Y = min(min_Y, y1)
                max_X = max(max_X, x2)
                max_Y = max(max_Y, y2)

            offset = 0.02
            corners = np.array([
                [min_X - offset, min_Y - offset, 0],  
                [max_X + offset, min_Y - offset, 0],  
                [max_X + offset, max_Y + offset, 0],  
                [min_X - offset, max_Y + offset, 0],  
                [min_X - offset, min_Y - offset, 0]  
            ])

            center_x = (min_X + max_X) / 2
            center_y = (min_Y + max_Y) / 2
            center_z = 0  

            width = abs(max_X - min_X)
            height = abs(max_Y - min_Y)
            max_dim = max(width, height)

            rect = pv.PolyData(corners)
            rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))

            logger.log(f'Add zoom area on plot', level=LogLevel.DETAIL6)
            plotter.add_mesh(rect, color="red", line_width=6)
            plotter.add_point_labels([min_X - offset, max_Y + offset, 0], ["Zoom Area"],
                                     font_size=50,
                                     text_color="red",
                                     shadow=False,
                                     always_visible=True,
                                     shape=None)
            # endregion

            # region 13. Save FitView Image and Case File
            logger.log(f'Save FitView Image', level=LogLevel.DETAIL5)
            plotter.camera_position = "xy"
            plotter.remove_bounds_axes()
            plotter.background_color = 'white'
            plotter.camera.zoom(1.6)
            logger.log(f'Camera position : xy', level=LogLevel.DETAIL6)
            logger.log(f'Camera zoom : 1.6', level=LogLevel.DETAIL6)
            logger.log(f'Background color : white', level=LogLevel.DETAIL6)
            logger.log(f'FitView image file : {fit_file}', level=LogLevel.DETAIL6)
            plotter.screenshot(fit_file, window_size=self._screen_size)
            plotter.close()
            logger.log(f'FitView Case file : {Path(fit_file).with_suffix(".case")}', level=LogLevel.DETAIL6)
            # endregion

            # region 14. Save ZoomView Image
            logger.log(f'Save ZoomView Image', level=LogLevel.DETAIL5)
            zoom_plotter = pv.Plotter(off_screen=True, title="ZoomView")
            
            # 💡 [수정] 하드코딩된 값을 변수로 교체
            zoom_plotter.add_mesh(Mesh_grid, show_edges=False, scalars=None, color=bg_color, opacity=bg_opacity, show_scalar_bar=False)
            
            # 💡 [수정] 하드코딩된 값을 변수로 교체
            zoom_plotter.add_mesh(Field_grid, show_edges=False, scalars=Field_grid.active_scalars, cmap="rainbow", nan_color=nan_color, show_scalar_bar=True, scalar_bar_args={
                "title": "Voltage [V]",
                "vertical": True,
                "position_x": 0.03,
                "position_y": 0.5,
                "width": 0.1,
                "title_font_size": 48,
                "label_font_size": 40,
                "fmt": "%.3f",
                "font_family": "arial",
                "bold": True
            })

            logger.log(f'Draw Target Components', level=LogLevel.DETAIL6)
            total_comps = len(target_comp)
            for comp_name, comp_inst in tqdm(target_comp.items(), total=total_comps, desc="Generating ZoomView"):
                x1, y1, x2, y2 = comp_inst.bounding_box
                width = x2 - x1
                height = y2 - y1

                corners = np.array([
                    [x1, y1, 0],
                    [x2, y1, 0],
                    [x2, y2, 0],
                    [x1, y2, 0],
                    [x1, y1, 0]
                ])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))
                zoom_plotter.add_mesh(rect, color="black", line_width=3)
                text_center = [(x1 + x2) / 2, (y1 + y2) / 2, 0]
                text = pv.Text3D(comp_name, center=text_center, height=0.2 * min(width, height))
                zoom_plotter.add_mesh(text, color="blue", lighting=False, style="surface")

            logger.log(f'Save ZoomView Image', level=LogLevel.DETAIL6)

            zoom_bounds = [min_X, max_X, min_Y, max_Y, -0.1, 0.1]
            logger.log(f'Zoom Boundaries : {zoom_bounds}', level=LogLevel.DETAIL6)
            set_parallel_camera_to_bounds(zoom_plotter, zoom_bounds, margin=0.01)
            zoom_plotter.screenshot(zoom_file, window_size=self._screen_size)
            logger.log(f'ZoomView image file : {zoom_file}', level=LogLevel.DETAIL6)
            zoom_plotter.close()
            # endregion

            # region 15. Save
            logger.log("Save", level=LogLevel.DETAIL6)
            self._h3dl.save_project()
            # endregion

        except Exception:
            logger.log(
                f"An error occurred while extracting case & image files : {traceback.format_exc()}",
                level=LogLevel.ERROR,
            )
            raise

        finally:
            try:
                self._h3dl.close_project()
                logger.log(f"Close HFSS 3D Layout : {prj_name}", level=LogLevel.DETAIL6)
            except Exception as close_error:
                logger.log(
                    f"Failed to close HFSS 3D Layout project {prj_name}: {close_error}",
                    level=LogLevel.WARNING,
                )


    def extract_results(self, version):
        """Extract results from the SIwave instance."""
        viewer_artifacts = []
        viewer_cases = []
        
        # 💡 [추가] AEDT 초기화 재시도 및 검증을 위한 내부 헬퍼 함수
        def init_aedt_with_retry(ver, max_retries=3):
            for attempt in range(max_retries):
                try:
                    h3dl = Hfss3dLayout(version=ver, non_graphical=False)
                    # 핵심 검증: odesign 속성이 정상적으로 생성되었는지 확인
                    if not hasattr(h3dl, '_odesign') or h3dl._odesign is None:
                        raise ValueError("AEDT Design 연결 실패 (불완전한 객체)")
                    return h3dl
                except Exception as e:
                    self._logger.log(f"[WARNING] AEDT 초기화 실패 (시도 {attempt+1}/{max_retries}): {e}", level=LogLevel.WARNING)
                    time.sleep(10.0)
                    if attempt == max_retries - 1:
                        self._logger.log("[ERROR] AEDT 객체 초기화 최종 실패", level=LogLevel.ERROR)
                        raise DCIRSessionException(ErrorCode.AEDT_LAUNCH_FAILURE, ver)

        try:
            self._logger.log("Dump results to JSON files", level=LogLevel.DETAIL2)
            for key, val in self._exportData.items():
                output_file = os.path.join(self._outputFolder, f'{key}.json')
                with open(output_file, 'w') as file:
                    json.dump(val, file, indent=2, default=str)
                self._logger.log(f"{output_file}", level=LogLevel.DETAIL3)

            completed_cases = [
                (idx, case)
                for idx, case in enumerate(self._summary)
                if case.get('is_done')
            ]
            if not completed_cases:
                self._logger.log(
                    "Viewer export skipped: no completed Local result was detected.",
                    level=LogLevel.WARNING,
                )
                return viewer_artifacts

            # Export every Post-owned AEDB before launching AEDT. Keeping SIWave
            # and AEDT open at the same time caused intermittent COM/license
            # stalls during consecutive FullBatch/Post runs.
            self._logger.log("Export fresh Post AEDBs", level=LogLevel.DETAIL2)
            for idx, case in completed_cases:
                viewer_record = {
                    "Case_Index": idx + 1,
                    "IC": case.get('IC', ''),
                    "Net": case.get('Net', ''),
                    "Source_Siw": case.get('_viewer_siw', ''),
                    "Edb_Folder": Path(case['edb']).name,
                    "Edb_Status": "Pending",
                    "Viewer_Status": "Pending",
                }
                try:
                    source_siw = case.get('_viewer_siw')
                    if not source_siw:
                        raise RuntimeError(
                            f"No latest completed SIW was selected for case #{idx + 1}"
                        )
                    case['edb'] = self.export_edb(source_siw, case['edb'], version)
                    viewer_record["Edb_Status"] = "Complete"
                    viewer_cases.append((idx, case, viewer_record))
                except Exception as exc:
                    viewer_record["Edb_Status"] = "Error"
                    viewer_record["Viewer_Status"] = "Error"
                    viewer_record["Error_Phase"] = "edb_export"
                    viewer_record["Error"] = str(exc)
                    self._logger.log(
                        f"[WARNING] Case #{idx + 1} ({case.get('Net')}) "
                        f"Post AEDB export failed: {exc}",
                        level=LogLevel.WARNING,
                    )
                viewer_artifacts.append(viewer_record)

            if not viewer_cases:
                self._logger.log(
                    "Viewer export skipped: every Post AEDB export failed.",
                    level=LogLevel.ERROR,
                )
                return viewer_artifacts

            self._logger.log("Export 3D & 2D Voltage Drop Contour Plot", level=LogLevel.DETAIL2)
            self._logger.log(f"Start : {time.strftime('%Y.%m.%d, %H:%M:%S')}", level=LogLevel.DETAIL3)

            Settings.use_grpc_api = False
            
            # 💡 [수정] 최초 AEDT 실행 시 재시도 로직 적용
            self._h3dl = init_aedt_with_retry(version)
            restart_aedt = False

            for viewer_idx, (idx, case, viewer_record) in enumerate(viewer_cases):
                self._logger.log(f"Case #{idx + 1}", level=LogLevel.DETAIL3)

                if restart_aedt or (viewer_idx > 0 and viewer_idx % 5 == 0):
                    self._logger.log("Restarting AEDT instance to prevent memory leak...", level=LogLevel.DETAIL3)
                    if self._h3dl:
                        try:
                            # 💡 [수정] 확실한 프로세스 종료 옵션 추가
                            self._h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                    time.sleep(10.0)  
                    
                    # 💡 [수정] 재시작 시 재시도 로직 적용
                    self._h3dl = init_aedt_with_retry(version)
                    restart_aedt = False

                try:
                    for output_path in (
                        case['FitView'],
                        case['ZoomView'],
                        case['Field_Case'],
                        case['Mesh_Case'],
                    ):
                        remove_artifact_path(Path(output_path))
                    prj_name, layer_list, sources = self.import_edb(case['edb'], version)
                    self.extract_case_img(prj_name, layer_list, case, self._gnd_net, sources, case['FitView'], case['ZoomView'], case['Field_Case'], case['Mesh_Case'], version)

                    expected_outputs = [
                        Path(case['FitView']),
                        Path(case['ZoomView']),
                        Path(case['Field_Case']),
                        Path(case['Mesh_Case']),
                    ]
                    missing_outputs = [path.name for path in expected_outputs if not path.exists()]
                    if missing_outputs:
                        raise RuntimeError(
                            f"Viewer output(s) missing: {', '.join(missing_outputs)}"
                        )
                    viewer_record["Viewer_Status"] = "Complete"
                    viewer_record["Outputs"] = [path.name for path in expected_outputs]
                    time.sleep(2.0)
                except Exception as exc:
                    viewer_record["Viewer_Status"] = "Error"
                    viewer_record["Error_Phase"] = "viewer_generation"
                    viewer_record["Error"] = str(exc)
                    restart_aedt = True
                    self._logger.log(
                        f"[WARNING] Case #{idx + 1} ({case.get('Net')}) "
                        f"Viewer generation failed: {exc}",
                        level=LogLevel.WARNING,
                    )
                finally:
                    time.sleep(3.0)
                    gc.collect()

        except Exception as e:
            self._logger.fatal(f"An error occurred on extract_results : {traceback.format_exc()}")
            raise e

        finally:
            if self._h3dl:
                try:
                    # 💡 [수정] 확실한 프로세스 종료 옵션 추가
                    self._h3dl.release_desktop(close_projects=True, close_desktop=True)
                except Exception:
                    pass
                self._logger.log("Close AEDT", level=LogLevel.DETAIL3)

        return viewer_artifacts
