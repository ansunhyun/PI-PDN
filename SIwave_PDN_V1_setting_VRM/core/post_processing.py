# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import traceback
import time
import os
import copy
import gc  
import ctypes
import pyvista as pv
import numpy as np

from core.database import PDNSessionException, ErrorCode
from core.post_stage import export_post_edb, remove_artifact_path
from datetime import datetime
from pyaedt import Edb, Hfss3dLayout
from ansys.aedt.core.generic.settings import Settings
from EBU_lib.SIwave import SIwave
from core.logger import LogLevel
from pathlib import Path
from tqdm import tqdm


def set_parallel_camera_to_bounds(plotter, bounds, margin=0.05):
    """Set PyVista camera to fit the given bounds for ZoomView."""
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
    bounds_aspect = width / height if height != 0 else 1

    if bounds_aspect > window_aspect:
        parallel_scale = width * (1 + margin * 2) / 2
    else:
        parallel_scale = height * (1 + margin * 2) / 2

    plotter.camera.parallel_scale = parallel_scale
    return parallel_scale


class PostProcessing:
    """Class to extract AC PDN (Impedance) results and Net Path images from SIwave/AEDT simulation."""

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

        if self._setting['Request']['CAE_type'] == 'PI-PDN':
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
            self._exportData = {}

    def _collect_h3dl_ports(self):
        """Return stable port-name list from HFSS 3D Layout with fallback paths."""
        ports = []
        try:
            raw = self._h3dl.excitations if self._h3dl else []
            if isinstance(raw, dict):
                ports.extend([str(k).strip() for k in raw.keys() if str(k).strip()])
            elif isinstance(raw, (list, tuple, set)):
                ports.extend([str(v).strip() for v in raw if str(v).strip()])
            elif raw:
                try:
                    ports.extend([str(v).strip() for v in list(raw) if str(v).strip()])
                except Exception:
                    pass
        except Exception:
            pass

        if not ports:
            try:
                oexc = getattr(self._h3dl, "oexcitation", None)
                if oexc:
                    for fn_name in ("GetAllPortsList", "GetAllPorts", "GetAllExcitations"):
                        fn = getattr(oexc, fn_name, None)
                        if not fn:
                            continue
                        vals = fn()
                        if isinstance(vals, (list, tuple)):
                            ports.extend([str(v).strip() for v in vals if str(v).strip()])
                        elif vals:
                            ports.append(str(vals).strip())
            except Exception:
                pass

        uniq = []
        seen = set()
        for p in ports:
            key = p.upper()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        return uniq

    @staticmethod
    def _resolve_setup_and_sweep(h3dl):
        setups = h3dl.setups or []
        if not setups:
            return "", ""
        setup_name = setups[0].name
        sweeps = setups[0].sweeps if setups else []
        sweep_name = sweeps[0].name if sweeps else ""
        return setup_name, sweep_name

    @staticmethod
    def _pick_latest_file(candidates):
        files = [Path(p) for p in candidates if Path(p).exists() and Path(p).is_file()]
        if not files:
            return None
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0]

    def _export_touchstone_best_effort(self, setup_name, sweep_name, ts_file: Path):
        errs = []
        calls = []
        if setup_name and sweep_name:
            calls.append(("setup+sweep+path", lambda: self._h3dl.export_touchstone(setup_name, sweep_name, str(ts_file))))
        if setup_name:
            calls.append(("setup+path", lambda: self._h3dl.export_touchstone(setup_name, str(ts_file))))
        calls.append(("path-only", lambda: self._h3dl.export_touchstone(str(ts_file))))
        for label, fn in calls:
            try:
                fn()
                if ts_file.exists():
                    return str(ts_file), ""
            except Exception as e:
                errs.append(f"{label}: {e}")
        return "", "; ".join(errs)

    def set_PDN_results(self, startTime, endTime):
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
            case.pop('Source', None)
            
        public_summary = [
            {key: value for key, value in case.items() if not key.startswith('_')}
            for case in self._summary
        ]
        
        def update_case_keys(case_list):
            for case in case_list:
                if case.get('is_done'):
                    case['Impedance_Plot'] = Path(case.get('Impedance_Plot', '')).name if case.get('Impedance_Plot') else ""
                    case['Impedance_CSV'] = Path(case.get('Impedance_CSV', '')).name if case.get('Impedance_CSV') else ""
                    case['Touchstone'] = Path(case.get('Touchstone', '')).name if case.get('Touchstone') else ""
                    case['FitView'] = Path(case.get('FitView', '')).name if case.get('FitView') else ""
                    case['ZoomView'] = Path(case.get('ZoomView', '')).name if case.get('ZoomView') else ""
                    # Remove legacy unused keys.
                    for k in ['Field_Case', 'Mesh_Case']:
                        case.pop(k, None)

        self._exportData["result"]["summary"] = copy.deepcopy(public_summary)
        update_case_keys(self._exportData["result"]["summary"])

        self._logger.log(f"Simulation Setting Info.", level=LogLevel.DETAIL2)
        engine_conf = self._conf.get('PDN', {})
        backend = str(engine_conf.get("runtime", {}).get("solver_backend", "siwave")).strip().lower()
        tool_name = "SIwave" if backend != "aedt_cutout" else "AEDT-HFSS3DLayout"
        self._exportData["setting"]['tool']["name"] = tool_name
        self._exportData["setting"]['tool']["version"] = engine_conf.get('version', '').replace('.', ' R')
        self._exportData["setting"]["stackup"] = "stackup.xml"
        self._exportData["setting"]["setting"] = copy.deepcopy(public_summary)
        update_case_keys(self._exportData["setting"]["setting"])

        self._logger.log(f"Simulation Result - Detail", level=LogLevel.DETAIL2)
        self._exportData["result_detail"]["result"] = copy.deepcopy(public_summary)
        update_case_keys(self._exportData["result_detail"]["result"])


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


    def extract_net_path_images(self, edb_path, case, version):
        """Extract FitView and ZoomView images showing the physical net path using PyVista."""
        logger = self._logger
        logger.log("Extract Net Path Images (FitView & ZoomView)", level=LogLevel.DETAIL4)
        
        target_net_list = case.get('Full_Net_Chain') or []
        if not target_net_list:
            for key in ['Net', 'Source_net']:
                val = case.get(key)
                if isinstance(val, str) and val:
                    target_net_list.append(val)
                elif isinstance(val, list):
                    target_net_list.extend(val)
        target_nets = list(set(target_net_list))
        
        if not target_nets:
            logger.log("Warning: No target net found. Skipping net path plot.", level=LogLevel.WARNING)
            return

        siw_base_name = Path(edb_path).stem 
        ic_name = case.get('IC', '')
        display_name = siw_base_name
        if ic_name and f"_{ic_name}_" in siw_base_name:
            net_part = siw_base_name.split(f"_{ic_name}_")[-1]
            display_name = f"{ic_name}_{net_part}"
            
        out_dir = Path(edb_path).parent
        fit_file = out_dir / f"{display_name}_FitView.jpg"
        zoom_file = out_dir / f"{display_name}_ZoomView.jpg"
        
        case['FitView'] = str(fit_file)
        case['ZoomView'] = str(zoom_file)

        edb = None
        try:
            edb = Edb(str(edb_path), edbversion=version)
            pv.set_plot_theme("document")
            
            # --- FitView ---
            fit_plotter = pv.Plotter(off_screen=True, title="FitView")
            fit_plotter.background_color = 'white'
            
            min_X, min_Y = float('inf'), float('inf')
            max_X, max_Y = float('-inf'), float('-inf')
            
            # 1. Draw Net Path (Primitives Bounding Boxes)
            for net_name in target_nets:
                net = edb.nets.nets.get(net_name)
                if not net: continue
                for prim in net.primitives:
                    bbox = prim.bbox
                    if bbox and len(bbox) == 4:
                        x1, y1, x2, y2 = bbox
                        min_X = min(min_X, x1)
                        min_Y = min(min_Y, y1)
                        max_X = max(max_X, x2)
                        max_Y = max(max_Y, y2)
                        
                        corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0]])
                        face = pv.Polygon(corners)
                        fit_plotter.add_mesh(face, color="orange", opacity=0.6)
                        
            # 2. Draw Components
            target_comp = {}
            if case.get('IC') in edb.components.components:
                target_comp[case['IC']] = edb.components.components[case['IC']]
            if case.get('Source_name') in edb.components.components:
                target_comp[case['Source_name']] = edb.components.components[case['Source_name']]
                
            font_scale = 10000
            min_font_size = 6
            max_font_size = 30
            
            for comp_name, comp_inst in target_comp.items():
                x1, y1, x2, y2 = comp_inst.bounding_box
                width = x2 - x1
                height = y2 - y1
                
                min_X = min(min_X, x1)
                min_Y = min(min_Y, y1)
                max_X = max(max_X, x2)
                max_Y = max(max_Y, y2)

                corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0], [x1, y1, 0]])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))
                fit_plotter.add_mesh(rect, color="black", line_width=3)

                font_size = int(font_scale * min(width, height))
                font_size = max(min_font_size, min(font_size, max_font_size))
                text_center = [(x1 + x2) / 2, (y1 + y2) / 2, 0]
                fit_plotter.add_point_labels([text_center], [comp_name], font_size=font_size, text_color="blue", shape=None)

            if min_X == float('inf'):
                logger.log("Warning: Could not determine bounds for net path. Skipping images.", level=LogLevel.WARNING)
                fit_plotter.close()
                return

            # 3. Draw Zoom Area Box on FitView
            offset = 0.02
            z_x1, z_y1 = min_X - offset, min_Y - offset
            z_x2, z_y2 = max_X + offset, max_Y + offset
            
            z_corners = np.array([[z_x1, z_y1, 0], [z_x2, z_y1, 0], [z_x2, z_y2, 0], [z_x1, z_y2, 0], [z_x1, z_y1, 0]])
            z_rect = pv.PolyData(z_corners)
            z_rect.lines = np.hstack([[len(z_corners)]] + list(range(len(z_corners))))
            fit_plotter.add_mesh(z_rect, color="red", line_width=6)
            fit_plotter.add_point_labels([[z_x1, z_y2, 0]], ["Zoom Area"], font_size=50, text_color="red", shape=None)

            fit_plotter.camera_position = "xy"
            fit_plotter.remove_bounds_axes()
            fit_plotter.screenshot(str(fit_file), window_size=self._screen_size)
            fit_plotter.close()
            logger.log(f"Saved FitView: {fit_file}", level=LogLevel.DETAIL5)
            
            # --- ZoomView ---
            zoom_plotter = pv.Plotter(off_screen=True, title="ZoomView")
            zoom_plotter.background_color = 'white'
            
            # Redraw Net Path
            for net_name in target_nets:
                net = edb.nets.nets.get(net_name)
                if not net: continue
                for prim in net.primitives:
                    bbox = prim.bbox
                    if bbox and len(bbox) == 4:
                        x1, y1, x2, y2 = bbox
                        corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0]])
                        face = pv.Polygon(corners)
                        zoom_plotter.add_mesh(face, color="orange", opacity=0.6)
                        
            # Redraw Components
            for comp_name, comp_inst in target_comp.items():
                x1, y1, x2, y2 = comp_inst.bounding_box
                width = x2 - x1
                height = y2 - y1
                corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0], [x1, y1, 0]])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))
                zoom_plotter.add_mesh(rect, color="black", line_width=3)
                text_center = [(x1 + x2) / 2, (y1 + y2) / 2, 0]
                text = pv.Text3D(comp_name, center=text_center, height=0.2 * min(width, height))
                zoom_plotter.add_mesh(text, color="blue", lighting=False, style="surface")

            zoom_bounds = [z_x1, z_x2, z_y1, z_y2, -0.1, 0.1]
            set_parallel_camera_to_bounds(zoom_plotter, zoom_bounds, margin=0.01)
            zoom_plotter.screenshot(str(zoom_file), window_size=self._screen_size)
            zoom_plotter.close()
            logger.log(f"Saved ZoomView: {zoom_file}", level=LogLevel.DETAIL5)
            
        except Exception as e:
            logger.log(f"Warning: Failed to extract net path images: {e}", level=LogLevel.WARNING)
        finally:
            if edb:
                edb.close()


    def import_edb(self, edb_path, version):
        edb_path = Path(edb_path)
        prj_name = edb_path.with_suffix('.aedt')
        edb = None
        try:
            self._logger.log("Open EDB", level=LogLevel.DETAIL4)
            self._logger.log(f"{edb_path}", level=LogLevel.DETAIL5)
            edb = Edb(str(edb_path), edbversion=version)

            ports = edb.excitations
            self._logger.log(f"Ports : {list(ports.keys())}", level=LogLevel.DETAIL5)

            close_fn = getattr(edb, "close_edb", None) or getattr(edb, "close", None)
            if close_fn:
                close_fn()
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
                close_fn = getattr(edb, "close_edb", None) or getattr(edb, "close", None)
                if close_fn:
                    close_fn()
                self._logger.log(f"Close EDB : {edb_path}", level=LogLevel.DETAIL5)

        self._logger.log(f"Saved AEDT file : {prj_name}", level=LogLevel.DETAIL5)
        return prj_name


    def extract_ac_results(self, prj_name, case, version):
        """Extract AC PDN (Impedance) results and Touchstone files."""
        try:
            logger = self._logger
            logger.log("Extract AC PDN Results (Impedance & Touchstone)", level=LogLevel.DETAIL4)

            # 1. Export Layer Stackup XML
            stackup_xml = Path(self._outputFolder / "stackup.xml")
            if not stackup_xml.exists():
                logger.log("Save Stackup XML", level=LogLevel.DETAIL5)
                self._h3dl.oeditor.ExportStackupXML(stackup_xml)

            # 2. Get Setup and Sweep
            setup_name, sweep_name = self._resolve_setup_and_sweep(self._h3dl)
            if not setup_name:
                raise RuntimeError("No SYZ setup found in the project.")
            solution_name = f"{setup_name} : {sweep_name}" if sweep_name else setup_name

            # 3. Analyze
            logger.log(f"Analyze : {setup_name}", level=LogLevel.DETAIL5)
            self._h3dl.analyze(setup=setup_name)
            
            max_save_attempts = 5
            for attempt in range(max_save_attempts):
                try:
                    self._h3dl.save_project()
                    break
                except Exception as e:
                    if attempt < max_save_attempts - 1:
                        time.sleep(2)
                    else:
                        logger.log(f"All save attempts failed: {str(e)}", level=LogLevel.WARNING)

            # 4. Identify Ports
            ports = self._collect_h3dl_ports()
            if not ports:
                logger.log("Warning: No ports found for AC PDN extraction.", level=LogLevel.WARNING)
                return

            # 5. Create Impedance Plot
            siw_base_name = prj_name.stem 
            ic_name = case.get('IC', '')
            display_name = siw_base_name
            if ic_name and f"_{ic_name}_" in siw_base_name:
                net_part = siw_base_name.split(f"_{ic_name}_")[-1]
                display_name = f"{ic_name}_{net_part}"

            plot_name = f"Z_Param_{display_name}"
            # Z-Parameter Magnitude (Self-Impedance) for all ports
            expressions = [f"mag(Z({str(p)},{str(p)}))" for p in ports]
            
            logger.log(f"Create Report: {plot_name}", level=LogLevel.DETAIL5)
            self._h3dl.post.create_report(
                expressions=expressions,
                setup_sweep_name=solution_name,
                domain="Sweep",
                plot_type="Rectangular Plot",
                plot_name=plot_name
            )
            
            # 6. Export Image, CSV, and Touchstone
            out_dir = prj_name.parent
            img_file = out_dir / f"{plot_name}.jpg"
            csv_file = out_dir / f"{plot_name}.csv"
            ts_file = out_dir / f"{plot_name}.s{len(ports)}p"

            logger.log(f"Exporting Plot Image: {img_file}", level=LogLevel.DETAIL6)
            self._h3dl.post.export_report_to_jpg(str(out_dir), plot_name)
            
            logger.log(f"Exporting CSV: {csv_file}", level=LogLevel.DETAIL6)
            self._h3dl.post.export_report_to_file(str(out_dir), plot_name, extension=".csv")
            
            logger.log(f"Exporting Touchstone: {ts_file}", level=LogLevel.DETAIL6)
            ts_path, ts_err = self._export_touchstone_best_effort(setup_name, sweep_name, ts_file)
            if not ts_path:
                candidates = []
                candidates.extend(out_dir.glob(f"{plot_name}*.s*p"))
                aedt_results = prj_name.with_suffix(".aedtresults")
                if aedt_results.exists():
                    candidates.extend(aedt_results.rglob("*.s*p"))
                latest = self._pick_latest_file(candidates)
                if latest:
                    ts_path = str(latest)
            if not ts_path and ts_err:
                logger.log(f"Warning: Failed to export Touchstone: {ts_err}", level=LogLevel.WARNING)

            # Update case dict with new AC PDN artifacts
            case['Impedance_Plot'] = str(img_file)
            case['Impedance_CSV'] = str(csv_file)
            case['Touchstone'] = str(ts_path) if ts_path else ""

            self._h3dl.save_project()

        except Exception:
            logger.log(
                f"An error occurred while extracting AC PDN results : {traceback.format_exc()}",
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
        
        def init_aedt_with_retry(ver, max_retries=3):
            for attempt in range(max_retries):
                try:
                    h3dl = Hfss3dLayout(version=ver, non_graphical=True)
                    if not hasattr(h3dl, '_odesign') or h3dl._odesign is None:
                        raise ValueError("AEDT design initialization failed (invalid design object).")
                    return h3dl
                except Exception as e:
                    self._logger.log(f"[WARNING] AEDT initialization failed (attempt {attempt+1}/{max_retries}): {e}", level=LogLevel.WARNING)
                    time.sleep(10.0)
                    if attempt == max_retries - 1:
                        self._logger.log("[ERROR] AEDT initialization failed after retries.", level=LogLevel.ERROR)
                        raise PDNSessionException(ErrorCode.AEDT_LAUNCH_FAILURE, ver)

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

            self._logger.log("Extract AC PDN Impedance Results & Net Path Images", level=LogLevel.DETAIL2)
            self._logger.log(f"Start : {time.strftime('%Y.%m.%d, %H:%M:%S')}", level=LogLevel.DETAIL3)

            Settings.use_grpc_api = False
            self._h3dl = init_aedt_with_retry(version)
            restart_aedt = False

            for viewer_idx, (idx, case, viewer_record) in enumerate(viewer_cases):
                self._logger.log(f"Case #{idx + 1}", level=LogLevel.DETAIL3)

                if restart_aedt or (viewer_idx > 0 and viewer_idx % 5 == 0):
                    self._logger.log("Restarting AEDT instance to prevent memory leak...", level=LogLevel.DETAIL3)
                    if self._h3dl:
                        try:
                            self._h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                    time.sleep(10.0)  
                    self._h3dl = init_aedt_with_retry(version)
                    restart_aedt = False

                try:
                    for output_path in (
                        case.get('Impedance_Plot', ''),
                        case.get('Impedance_CSV', ''),
                        case.get('Touchstone', ''),
                        case.get('FitView', ''),
                        case.get('ZoomView', '')
                    ):
                        if output_path:
                            remove_artifact_path(Path(output_path))
                            
                    # 1. Extract Net Path Images (FitView, ZoomView) using EDB directly
                    self.extract_net_path_images(case['edb'], case, version)
                            
                    # 2. Import EDB to HFSS 3D Layout
                    prj_name = self.import_edb(case['edb'], version)
                    
                    # 3. Extract AC PDN Results (Impedance Plot, CSV, Touchstone)
                    self.extract_ac_results(prj_name, case, version)

                    expected_outputs = [
                        Path(case.get('Impedance_Plot', '')),
                        Path(case.get('Impedance_CSV', '')),
                        Path(case.get('FitView', '')),
                        Path(case.get('ZoomView', ''))
                    ]
                    missing_outputs = [path.name for path in expected_outputs if path.name and not path.exists()]
                    if missing_outputs:
                        raise RuntimeError(
                            f"Viewer output(s) missing: {', '.join(missing_outputs)}"
                        )
                    viewer_record["Viewer_Status"] = "Complete"
                    viewer_record["Outputs"] = [path.name for path in expected_outputs if path.name]
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
                    self._h3dl.release_desktop(close_projects=True, close_desktop=True)
                except Exception:
                    pass
                self._logger.log("Close AEDT", level=LogLevel.DETAIL3)

        return viewer_artifacts

