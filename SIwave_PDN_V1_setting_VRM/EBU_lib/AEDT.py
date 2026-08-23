# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import math
import shutil
import time
from pathlib import Path

from core.logger import LogLevel


class AEDT:
    """AEDT-side helper for PDN cutout and solve flow."""

    def __init__(self, version="2025.1", logger=None):
        self.version = version
        self.logger = logger

    def _log(self, msg, level=LogLevel.INFO):
        if self.logger:
            self.logger.log(msg, level=level)

    @staticmethod
    def _safe(text):
        return "".join(c for c in str(text or "") if c.isalnum() or c in ("_", "-")).strip("_-")

    @staticmethod
    def _collect_signal_nets(case):
        net = str(case.get("Net", ""))
        chain = list(case.get("Full_Net_Chain", [])) or [net]
        signal_nets = []
        seen = set()
        for n in chain + [net]:
            name = str(n).strip()
            if not name:
                continue
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            signal_nets.append(name)
        return signal_nets

    @staticmethod
    def _parse_touchstone_header(header_line: str):
        unit_mult = {
            "HZ": 1.0,
            "KHZ": 1e3,
            "MHZ": 1e6,
            "GHZ": 1e9,
        }
        fmt = "MA"
        z0 = 50.0
        tokens = [t.strip().upper() for t in str(header_line or "").split() if t.strip()]
        for tok in tokens:
            if tok in unit_mult:
                freq_mul = unit_mult[tok]
                break
        else:
            freq_mul = 1.0
        if "RI" in tokens:
            fmt = "RI"
        elif "DB" in tokens:
            fmt = "DB"
        elif "MA" in tokens:
            fmt = "MA"
        if "R" in tokens:
            try:
                ridx = tokens.index("R")
                z0 = float(tokens[ridx + 1])
            except Exception:
                pass
        return freq_mul, fmt, z0

    def _extract_z11_from_touchstone(self, ts_path: Path):
        ts_path = Path(ts_path)
        if not ts_path.exists():
            return []
        data = []
        freq_mul, fmt, z0 = 1.0, "MA", 50.0
        try:
            with ts_path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("!"):
                        continue
                    if line.startswith("#"):
                        freq_mul, fmt, z0 = self._parse_touchstone_header(line)
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    try:
                        freq_hz = float(parts[0]) * freq_mul
                        a = float(parts[1])
                        b = float(parts[2])
                    except Exception:
                        continue
                    if fmt == "RI":
                        s11 = complex(a, b)
                    elif fmt == "DB":
                        mag = 10.0 ** (a / 20.0)
                        ang = math.radians(b)
                        s11 = complex(mag * math.cos(ang), mag * math.sin(ang))
                    else:  # MA
                        ang = math.radians(b)
                        s11 = complex(a * math.cos(ang), a * math.sin(ang))
                    denom = (1.0 - s11)
                    if abs(denom) < 1e-12:
                        continue
                    zin = z0 * (1.0 + s11) / denom
                    data.append((freq_hz, abs(zin)))
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] Touchstone parse failed: {e}", level=LogLevel.WARNING)
            return []
        return data

    def _write_impedance_artifacts_from_touchstone(self, ts_path: Path, z_csv: Path, z_plot: Path):
        try:
            from PIL import Image, ImageDraw
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] PIL import failed for plot generation: {e}", level=LogLevel.WARNING)
            return False
        points = self._extract_z11_from_touchstone(ts_path)
        if len(points) < 2:
            return False
        try:
            z_csv.parent.mkdir(parents=True, exist_ok=True)
            with z_csv.open("w", encoding="utf-8") as f:
                f.write("Freq_Hz,Z11_Ohm_Mag\n")
                for freq, mag in points:
                    f.write(f"{freq:.6e},{mag:.6e}\n")
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] CSV write failed: {e}", level=LogLevel.WARNING)
        try:
            w, h = 1600, 900
            ml, mr, mt, mb = 95, 35, 45, 70
            img = Image.new("RGB", (w, h), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.rectangle((ml, mt, w - mr, h - mb), outline=(70, 70, 70), width=2)

            fmin = min(p[0] for p in points)
            fmax = max(p[0] for p in points)
            zmin = min(p[1] for p in points)
            zmax = max(p[1] for p in points)
            if fmax <= fmin:
                fmax = fmin + 1.0
            if zmax <= zmin:
                zmax = zmin + 1.0

            poly = []
            for freq, mag in points:
                x = ml + int((freq - fmin) / (fmax - fmin) * (w - ml - mr))
                y = mt + int((zmax - mag) / (zmax - zmin) * (h - mt - mb))
                poly.append((x, y))
            if len(poly) >= 2:
                draw.line(poly, fill=(230, 110, 20), width=3)
            draw.text((ml, 14), "Impedance Magnitude from Touchstone", fill=(20, 20, 20))
            draw.text((ml, h - mb + 18), f"Freq: {fmin:.3e} ~ {fmax:.3e} Hz", fill=(50, 50, 50))
            draw.text((ml + 420, h - mb + 18), f"|Z11|: {zmin:.3e} ~ {zmax:.3e} Ohm", fill=(50, 50, 50))
            img.save(str(z_plot), quality=95)
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] Plot write failed: {e}", level=LogLevel.WARNING)
        return z_csv.exists() or z_plot.exists()

    def _find_touchstone_artifact(self, output_dir: Path, aedt_proj: Path, safe_case: str):
        patterns = [
            f"Z_Param_{safe_case}.s*p",
            f"*{safe_case}*.s*p",
            "*.s*p",
            "*.ts",
            "*.touchstone",
        ]
        candidates = []
        for pat in patterns:
            candidates.extend(output_dir.glob(pat))
        aedt_results = aedt_proj.with_suffix(".aedtresults")
        if aedt_results.exists():
            for pat in patterns:
                candidates.extend(aedt_results.rglob(pat))
            # Common solver output folders may hide touchstone deeper than the report root.
            for sub in ("", "Results", "Data", "ProjectPreview", "HFSS3DLayoutDesign1"):
                probe = aedt_results / sub if sub else aedt_results
                if probe.exists():
                    for pat in patterns:
                        candidates.extend(probe.rglob(pat))
        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            self._log(
                f"[AEDT][ART][INFO] No touchstone found for case={safe_case} under {output_dir} / {aedt_results}",
                level=LogLevel.DETAIL1,
            )
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def _collect_port_names(self, h3dl, cutout_path: Path):
        ports = []
        try:
            excitations_raw = h3dl.excitations or []
            if isinstance(excitations_raw, dict):
                ports.extend([str(k).strip() for k in excitations_raw.keys() if str(k).strip()])
            elif isinstance(excitations_raw, (list, tuple, set)):
                ports.extend([str(p).strip() for p in excitations_raw if str(p).strip()])
            else:
                try:
                    ports.extend([str(p).strip() for p in list(excitations_raw) if str(p).strip()])
                except Exception:
                    pass
        except Exception:
            pass

        # COM fallback path for sessions where pyaedt.excitations is empty.
        if not ports:
            oexc = getattr(h3dl, "oexcitation", None)
            if oexc:
                for name in ("GetAllPortsList", "GetAllPorts", "GetAllExcitations"):
                    fn = getattr(oexc, name, None)
                    if not fn:
                        continue
                    try:
                        vals = fn()
                        if isinstance(vals, (list, tuple)):
                            ports.extend([str(v).strip() for v in vals if str(v).strip()])
                        elif vals:
                            ports.append(str(vals).strip())
                    except Exception:
                        continue

        # EDB fallback for portability: use existing excitation names in cutout DB.
        if not ports:
            edb = None
            try:
                from pyaedt import Edb
                edb = Edb(str(cutout_path), edbversion=self.version)
                ex = edb.excitations or {}
                if isinstance(ex, dict):
                    ports.extend([str(k).strip() for k in ex.keys() if str(k).strip()])
            except Exception:
                pass
            finally:
                if edb:
                    try:
                        edb.close_edb()
                    except Exception:
                        pass

        # Keep stable order and uniqueness.
        norm = []
        seen = set()
        for p in ports:
            key = p.upper()
            if key in seen:
                continue
            seen.add(key)
            norm.append(p)
        return norm

    def _export_touchstone_best_effort(self, h3dl, setup_name, sweep_name, out_path: Path):
        out_path = Path(out_path)
        export_errs = []
        calls = []
        if setup_name and sweep_name:
            calls.append(("setup+sweep+path", lambda: h3dl.export_touchstone(setup_name, sweep_name, str(out_path))))
        if setup_name:
            calls.append(("setup+path", lambda: h3dl.export_touchstone(setup_name, str(out_path))))
        calls.append(("path-only", lambda: h3dl.export_touchstone(str(out_path))))

        for label, fn in calls:
            try:
                fn()
                if out_path.exists():
                    return str(out_path), None
            except Exception as e:
                export_errs.append(f"{label}: {e}")
        return "", "; ".join(export_errs)

    def _render_fullboard_highlight_images(
        self,
        full_edb_path: Path,
        target_nets,
        fit_view_path: Path,
        zoom_view_path: Path,
        title_text: str = "",
    ):
        """
        Render full-board images from the original EDB:
        - FitView: whole PCB with target nets highlighted
        - ZoomView: zoomed region around highlighted nets
        """
        try:
            from pyaedt import Edb
            from PIL import Image, ImageDraw
        except Exception as e:
            self._log(f"[AEDT][IMG][WARNING] Full-board render dependencies unavailable: {e}", level=LogLevel.WARNING)
            return False

        full_edb_path = Path(full_edb_path).resolve()
        fit_view_path = Path(fit_view_path).resolve()
        zoom_view_path = Path(zoom_view_path).resolve()
        fit_view_path.parent.mkdir(parents=True, exist_ok=True)

        target_set = {str(n).strip().upper() for n in (target_nets or []) if str(n).strip()}
        if not target_set:
            return False

        edb = None
        try:
            edb = Edb(str(full_edb_path), edbversion=self.version)

            all_boxes = []
            target_boxes = []
            min_x, min_y = float("inf"), float("inf")
            max_x, max_y = float("-inf"), float("-inf")

            for net_name, net_obj in (edb.nets.nets or {}).items():
                if not net_obj:
                    continue
                is_target = str(net_name).strip().upper() in target_set
                try:
                    prims = list(net_obj.primitives or [])
                except Exception:
                    prims = []
                for prim in prims:
                    try:
                        bbox = prim.bbox
                        if not bbox or len(bbox) != 4:
                            continue
                        x1, y1, x2, y2 = [float(v) for v in bbox]
                        if x2 < x1:
                            x1, x2 = x2, x1
                        if y2 < y1:
                            y1, y2 = y2, y1
                        if (x2 - x1) <= 0 or (y2 - y1) <= 0:
                            continue
                        all_boxes.append((x1, y1, x2, y2, is_target))
                        min_x = min(min_x, x1)
                        min_y = min(min_y, y1)
                        max_x = max(max_x, x2)
                        max_y = max(max_y, y2)
                        if is_target:
                            target_boxes.append((x1, y1, x2, y2))
                    except Exception:
                        continue

            if not all_boxes:
                return False

            # DCIR-like output quality: render at 8K canvas and keep strong supersampling.
            width, height = 7680, 4320
            ss = 2
            canvas_w, canvas_h = width * ss, height * ss
            margin = 0.03
            dx = max(max_x - min_x, 1e-12)
            dy = max(max_y - min_y, 1e-12)

            x_lo = min_x - dx * margin
            x_hi = max_x + dx * margin
            y_lo = min_y - dy * margin
            y_hi = max_y + dy * margin
            sx = (canvas_w - 1) / max(x_hi - x_lo, 1e-12)
            sy = (canvas_h - 1) / max(y_hi - y_lo, 1e-12)

            def _map_rect(x1, y1, x2, y2):
                px1 = int((x1 - x_lo) * sx)
                px2 = int((x2 - x_lo) * sx)
                py1 = int((y_hi - y1) * sy)
                py2 = int((y_hi - y2) * sy)
                left, right = min(px1, px2), max(px1, px2)
                top, bottom = min(py1, py2), max(py1, py2)
                if right - left < ss:
                    right = left + ss
                if bottom - top < ss:
                    bottom = top + ss
                return left, top, right, bottom

            # DCIR-like visual tuning: stronger contrast + clear boundary emphasis.
            bg_color = (244, 249, 253)
            other_net_color = (182, 199, 214)
            other_edge = (146, 165, 180)
            target_fill = (245, 141, 36)
            target_edge = (155, 58, 0)
            target_halo = (255, 214, 120)

            fit_img = Image.new("RGB", (canvas_w, canvas_h), bg_color)
            draw = ImageDraw.Draw(fit_img)

            board_area = max(dx * dy, 1e-12)
            non_target_boxes = []
            target_draw_boxes = []
            for x1, y1, x2, y2, is_target in all_boxes:
                if is_target:
                    target_draw_boxes.append((x1, y1, x2, y2))
                else:
                    non_target_boxes.append((x1, y1, x2, y2))

            # Draw background first; skip giant polygons that make image look "blank".
            for x1, y1, x2, y2 in non_target_boxes:
                rect = _map_rect(x1, y1, x2, y2)
                area_ratio = ((x2 - x1) * (y2 - y1)) / board_area
                if area_ratio > 0.70:
                    draw.rectangle(rect, outline=other_edge, width=max(1, ss))
                else:
                    draw.rectangle(rect, fill=other_net_color, outline=other_edge, width=max(1, ss))

            # Draw target nets last so they are always visible.
            for x1, y1, x2, y2 in target_draw_boxes:
                rect = _map_rect(x1, y1, x2, y2)
                # Halo first, then target shape for high visibility on dense planes.
                h = max(3, ss * 2)
                l, t, r, b = rect
                draw.rectangle((l - h, t - h, r + h, b + h), fill=target_halo, outline=None)
                draw.rectangle(rect, fill=target_fill, outline=target_edge, width=max(3, ss * 2))

            # Board boundary cue (helps visual orientation like DCIR capture).
            draw.rectangle(
                (12 * ss, 12 * ss, canvas_w - 12 * ss, canvas_h - 12 * ss),
                outline=(150, 176, 197),
                width=max(2, ss),
            )

            if title_text:
                draw.rectangle(
                    (20 * ss, 16 * ss, min(canvas_w - 20 * ss, 1800 * ss), 90 * ss),
                    fill=(255, 255, 255),
                    outline=(60, 60, 60),
                    width=max(1, ss),
                )
                draw.text((32 * ss, 36 * ss), title_text, fill=(30, 30, 30))

            fit_final = fit_img.resize((width, height), resample=Image.Resampling.LANCZOS)
            fit_final.save(str(fit_view_path), quality=100, subsampling=0, optimize=True)

            # Zoom around highlighted nets; if none found, duplicate fit image.
            if target_boxes:
                tmin_x = min(b[0] for b in target_boxes)
                tmin_y = min(b[1] for b in target_boxes)
                tmax_x = max(b[2] for b in target_boxes)
                tmax_y = max(b[3] for b in target_boxes)
                pad_x = max((tmax_x - tmin_x) * 0.35, dx * 0.03)
                pad_y = max((tmax_y - tmin_y) * 0.35, dy * 0.03)
                zrect = _map_rect(tmin_x - pad_x, tmin_y - pad_y, tmax_x + pad_x, tmax_y + pad_y)
                zl, zt, zr, zb = zrect
                zl = max(0, min(canvas_w - 1, zl))
                zr = max(zl + 1, min(canvas_w, zr))
                zt = max(0, min(canvas_h - 1, zt))
                zb = max(zt + 1, min(canvas_h, zb))
                zoom_img = fit_img.crop((zl, zt, zr, zb)).resize((width, height), resample=Image.Resampling.LANCZOS)
                # Add zoom border for readability in reports.
                zdraw = ImageDraw.Draw(zoom_img)
                zdraw.rectangle((8, 8, width - 8, height - 8), outline=(120, 145, 165), width=3)
                zoom_img.save(str(zoom_view_path), quality=100, subsampling=0, optimize=True)
            else:
                shutil.copy2(fit_view_path, zoom_view_path)

            self._log(
                f"[AEDT][IMG] Full-board highlight images exported: {fit_view_path.name}, {zoom_view_path.name}",
                level=LogLevel.INFO,
            )
            return fit_view_path.exists() and zoom_view_path.exists()
        except Exception as e:
            self._log(f"[AEDT][IMG][WARNING] Full-board highlight render failed: {e}", level=LogLevel.WARNING)
            return False
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass

    def run_cutout_batch(self, cases, model_name: str, ref_edb_path: Path, output_dir: Path, conf_data: dict):
        try:
            from pyaedt import Edb, Hfss3dLayout
            from ansys.aedt.core.generic.settings import Settings
        except Exception as e:
            raise RuntimeError(f"AEDT import failed: {e}")

        cut_cfg = conf_data.get("PDN", {}).get("setup", {}).get("aedtCutout", {})
        extent_type = str(cut_cfg.get("extent_type", "Bounding"))
        expansion_size = float(cut_cfg.get("expansion_size", 0.002))
        include_pingroups = bool(cut_cfg.get("include_pingroups", True))
        check_terminals = bool(cut_cfg.get("check_terminals", True))
        preserve_models = bool(cut_cfg.get("preserve_components_with_model", True))
        non_graphical = bool(cut_cfg.get("non_graphical", False))

        Settings.use_grpc_api = False
        summary = {"Total": len(cases or []), "Done": 0, "Skipped": 0, "Records": []}
        ref_edb_path = Path(ref_edb_path).resolve()
        output_dir = Path(output_dir).resolve()
        source_master = output_dir / f"{model_name}_CUTOUT_SOURCE_MASTER.aedb"
        try:
            if source_master.exists():
                shutil.rmtree(source_master, ignore_errors=True)
            shutil.copytree(ref_edb_path, source_master)
            self._log(f"[AEDT][CUTOUT] Saved source master EDB: {source_master}", level=LogLevel.DETAIL1)
        except Exception as e:
            self._log(f"[AEDT][CUTOUT][WARNING] Failed to save source master EDB: {e}", level=LogLevel.WARNING)

        for idx, case in enumerate(cases or [], start=1):
            ic = str(case.get("IC", ""))
            net = str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))))
            pcb_net = str(case.get("Net", ""))
            gnd = str(case.get("GND_Net", "GND"))
            signal_nets = self._collect_signal_nets(case)
            if not signal_nets:
                summary["Skipped"] += 1
                summary["Records"].append(
                    {"Case_Index": idx, "IC": ic, "Net": net, "PCB_Net": pcb_net, "Status": "Skipped", "Reason": "No signal nets"}
                )
                continue

            cutout_name = f"{model_name}_CUTOUT_{idx:03d}_{self._safe(ic)}_{self._safe(net)}.aedb"
            cutout_path = output_dir / cutout_name
            aedt_proj = cutout_path.with_suffix(".aedt")
            case_src_edb = output_dir / f"{model_name}_CUTSRC_{idx:03d}.aedb"

            edb = None
            h3dl = None
            try:
                self._log(
                    f"[AEDT][CUTOUT] Case#{idx}: IC={ic}, Net={net}, PCB_Net={pcb_net}, signal_nets={signal_nets}, gnd={gnd}",
                    level=LogLevel.INFO,
                )
                if case_src_edb.exists():
                    shutil.rmtree(case_src_edb, ignore_errors=True)
                shutil.copytree(ref_edb_path, case_src_edb)

                edb = Edb(str(case_src_edb), edbversion=self.version)
                edb.cutout(
                    signal_list=signal_nets,
                    reference_list=[gnd],
                    extent_type=extent_type,
                    expansion_size=expansion_size,
                    output_aedb_path=str(cutout_path),
                    open_cutout_at_end=False,
                    use_pyaedt_cutout=True,
                    include_pingroups=include_pingroups,
                    check_terminals=check_terminals,
                    preserve_components_with_model=preserve_models,
                )
                edb.close_edb()
                edb = None

                h3dl = Hfss3dLayout(version=self.version, non_graphical=non_graphical)
                imported = h3dl.import_edb(str(cutout_path))
                if imported is False:
                    raise RuntimeError(f"Failed to import cutout EDB: {cutout_path}")
                setup = h3dl.create_setup(name=f"SYZ_CUTOUT_{idx}")
                h3dl.analyze(setup=setup.name if setup else None)
                h3dl.save_project(file_name=str(aedt_proj))

                # Best-effort extraction of impedance artifacts for result payload.
                safe_case = f"{self._safe(ic)}_{self._safe(net)}"
                z_plot = output_dir / f"Z_Param_{safe_case}.jpg"
                z_csv = output_dir / f"Z_Param_{safe_case}.csv"
                fit_view = output_dir / f"{safe_case}_FitView.jpg"
                zoom_view = output_dir / f"{safe_case}_ZoomView.jpg"
                touchstone = ""
                msg = "OK"
                try:
                    ports = self._collect_port_names(h3dl, cutout_path)
                    self._log(f"[AEDT][ART] Ports detected for case#{idx}: {ports}", level=LogLevel.DETAIL1)
                    setups = h3dl.setups or []
                    setup_name = setup.name if setup else (setups[0].name if setups else None)
                    sweeps = setups[0].sweeps if setups else []
                    sweep_name = sweeps[0].name if sweeps else None
                    solution_name = f"{setup_name} : {sweep_name}" if (setup_name and sweep_name) else setup_name

                    if setup_name and ports:
                        expressions = [f"mag(Z({p},{p}))" for p in ports]
                        plot_name = f"Z_Param_{safe_case}"
                        h3dl.post.create_report(
                            expressions=expressions,
                            setup_sweep_name=solution_name,
                            domain="Sweep",
                            plot_type="Rectangular Plot",
                            plot_name=plot_name,
                        )
                        h3dl.post.export_report_to_jpg(str(output_dir), plot_name)
                        h3dl.post.export_report_to_file(str(output_dir), plot_name, extension=".csv")
                        jpg_candidates = sorted(output_dir.glob(f"{plot_name}*.jpg"))
                        csv_candidates = sorted(output_dir.glob(f"{plot_name}*.csv"))
                        if jpg_candidates and not z_plot.exists():
                            z_plot = jpg_candidates[0]
                        if csv_candidates and not z_csv.exists():
                            z_csv = csv_candidates[0]
                        ts_file = output_dir / f"{plot_name}.s{max(1, len(ports))}p"
                        touchstone, ts_err = self._export_touchstone_best_effort(h3dl, setup_name, sweep_name, ts_file)
                        if ts_err and not touchstone:
                            self._log(
                                f"[AEDT][ART][WARNING] Touchstone export failed for case#{idx}: {ts_err}",
                                level=LogLevel.WARNING,
                            )
                    else:
                        self._log(
                            f"[AEDT][ART][WARNING] Report export skipped for case#{idx}: setup={setup_name}, ports={ports}",
                            level=LogLevel.WARNING,
                        )

                    if not touchstone:
                        ts_found = self._find_touchstone_artifact(output_dir, aedt_proj, safe_case)
                        if ts_found:
                            touchstone = str(ts_found)

                    if touchstone and (not z_plot.exists() or not z_csv.exists()):
                        self._write_impedance_artifacts_from_touchstone(
                            ts_path=Path(touchstone),
                            z_csv=z_csv,
                            z_plot=z_plot,
                        )

                    # Image policy: use full-board EDB highlight images first (not cutout preview).
                    fullboard_img_ok = self._render_fullboard_highlight_images(
                        full_edb_path=ref_edb_path,
                        target_nets=signal_nets,
                        fit_view_path=fit_view,
                        zoom_view_path=zoom_view,
                        title_text=f"{ic} | {net}",
                    )
                    if not fullboard_img_ok:
                        preview_ok = h3dl.export_design_preview_to_jpg(str(fit_view))
                        if (preview_ok is False) or (not fit_view.exists()):
                            msg = "OK (solve done, image unavailable)"
                        else:
                            shutil.copy2(fit_view, zoom_view)
                except Exception as artifact_err:
                    self._log(
                        f"[AEDT][ART][WARNING] Artifact export partial for case#{idx}: {artifact_err}",
                        level=LogLevel.WARNING,
                    )
                    msg = f"OK (solve done, artifact export partial: {artifact_err})"

                summary["Done"] += 1
                summary["Records"].append(
                    {
                        "Case_Index": idx,
                        "IC": ic,
                        "Net": net,
                        "PCB_Net": pcb_net,
                        "Status": "Done",
                        "Cutout_Edb": str(cutout_path),
                        "Aedt_Project": str(aedt_proj),
                        "Impedance_Plot": str(z_plot) if z_plot.exists() else "",
                        "Impedance_CSV": str(z_csv) if z_csv.exists() else "",
                        "Touchstone": touchstone,
                        "FitView": str(fit_view) if fit_view.exists() else "",
                        "ZoomView": str(zoom_view) if zoom_view.exists() else "",
                        "Message": msg,
                    }
                )
            except Exception as e:
                summary["Skipped"] += 1
                summary["Records"].append(
                    {"Case_Index": idx, "IC": ic, "Net": net, "PCB_Net": pcb_net, "Status": "Skipped", "Reason": str(e)}
                )
                self._log(f"[AEDT][CUTOUT][WARNING] Case#{idx} failed: {e}", level=LogLevel.WARNING)
            finally:
                if edb:
                    try:
                        edb.close_edb()
                    except Exception:
                        pass
                if h3dl:
                    try:
                        h3dl.release_desktop(close_projects=True, close_desktop=True)
                    except Exception:
                        pass
                if case_src_edb.exists():
                    try:
                        shutil.rmtree(case_src_edb, ignore_errors=True)
                    except Exception:
                        pass

        out = output_dir / "aedt_cutout_result.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        self._log(
            f"[AEDT][CUTOUT] Summary: total={summary['Total']}, done={summary['Done']}, skipped={summary['Skipped']}",
            level=LogLevel.INFO,
        )
        self._log(f"[AEDT][CUTOUT] Exported: {out}", level=LogLevel.DETAIL1)
        return summary

    def export_full_presolve_aedt(self, ref_edb_path: Path, output_dir: Path, project_stem: str):
        """Create full-board pre-solve AEDT project from final configured EDB (before cutout)."""
        try:
            from pyaedt import Hfss3dLayout
        except Exception as e:
            raise RuntimeError(f"AEDT import failed: {e}")

        ref_edb_path = Path(ref_edb_path).resolve()
        output_dir = Path(output_dir).resolve()
        aedt_path = output_dir / f"{project_stem}_full_presolve.aedt"
        h3dl = None
        try:
            h3dl = Hfss3dLayout(version=self.version, non_graphical=True)
            imported = h3dl.import_edb(str(ref_edb_path))
            if imported is False:
                raise RuntimeError(f"Failed to import full pre-solve EDB: {ref_edb_path}")
            h3dl.save_project(file_name=str(aedt_path))
            self._log(f"[AEDT][FULL] Exported pre-solve AEDT: {aedt_path}", level=LogLevel.INFO)
            return str(aedt_path)
        finally:
            if h3dl:
                try:
                    h3dl.release_desktop(close_projects=True, close_desktop=True)
                except Exception:
                    pass

    def export_edb_preview_images(self, ref_edb_path: Path, output_dir: Path):
        """
        Export board preview images in fully non-graphical mode.
        No AEDT/HFSS3DLayout session is required; EDB primitives are rasterized by PIL.
        """
        try:
            from pyaedt import Edb
            from PIL import Image, ImageDraw
        except Exception as e:
            raise RuntimeError(f"Offline preview dependency import failed: {e}")

        ref_edb_path = Path(ref_edb_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        top_img = output_dir / "top.png"
        btm_img = output_dir / "btm.png"
        top_jpg = output_dir / "top.jpg"
        btm_jpg = output_dir / "btm.jpg"

        edb = None
        try:
            edb = Edb(str(ref_edb_path), edbversion=self.version)
            boxes = []
            min_x, min_y = float("inf"), float("inf")
            max_x, max_y = float("-inf"), float("-inf")

            for net_obj in (edb.nets.nets or {}).values():
                if not net_obj:
                    continue
                try:
                    prims = list(net_obj.primitives or [])
                except Exception:
                    prims = []
                for prim in prims:
                    try:
                        bbox = prim.bbox
                        if not bbox or len(bbox) != 4:
                            continue
                        x1, y1, x2, y2 = [float(v) for v in bbox]
                        if x2 < x1:
                            x1, x2 = x2, x1
                        if y2 < y1:
                            y1, y2 = y2, y1
                        if (x2 - x1) <= 0 or (y2 - y1) <= 0:
                            continue
                        boxes.append((x1, y1, x2, y2))
                        min_x = min(min_x, x1)
                        min_y = min(min_y, y1)
                        max_x = max(max_x, x2)
                        max_y = max(max_y, y2)
                    except Exception:
                        continue

            if not boxes:
                raise RuntimeError("No drawable EDB primitives found for preview export.")

            # High-quality preview for report thumbnails.
            width, height = 3840, 2160
            ss = 3
            canvas_w, canvas_h = width * ss, height * ss
            margin = 0.03
            dx = max(max_x - min_x, 1e-12)
            dy = max(max_y - min_y, 1e-12)
            x_lo = min_x - dx * margin
            x_hi = max_x + dx * margin
            y_lo = min_y - dy * margin
            y_hi = max_y + dy * margin
            sx = (canvas_w - 1) / max(x_hi - x_lo, 1e-12)
            sy = (canvas_h - 1) / max(y_hi - y_lo, 1e-12)

            def _map_rect(x1, y1, x2, y2):
                px1 = int((x1 - x_lo) * sx)
                px2 = int((x2 - x_lo) * sx)
                py1 = int((y_hi - y1) * sy)
                py2 = int((y_hi - y2) * sy)
                left, right = min(px1, px2), max(px1, px2)
                top, bottom = min(py1, py2), max(py1, py2)
                if right - left < ss:
                    right = left + ss
                if bottom - top < ss:
                    bottom = top + ss
                return left, top, right, bottom

            base = Image.new("RGB", (canvas_w, canvas_h), (244, 249, 253))
            draw = ImageDraw.Draw(base)
            board_area = max(dx * dy, 1e-12)
            for x1, y1, x2, y2 in boxes:
                rect = _map_rect(x1, y1, x2, y2)
                area_ratio = ((x2 - x1) * (y2 - y1)) / board_area
                if area_ratio > 0.70:
                    draw.rectangle(rect, outline=(150, 176, 197), width=max(1, ss))
                else:
                    draw.rectangle(rect, fill=(182, 199, 214), outline=(146, 165, 180), width=max(1, ss))

            draw.rectangle(
                (12 * ss, 12 * ss, canvas_w - 12 * ss, canvas_h - 12 * ss),
                outline=(150, 176, 197),
                width=max(2, ss),
            )

            base_final = base.resize((width, height), resample=Image.Resampling.LANCZOS)
            base_final.save(str(top_jpg), quality=96)
            base_final.save(str(top_img))
            # Bottom image is mirrored for quick visual discrimination.
            btm = base_final.transpose(Image.FLIP_LEFT_RIGHT)
            btm.save(str(btm_jpg), quality=95)
            btm.save(str(btm_img))

            self._log(
                f"[AEDT][IMG] Offline board previews exported: {top_img}, {btm_img}",
                level=LogLevel.INFO,
            )
            return {
                "top": str(top_img),
                "btm": str(btm_img),
                "top_jpg": str(top_jpg),
                "btm_jpg": str(btm_jpg),
                "project": "",
            }
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
