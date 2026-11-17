# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import traceback
import time
import os
import copy
import gc  
import ctypes
import re
import csv
import hashlib
import pyvista as pv
import numpy as np

from core.database import PDNSessionException, ErrorCode
from core.post_stage import export_post_edb, remove_artifact_path
from datetime import datetime
from pyaedt import Edb, Hfss3dLayout
from ansys.aedt.core.generic.settings import Settings
from EBU_lib.SIwave import SIwave
from core.logger import LogLevel
from core.aedt_version import aedt_display_version, normalize_aedt_version
from pathlib import Path
from tqdm import tqdm


def _find_impedance_peak(frequency_hz, magnitude_ohm, min_hz=1e5, max_hz=1e9):
    """Return the maximum finite impedance point inside the displayed frequency range."""
    frequency = np.asarray(frequency_hz, dtype=float)
    magnitude = np.asarray(magnitude_ohm, dtype=float)
    valid = (
        np.isfinite(frequency)
        & np.isfinite(magnitude)
        & (frequency >= float(min_hz))
        & (frequency <= float(max_hz))
        & (magnitude >= 0.0)
    )
    indices = np.flatnonzero(valid)
    if indices.size == 0:
        return None
    peak_index = int(indices[np.argmax(magnitude[indices])])
    return peak_index, float(frequency[peak_index]), float(magnitude[peak_index])


def _validate_pdn_plot_artifacts(csv_path, image_path, expected_rows):
    """Re-open exported artifacts instead of trusting the plotting call."""
    from PIL import Image

    csv_path = Path(csv_path).resolve()
    image_path = Path(image_path).resolve()
    expected_header = ["Frequency_Hz", "Z_Magnitude_Ohm", "Z_Real_Ohm", "Z_Imag_Ohm"]
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] != expected_header:
        raise RuntimeError(f"PDN impedance CSV header differs: {csv_path}")
    if len(rows) - 1 != int(expected_rows):
        raise RuntimeError(
            f"PDN impedance CSV row count differs: expected={expected_rows}, actual={len(rows) - 1}"
        )
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != 4 or not all(np.isfinite(float(value)) for value in row):
            raise RuntimeError(f"PDN impedance CSV contains invalid data at row {line_number}")

    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise RuntimeError(f"PDN impedance image is missing or empty: {image_path}")
    with Image.open(image_path) as image:
        image.load()
        width, height = image.size
        image_format = image.format
    if image_format != "JPEG" or (width, height) != (1400, 800):
        raise RuntimeError(
            f"PDN impedance image contract differs: format={image_format}, size={width}x{height}"
        )
    return {
        "status": "verified",
        "csvRows": len(rows) - 1,
        "imageFormat": image_format,
        "widthPx": width,
        "heightPx": height,
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
    }


def _create_unfilled_siw_capture_copy(project_path, layer_color="0xe1e1e1"):
    """Create a disposable SIwave view with every metal layer rendered unfilled."""
    source = Path(project_path).resolve()
    capture_copy = source.with_name(f".{source.stem}.capture_unfilled_{os.getpid()}{source.suffix}")
    lines = source.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    in_layers = False
    unfilled_layers = 0
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped == "B_LAYERS":
            in_layers = True
            output.append(line)
            continue
        if stripped == "E_LAYERS":
            in_layers = False
            output.append(line)
            continue
        values = line.split()
        if in_layers and len(values) > 15 and values[2] == "METAL":
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            values[6] = layer_color
            values[7] = "0"
            values[11:16] = ["1"] * 5
            line = " ".join(values) + newline
            unfilled_layers += 1
        output.append(line)
    if unfilled_layers == 0:
        raise RuntimeError(f"No metal layer record found in SIwave project: {source}")
    capture_copy.write_text("".join(output), encoding="utf-8")
    return capture_copy, unfilled_layers


def _fill_target_pixels(
    image_path,
    marker_rgb=(255, 255, 0),
    dilation_px=1,
    draw_box=False,
    box_padding=18,
    box_width=4,
):
    """Convert a private target marker into a visible red filled region in place."""
    from PIL import Image
    from scipy import ndimage

    path = Path(image_path)
    with Image.open(path) as source:
        pixels = np.asarray(source.convert("RGB")).copy()
    marker = np.asarray(marker_rgb, dtype=np.int16)
    target = np.all(np.abs(pixels.astype(np.int16) - marker) <= 45, axis=2)
    closed = ndimage.binary_closing(target, structure=np.ones((3, 3), dtype=bool))
    filled = ndimage.binary_fill_holes(closed)
    if dilation_px > 0:
        filled = ndimage.binary_dilation(
            filled,
            structure=np.ones((3, 3), dtype=bool),
            iterations=int(dilation_px),
        )
    rendered_target = target | filled
    pixels[rendered_target] = (255, 0, 0)
    image = Image.fromarray(pixels)
    box = None
    if draw_box and np.any(rendered_target):
        from PIL import ImageDraw

        # SIwave renders the selected SoC/package outline in bright green.
        # Include it in the FitView callout so the reader can immediately see
        # both the load location and the highlighted power route.
        soc_green = (
            (pixels[:, :, 1] >= 180)
            & (pixels[:, :, 0] <= 120)
            & (pixels[:, :, 2] <= 160)
        )
        box_region = rendered_target | soc_green
        ys, xs = np.nonzero(box_region)
        left = max(0, int(xs.min()) - box_padding)
        top = max(0, int(ys.min()) - box_padding)
        right = min(image.width - 1, int(xs.max()) + box_padding)
        bottom = min(image.height - 1, int(ys.max()) + box_padding)
        box = (left, top, right, bottom)
        ImageDraw.Draw(image).rectangle(box, outline=(255, 0, 0), width=box_width)
    image.save(path, format="PNG")
    return int(rendered_target.sum()), box


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


def _filled_quad(x1, y1, x2, y2, z=0.0):
    """Return a filled PyVista quad from EDB's 2-D bounding-box coordinates."""
    points = np.array(
        [[x1, y1, z], [x2, y1, z], [x2, y2, z], [x1, y2, z]],
        dtype=float,
    )
    return pv.PolyData(points, faces=np.array([4, 0, 1, 2, 3]))


def _safe_artifact_name(value):
    """Make a stable Windows-safe artifact name without losing case identity."""
    text = str(value or "").strip().replace("+", "")
    text = re.sub(r'[<>:"/\\|?*\s]+', "_", text)
    return text.strip("._") or "case"


def _resolve_touchstone_project(case):
    """Resolve the root SIW project that owns the .siwaveresults/.asol data."""
    explicit = case.get('_viewer_project')
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    run_siw = Path(case.get('_viewer_siw') or '')
    for parent in run_siw.parents:
        if parent.name.lower().endswith('.siwaveresults'):
            project = parent.with_suffix('.siw')
            if project.is_file():
                return project
            break
    raise RuntimeError(
        f"Original SIwave project for Touchstone export was not found: {run_siw}"
    )


def _is_touchstone_file(path):
    suffix = Path(path).suffix.lower()
    return suffix == ".ts" or bool(re.fullmatch(r"\.s\d+p", suffix))


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
            calls.append(("setup+sweep+path", lambda: self._h3dl.export_touchstone(
                setup=setup_name, sweep=sweep_name, output_file=str(ts_file))))
            calls.append(("setup+sweep", lambda: self._h3dl.export_touchstone(
                setup=setup_name, sweep=sweep_name)))
        if setup_name:
            calls.append(("setup+path", lambda: self._h3dl.export_touchstone(
                setup=setup_name, output_file=str(ts_file))))
        calls.append(("default", lambda: self._h3dl.export_touchstone()))
        for label, fn in calls:
            try:
                result = fn()
                if ts_file.exists():
                    return str(ts_file), ""
                if isinstance(result, str) and Path(result).exists():
                    return str(Path(result)), ""
                errs.append(f"{label}: no file generated")
            except Exception as e:
                errs.append(f"{label}: {e}")

        # Same low-level fallback used by the legacy Post flow when the
        # PyAEDT wrapper reports success but does not materialize a file.
        if setup_name:
            solution = f"{setup_name} : {sweep_name}" if sweep_name else setup_name
            odesign = getattr(self._h3dl, "odesign", None)
            export_network_data = getattr(odesign, "ExportNetworkData", None)
            if export_network_data:
                try:
                    variations = self._h3dl.available_variations.nominal_w_values_dict
                except Exception:
                    variations = ""
                for args in (
                    (variations, [solution], 3, str(ts_file).replace("\\", "/"), ["all"], False, 50, "S", -1, 0, 15, False, False, False),
                    (variations, solution, 3, str(ts_file).replace("\\", "/"), ["all"], False, 50, "S", -1, 0, 15, False, False, False),
                ):
                    try:
                        export_network_data(*args)
                        if ts_file.exists():
                            return str(ts_file), ""
                    except Exception as e:
                        errs.append(f"ExportNetworkData: {e}")
        return "", "; ".join(errs)

    def _export_report_csv(
        self, out_dir: Path, plot_name: str, csv_file: Path,
        expressions=None, solution_name=None,
    ):
        """Export a report and verify the file, with a direct COM fallback."""
        errors = []

        def wait_for_csv(timeout=0.75):
            deadline = time.time() + timeout
            while time.time() < deadline:
                if csv_file.is_file() and csv_file.stat().st_size > 0:
                    return csv_file
                time.sleep(0.25)
            return None

        try:
            result = self._h3dl.post.export_report_to_file(
                str(out_dir), plot_name, extension=".csv"
            )
            if result is False:
                errors.append("PyAEDT returned False")
        except Exception as exc:
            errors.append(f"PyAEDT: {exc}")
        created = wait_for_csv()
        if created:
            return created

        # ``oreportsetup`` belongs to the PostProcessor in current PyAEDT;
        # looking for it on Hfss3dLayout itself silently skipped this fallback.
        oreport = getattr(getattr(self._h3dl, "post", None), "oreportsetup", None)
        if oreport is None:
            oreport = getattr(self._h3dl, "oreportsetup", None)
        export_to_file = getattr(oreport, "ExportToFile", None)
        if export_to_file:
            for args in ((plot_name, str(csv_file), False), (plot_name, str(csv_file))):
                try:
                    result = export_to_file(*args)
                    created = wait_for_csv()
                    if created:
                        return created
                    errors.append(f"ExportToFile{len(args)} returned {result!r} without a file")
                except Exception as exc:
                    errors.append(f"ExportToFile: {exc}")
        else:
            errors.append("oreportsetup.ExportToFile unavailable")

        # A report can render to JPG while AEDT refuses ExportToFile.  Read
        # the already-solved data directly and let PyAEDT serialize it.
        if expressions and solution_name:
            try:
                solution_data = self._h3dl.post.get_solution_data(
                    expressions=expressions,
                    setup_sweep_name=solution_name,
                    domain="Sweep",
                )
                if solution_data:
                    result = solution_data.export_data_to_csv(
                        str(csv_file), delimiter=","
                    )
                    created = wait_for_csv()
                    if created:
                        return created
                    errors.append(f"SolutionData export returned {result!r} without a file")
                else:
                    errors.append("get_solution_data returned no data")
            except Exception as exc:
                errors.append(f"SolutionData: {exc}")

        candidate = self._pick_latest_file(out_dir.glob(f"{plot_name}*.csv"))
        if candidate:
            return candidate
        raise RuntimeError("CSV export did not create a file: " + " | ".join(errors))

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
                    case['Z_Parameter_Plot'] = Path(case.get('Z_Parameter_Plot', '')).name if case.get('Z_Parameter_Plot') else ""
                    case['Z_Parameter_CSV'] = Path(case.get('Z_Parameter_CSV', '')).name if case.get('Z_Parameter_CSV') else ""
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
        self._exportData["setting"]['tool']["version"] = aedt_display_version(engine_conf.get('version', ''))
        self._exportData["setting"]["stackup"] = "stackup.xml"
        self._exportData["setting"]["setting"] = copy.deepcopy(public_summary)
        update_case_keys(self._exportData["setting"]["setting"])

        self._logger.log(f"Simulation Result - Detail", level=LogLevel.DETAIL2)
        self._exportData["result_detail"]["result"] = copy.deepcopy(public_summary)
        update_case_keys(self._exportData["result_detail"]["result"])

    def _sync_post_artifact_metadata(self):
        """Rewrite JSON metadata from verified, final Post artifact paths."""
        artifacts = (
            'Impedance_Plot', 'Impedance_CSV', 'Touchstone',
            'Z_Parameter_Plot', 'Z_Parameter_CSV', 'Report_Type', 'Z_Parameter',
            'FitView', 'ZoomView',
        )
        current = {
            (str(case.get('IC', '')), str(case.get('Net', ''))): case
            for case in self._summary
        }
        row_groups = (
            self._exportData.get('result', {}).get('summary', []),
            self._exportData.get('setting', {}).get('setting', []),
            self._exportData.get('result_detail', {}).get('result', []),
        )
        for rows in row_groups:
            for row in rows:
                source = current.get((str(row.get('IC', '')), str(row.get('Net', ''))))
                if not source:
                    continue
                for field in artifacts:
                    value = source.get(field, '')
                    row[field] = Path(value).name if value else ""

        for key, value in self._exportData.items():
            output_file = Path(self._outputFolder) / f"{key}.json"
            temporary = output_file.with_suffix(output_file.suffix + ".tmp")
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(value, stream, indent=2, ensure_ascii=False, default=str)
            temporary.replace(output_file)
        self._logger.log(
            "Synchronized JSON metadata with verified Post artifacts",
            level=LogLevel.DETAIL2,
        )


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

    def _export_touchstone_from_siw(self, siw_file, version):
        """Export the completed SYZ result directly from SIwave, without solving."""
        source = Path(siw_file)
        out_dir = Path(self._outputFolder)
        base = out_dir / f"{source.stem}_SYZ"
        errors = []
        siw = None
        try:
            self._logger.log("Export Touchstone from completed SIwave result", level=LogLevel.DETAIL2)
            self._logger.log(f"Source SIW: {source}", level=LogLevel.DETAIL3)
            siw = SIwave(version=version, logger=self._logger)
            siw.open_project(source)
            export_fn = getattr(siw.oproject, "ScrExportSyzSimToTouchstone", None)
            if not export_fn:
                raise RuntimeError("ScrExportSyzSimToTouchstone API is unavailable")

            def candidates():
                return [
                    path for path in out_dir.glob(f"{base.name}*")
                    if path.is_file() and _is_touchstone_file(path)
                ]

            for solution_name in ("SYZ Sweep 1", "SYZ Sweep", "solution"):
                before = {
                    path.resolve(): path.stat().st_mtime_ns
                    for path in candidates()
                }
                try:
                    result = export_fn(solution_name, str(base))
                    generated_candidates = sorted(
                        candidates(),
                        key=lambda path: path.stat().st_mtime_ns,
                        reverse=True,
                    )
                    generated = [
                        path for path in generated_candidates
                        if path.resolve() not in before
                        or path.stat().st_mtime_ns > before[path.resolve()]
                    ]
                    if generated:
                        self._logger.log(
                            f"Touchstone exported: {generated[0]} (solution={solution_name})",
                            level=LogLevel.DETAIL3,
                        )
                        return generated[0]
                    if result == 0:
                        errors.append(
                            f"{solution_name}: API returned success but no .ts/.sNp file was updated"
                        )
                        break
                    errors.append(f"{solution_name}: return={result!r}, no file generated")
                except Exception as exc:
                    errors.append(f"{solution_name}: {exc}")
            raise RuntimeError(" | ".join(errors))
        finally:
            if siw is not None:
                siw.quit_application()

    @staticmethod
    def _case_port_index(network, case, case_index):
        names = list(network.port_names or [])
        target = _safe_artifact_name(
            f"PORT_{case.get('IC', '')}_{case.get('Net', '')}"
        ).upper().replace(".", "_")
        for index, name in enumerate(names):
            normalized = _safe_artifact_name(name).upper().replace(".", "_")
            if normalized == target or normalized.startswith(target + "_"):
                return index
        # SIwave omits port comments in some Touchstone revisions. Ports are
        # created in summary order, so use that stable order only when names
        # are unavailable.
        if not names and 0 <= case_index < network.nports:
            return case_index
        raise RuntimeError(
            f"Touchstone port not found for {case.get('IC')}:{case.get('Net')}; "
            f"port_names={names}"
        )

    def _load_and_validate_touchstone(self, touchstone, cases):
        import skrf as rf

        network = rf.Network(str(touchstone))
        if network.nports != len(cases):
            raise RuntimeError(
                f"Touchstone port contract mismatch: ports={network.nports}, cases={len(cases)}; "
                "VRM terminations must be embedded components, not exported ports"
            )
        if len(network.f) < 2 or np.any(~np.isfinite(network.f)):
            raise RuntimeError("Touchstone frequency data is missing or invalid")
        if np.any(network.f <= 0) or np.any(np.diff(network.f) <= 0):
            raise RuntimeError("Touchstone frequencies must be positive and strictly increasing")
        if np.any(~np.isfinite(network.z.real)) or np.any(~np.isfinite(network.z.imag)):
            raise RuntimeError("Touchstone contains non-finite impedance data")

        port_indices = [
            self._case_port_index(network, case, index)
            for index, case in enumerate(cases)
        ]
        if len(set(port_indices)) != len(port_indices):
            raise RuntimeError(f"Touchstone case ports are not unique: {port_indices}")
        self._logger.log(
            f"Touchstone validated: format={Path(touchstone).suffix}, "
            f"ports={network.nports}, frequencies={len(network.f)}, "
            f"range={network.f[0]:.6g}..{network.f[-1]:.6g} Hz",
            level=LogLevel.DETAIL3,
        )
        return network, port_indices

    @staticmethod
    def _case_target_nets(case):
        values = case.get('Full_Net_Chain') or case.get('Source_net') or []
        if isinstance(values, str):
            values = [values]
        values = list(values)
        if case.get('Net'):
            values.append(case['Net'])
        result = []
        seen = set()
        for value in values:
            name = str(value or '').strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    @staticmethod
    def _normalize_native_capture(source_png, output_jpg, size=(2400, 1350)):
        from PIL import Image, ImageOps

        with Image.open(source_png) as image:
            normalized = ImageOps.pad(
                image.convert('RGB'),
                size,
                method=Image.Resampling.LANCZOS,
                color=(255, 255, 255),
                centering=(0.5, 0.5),
            )
            temporary = Path(output_jpg).with_name(
                f"{Path(output_jpg).stem}.tmp{Path(output_jpg).suffix}"
            )
            normalized.save(
                temporary,
                format='JPEG',
                quality=96,
                subsampling=0,
                optimize=True,
            )
            temporary.replace(output_jpg)

    def _capture_case_images_from_siw(self, project_path, edb_path, cases, version):
        """Capture native SIwave Fit/Zoom views in one graphical session."""
        siw = None
        inventory_edb = None
        capture_project = None
        captures = []
        out_dir = Path(self._outputFolder)
        try:
            self._logger.log(
                "Capture native SIwave FitView & ZoomView",
                level=LogLevel.DETAIL2,
            )
            capture_project, unfilled_layer_count = _create_unfilled_siw_capture_copy(project_path)
            self._logger.log(
                f"Prepared unfilled-context SIwave capture view: metal_layers={unfilled_layer_count}",
                level=LogLevel.DETAIL3,
            )
            siw = SIwave(version=version, logger=self._logger)
            siw.open_project(str(capture_project))
            try:
                siw.oSiwave.RestoreWindow()
            except Exception:
                pass
            siw._maximize_window()
            project = siw.oproject
            required = (
                'ScrUnselectAll', 'ScrSelectNet', 'ScrFitSelection',
                'ScrFitAll', 'ScrSaveToPngFile', 'ScrEditNetColor',
            )
            missing = [name for name in required if not callable(getattr(project, name, None))]
            if missing:
                raise RuntimeError(
                    "SIwave native capture API unavailable: " + ", ".join(missing)
                )

            inventory_edb = Edb(str(edb_path), edbversion=version)
            all_net_names = sorted(
                [str(name) for name in inventory_edb.nets.nets.keys() if str(name).strip()],
                key=lambda value: (value.casefold(), value),
            )
            if not all_net_names:
                raise RuntimeError(f"EDB returned no net inventory: {edb_path}")
            inventory_by_key = {name.casefold(): name for name in all_net_names}
            target_inventory = {
                net_name.casefold()
                for case in cases
                for net_name in self._case_target_nets(case)
            }
            missing_targets = sorted(
                key for key in target_inventory if key not in inventory_by_key
            )
            if missing_targets:
                raise RuntimeError(
                    "Capture target nets are missing from EDB: " + ", ".join(missing_targets)
                )

            context_rgb = (225, 225, 225)
            # Yellow is an internal marker chosen to avoid the native magenta
            # layer colors and red board dimensions. It is converted to solid
            # red after capture.
            selected_rgb = (255, 255, 0)
            color_failures = []
            for net_name in all_net_names:
                try:
                    result = project.ScrEditNetColor(net_name, 1, *context_rgb)
                    if result is False or result == 0:
                        color_failures.append(net_name)
                except Exception:
                    color_failures.append(net_name)
            if color_failures:
                self._logger.log(
                    f"[WARNING] SIwave context color failed for "
                    f"{len(color_failures)}/{len(all_net_names)} nets; "
                    f"examples={color_failures[:10]}",
                    level=LogLevel.WARNING,
                )
            self._logger.log(
                f"Applied EDB net inventory to SIwave capture: nets={len(all_net_names)}, "
                f"context_rgb={context_rgb}, selected_rgb={selected_rgb}",
                level=LogLevel.DETAIL3,
            )

            previous_target_nets = []
            for case_index, case in enumerate(cases, start=1):
                target_nets = self._case_target_nets(case)
                if not target_nets:
                    raise RuntimeError(f"Case #{case_index} has no target net for capture")
                display_name = "_".join(filter(None, (
                    _safe_artifact_name(case.get('IC', '')),
                    _safe_artifact_name(case.get('Net', '')),
                )))
                fit_file = out_dir / f"{display_name}_FitView.jpg"
                zoom_file = out_dir / f"{display_name}_ZoomView.jpg"
                fit_png = out_dir / f".{display_name}_FitView.native.png"
                zoom_png = out_dir / f".{display_name}_ZoomView.native.png"

                project.ScrUnselectAll()
                for net_name in previous_target_nets:
                    project.ScrEditNetColor(net_name, 1, *context_rgb)
                selected = []
                for net_name in target_nets:
                    canonical_name = inventory_by_key[net_name.casefold()]
                    result = project.ScrSelectNet(canonical_name, 1)
                    if result is not False and result != 0:
                        selected.append(canonical_name)
                    project.ScrEditNetColor(canonical_name, 1, *selected_rgb)
                if not selected:
                    raise RuntimeError(
                        f"Case #{case_index} SIwave selected no target net: {target_nets}"
                    )
                show_selected = getattr(project, 'ScrShowSelectedNetsOnly', None)
                if callable(show_selected):
                    show_selected(0)

                project.ScrFitAll()
                project.ScrUnselectAll()
                project.ScrSaveToPngFile(str(fit_png))
                if not fit_png.is_file() or fit_png.stat().st_size <= 0:
                    raise RuntimeError(f"SIwave FitView capture was not created: {fit_png}")
                _, fit_box = _fill_target_pixels(
                    fit_png,
                    marker_rgb=selected_rgb,
                    dilation_px=3,
                    draw_box=True,
                )
                if fit_box is None:
                    raise RuntimeError(f"Case #{case_index} target pixels not found in FitView")
                self._normalize_native_capture(fit_png, fit_file)

                project.ScrUnselectAll()
                for net_name in selected:
                    project.ScrSelectNet(net_name, 1)
                project.ScrFitSelection()
                project.ScrUnselectAll()
                project.ScrSaveToPngFile(str(zoom_png))
                if not zoom_png.is_file() or zoom_png.stat().st_size <= 0:
                    raise RuntimeError(f"SIwave ZoomView capture was not created: {zoom_png}")
                _fill_target_pixels(zoom_png, marker_rgb=selected_rgb, dilation_px=1)
                self._normalize_native_capture(zoom_png, zoom_file)
                for temporary in (fit_png, zoom_png):
                    if temporary.exists():
                        temporary.unlink()

                captures.append((fit_file, zoom_file))
                previous_target_nets = list(selected)
                self._logger.log(
                    f"Native SIwave images saved: case={case_index}, "
                    f"nets={selected}, fit={fit_file.name}, zoom={zoom_file.name}",
                    level=LogLevel.DETAIL4,
                )
            return captures
        finally:
            if inventory_edb is not None:
                close_fn = getattr(inventory_edb, 'close_edb', None) or getattr(inventory_edb, 'close', None)
                if close_fn:
                    close_fn()
            if siw is not None:
                try:
                    close_fn = getattr(siw.oproject, 'ScrCloseProjectNoSave', None)
                    if callable(close_fn):
                        close_fn()
                except Exception:
                    pass
                siw.quit_application()
            if capture_project is not None and Path(capture_project).exists():
                try:
                    Path(capture_project).unlink()
                except Exception as exc:
                    self._logger.log(
                        f"[WARNING] Temporary unfilled capture project cleanup failed: {exc}",
                        level=LogLevel.WARNING,
                    )

    def _create_case_ac_artifacts(self, touchstone, network, port_index, case):
        """Create case CSV/JPG from an exported Touchstone network."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        impedance = network.z[:, port_index, port_index]
        frequency = network.f
        display_name = "_".join(filter(None, (
            _safe_artifact_name(case.get('IC', '')),
            _safe_artifact_name(case.get('Net', '')),
        )))
        plot_name = f"Z_Param_{display_name}"
        out_dir = Path(self._outputFolder)
        csv_file = out_dir / f"{plot_name}.csv"
        img_file = out_dir / f"{plot_name}.jpg"
        csv_tmp = csv_file.with_suffix(csv_file.suffix + ".tmp")
        img_tmp = img_file.with_suffix(img_file.suffix + ".tmp")

        with csv_tmp.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("Frequency_Hz", "Z_Magnitude_Ohm", "Z_Real_Ohm", "Z_Imag_Ohm"))
            for freq, value in zip(frequency, impedance):
                writer.writerow((float(freq), float(abs(value)), float(value.real), float(value.imag)))
        csv_tmp.replace(csv_file)

        magnitude = np.abs(impedance)
        frequency_ghz = frequency / 1e9
        spec_ohm = 10.0
        # SI-TDR publication contract: deterministic 1400x800 output,
        # concise legend and strict post-export evidence checks.
        figure, axis = plt.subplots(figsize=(14, 8), dpi=100, facecolor='white')
        axis.set_facecolor('#fbfcfe')
        axis.loglog(
            frequency_ghz, magnitude,
            color='#0875d1', linewidth=2.2,
            label=f"|Z({port_index + 1},{port_index + 1})|  {case.get('IC')} / {case.get('Net')}",
        )
        axis.axhline(
            spec_ohm, color='#d64545', linewidth=1.6, linestyle='--',
            label=f"Target {spec_ohm:g} Ω",
        )
        peak = _find_impedance_peak(frequency, magnitude)
        if peak is not None:
            peak_index, peak_frequency_hz, peak_impedance_ohm = peak
            peak_frequency_ghz = peak_frequency_hz / 1e9
            axis.scatter(
                [peak_frequency_ghz], [peak_impedance_ohm],
                marker='D', s=72, color='#e31a1c', edgecolor='white',
                linewidth=1.2, zorder=6, label='Peak',
            )
            horizontal_offset = -118 if peak_frequency_ghz >= 0.01 else 24
            axis.annotate(
                f"Peak\n{peak_frequency_ghz:.6g} GHz\n{peak_impedance_ohm:.6g} Ω",
                xy=(peak_frequency_ghz, peak_impedance_ohm),
                xytext=(horizontal_offset, -58),
                textcoords='offset points',
                fontsize=10, fontweight='bold', color='#9d1113',
                ha='left', va='top',
                bbox=dict(boxstyle='round,pad=0.35', facecolor='white', edgecolor='#e31a1c', alpha=0.94),
                arrowprops=dict(arrowstyle='->', color='#e31a1c', linewidth=1.3),
                annotation_clip=False,
                zorder=7,
            )
            case['Peak_Frequency_Hz'] = peak_frequency_hz
            case['Peak_Impedance_Ohm'] = peak_impedance_ohm
        axis.set_xlim(0.0001, 1.0)
        positive = magnitude[magnitude > 0]
        if positive.size:
            y_min = max(float(positive.min()) / 2.0, 1e-6)
            y_max = max(float(positive.max()) * 2.0, spec_ohm * 2.0)
            axis.set_ylim(y_min, y_max)
        axis.set_title(
            f"Z-Parameter Plot — Z({port_index + 1},{port_index + 1})",
            fontsize=17, fontweight='bold', pad=14,
        )
        axis.set_xlabel("Frequency [GHz]", fontsize=12)
        axis.set_ylabel(
            f"Z-Parameter Magnitude |Z({port_index + 1},{port_index + 1})| [Ω]",
            fontsize=12,
        )
        axis.grid(True, which='major', color='#9aa4af', linestyle='-', alpha=0.38)
        axis.grid(True, which='minor', color='#c7ced6', linestyle=':', alpha=0.32)
        axis.legend(loc='best', frameon=True, framealpha=0.95, fontsize=10)
        axis.tick_params(axis='both', which='both', labelsize=10)
        figure.tight_layout()
        figure.savefig(
            img_tmp, dpi=100, format="jpg", facecolor='white',
            pil_kwargs={'quality': 96, 'subsampling': 0, 'optimize': True},
        )
        plt.close(figure)
        img_tmp.replace(img_file)

        plot_evidence = _validate_pdn_plot_artifacts(csv_file, img_file, len(frequency))

        case['Impedance_Plot'] = str(img_file)
        case['Impedance_CSV'] = str(csv_file)
        # Keep the legacy impedance keys for web compatibility while exposing
        # the report's actual electrical quantity explicitly.
        case['Z_Parameter_Plot'] = str(img_file)
        case['Z_Parameter_CSV'] = str(csv_file)
        case['Report_Type'] = 'Z-Parameter Plot'
        case['Z_Parameter'] = f"Z({port_index + 1},{port_index + 1})"
        case['Touchstone'] = str(touchstone)
        case['Plot_Evidence'] = plot_evidence
        self._logger.log(
            f"Created AC artifacts from Touchstone: port={port_index + 1}, "
            f"csv={csv_file.name}, plot={img_file.name}"
            + (
                f", peak={case['Peak_Impedance_Ohm']:.6g}ohm@{case['Peak_Frequency_Hz'] / 1e9:.6g}GHz"
                if 'Peak_Frequency_Hz' in case else ""
            ),
            level=LogLevel.DETAIL4,
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

        display_name = "_".join(filter(None, (
            _safe_artifact_name(case.get('IC', '')),
            _safe_artifact_name(case.get('Net', '')),
        )))
            
        out_dir = Path(edb_path).parent
        fit_file = out_dir / f"{display_name}_FitView.jpg"
        zoom_file = out_dir / f"{display_name}_ZoomView.jpg"
        fit_tmp = fit_file.with_name(f"{fit_file.stem}.tmp{fit_file.suffix}")
        zoom_tmp = zoom_file.with_name(f"{zoom_file.stem}.tmp{zoom_file.suffix}")
        
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
                        face = _filled_quad(x1, y1, x2, y2)
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
            fit_plotter.screenshot(str(fit_tmp), window_size=self._screen_size)
            fit_plotter.close()
            fit_tmp.replace(fit_file)
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
                        face = _filled_quad(x1, y1, x2, y2)
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
            zoom_plotter.screenshot(str(zoom_tmp), window_size=self._screen_size)
            zoom_plotter.close()
            zoom_tmp.replace(zoom_file)
            logger.log(f"Saved ZoomView: {zoom_file}", level=LogLevel.DETAIL5)
            
        except Exception as e:
            logger.log(f"Warning: Failed to extract net path images: {e}", level=LogLevel.WARNING)
        finally:
            for temporary in (fit_tmp, zoom_tmp):
                if temporary.exists():
                    temporary.unlink()
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

            # 3. Reuse the completed SYZ solution.  Post-processing must never
            # launch a solver run; the main solve stage is the sole owner of
            # analysis.  If AEDT cannot see the saved solution, report/export
            # below will fail explicitly instead of silently re-solving it.
            try:
                existing_solutions = [
                    str(value)
                    for value in (self._h3dl.existing_analysis_sweeps or [])
                ]
            except Exception:
                existing_solutions = []
            logger.log(
                f"Reuse existing solution (no analyze): {solution_name}; "
                f"available={existing_solutions or ['not reported by API']}",
                level=LogLevel.DETAIL5,
            )

            # 4. Identify Ports
            ports = self._collect_h3dl_ports()
            if not ports:
                logger.log("Warning: No ports found for AC PDN extraction.", level=LogLevel.WARNING)
                return

            # 5. Create Impedance Plot
            # The Post EDB is shared by all cases, so its stem cannot identify
            # a case.  Preserve the legacy Post convention: IC + target net.
            display_name = "_".join(filter(None, (
                _safe_artifact_name(case.get('IC', '')),
                _safe_artifact_name(case.get('Net', '')),
            )))

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
            csv_path = self._export_report_csv(
                out_dir,
                plot_name,
                csv_file,
                expressions=expressions,
                solution_name=solution_name,
            )
            
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
            case['Impedance_CSV'] = str(csv_path)
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
        version = normalize_aedt_version(version)
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
            exported_edbs = {}
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
                    export_key = (str(Path(source_siw).resolve()), str(Path(case['edb']).resolve()))
                    if export_key not in exported_edbs:
                        exported_edbs[export_key] = self.export_edb(
                            source_siw, case['edb'], version
                        )
                    else:
                        self._logger.log(
                            f"Reuse shared Post EDB: {exported_edbs[export_key]}",
                            level=LogLevel.DETAIL4,
                        )
                    case['edb'] = exported_edbs[export_key]
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

            # Every PDN case is solved in its own SIwave project.  Therefore a
            # completed case normally exports one s1p, not one port in a
            # shared N-port Touchstone.  Keep all export/validation/rendering
            # work case-scoped so a broken result cannot suppress the other
            # completed cases.
            for idx, case, viewer_record in viewer_cases:
                self._logger.log(f"Case #{idx + 1}", level=LogLevel.DETAIL3)

                try:
                    for output_path in (
                        case.get('Impedance_Plot', ''),
                        case.get('Impedance_CSV', ''),
                    ):
                        if output_path:
                            remove_artifact_path(Path(output_path))

                    touchstone_project = _resolve_touchstone_project(case)
                    try:
                        case_touchstone = self._export_touchstone_from_siw(
                            touchstone_project, version
                        )
                    except Exception as exc:
                        viewer_record["Error_Phase"] = "touchstone_export"
                        raise RuntimeError(f"Touchstone export failed: {exc}") from exc

                    try:
                        touchstone_network, port_indices = self._load_and_validate_touchstone(
                            case_touchstone, [case]
                        )
                    except Exception as exc:
                        viewer_record["Error_Phase"] = "touchstone_validation"
                        raise RuntimeError(f"Touchstone validation failed: {exc}") from exc

                    try:
                        native_captures = self._capture_case_images_from_siw(
                            touchstone_project, case['edb'], [case], version
                        )
                        case['FitView'] = str(native_captures[0][0])
                        case['ZoomView'] = str(native_captures[0][1])
                    except Exception as exc:
                        viewer_record["Error_Phase"] = "image_capture"
                        raise RuntimeError(f"SIwave image capture failed: {exc}") from exc

                    # Read only this completed case's Touchstone. No AEDT
                    # import and no solver call are allowed in Post.
                    try:
                        self._create_case_ac_artifacts(
                            case_touchstone,
                            touchstone_network,
                            port_indices[0],
                            case,
                        )
                    except Exception as exc:
                        viewer_record["Error_Phase"] = "plot_generation"
                        raise RuntimeError(f"Impedance artifact generation failed: {exc}") from exc

                    output_fields = (
                        'Impedance_Plot', 'Impedance_CSV', 'Touchstone',
                        'FitView', 'ZoomView',
                    )
                    expected_outputs = [
                        Path(case[field]) for field in output_fields if case.get(field)
                    ]
                    missing_outputs = [
                        field if not case.get(field) else Path(case[field]).name
                        for field in output_fields
                        if not case.get(field) or not Path(case[field]).exists()
                    ]
                    if missing_outputs:
                        raise RuntimeError(
                            f"Viewer output(s) missing: {', '.join(missing_outputs)}"
                        )
                    viewer_record["Viewer_Status"] = "Complete"
                    viewer_record["Outputs"] = [path.name for path in expected_outputs if path.name]
                    time.sleep(2.0)
                except Exception as exc:
                    viewer_record["Viewer_Status"] = "Error"
                    viewer_record.setdefault("Error_Phase", "viewer_generation")
                    viewer_record["Error"] = str(exc)
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

        self._sync_post_artifact_metadata()
        return viewer_artifacts

