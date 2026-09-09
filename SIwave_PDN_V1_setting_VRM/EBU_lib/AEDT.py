# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import math
import re
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

    def _find_touchstone_artifact(self, output_dir: Path, aedt_proj: Path, safe_case: str, extra_roots=None):
        patterns = [
            f"Z_Param_{safe_case}.s*p",
            f"*{safe_case}*.s*p",
            "*.s*p",
            "*.ts",
            "*.touchstone",
        ]
        candidates = []
        roots = [Path(output_dir), Path(aedt_proj).parent]
        aedt_results = aedt_proj.with_suffix(".aedtresults")
        roots.append(aedt_results)
        for r in (extra_roots or []):
            try:
                rp = Path(r)
            except Exception:
                continue
            roots.append(rp)

        scanned = set()
        for root in roots:
            try:
                root = root.resolve()
            except Exception:
                continue
            if root in scanned or not root.exists():
                continue
            scanned.add(root)
            for pat in patterns:
                try:
                    candidates.extend(root.rglob(pat))
                except Exception:
                    pass

        # Common solver output folders may hide touchstone deeper than the report root.
        if aedt_results.exists():
            for sub in ("", "Results", "Data", "ProjectPreview", "HFSS3DLayoutDesign1"):
                probe = aedt_results / sub if sub else aedt_results
                if probe.exists():
                    for pat in patterns:
                        try:
                            candidates.extend(probe.rglob(pat))
                        except Exception:
                            pass
        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            self._log(
                f"[AEDT][ART][INFO] No touchstone found for case={safe_case} under roots={[str(r) for r in scanned]}",
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

    @staticmethod
    def _is_usable_port_name(name: str) -> bool:
        n = str(name or "").strip()
        if not n:
            return False
        u = n.upper()
        # Filter out helper terminals and non-port artifacts.
        if not u.startswith("PORT_"):
            return False
        if u.endswith("_NEG") or u.endswith("_POS"):
            return False
        return True

    @staticmethod
    def _dedupe_keep_order(values):
        out = []
        seen = set()
        for v in values or []:
            s = str(v or "").strip()
            if not s:
                continue
            k = s.upper()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out

    @staticmethod
    def _port_token(text: str) -> str:
        return "".join(c for c in str(text or "").upper() if c.isalnum())

    def _collect_edb_excitation_names(self, cutout_path: Path):
        names = []
        err = ""
        edb = None
        try:
            from pyaedt import Edb
            edb = Edb(str(cutout_path), edbversion=self.version)
            ex = edb.excitations or {}
            if isinstance(ex, dict):
                names = [str(k).strip() for k in ex.keys() if str(k).strip()]
            elif isinstance(ex, (list, tuple, set)):
                names = [str(x).strip() for x in ex if str(x).strip()]
        except Exception as e:
            err = str(e)
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
        return self._dedupe_keep_order(names), err

    def _build_port_catalog(self, h3dl, cutout_path: Path, ensured_ports=None):
        by_source = {"h3dl": [], "oexcitation": [], "edb": [], "ensured": []}
        errors = {}

        # 1) h3dl.excitations
        try:
            raw = h3dl.excitations or []
            if isinstance(raw, dict):
                by_source["h3dl"] = [str(k).strip() for k in raw.keys() if str(k).strip()]
            elif isinstance(raw, (list, tuple, set)):
                by_source["h3dl"] = [str(x).strip() for x in raw if str(x).strip()]
            else:
                try:
                    by_source["h3dl"] = [str(x).strip() for x in list(raw) if str(x).strip()]
                except Exception:
                    by_source["h3dl"] = []
        except Exception as e:
            errors["h3dl"] = str(e)

        # 2) oexcitation COM fallback
        try:
            oexc = getattr(h3dl, "oexcitation", None)
            if oexc:
                vals = []
                for fn_name in ("GetAllPortsList", "GetAllPorts", "GetAllExcitations"):
                    fn = getattr(oexc, fn_name, None)
                    if not fn:
                        continue
                    try:
                        r = fn()
                        if isinstance(r, (list, tuple)):
                            vals.extend([str(x).strip() for x in r if str(x).strip()])
                        elif r:
                            vals.append(str(r).strip())
                    except Exception:
                        continue
                by_source["oexcitation"] = vals
        except Exception as e:
            errors["oexcitation"] = str(e)

        # 3) direct EDB inspection
        edb_names, edb_err = self._collect_edb_excitation_names(cutout_path)
        by_source["edb"] = edb_names
        if edb_err:
            errors["edb"] = edb_err

        # 4) ports created/seen during ensure stage
        by_source["ensured"] = self._dedupe_keep_order(ensured_ports or [])

        for key in list(by_source.keys()):
            by_source[key] = self._dedupe_keep_order(by_source[key])

        all_ports = self._dedupe_keep_order(
            by_source["h3dl"] + by_source["oexcitation"] + by_source["edb"] + by_source["ensured"]
        )
        usable_ports = [p for p in all_ports if self._is_usable_port_name(p)]
        if not usable_ports:
            # Fallback for API variants that return non-PORT_ names but still valid excitation names.
            usable_ports = [
                p for p in all_ports
                if p and (not str(p).upper().endswith("_NEG")) and (not str(p).upper().endswith("_POS"))
            ]
        return {
            "by_source": by_source,
            "all_ports": all_ports,
            "usable_ports": usable_ports,
            "errors": errors,
        }

    @staticmethod
    def _pick_case_ports(port_catalog: dict, case: dict, safe_case: str):
        usable_ports = list((port_catalog or {}).get("usable_ports", []) or [])
        if not usable_ports:
            return []

        # Prefer case-specific ports first to avoid cross-case expressions.
        ic_token = AEDT._port_token((case or {}).get("IC", ""))
        net_tokens = set()
        for key in ("Net", "Spec_Net", "Display_Net"):
            net_v = str((case or {}).get(key, "")).strip()
            if not net_v:
                continue
            net_tokens.add(AEDT._port_token(net_v))
            net_tokens.add(AEDT._port_token(net_v.replace(".", "_")))
            net_tokens.add(AEDT._port_token(net_v.replace(".", "")))
            net_tokens.add(AEDT._port_token(net_v.replace("+", "")))
        if safe_case:
            parts = str(safe_case).split("_", 1)
            if len(parts) == 2:
                net_tokens.add(AEDT._port_token(parts[1]))

        preferred = []
        for p in usable_ports:
            pt = AEDT._port_token(p)
            if not pt.startswith("PORT"):
                continue
            body = pt[4:]
            if ic_token and not body.startswith(ic_token):
                continue
            tail = body[len(ic_token):] if ic_token else body
            if tail in net_tokens:
                preferred.append(p)

        return preferred if preferred else usable_ports

    def _create_port_with_compat(self, edb, pos_obj, gnd_obj, port_name):
        # Preferred: non-circuit (lumped/EM) port.
        try:
            if hasattr(pos_obj, "create_terminal") and hasattr(gnd_obj, "create_terminal") and hasattr(edb, "create_port"):
                pos_term = pos_obj.create_terminal(f"{port_name}_POS")
                neg_term = gnd_obj.create_terminal(f"{port_name}_NEG")
                return edb.create_port(pos_term, neg_term, is_circuit_port=False, name=port_name)
        except Exception:
            pass
        # Fallback: pin-level non-circuit port.
        try:
            if hasattr(pos_obj, "create_port"):
                return pos_obj.create_port(name=port_name, reference=gnd_obj, is_circuit_port=False)
        except Exception:
            pass
        # Legacy compatibility fallbacks.
        if hasattr(edb, "ports") and hasattr(edb.ports, "create_port_between_pin_groups"):
            for args in ((pos_obj, gnd_obj), (pos_obj, gnd_obj, port_name)):
                try:
                    if len(args) == 2:
                        return edb.ports.create_port_between_pin_groups(*args, name=port_name)
                    return edb.ports.create_port_between_pin_groups(*args)
                except Exception:
                    pass
        if hasattr(edb, "ports") and hasattr(edb.ports, "create_port_between_pins"):
            for args in ((pos_obj, gnd_obj), (pos_obj, gnd_obj, port_name)):
                try:
                    if len(args) == 2:
                        return edb.ports.create_port_between_pins(*args, name=port_name)
                    return edb.ports.create_port_between_pins(*args)
                except Exception:
                    pass
        raise RuntimeError("Port creation API failed for both pin-group and pin-to-pin paths")

    @staticmethod
    def _pin_distance2(pin_a, pin_b):
        try:
            ax, ay = float(pin_a.position[0]), float(pin_a.position[1])
            bx, by = float(pin_b.position[0]), float(pin_b.position[1])
            return (ax - bx) ** 2 + (ay - by) ** 2
        except Exception:
            return float("inf")

    @staticmethod
    def _norm_net(net_name):
        return "".join(c for c in str(net_name or "").upper() if c.isalnum())

    def _resolve_case_port_pins(self, edb, case, gnd_net: str):
        ic = str(case.get("IC", "")).strip()
        ic_pin_name = str(case.get("IC_pin", "")).strip()
        if not ic:
            return None, None
        comp = (edb._components.components or {}).get(ic)
        if not comp:
            return None, None

        pos_pin = comp.pins.get(ic_pin_name) if ic_pin_name else None
        if pos_pin is None and ic_pin_name:
            # Token fallback for display/API pin names.
            target_tok = "".join(c for c in ic_pin_name.upper() if c.isalnum())
            for p_name, p_inst in (comp.pins or {}).items():
                cand = "".join(c for c in str(p_name).upper() if c.isalnum())
                if cand == target_tok:
                    pos_pin = p_inst
                    break
        if pos_pin is None:
            # Net-driven fallback for cutout where pin name mapping can change.
            net_candidates = []
            for key in ("Net", "Spec_Net", "Display_Net"):
                v = str(case.get(key, "")).strip()
                if v:
                    net_candidates.append(v)
            net_tokens = {self._norm_net(n) for n in net_candidates if self._norm_net(n)}
            for p_inst in (comp.pins or {}).values():
                p_net = self._norm_net(getattr(p_inst, "net_name", ""))
                if p_net and p_net in net_tokens:
                    pos_pin = p_inst
                    break
        if pos_pin is None:
            return None, None

        gnd_u = str(gnd_net or "GND").strip().upper()
        # Prefer same-component GND pin.
        gnd_candidates = [p for p in (comp.pins or {}).values() if str(getattr(p, "net_name", "")).strip().upper() == gnd_u]
        if gnd_candidates:
            gnd_pin = min(gnd_candidates, key=lambda p: self._pin_distance2(pos_pin, p))
            return pos_pin, gnd_pin

        # Fallback: nearest global GND pin.
        best = None
        best_d2 = float("inf")
        for c in (edb._components.components or {}).values():
            for p in (c.pins or {}).values():
                if str(getattr(p, "net_name", "")).strip().upper() != gnd_u:
                    continue
                d2 = self._pin_distance2(pos_pin, p)
                if d2 < best_d2:
                    best_d2 = d2
                    best = p
        return pos_pin, best

    def _ensure_case_port_in_cutout_edb(self, cutout_path: Path, case, gnd_net: str, safe_case: str):
        try:
            from pyaedt import Edb
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] EDB import failed while ensuring cutout port: {e}", level=LogLevel.WARNING)
            return []

        edb = None
        try:
            edb = Edb(str(cutout_path), edbversion=self.version)
            ex = edb.excitations or {}
            existing = list(ex.keys()) if isinstance(ex, dict) else []
            existing = [str(x).strip() for x in existing if str(x).strip()]
            if existing:
                case_port = self._resolve_case_port_name_from_list(existing, case, safe_case)
                if case_port:
                    existing = self._prune_cutout_ports_keep_bundle(edb, existing, keep_base=case_port)
                vrm_changes = self._deactivate_noncase_vrm_terminations(edb, case, safe_case)
                self._save_edb_best_effort(edb)
                self._log(
                    f"[AEDT][ART] Existing cutout excitations isolated for case={safe_case}: "
                    f"keep_port={case_port or '<none>'}, ports={existing}, vrm_changes={vrm_changes}",
                    level=LogLevel.DETAIL1,
                )
                return existing

            pos_pin, gnd_pin = self._resolve_case_port_pins(edb, case, gnd_net)
            if pos_pin is None or gnd_pin is None:
                self._log(
                    f"[AEDT][ART][WARNING] Cannot create cutout port for case={safe_case}: unresolved pos/gnd pin. "
                    f"IC={case.get('IC','')}, IC_pin={case.get('IC_pin','')}, Net={case.get('Net','')}, "
                    f"Spec_Net={case.get('Spec_Net','')}, Display_Net={case.get('Display_Net','')}",
                    level=LogLevel.WARNING,
                )
                return []

            port_name = f"PORT_{safe_case}"
            self._create_port_with_compat(edb, pos_pin, gnd_pin, port_name)
            try:
                edb.save_edb()
            except Exception:
                pass
            try:
                if hasattr(edb, "save"):
                    edb.save()
            except Exception:
                pass
            # Re-open once to ensure persistence across session boundaries.
            try:
                edb.close_edb()
                edb = Edb(str(cutout_path), edbversion=self.version)
            except Exception:
                pass
            ex2 = edb.excitations or {}
            created = list(ex2.keys()) if isinstance(ex2, dict) else [port_name]
            created = [str(x).strip() for x in created if str(x).strip()]
            case_port = self._resolve_case_port_name_from_list(created, case, safe_case) or port_name
            created = self._prune_cutout_ports_keep_bundle(edb, created, keep_base=case_port)
            vrm_changes = self._deactivate_noncase_vrm_terminations(edb, case, safe_case)
            self._save_edb_best_effort(edb)
            self._log(
                f"[AEDT][ART] Cutout port ensured/isolated for case={safe_case}: "
                f"keep_port={case_port}, ports={created if created else [port_name]}, vrm_changes={vrm_changes}",
                level=LogLevel.DETAIL1,
            )
            return created if created else [port_name]
        except Exception as e:
            self._log(f"[AEDT][ART][WARNING] Cutout port creation failed for case={safe_case}: {e}", level=LogLevel.WARNING)
            return []
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass

    @staticmethod
    def _name_token(text: str) -> str:
        return "".join(c for c in str(text or "").upper() if c.isalnum())

    @staticmethod
    def _is_vrm_component_name(name: str) -> bool:
        return str(name or "").strip().upper().startswith("RVRM_")

    def _set_component_enabled(self, comp, enable: bool = True) -> bool:
        target = bool(enable)
        for attr in ("enabled", "is_enabled"):
            if hasattr(comp, attr):
                try:
                    setattr(comp, attr, target)
                    return True
                except Exception:
                    pass
        for method_name in (("enable", "Enable") if target else ("disable", "Disable")):
            fn = getattr(comp, method_name, None)
            if callable(fn):
                try:
                    fn()
                    return True
                except Exception:
                    continue
        return False

    def _collect_case_protected_component_tokens(self, cutout_path: Path, case: dict, signal_nets, gnd_net: str, bom_designator_tokens=None):
        """Collect components that must stay active for physically meaningful PDN solve.
        - target-net VRM termination (Rvrm_*)
        - capacitors touching target signal nets (BOM matched when BOM exists)
        """
        protected_tokens = set()
        details = {"caps": [], "vrm": [], "bom_blocked": [], "errors": []}
        sig_set = {str(n).strip().upper() for n in (signal_nets or []) if str(n).strip()}
        gnd_u = str(gnd_net or "GND").strip().upper()
        bom_tokens = {self._name_token(x) for x in (bom_designator_tokens or set()) if self._name_token(x)}
        gnd_u = str(gnd_net or "GND").strip().upper()
        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            comps = {}
            try:
                comps = (edb._components.components or {})
            except Exception:
                comps = {}

            for comp_name, comp in comps.items():
                cname = str(comp_name or "").strip()
                ctoken = self._name_token(cname)
                if not ctoken:
                    continue
                pins = {}
                try:
                    pins = comp.pins or {}
                except Exception:
                    pins = {}
                pin_items = list(pins.items()) if isinstance(pins, dict) else []
                pin_count = len(pin_items)
                net_set_u = set()
                for _, p in pin_items:
                    try:
                        net_name = str(getattr(p, "net_name", "") or "").strip().upper()
                    except Exception:
                        net_name = ""
                    if net_name:
                        net_set_u.add(net_name)

                if self._is_vrm_component_name(cname):
                    if sig_set and (len(net_set_u.intersection(sig_set)) > 0):
                        protected_tokens.add(ctoken)
                        details["vrm"].append(cname)
                    continue

                if cname.upper().startswith("C"):
                    on_case_net = sig_set and (len(net_set_u.intersection(sig_set)) > 0)
                    near_decap = (len(net_set_u) == 2) and (gnd_u in net_set_u) and on_case_net
                    if on_case_net or near_decap:
                        if bom_tokens and (ctoken not in bom_tokens):
                            details["bom_blocked"].append(cname)
                            continue
                        protected_tokens.add(ctoken)
                        details["caps"].append(cname)
        except Exception as e:
            details["errors"].append(str(e))
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
        return protected_tokens, details
    def _collect_direct_signal_capacitor_tokens(self, cutout_path: Path, signal_nets, gnd_net: str):
        """Collect decap tokens directly connected to current case signal nets.
        Criteria: 2-pin capacitor, includes GND, and touches case signal net.
        """
        details = {"caps": [], "errors": []}
        tokens = set()
        sig_set = {str(n).strip().upper() for n in (signal_nets or []) if str(n).strip()}
        gnd_u = str(gnd_net or "GND").strip().upper()
        if not sig_set:
            return tokens, details
        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            comps = {}
            try:
                comps = (edb._components.components or {})
            except Exception:
                comps = {}

            for comp_name, comp in comps.items():
                cname = str(comp_name or "").strip()
                if not cname.upper().startswith("C"):
                    continue
                ctoken = self._name_token(cname)
                if not ctoken:
                    continue
                pins = {}
                try:
                    pins = comp.pins or {}
                except Exception:
                    pins = {}
                pin_items = list(pins.items()) if isinstance(pins, dict) else []
                pin_count = len(pin_items)
                net_set_u = set()
                for _, p in pin_items:
                    try:
                        net_name = str(getattr(p, "net_name", "") or "").strip().upper()
                    except Exception:
                        net_name = ""
                    if net_name:
                        net_set_u.add(net_name)
                is_decap = (pin_count == 2) and (gnd_u in net_set_u) and (len(net_set_u.intersection(sig_set)) > 0)
                if is_decap:
                    tokens.add(ctoken)
                    details["caps"].append(cname)
        except Exception as e:
            details["errors"].append(str(e))
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
        return tokens, details

    def _filter_substitute_targets(self, component_names, protected_tokens=None, skip_vrm=True):
        protected = {self._name_token(x) for x in (protected_tokens or set()) if self._name_token(x)}
        out = []
        seen = set()
        for n in (component_names or []):
            s = str(n or "").strip()
            if not s:
                continue
            if skip_vrm and self._is_vrm_component_name(s):
                continue
            tok = self._name_token(s)
            if not tok:
                continue
            if tok in protected:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(s)
        return out

    def _extract_bom_designator_tokens(self, bom_info):
        tokens = set()
        info = bom_info if isinstance(bom_info, dict) else {}
        for src in (info.get("Designators", []), info.get("Designator", [])):
            if isinstance(src, set):
                iterable = list(src)
            elif isinstance(src, (list, tuple)):
                iterable = list(src)
            else:
                iterable = [src] if src else []
            for item in iterable:
                for part in str(item or "").split(","):
                    tok = self._name_token(part)
                    if tok:
                        tokens.add(tok)
        return tokens

    def _apply_bom_capacitor_policy(self, cutout_path: Path, signal_nets, bom_designator_tokens, case_idx: int, profile_tag: str, gnd_net: str = "GND"):
        """BOM-first policy for case-net capacitors:
        - on case net + in BOM => force enable
        - on case net + not in BOM => disable
        """
        changes = {"enabled": [], "disabled": [], "skipped": [], "errors": []}
        sig_set = {str(n).strip().upper() for n in (signal_nets or []) if str(n).strip()}
        gnd_u = str(gnd_net or "GND").strip().upper()
        bom_tokens = {self._name_token(x) for x in (bom_designator_tokens or set()) if self._name_token(x)}
        if not sig_set or not bom_tokens:
            return changes

        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            comps = {}
            try:
                comps = (edb._components.components or {})
            except Exception:
                comps = {}

            for comp_name, comp in comps.items():
                cname = str(comp_name or "").strip()
                if not cname.upper().startswith("C"):
                    continue
                ctoken = self._name_token(cname)
                pins = {}
                try:
                    pins = comp.pins or {}
                except Exception:
                    pins = {}
                pin_items = list(pins.items()) if isinstance(pins, dict) else []
                pin_count = len(pin_items)
                net_set_u = set()
                for _, p in pin_items:
                    try:
                        n = str(getattr(p, "net_name", "") or "").strip().upper()
                    except Exception:
                        n = ""
                    if n:
                        net_set_u.add(n)

                if len(net_set_u.intersection(sig_set)) == 0:
                    continue

                if ctoken in bom_tokens:
                    if self._set_component_enabled(comp, True):
                        changes["enabled"].append(cname)
                    else:
                        changes["errors"].append(f"{cname}: enable unsupported")
                else:
                    if self._set_component_enabled(comp, False):
                        changes["disabled"].append(cname)
                    else:
                        changes["errors"].append(f"{cname}: disable unsupported")

            self._save_edb_best_effort(edb)
        except Exception as e:
            changes["errors"].append(str(e))
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass

        self._log(
            f"[AEDT][CUTOUT] BOM capacitor policy for case#{case_idx} ({profile_tag}): "
            f"enabled={len(changes.get('enabled', []))}, disabled={len(changes.get('disabled', []))}, "
            f"errors={changes.get('errors', [])[:3]}",
            level=LogLevel.DETAIL1 if not changes.get("errors") else LogLevel.WARNING,
        )
        return changes

    def _resolve_case_port_name_from_list(self, port_names, case: dict, safe_case: str):
        names = [str(x).strip() for x in (port_names or []) if str(x).strip()]
        if not names:
            return ""
        usable = [n for n in names if self._is_usable_port_name(n)]
        if not usable:
            usable = names
        picked = self._pick_case_ports({"usable_ports": usable}, case, safe_case)
        if picked:
            return str(picked[0]).strip()
        fallback = f"PORT_{safe_case}".strip()
        for n in usable:
            if n.upper() == fallback.upper():
                return n
        return str(usable[0]).strip() if usable else ""

    def _prune_cutout_ports_keep_bundle(self, edb, port_names, keep_base: str):
        keep = str(keep_base or "").strip()
        names = [str(x).strip() for x in (port_names or []) if str(x).strip()]
        if not keep or not names:
            return names
        keep_u = keep.upper()
        keep_set = {keep_u, f"{keep_u}_NEG", f"{keep_u}_POS"}

        # Keep matching VRM termination bundle for this case as well.
        # keep_base may be PORT_IC100_CPU_1V0 while VRM is named Rvrm_CPU_1V0.
        case_suffix = keep_u[5:] if keep_u.startswith("PORT_") else keep_u
        vrm_suffix_candidates = []
        if case_suffix:
            vrm_suffix_candidates.append(case_suffix)
            # Drop leading component token when present: IC100_CPU_1V0 -> CPU_1V0
            if "_" in case_suffix:
                vrm_suffix_candidates.append(case_suffix.split("_", 1)[1])

        for suffix in vrm_suffix_candidates:
            suffix = str(suffix or "").strip()
            if not suffix:
                continue
            vrm_base = f"RVRM_{suffix}"
            keep_set.update({vrm_base, f"{vrm_base}_NEG", f"{vrm_base}_POS"})
        to_remove = [n for n in names if n.upper() not in keep_set]
        if not to_remove:
            return [n for n in names if n.upper() in keep_set] or names

        removed = []
        ex_map = {}
        try:
            ex_map = edb.excitations or {}
        except Exception:
            ex_map = {}
        for n in to_remove:
            ok = False
            ex_obj = ex_map.get(n) if isinstance(ex_map, dict) else None
            for cand in (ex_obj,):
                if cand is None:
                    continue
                for m in ("delete", "Delete", "DeleteObj", "DeleteObject"):
                    fn = getattr(cand, m, None)
                    if callable(fn):
                        try:
                            fn()
                            ok = True
                            break
                        except Exception:
                            continue
                if ok:
                    break
                raw = getattr(cand, "_edb_obj", None)
                for m in ("Delete", "DeleteObj", "DeleteObject"):
                    fn = getattr(raw, m, None)
                    if callable(fn):
                        try:
                            fn()
                            ok = True
                            break
                        except Exception:
                            continue
                if ok:
                    break
            if ok:
                removed.append(n)

        remaining = [n for n in names if n not in removed]
        remaining = [n for n in remaining if n.upper() in keep_set] or remaining
        if removed:
            self._log(
                f"[AEDT][ART] Pruned non-case ports: removed={removed}, keep_bundle={sorted(keep_set)}",
                level=LogLevel.DETAIL1,
            )
        return remaining

    def _save_edb_best_effort(self, edb):
        try:
            edb.save_edb()
        except Exception:
            pass
        try:
            if hasattr(edb, "save"):
                edb.save()
        except Exception:
            pass

    def _deactivate_noncase_vrm_terminations(self, edb, case: dict, safe_case: str):
        changes = {"disabled": [], "deleted": [], "kept": [], "errors": []}
        comps = {}
        try:
            comps = (edb._components.components or {})
        except Exception:
            return changes

        net_tokens = set()
        for key in ("Net", "Spec_Net", "Display_Net"):
            v = str((case or {}).get(key, "")).strip()
            if not v:
                continue
            net_tokens.add(self._name_token(v))
            net_tokens.add(self._name_token(v.replace(".", "_")))
            net_tokens.add(self._name_token(v.replace(".", "")))
            net_tokens.add(self._name_token(v.replace("+", "")))
        if safe_case and "_" in safe_case:
            net_tokens.add(self._name_token(safe_case.split("_", 1)[1]))

        for comp_name, comp in comps.items():
            cname = str(comp_name or "").strip()
            if not cname.upper().startswith("RVRM_"):
                continue
            suffix = cname[5:]
            ctoken = self._name_token(suffix)
            if ctoken in net_tokens:
                changes["kept"].append(cname)
                continue

            disabled = False
            for m in ("disable", "Disable"):
                fn = getattr(comp, m, None)
                if callable(fn):
                    try:
                        fn()
                        disabled = True
                        break
                    except Exception:
                        continue
            if (not disabled) and hasattr(comp, "enabled"):
                try:
                    setattr(comp, "enabled", False)
                    disabled = True
                except Exception:
                    pass
            if (not disabled) and hasattr(comp, "is_enabled"):
                try:
                    setattr(comp, "is_enabled", False)
                    disabled = True
                except Exception:
                    pass
            if disabled:
                changes["disabled"].append(cname)
                continue

            deleted = False
            for m in ("delete", "Delete", "DeleteObj", "DeleteObject"):
                fn = getattr(comp, m, None)
                if callable(fn):
                    try:
                        fn()
                        deleted = True
                        break
                    except Exception:
                        continue
            if deleted:
                changes["deleted"].append(cname)
            else:
                changes["errors"].append(f"{cname}: deactivate/delete unsupported")

        return changes

    def _prune_cutout_ports(self, edb, port_names, keep_name: str):
        keep_u = str(keep_name or "").strip().upper()
        if not keep_u:
            return [str(x).strip() for x in (port_names or []) if str(x).strip()]
        names = [str(x).strip() for x in (port_names or []) if str(x).strip()]
        if not names:
            return names
        to_remove = [n for n in names if n.upper() != keep_u]
        if not to_remove:
            return names
        removed = []
        ex_map = {}
        try:
            ex_map = edb.excitations or {}
        except Exception:
            ex_map = {}
        for n in to_remove:
            ok = False
            ex_obj = ex_map.get(n) if isinstance(ex_map, dict) else None
            for cand in (ex_obj,):
                if cand is None:
                    continue
                for m in ("delete", "Delete", "DeleteObj", "DeleteObject"):
                    fn = getattr(cand, m, None)
                    if callable(fn):
                        try:
                            fn()
                            ok = True
                            break
                        except Exception:
                            continue
                if ok:
                    break
                raw = getattr(cand, "_edb_obj", None)
                for m in ("Delete", "DeleteObj", "DeleteObject"):
                    fn = getattr(raw, m, None)
                    if callable(fn):
                        try:
                            fn()
                            ok = True
                            break
                        except Exception:
                            continue
                if ok:
                    break
            if ok:
                removed.append(n)
        if removed:
            try:
                edb.save_edb()
            except Exception:
                pass
            self._log(
                f"[AEDT][ART] Pruned non-case ports: removed={removed}, keep={keep_name}",
                level=LogLevel.DETAIL1,
            )
        kept = [keep_name] if keep_name in names or removed else [n for n in names if n.upper() == keep_u]
        if not kept:
            kept = [n for n in names if n.upper() == keep_u]
        return kept if kept else names

    def _export_touchstone_best_effort(
        self,
        h3dl,
        setup_name,
        sweep_name,
        out_path: Path,
        debug_case="",
        debug_output_dir=None,
        export_port=None,
    ):
        out_path = Path(out_path)
        export_errs = []

        def _safe_root(path_like):
            try:
                p = Path(path_like).resolve()
                return p if p.exists() else None
            except Exception:
                return None

        roots = []
        for c in (
            out_path.parent,
            getattr(h3dl, "working_directory", None),
            Path(getattr(h3dl, "project_path", "")).parent if getattr(h3dl, "project_path", None) else None,
        ):
            r = _safe_root(c) if c else None
            if r and r not in roots:
                roots.append(r)

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        writable = False
        writable_err = ""
        probe_file = out_path.parent / f".touchstone_write_probe_{int(time.time() * 1000)}.tmp"
        try:
            probe_file.write_text("probe", encoding="utf-8")
            writable = True
        except Exception as e:
            writable_err = str(e)
        finally:
            try:
                if probe_file.exists():
                    probe_file.unlink()
            except Exception:
                pass

        setup_map = {}
        try:
            for st in (h3dl.setups or []):
                sname = str(getattr(st, "name", "") or "")
                sw_names = []
                for sw in (getattr(st, "sweeps", None) or []):
                    sw_names.append(str(getattr(sw, "name", "") or ""))
                if sname:
                    setup_map[sname] = sw_names
        except Exception as e:
            export_errs.append(f"setup_map: {e}")

        existing_sweeps = []
        try:
            existing_sweeps = [str(x) for x in (getattr(h3dl, "existing_analysis_sweeps", None) or [])]
        except Exception as e:
            export_errs.append(f"existing_analysis_sweeps_read: {e}")

        variation_keys = []
        variation_vals = []
        design_variations = ""
        try:
            av = getattr(h3dl, "available_variations", None)
            nominal = av.get_independent_nominal_values() if av else {}
            if nominal:
                variation_keys = list(nominal.keys())
                variation_vals = [str(v) for v in nominal.values()]
                design_variations = " ".join(
                    "{}='{}'".format(k, str(v).replace("'", "")) for k, v in zip(variation_keys, variation_vals)
                )
        except Exception as e:
            export_errs.append(f"design_variations: {e}")

        def _scan_touchstones():
            files = []
            for r in roots:
                for pat in ("*.s*p", "*.ts", "*.touchstone"):
                    try:
                        files.extend([p for p in r.rglob(pat) if p.is_file()])
                    except Exception:
                        pass
            return {str(p.resolve()): p for p in files}

        def _get_solution_candidates():
            pairs = []
            seen = set()

            def _add_pair(s, w):
                ss = str(s or "").strip()
                ww = str(w or "").strip()
                if not ss or not ww:
                    return
                k = f"{ss}::{ww}".upper()
                if k in seen:
                    return
                seen.add(k)
                pairs.append((ss, ww))

            _add_pair(setup_name, sweep_name)
            try:
                for sol in (getattr(h3dl, "existing_analysis_sweeps", None) or []):
                    text = str(sol or "").strip()
                    if not text or ":" not in text:
                        continue
                    left, right = text.split(":", 1)
                    _add_pair(left.strip(), right.strip())
            except Exception as e:
                export_errs.append(f"existing_analysis_sweeps: {e}")

            if setup_name:
                # Common adaptive names for cases where explicit sweep was not truly solved.
                _add_pair(setup_name, "Last Adaptive")
                _add_pair(setup_name, "LastAdaptive")
                _add_pair(setup_name, "AdaptivePass")
            return pairs

        def _check_generated(before):
            if out_path.exists():
                return str(out_path)
            after = _scan_touchstones()
            new_files = [p for k, p in after.items() if k not in before]
            if new_files:
                new_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                return str(new_files[0])
                return ""

        def _export_touchstone_via_odesign(sol_setup, sol_sweep):
            try:
                odesign = getattr(h3dl, "odesign", None)
            except Exception:
                odesign = None
            if not odesign:
                return False, "odesign is unavailable"
            try:
                solution_selections = [
                    f"{sol_setup}:{sol_sweep}",
                    f"{sol_setup} : {sol_sweep}",
                ]
                file_format = 3
                out_file = str(out_path).replace("\\", "/")
                freqs_array = ["all"]
                do_renorm = False
                renorm_imped = 50
                data_type = "S"
                pass_id = -1
                complex_format = 0
                digits_precision = 15
                include_gamma = False
                non_standard_ext = False

                last_err = ""
                for sel in solution_selections:
                    try:
                        odesign.ExportNetworkData(
                            design_variations,
                            [sel],
                            file_format,
                            out_file,
                            freqs_array,
                            do_renorm,
                            renorm_imped,
                            data_type,
                            pass_id,
                            complex_format,
                            digits_precision,
                            False,
                            include_gamma,
                            non_standard_ext,
                        )
                        return True, ""
                    except Exception as e:
                        last_err = str(e)
                return False, last_err or "ExportNetworkData failed"
            except Exception as e:
                return False, str(e)

        def _write_touchstone_s1p_from_solution(sol_setup, sol_sweep, port_name):
            p = str(port_name or "").strip()
            if not p:
                return False, "port name is empty for solution-data fallback"
            sol_name = f"{sol_setup} : {sol_sweep}"
            expr = f"S({p},{p})"
            try:
                sol_data = h3dl.post.get_solution_data(
                    expressions=expr,
                    setup_sweep_name=sol_name,
                    domain="Sweep",
                )
            except Exception as e:
                return False, f"get_solution_data failed: {e}"
            if not sol_data:
                return False, "get_solution_data returned empty"

            freqs = list(getattr(sol_data, "primary_sweep_values", []) or [])
            real_vals = []
            imag_vals = []
            for candidate_expr in (expr, None):
                try:
                    real_vals = list(sol_data.data_real(candidate_expr) or [])
                    imag_vals = list(sol_data.data_imag(candidate_expr) or [])
                except Exception:
                    real_vals, imag_vals = [], []
                if real_vals and imag_vals:
                    break

            if (not freqs) or (not real_vals) or (not imag_vals):
                return False, f"solution data incomplete: freq={len(freqs)}, real={len(real_vals)}, imag={len(imag_vals)}"

            n = min(len(freqs), len(real_vals), len(imag_vals))
            if n < 2:
                return False, f"not enough points for touchstone: n={n}"

            try:
                with out_path.open("w", encoding="utf-8") as f:
                    f.write("! Generated from AEDT solution-data fallback\n")
                    f.write(f"! case={debug_case} setup={sol_setup} sweep={sol_sweep} expr={expr}\n")
                    f.write("# Hz S RI R 50\n")
                    for i in range(n):
                        f.write(f"{float(freqs[i]):.12e} {float(real_vals[i]):.12e} {float(imag_vals[i]):.12e}\n")
                return out_path.exists(), "" if out_path.exists() else "touchstone file not created"
            except Exception as e:
                return False, f"touchstone write failed: {e}"

        solution_pairs = _get_solution_candidates()
        debug_payload = {
            "case": debug_case,
            "setup_name": setup_name,
            "sweep_name": sweep_name,
            "solution_pairs": solution_pairs,
            "design_name": str(getattr(h3dl, "design_name", "") or ""),
            "project_name": str(getattr(h3dl, "project_name", "") or ""),
            "project_path": str(getattr(h3dl, "project_path", "") or ""),
            "working_directory": str(getattr(h3dl, "working_directory", "") or ""),
            "requested_output": str(out_path),
            "output_parent_writable": writable,
            "output_parent_write_error": writable_err,
            "setup_map": setup_map,
            "existing_analysis_sweeps": existing_sweeps,
            "variation_keys": variation_keys,
            "variation_values": variation_vals,
            "design_variations": design_variations,
        }
        self._log(
            f"[AEDT][ART][DEBUG] Export context case={debug_case or '<none>'}: "
            f"design={debug_payload['design_name']}, project={debug_payload['project_path']}, "
            f"setup_map={setup_map}, existing_sweeps={existing_sweeps}, "
            f"out={out_path}, writable={writable}",
            level=LogLevel.DETAIL1,
        )

        calls = []
        for s_name, sw_name in solution_pairs:
            calls.append((f"setup+sweep[{s_name}:{sw_name}]", {"setup": s_name, "sweep": sw_name}))
            calls.append(
                (
                    f"setup+sweep+path[{s_name}:{sw_name}]",
                    {"setup": s_name, "sweep": sw_name, "output_file": str(out_path)},
                )
            )
        if setup_name:
            calls.append((f"setup-only[{setup_name}]", {"setup": setup_name}))
            calls.append((f"setup+path[{setup_name}]", {"setup": setup_name, "output_file": str(out_path)}))
        calls.append(("default", {}))

        for label, kwargs in calls:
            before = _scan_touchstones()
            try:
                ret = h3dl.export_touchstone(**kwargs)
                if isinstance(ret, str) and ret.strip():
                    rp = Path(ret.strip())
                    if rp.exists():
                        return str(rp), None
                gen = _check_generated(before)
                if gen:
                    return gen, None
                if isinstance(ret, bool) and not ret:
                    export_errs.append(f"{label}: API returned False")
                else:
                    export_errs.append(f"{label}: no file generated")
            except Exception as e:
                export_errs.append(f"{label}: {e}")

        # COM-level fallback: call oDesign.ExportNetworkData directly.
        for s_name, sw_name in solution_pairs:
            before = _scan_touchstones()
            ok, err = _export_touchstone_via_odesign(s_name, sw_name)
            gen = _check_generated(before)
            if ok and gen:
                self._log(
                    f"[AEDT][ART] Touchstone exported via oDesign.ExportNetworkData: setup={s_name}, sweep={sw_name}",
                    level=LogLevel.DETAIL1,
                )
                return gen, None
            export_errs.append(
                f"odesign[{s_name}:{sw_name}]: {'no file generated' if ok else (err or 'failed')}"
            )

        # Solution-data fallback: build S1P directly when export APIs fail.
        fallback_port = str(export_port or "").strip()
        if fallback_port:
            for s_name, sw_name in solution_pairs:
                ok, err = _write_touchstone_s1p_from_solution(s_name, sw_name, fallback_port)
                if ok and out_path.exists():
                    self._log(
                        f"[AEDT][ART] Touchstone generated from solution-data fallback: setup={s_name}, sweep={sw_name}, port={fallback_port}",
                        level=LogLevel.DETAIL1,
                    )
                    return str(out_path), None
                export_errs.append(f"solution_data[{s_name}:{sw_name}:{fallback_port}]: {err or 'failed'}")
        else:
            export_errs.append("solution_data: skipped (no export_port)")

        debug_payload["export_errors"] = export_errs
        if debug_output_dir:
            try:
                dbg_dir = Path(debug_output_dir)
                dbg_dir.mkdir(parents=True, exist_ok=True)
                dbg_name = f"touchstone_export_debug_{self._safe(debug_case) if debug_case else 'case'}.json"
                dbg_path = dbg_dir / dbg_name
                with open(dbg_path, "w", encoding="utf-8") as df:
                    json.dump(debug_payload, df, indent=2, ensure_ascii=False)
                self._log(
                    f"[AEDT][ART][DEBUG] Export debug bundle written: {dbg_path}",
                    level=LogLevel.DETAIL1,
                )
            except Exception as e:
                self._log(f"[AEDT][ART][WARNING] Failed to write export debug bundle: {e}", level=LogLevel.WARNING)
        return "", "; ".join(export_errs)

    def _get_setup_and_sweep_names(self, h3dl, preferred_setup_name=None):
        setup_name = preferred_setup_name
        sweep_name = None
        setups = h3dl.setups or []
        target_setup = None
        if setup_name:
            for s in setups:
                if str(getattr(s, "name", "")) == str(setup_name):
                    target_setup = s
                    break
        if target_setup is None and setups:
            target_setup = setups[0]
            setup_name = str(getattr(target_setup, "name", "") or "")
        if target_setup is not None:
            sweeps = getattr(target_setup, "sweeps", None) or []
            if sweeps:
                sweep_name = str(getattr(sweeps[0], "name", "") or "")
        return setup_name, sweep_name, target_setup

    def _ensure_sweep_for_touchstone(self, h3dl, setup, idx: int):
        return self._ensure_sweep_for_touchstone_with_mode(h3dl, setup, idx, force_recreate=False)

    def _ensure_sweep_for_touchstone_with_mode(self, h3dl, setup, idx: int, force_recreate: bool = False):
        setup_name, sweep_name, target_setup = self._get_setup_and_sweep_names(
            h3dl,
            preferred_setup_name=(getattr(setup, "name", "") if setup else None),
        )
        if sweep_name and (not force_recreate):
            return setup_name, sweep_name

        desired = f"Sweep_{idx}_1G"
        create_errors = []

        def _force_sweep_1ghz(sw_obj):
            """Best-effort hard enforcement of 1kHz~1GHz sweep window."""
            if sw_obj is None:
                return
            target_data = "LINC 0.0001GHz 1GHz 501"

            def _force_freq_fields(obj):
                if isinstance(obj, dict):
                    for k in list(obj.keys()):
                        lk = str(k).lower()
                        v = obj.get(k)
                        if lk == "data" and isinstance(v, str):
                            vu = v.upper().strip()
                            if vu.startswith("LINC ") or vu.startswith("DEC "):
                                obj[k] = target_data
                                continue
                        if ("start" in lk and "freq" in lk) or lk in {"rangestart", "startvalue"}:
                            obj[k] = "0.0001GHz"
                            continue
                        if (("stop" in lk or "end" in lk) and "freq" in lk) or lk in {"rangeend", "stopvalue", "endvalue"}:
                            obj[k] = "1GHz"
                            continue
                        if lk == "frequencies":
                            # Let solver regenerate from Data/Range to avoid stale 1~5GHz list.
                            if isinstance(v, str):
                                obj[k] = ""
                            elif isinstance(v, list):
                                obj[k] = []
                            continue
                        if isinstance(v, (dict, list)):
                            _force_freq_fields(v)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, (dict, list)):
                            _force_freq_fields(item)
            try:
                props = getattr(sw_obj, "props", None)
            except Exception:
                props = None
            if isinstance(props, dict):
                try:
                    props["RangeType"] = "LinearCount"
                    props["RangeStart"] = "0.0001GHz"
                    props["RangeEnd"] = "1GHz"
                    props["RangeCount"] = 501
                    props["Data"] = target_data
                    _force_freq_fields(props)
                    if hasattr(sw_obj, "update"):
                        sw_obj.update()
                    # Second pass after update: some APIs repopulate Data/Frequencies.
                    _force_freq_fields(props)
                    if hasattr(sw_obj, "update"):
                        sw_obj.update()
                except Exception:
                    pass

        if force_recreate and target_setup is not None:
            # Try to clear existing sweeps to avoid empty/invalid sweep reuse.
            try:
                sweeps = list(getattr(target_setup, "sweeps", None) or [])
            except Exception:
                sweeps = []
            for sw in sweeps:
                for m in ("delete", "Delete", "remove", "Remove"):
                    fn = getattr(sw, m, None)
                    if callable(fn):
                        try:
                            fn()
                            break
                        except Exception:
                            continue

        # 1) Try setup object APIs first.
        if target_setup is not None:
            for fn_name in ("add_sweep", "create_sweep"):
                fn = getattr(target_setup, fn_name, None)
                if not callable(fn):
                    continue
                for args, kwargs in (
                    ((), {"name": desired}),
                    ((desired,), {}),
                    ((), {}),
                ):
                    try:
                        sw = fn(*args, **kwargs)
                        _force_sweep_1ghz(sw)
                        break
                    except Exception as e:
                        create_errors.append(f"{fn_name}{args}{kwargs}: {e}")
                # Re-check after each method.
                setup_name, sweep_name, target_setup = self._get_setup_and_sweep_names(
                    h3dl,
                    preferred_setup_name=setup_name,
                )
                if target_setup is not None:
                    try:
                        sweeps = list(getattr(target_setup, "sweeps", None) or [])
                    except Exception:
                        sweeps = []
                    desired_sw = None
                    for sw in sweeps:
                        sw_name = str(getattr(sw, "name", "") or "")
                        if sw_name == desired:
                            desired_sw = sw
                            break
                    if desired_sw is not None:
                        _force_sweep_1ghz(desired_sw)
                        try:
                            dbg_props = getattr(desired_sw, "props", None)
                            dbg_data = ""
                            if isinstance(dbg_props, dict):
                                dbg_data = str(dbg_props.get("Data", ""))[:120]
                            self._log(
                                f"[AEDT][ART] Sweep enforcement applied: setup={setup_name}, sweep={desired}, data='{dbg_data}'",
                                level=LogLevel.DETAIL1,
                            )
                        except Exception:
                            pass
                        return setup_name, desired
                if sweep_name:
                    return setup_name, sweep_name

        # 2) Try top-level H3DL APIs with common signatures.
        for fn_name in ("create_linear_count_sweep", "create_frequency_sweep", "create_linear_step_sweep"):
            fn = getattr(h3dl, fn_name, None)
            if not callable(fn):
                continue
            kwargs_candidates = [
                {
                    "setupname": setup_name,
                    "sweep_name": desired,
                    "unit": "GHz",
                    "start_frequency": 0.0001,
                    "stop_frequency": 1.0,
                    "num_of_freq_points": 401,
                    "sweep_type": "Interpolating",
                },
                {
                    "setup_name": setup_name,
                    "sweep_name": desired,
                    "unit": "GHz",
                    "start_frequency": 0.0001,
                    "stop_frequency": 1.0,
                    "num_of_freq_points": 401,
                    "sweep_type": "Interpolating",
                },
                {
                    "setupname": setup_name,
                    "sweepname": desired,
                    "unit": "GHz",
                    "startfrequency": 0.0001,
                    "stopfrequency": 1.0,
                    "num_of_freq_points": 401,
                    "sweep_type": "Interpolating",
                },
            ]
            for kwargs in kwargs_candidates:
                try:
                    fn(**kwargs)
                    break
                except Exception as e:
                    create_errors.append(f"{fn_name}{kwargs}: {e}")
            setup_name, sweep_name, target_setup = self._get_setup_and_sweep_names(
                h3dl,
                preferred_setup_name=setup_name,
            )
            if target_setup is not None:
                try:
                    sweeps = list(getattr(target_setup, "sweeps", None) or [])
                except Exception:
                    sweeps = []
                for sw in sweeps:
                    if str(getattr(sw, "name", "") or "") == desired:
                        _force_sweep_1ghz(sw)
                        return setup_name, desired
            if sweep_name:
                return setup_name, sweep_name

        self._log(
            f"[AEDT][ART][WARNING] Sweep not created for setup={setup_name}. Touchstone export may be unavailable. "
            f"attempts={create_errors[:6]}",
            level=LogLevel.WARNING,
        )
        return setup_name, ""

    def _force_setup_adaptive_1ghz(self, h3dl, setup):
        target = setup
        if target is None:
            try:
                setups = h3dl.setups or []
                target = setups[0] if setups else None
            except Exception:
                target = None
        if target is None:
            return False

        changed = []

        def _freq_value_to_hz(v):
            s = str(v or "").strip().upper().replace(" ", "")
            if not s:
                return None
            m = re.match(r"^([0-9]*\.?[0-9]+)(GHZ|MHZ|KHZ|HZ)$", s)
            if not m:
                return None
            val = float(m.group(1))
            unit = m.group(2)
            mul = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}.get(unit, 1.0)
            return val * mul

        def _should_force_key(key_name: str) -> bool:
            k = str(key_name or "").strip().lower()
            if not k:
                return False
            if "freq" in k:
                return True
            if "rangeend" in k or "stop" in k:
                return True
            return False

        def _walk_and_force(obj, path=""):
            local_changes = []
            if isinstance(obj, dict):
                for k, v in list(obj.items()):
                    key_path = f"{path}.{k}" if path else str(k)
                    if isinstance(v, (dict, list)):
                        local_changes.extend(_walk_and_force(v, key_path))
                        continue
                    if not _should_force_key(k):
                        continue
                    hz = _freq_value_to_hz(v)
                    if hz is None:
                        continue
                    if hz > 1e9 + 1:
                        old = str(v)
                        obj[k] = "1GHz"
                        local_changes.append(f"{key_path}:{old}->1GHz")
            elif isinstance(obj, list):
                for i, item in enumerate(list(obj)):
                    key_path = f"{path}[{i}]"
                    if isinstance(item, (dict, list)):
                        local_changes.extend(_walk_and_force(item, key_path))
            return local_changes

        try:
            props = getattr(target, "props", None)
            if isinstance(props, dict):
                changed.extend(_walk_and_force(props))
                # Conservative fallback: ensure top-level Frequency is 1GHz if present.
                if "Frequency" in props and str(props.get("Frequency")) != "1GHz":
                    old = str(props.get("Frequency"))
                    props["Frequency"] = "1GHz"
                    changed.append(f"Frequency:{old}->1GHz")

                if hasattr(target, "update"):
                    target.update()
        except Exception as e:
            self._log(f"[AEDT][CUTOUT][WARNING] Failed to enforce 1GHz adaptive setup: {e}", level=LogLevel.WARNING)
            return False

        if changed:
            self._log(
                f"[AEDT][CUTOUT] Adaptive setup frequency forced to 1GHz (changes={changed[:10]})",
                level=LogLevel.DETAIL1,
            )
            return True

        self._log(
            "[AEDT][CUTOUT] Adaptive setup frequency force requested, but compatible keys were not found.",
            level=LogLevel.DETAIL1,
        )
        return False

    @staticmethod
    def _is_mesh_related_text(text: str) -> bool:
        t = str(text or "").lower()
        markers = (
            "mesh",
            "meshing",
            "failed to collect any polygons",
            "polygon",
            "sliver",
            "tetra",
            "triang",
        )
        return any(m in t for m in markers)

    @staticmethod
    def _is_model_freq_coverage_text(text: str) -> bool:
        t = str(text or "").lower()
        markers = (
            "does not have frequency data spanning all of the adaptive mesh frequencies",
            "simulation data interpolation failed",
            "lumped component",
            "frequency data spanning",
            "number of pins in",
            "n-port model",
            "low compatibility with the solver",
            "embedded circuit element",
        )
        return any(m in t for m in markers)

    @staticmethod
    def _extract_interpolation_failed_components(text: str):
        src = str(text or "")
        found = []
        patterns = (
            r"interpolation failed[^'\n\r]*component\s+'([^']+)'",
            r"circuit element\s+'([^']+)'",
            r"embedded circuit element\s+'([^']+)'",
            r"number of pins in\s+([A-Za-z0-9_]+)\s+do not match assigned n-port model",
        )
        for pat in patterns:
            for m in re.finditer(pat, src, flags=re.IGNORECASE):
                raw = str(m.group(1) or "").strip()
                if not raw:
                    continue
                comp = raw.split(":", 1)[0].strip()
                if comp:
                    found.append(comp)
        out = []
        seen = set()
        for n in found:
            key = str(n).upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

    def _deactivate_components_in_cutout(self, cutout_path: Path, component_names, protected_tokens=None):
        changes = {"requested": [], "disabled": [], "deleted": [], "missing": [], "errors": []}
        targets = []
        seen = set()
        for n in (component_names or []):
            s = str(n or "").strip()
            if not s:
                continue
            k = s.upper()
            if k in seen:
                continue
            seen.add(k)
            targets.append(s)
        changes["requested"] = targets
        if not targets:
            return changes

        target_tokens = {self._name_token(x) for x in targets if self._name_token(x)}
        protected = {self._name_token(x) for x in (protected_tokens or set()) if self._name_token(x)}
        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            comps = {}
            try:
                comps = (edb._components.components or {})
            except Exception:
                comps = {}
            matched = set()
            for comp_name, comp in comps.items():
                cname = str(comp_name or "").strip()
                ctoken = self._name_token(cname)
                if ctoken not in target_tokens:
                    continue
                if ctoken in protected:
                    matched.add(ctoken)
                    continue
                matched.add(ctoken)
                disabled = False
                for m in ("disable", "Disable"):
                    fn = getattr(comp, m, None)
                    if callable(fn):
                        try:
                            fn()
                            disabled = True
                            break
                        except Exception:
                            continue
                if (not disabled) and hasattr(comp, "enabled"):
                    try:
                        setattr(comp, "enabled", False)
                        disabled = True
                    except Exception:
                        pass
                if (not disabled) and hasattr(comp, "is_enabled"):
                    try:
                        setattr(comp, "is_enabled", False)
                        disabled = True
                    except Exception:
                        pass
                if disabled:
                    changes["disabled"].append(cname)
                    continue

                deleted = False
                for m in ("delete", "Delete", "DeleteObj", "DeleteObject"):
                    fn = getattr(comp, m, None)
                    if callable(fn):
                        try:
                            fn()
                            deleted = True
                            break
                        except Exception:
                            continue
                if deleted:
                    changes["deleted"].append(cname)
                else:
                    changes["errors"].append(f"{cname}: deactivate/delete unsupported")

            for t in target_tokens:
                if t not in matched:
                    changes["missing"].append(t)
            self._save_edb_best_effort(edb)
        except Exception as e:
            changes["errors"].append(str(e))
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
        return changes

    def _sanitize_cutout_capacitor_models(self, cutout_path: Path, gnd_net: str, signal_nets=None, protected_tokens=None, force_keep_tokens=None):
        """
        Cutout ?덉젙???곗꽑 ?뺤콉:
        - Capacitor(C*) 以?紐⑤뜽 遺덉씪移?媛?μ꽦????遺?덉쓣 ?좎젣 鍮꾪솢?깊솕
          1) ? ??!= 2
          2) GND ????놁쓬
          3) pin net 媛쒖닔媛 怨쇰떎(>2)
        """
        changes = {
            "disabled": [],
            "kept": [],
            "force_kept": [],
            "skipped_noncap": 0,
            "unsafe_pin_count": [],
            "unsafe_no_gnd": [],
            "unsafe_multi_net": [],
            "errors": [],
        }
        gnd_u = str(gnd_net or "GND").strip().upper()
        sig_set = {str(n).strip().upper() for n in (signal_nets or []) if str(n).strip()}
        gnd_u = str(gnd_net or "GND").strip().upper()
        protected = {self._name_token(x) for x in (protected_tokens or set()) if self._name_token(x)}
        force_keep = {self._name_token(x) for x in (force_keep_tokens or set()) if self._name_token(x)}
        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            comps = {}
            try:
                comps = (edb._components.components or {})
            except Exception:
                comps = {}

            for comp_name, comp in comps.items():
                cname = str(comp_name or "").strip()
                if not cname.upper().startswith("C"):
                    changes["skipped_noncap"] += 1
                    continue
                ctoken = self._name_token(cname)

                pins = {}
                try:
                    pins = comp.pins or {}
                except Exception:
                    pins = {}
                pin_items = list(pins.items()) if isinstance(pins, dict) else []
                pin_count = len(pin_items)
                net_names = []
                for _, p in pin_items:
                    try:
                        net_names.append(str(getattr(p, "net_name", "") or "").strip())
                    except Exception:
                        net_names.append("")
                net_set_u = {n.upper() for n in net_names if n}
                has_gnd = gnd_u in net_set_u
                # keep only caps that look like simple 2-pin decoupling topology
                unsafe = False
                if pin_count != 2:
                    unsafe = True
                    changes["unsafe_pin_count"].append(cname)
                if len(net_set_u) > 2:
                    unsafe = True
                    changes["unsafe_multi_net"].append(cname)
                if not has_gnd:
                    unsafe = True
                    changes["unsafe_no_gnd"].append(cname)

                # Optional strictness: if signal nets are known, keep at least one signal-net relation.
                if has_gnd and sig_set and (len(net_set_u.intersection(sig_set)) == 0):
                    # not immediately unsafe, but not case-relevant decap. disable for cutout stability.
                    unsafe = True

                if (ctoken in protected):
                    self._set_component_enabled(comp, True)
                    changes["kept"].append(cname)
                    continue

                if (ctoken in force_keep):
                    self._set_component_enabled(comp, True)
                    changes["kept"].append(cname)
                    changes["force_kept"].append(cname)
                    continue

                if not unsafe:
                    self._set_component_enabled(comp, True)
                    changes["kept"].append(cname)
                    continue

                disabled = False
                for m in ("disable", "Disable"):
                    fn = getattr(comp, m, None)
                    if callable(fn):
                        try:
                            fn()
                            disabled = True
                            break
                        except Exception:
                            continue
                if (not disabled) and hasattr(comp, "enabled"):
                    try:
                        setattr(comp, "enabled", False)
                        disabled = True
                    except Exception:
                        pass
                if (not disabled) and hasattr(comp, "is_enabled"):
                    try:
                        setattr(comp, "is_enabled", False)
                        disabled = True
                    except Exception:
                        pass
                if disabled:
                    changes["disabled"].append(cname)
                else:
                    changes["errors"].append(f"{cname}: disable unsupported")

            self._save_edb_best_effort(edb)
        except Exception as e:
            changes["errors"].append(str(e))
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass
        return changes

    @staticmethod
    def _is_retry_worthy_solve_text(text: str) -> bool:
        t = str(text or "").lower()
        markers = (
            "mesh",
            "meshing",
            "failed to collect any polygons",
            "polygon",
            "sliver",
            "tetra",
            "triang",
            "no excitations are defined",
            "design validation failed",
            "no trace data",
            "setup/sweep/port missing",
            "solution data empty",
            "precheck=no_case_excitation",
            "simulation data interpolation failed",
            "does not have frequency data spanning all of the adaptive mesh frequencies",
            "number of pins in",
            "n-port model",
            "low compatibility with the solver",
            "embedded circuit element",
        )
        return any(m in t for m in markers)

    def _validate_case_excitation(self, h3dl, cutout_path: Path, case: dict, safe_case: str, ensured_ports=None):
        catalog = self._build_port_catalog(
            h3dl=h3dl,
            cutout_path=cutout_path,
            ensured_ports=ensured_ports or [],
        )
        ports = self._pick_case_ports(catalog, case, safe_case)
        ok = bool(ports)
        detail = ""
        if not ok:
            detail = (
                f"precheck=no_case_excitation, safe_case={safe_case}, "
                f"all_ports={catalog.get('all_ports', [])}, "
                f"sources={catalog.get('by_source', {})}, "
                f"errors={catalog.get('errors', {})}"
            )
        return ok, ports, detail, catalog

    @staticmethod
    def _validate_case_port_bundle(port_catalog: dict, case_port: str):
        cp = str(case_port or "").strip()
        if not cp:
            return False, "case port is empty"
        all_ports = [str(x).strip() for x in ((port_catalog or {}).get("all_ports", []) or []) if str(x).strip()]
        all_u = {p.upper() for p in all_ports}
        cp_u = cp.upper()
        if cp_u not in all_u:
            return False, f"case port missing in catalog: {cp}"
        # Require at least one terminal mate for robust solve.
        if (f"{cp_u}_NEG" not in all_u) and (f"{cp_u}_POS" not in all_u):
            return False, f"port terminal pair missing for {cp}"
        return True, ""

    def _validate_cutout_edb(self, cutout_path: Path, signal_nets=None, gnd_net: str = "GND"):
        """Lightweight cutout validation right after generation (before solve/import)."""
        cutout_path = Path(cutout_path)
        if not cutout_path.exists():
            return False, f"cutout_missing:{cutout_path}"
        edb_def = cutout_path / "edb.def"
        if not edb_def.exists():
            return False, f"cutout_missing_edb_def:{edb_def}"

        comp_count = 0
        net_set_u = set()
        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            try:
                comps = (edb._components.components or {})
                comp_count = len(comps)
            except Exception:
                comp_count = 0

            # best-effort net extraction over API variants
            nets_obj = getattr(edb, "nets", None)
            if nets_obj is not None:
                candidates = []
                for attr in ("netlist", "nets", "signal_nets", "power_nets"):
                    try:
                        v = getattr(nets_obj, attr, None)
                    except Exception:
                        v = None
                    if v:
                        candidates.append(v)
                for v in candidates:
                    try:
                        if isinstance(v, dict):
                            net_set_u.update(str(k).strip().upper() for k in v.keys() if str(k).strip())
                        elif isinstance(v, (list, tuple, set)):
                            net_set_u.update(str(x).strip().upper() for x in v if str(x).strip())
                    except Exception:
                        continue
        except Exception as e:
            return False, f"cutout_open_failed:{e}"
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass

        if comp_count <= 0:
            return False, "cutout_empty_components"

        gnd_u = str(gnd_net or "GND").strip().upper()
        sig_u = {str(n).strip().upper() for n in (signal_nets or []) if str(n).strip()}
        if net_set_u:
            if gnd_u and gnd_u not in net_set_u:
                return False, f"cutout_missing_gnd:{gnd_u}"
            if sig_u and not (sig_u & net_set_u):
                return False, f"cutout_missing_signal_nets:{sorted(list(sig_u))[:5]}"

        return True, f"ok:components={comp_count},nets={len(net_set_u)}"

    def _create_cutout_with_validation(
        self,
        source_edb_path: Path,
        output_aedb_path: Path,
        signal_nets,
        gnd_net: str,
        extent_type: str,
        expansion_size: float,
        include_pingroups: bool,
        check_terminals: bool,
        preserve_models: bool,
        case_idx: int,
        profile_tag: str,
    ):
        """Generate cutout and immediately validate; auto-remediate on failure."""
        from pyaedt import Edb

        attempts = []
        attempts.append(
            {
                "label": "base",
                "extent": str(extent_type),
                "expand": float(expansion_size),
                "include_pg": bool(include_pingroups),
                "check_terms": bool(check_terminals),
            }
        )
        attempts.append(
            {
                "label": "validated_expand",
                "extent": str(extent_type),
                "expand": max(float(expansion_size) * 1.5, float(expansion_size) + 0.0015),
                "include_pg": False,
                "check_terms": False,
            }
        )
        attempts.append(
            {
                "label": "validated_conforming",
                "extent": "Conforming",
                "expand": max(float(expansion_size) * 2.0, float(expansion_size) + 0.0025),
                "include_pg": False,
                "check_terms": False,
            }
        )

        # drop duplicates while preserving order
        uniq = []
        seen = set()
        for a in attempts:
            key = (a["extent"], round(float(a["expand"]), 6), bool(a["include_pg"]), bool(a["check_terms"]))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(a)
        attempts = uniq

        last_err = "cutout_validation_failed"
        used = attempts[0]
        for a in attempts:
            used = a
            if output_aedb_path.exists():
                shutil.rmtree(output_aedb_path, ignore_errors=True)
            edb = None
            try:
                edb = Edb(str(source_edb_path), edbversion=self.version)
                edb.cutout(
                    signal_list=signal_nets,
                    reference_list=[gnd_net],
                    extent_type=a["extent"],
                    expansion_size=float(a["expand"]),
                    output_aedb_path=str(output_aedb_path),
                    open_cutout_at_end=False,
                    use_pyaedt_cutout=True,
                    include_pingroups=bool(a["include_pg"]),
                    check_terminals=bool(a["check_terms"]),
                    preserve_components_with_model=bool(preserve_models),
                )
                self._save_edb_best_effort(edb)
            except Exception as e:
                last_err = f"cutout_create_failed:{e}"
            finally:
                if edb:
                    try:
                        edb.close_edb()
                    except Exception:
                        pass

            ok, detail = self._validate_cutout_edb(output_aedb_path, signal_nets=signal_nets, gnd_net=gnd_net)
            if ok:
                self._log(
                    f"[AEDT][CUTOUT] Validation passed for case#{case_idx} ({profile_tag}/{a['label']}): "
                    f"extent={a['extent']}, expansion={a['expand']}, include_pg={a['include_pg']}, "
                    f"check_terms={a['check_terms']}, detail={detail}",
                    level=LogLevel.DETAIL1,
                )
                return {
                    "ok": True,
                    "extent": a["extent"],
                    "expand": float(a["expand"]),
                    "include_pg": bool(a["include_pg"]),
                    "check_terms": bool(a["check_terms"]),
                    "detail": detail,
                }

            last_err = detail or last_err
            self._log(
                f"[AEDT][CUTOUT][WARNING] Validation failed for case#{case_idx} ({profile_tag}/{a['label']}): "
                f"extent={a['extent']}, expansion={a['expand']}, include_pg={a['include_pg']}, "
                f"check_terms={a['check_terms']}, detail={last_err}",
                level=LogLevel.WARNING,
            )

        return {
            "ok": False,
            "extent": used["extent"],
            "expand": float(used["expand"]),
            "include_pg": bool(used["include_pg"]),
            "check_terms": bool(used["check_terms"]),
            "detail": last_err,
        }

    def _import_cutout_with_retry(self, h3dl, cutout_path: Path, case_idx: int, profile_tag: str, max_retry: int = 3):
        cutout_path = Path(cutout_path)
        edb_def = cutout_path / "edb.def"
        if not cutout_path.exists():
            return False, f"cutout_missing:{cutout_path}"
        if not edb_def.exists():
            return False, f"cutout_missing_edb_def:{edb_def}"

        last_err = ""
        for attempt in range(1, max_retry + 1):
            try:
                imported = h3dl.import_edb(str(cutout_path))
                if imported is not False:
                    if attempt > 1:
                        self._log(
                            f"[AEDT][CUTOUT] import_edb recovered on retry for case#{case_idx} ({profile_tag}): "
                            f"attempt={attempt}/{max_retry}, cutout={cutout_path}",
                            level=LogLevel.WARNING,
                        )
                    return True, f"ok:attempt={attempt}/{max_retry}"
                last_err = "import_edb returned False"
            except Exception as e:
                last_err = str(e)

            self._log(
                f"[AEDT][CUTOUT][WARNING] import_edb failed for case#{case_idx} ({profile_tag}): "
                f"attempt={attempt}/{max_retry}, detail={last_err}",
                level=LogLevel.WARNING,
            )
            if attempt < max_retry:
                time.sleep(min(1.2 * attempt, 3.0))

        return False, f"import_failed:{last_err}"

    def _repair_and_validate_cutout_before_import(
        self,
        cutout_path: Path,
        signal_nets=None,
        gnd_net: str = "GND",
        case_idx: int = 0,
        profile_tag: str = "base",
    ):
        """
        Enforce order:
        cutout -> validate -> repair/save -> validate again -> import.
        """
        cutout_path = Path(cutout_path)
        ok0, detail0 = self._validate_cutout_edb(cutout_path, signal_nets=signal_nets, gnd_net=gnd_net)
        if not ok0:
            return False, f"pre_repair_validate_failed:{detail0}"

        edb = None
        try:
            from pyaedt import Edb

            edb = Edb(str(cutout_path), edbversion=self.version)
            # Best-effort finalize write so later importer reads a stable, fully saved EDB state.
            self._save_edb_best_effort(edb)
        except Exception as e:
            return False, f"repair_open_or_save_failed:{e}"
        finally:
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    pass

        time.sleep(0.25)
        ok1, detail1 = self._validate_cutout_edb(cutout_path, signal_nets=signal_nets, gnd_net=gnd_net)
        if not ok1:
            return False, f"post_repair_validate_failed:{detail1}"

        self._log(
            f"[AEDT][CUTOUT] Repair+validate passed for case#{case_idx} ({profile_tag}): "
            f"before={detail0}, after={detail1}",
            level=LogLevel.DETAIL1,
        )
        return True, f"ok:{detail1}"

    def _collect_design_messages(self, h3dl, max_items=80):
        msgs = []
        seen = set()
        try:
            odesk = getattr(h3dl, "odesktop", None)
            if not odesk:
                return msgs
            proj = str(getattr(h3dl, "project_name", "") or "")
            design = str(getattr(h3dl, "design_name", "") or "")
            for lvl in (0, 1, 2, 3):
                arr = []
                for args in ((proj, design, lvl), ("", "", lvl)):
                    try:
                        arr = odesk.GetMessages(*args) or []
                        if arr:
                            break
                    except Exception:
                        continue
                for m in arr:
                    s = str(m or "").strip()
                    if not s:
                        continue
                    k = s.lower()
                    if k in seen:
                        continue
                    seen.add(k)
                    msgs.append(s)
                    if len(msgs) >= max_items:
                        return msgs
        except Exception:
            return msgs
        return msgs

    def _probe_solve_success(self, h3dl, setup_name: str, sweep_name: str, port_name: str):
        if not setup_name or not sweep_name or not port_name:
            return False, "no_valid_sweep_or_port"
        expr = f"S({port_name},{port_name})"
        sol_name = f"{setup_name} : {sweep_name}"
        try:
            sd = h3dl.post.get_solution_data(
                expressions=expr,
                setup_sweep_name=sol_name,
                domain="Sweep",
            )
            pts = list(getattr(sd, "primary_sweep_values", []) or []) if sd else []
            if len(pts) >= 2:
                return True, f"probe points={len(pts)}"
            return False, "no_trace_data"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _span_to_mm(span_val: float):
        try:
            v = abs(float(span_val))
        except Exception:
            return 0.0
        # Heuristic: EDB spans are usually in meters for this flow.
        if v <= 2.0:
            return v * 1000.0
        return v

    @staticmethod
    def _draw_arrowed_dim(draw, x1, y1, x2, y2, color=(185, 18, 18), width=3, head=14):
        draw.line((x1, y1, x2, y2), fill=color, width=width)
        if x1 == x2 and y1 == y2:
            return
        ang = math.atan2((y2 - y1), (x2 - x1))
        for px, py, base_ang in ((x1, y1, ang + math.pi), (x2, y2, ang)):
            a1 = base_ang + math.radians(22)
            a2 = base_ang - math.radians(22)
            p1 = (px + head * math.cos(a1), py + head * math.sin(a1))
            p2 = (px + head * math.cos(a2), py + head * math.sin(a2))
            draw.polygon([(px, py), p1, p2], fill=color)

    def _draw_board_dimensions_overlay(self, image, board_rect_px, span_x_raw, span_y_raw):
        try:
            from PIL import ImageDraw
        except Exception:
            return image
        w, h = image.size
        l, t, r, b = board_rect_px
        l = max(0, min(w - 1, int(l)))
        r = max(0, min(w - 1, int(r)))
        t = max(0, min(h - 1, int(t)))
        b = max(0, min(h - 1, int(b)))
        if r - l < 8 or b - t < 8:
            return image

        draw = ImageDraw.Draw(image)
        c = (185, 18, 18)

        x_dim_y = min(h - 22, b + 26)
        draw.line((l, b, l, x_dim_y), fill=c, width=3)
        draw.line((r, b, r, x_dim_y), fill=c, width=3)
        self._draw_arrowed_dim(draw, l, x_dim_y, r, x_dim_y, color=c, width=3, head=13)
        x_mm = self._span_to_mm(span_x_raw)
        draw.text((max(8, int((l + r) / 2 - 85)), max(8, x_dim_y - 16)), f"{x_mm:.2f} mm", fill=c)

        y_dim_x = max(20, l - 34)
        draw.line((l, t, y_dim_x, t), fill=c, width=3)
        draw.line((l, b, y_dim_x, b), fill=c, width=3)
        self._draw_arrowed_dim(draw, y_dim_x, b, y_dim_x, t, color=c, width=3, head=13)
        y_mm = self._span_to_mm(span_y_raw)
        draw.text((max(8, y_dim_x - 20), int((t + b) / 2 - 12)), f"{y_mm:.2f} mm", fill=c)
        return image

    @staticmethod
    def _set_parallel_camera_to_bounds(plotter, bounds, margin=0.01):
        xmin, xmax, ymin, ymax, _, _ = bounds
        dx = max(float(xmax) - float(xmin), 1e-9)
        dy = max(float(ymax) - float(ymin), 1e-9)
        cx = (float(xmin) + float(xmax)) * 0.5
        cy = (float(ymin) + float(ymax)) * 0.5
        half_span = max(dx, dy) * (0.5 + max(0.0, float(margin)))

        plotter.camera_position = "xy"
        cam = plotter.camera
        cam.parallel_projection = True
        cam.focal_point = (cx, cy, 0.0)
        cam.position = (cx, cy, 1.0)
        cam.parallel_scale = half_span

    def _render_dcir_style_net_path_images(
        self,
        edb_path: Path,
        target_nets,
        fit_view_path: Path,
        zoom_view_path: Path,
        ic_name: str = "",
        source_name: str = "",
    ):
        """
        DCIR-style Fit/Zoom capture using PyVista (same style family as DCIR post viewer).
        Fallback-safe: returns False when dependencies or geometry are unavailable.
        """
        try:
            import numpy as np
            import pyvista as pv
            from pyaedt import Edb
        except Exception as e:
            self._log(f"[AEDT][IMG][WARNING] DCIR-style dependencies unavailable: {e}", level=LogLevel.WARNING)
            return False

        target_set = {str(n).strip().upper() for n in (target_nets or []) if str(n).strip()}
        if not target_set:
            return False

        edb_path = Path(edb_path).resolve()
        fit_view_path = Path(fit_view_path).resolve()
        zoom_view_path = Path(zoom_view_path).resolve()
        fit_view_path.parent.mkdir(parents=True, exist_ok=True)

        edb = None
        fit_plotter = None
        zoom_plotter = None
        try:
            edb = Edb(str(edb_path), edbversion=self.version)
            pv.set_plot_theme("document")

            fit_plotter = pv.Plotter(off_screen=True, title="FitView")
            fit_plotter.background_color = "white"

            min_x, min_y = float("inf"), float("inf")
            max_x, max_y = float("-inf"), float("-inf")
            target_bbox = [float("inf"), float("inf"), float("-inf"), float("-inf")]
            bg_shapes = []
            fg_shapes = []

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
                        min_x = min(min_x, x1)
                        min_y = min(min_y, y1)
                        max_x = max(max_x, x2)
                        max_y = max(max_y, y2)

                        corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0]])
                        face = pv.Polygon(corners)
                        if is_target:
                            fg_shapes.append(face)
                            target_bbox[0] = min(target_bbox[0], x1)
                            target_bbox[1] = min(target_bbox[1], y1)
                            target_bbox[2] = max(target_bbox[2], x2)
                            target_bbox[3] = max(target_bbox[3], y2)
                        else:
                            bg_shapes.append(face)
                    except Exception:
                        continue

            if min_x == float("inf"):
                return False

            # Background context (light gray), then target net overlays (orange).
            for shape in bg_shapes:
                fit_plotter.add_mesh(shape, color="#D5DADF", opacity=0.12, lighting=False)
            for shape in fg_shapes:
                fit_plotter.add_mesh(shape, color="#F39229", opacity=0.72, lighting=False)

            # Target component outlines/labels (same DCIR style family).
            target_comp = {}
            if ic_name and ic_name in edb.components.components:
                target_comp[ic_name] = edb.components.components[ic_name]
            if source_name and source_name in edb.components.components:
                target_comp[source_name] = edb.components.components[source_name]

            for comp_name, comp_inst in target_comp.items():
                x1, y1, x2, y2 = comp_inst.bounding_box
                min_x = min(min_x, x1)
                min_y = min(min_y, y1)
                max_x = max(max_x, x2)
                max_y = max(max_y, y2)
                corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0], [x1, y1, 0]])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))
                fit_plotter.add_mesh(rect, color="black", line_width=3, lighting=False)
                tc = [(x1 + x2) * 0.5, (y1 + y2) * 0.5, 0.0]
                fit_plotter.add_point_labels([tc], [comp_name], font_size=26, text_color="blue", shape=None)

            if target_bbox[0] == float("inf"):
                target_bbox = [min_x, min_y, max_x, max_y]

            dx = max(max_x - min_x, 1e-9)
            dy = max(max_y - min_y, 1e-9)
            pad_x = max((target_bbox[2] - target_bbox[0]) * 0.28, dx * 0.02)
            pad_y = max((target_bbox[3] - target_bbox[1]) * 0.28, dy * 0.02)
            z_x1 = target_bbox[0] - pad_x
            z_y1 = target_bbox[1] - pad_y
            z_x2 = target_bbox[2] + pad_x
            z_y2 = target_bbox[3] + pad_y

            z_corners = np.array([[z_x1, z_y1, 0], [z_x2, z_y1, 0], [z_x2, z_y2, 0], [z_x1, z_y2, 0], [z_x1, z_y1, 0]])
            z_rect = pv.PolyData(z_corners)
            z_rect.lines = np.hstack([[len(z_corners)]] + list(range(len(z_corners))))
            fit_plotter.add_mesh(z_rect, color="red", line_width=5, lighting=False)
            fit_plotter.add_point_labels([[z_x1, z_y2, 0]], ["Zoom Area"], font_size=34, text_color="red", shape=None)

            fit_plotter.camera_position = "xy"
            fit_plotter.remove_bounds_axes()
            fit_plotter.screenshot(str(fit_view_path), window_size=[2560, 1440])
            fit_plotter.close()
            fit_plotter = None

            zoom_plotter = pv.Plotter(off_screen=True, title="ZoomView")
            zoom_plotter.background_color = "white"
            for shape in bg_shapes:
                zoom_plotter.add_mesh(shape, color="#D5DADF", opacity=0.14, lighting=False)
            for shape in fg_shapes:
                zoom_plotter.add_mesh(shape, color="#F39229", opacity=0.78, lighting=False)
            for comp_name, comp_inst in target_comp.items():
                x1, y1, x2, y2 = comp_inst.bounding_box
                corners = np.array([[x1, y1, 0], [x2, y1, 0], [x2, y2, 0], [x1, y2, 0], [x1, y1, 0]])
                rect = pv.PolyData(corners)
                rect.lines = np.hstack([[len(corners)]] + list(range(len(corners))))
                zoom_plotter.add_mesh(rect, color="black", line_width=3, lighting=False)
                tc = [(x1 + x2) * 0.5, (y1 + y2) * 0.5, 0.0]
                zoom_plotter.add_point_labels([tc], [comp_name], font_size=26, text_color="blue", shape=None)

            self._set_parallel_camera_to_bounds(zoom_plotter, [z_x1, z_x2, z_y1, z_y2, -0.1, 0.1], margin=0.02)
            zoom_plotter.remove_bounds_axes()
            zoom_plotter.screenshot(str(zoom_view_path), window_size=[2560, 1440])
            zoom_plotter.close()
            zoom_plotter = None

            self._log(
                f"[AEDT][IMG] DCIR-style net-path images exported: {fit_view_path.name}, {zoom_view_path.name}",
                level=LogLevel.INFO,
            )
            return fit_view_path.exists() and zoom_view_path.exists()
        except Exception as e:
            self._log(f"[AEDT][IMG][WARNING] DCIR-style net-path render failed: {e}", level=LogLevel.WARNING)
            return False
        finally:
            if fit_plotter:
                try:
                    fit_plotter.close()
                except Exception:
                    pass
            if zoom_plotter:
                try:
                    zoom_plotter.close()
                except Exception:
                    pass
            if edb:
                try:
                    edb.close_edb()
                except Exception:
                    try:
                        edb.close()
                    except Exception:
                        pass

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

            # DCIR-like output quality: strong supersampling + high-contrast overlay.
            width, height = 3840, 2160
            ss = 4
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

            bg_color = (250, 250, 250, 255)
            # Keep board context visible without turning entire board into a filled rectangle.
            other_fill_soft = (210, 216, 220, 58)
            other_fill_trace = (188, 197, 204, 102)
            other_edge = (185, 192, 198, 120)
            target_fill = (243, 152, 41, 215)
            target_edge = (123, 45, 0, 255)
            target_halo = (255, 205, 125, 148)

            fit_img = Image.new("RGBA", (canvas_w, canvas_h), bg_color)
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
                rw = max(1.0, x2 - x1)
                rh = max(1.0, y2 - y1)
                area_ratio = ((x2 - x1) * (y2 - y1)) / board_area
                elong = max(rw, rh) / max(min(rw, rh), 1e-9)
                if area_ratio > 0.50:
                    # Very large planes: keep contour only.
                    draw.rectangle(rect, outline=other_edge, width=max(1, ss))
                else:
                    if area_ratio < 0.0015 and elong > 7.0:
                        draw.rectangle(rect, fill=other_fill_trace, outline=other_edge, width=max(1, ss))
                    else:
                        draw.rectangle(rect, fill=other_fill_soft, outline=other_edge, width=max(1, ss))

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
                outline=(166, 173, 179, 170),
                width=max(2, ss),
            )

            # FitView style: add explicit zoom area on full-board view.
            z_fit_rect = None
            if target_boxes:
                tmin_x = min(b[0] for b in target_boxes)
                tmin_y = min(b[1] for b in target_boxes)
                tmax_x = max(b[2] for b in target_boxes)
                tmax_y = max(b[3] for b in target_boxes)
                pad_x = max((tmax_x - tmin_x) * 0.35, dx * 0.03)
                pad_y = max((tmax_y - tmin_y) * 0.35, dy * 0.03)
                zl, zt, zr, zb = _map_rect(tmin_x - pad_x, tmin_y - pad_y, tmax_x + pad_x, tmax_y + pad_y)
                z_fit_rect = (
                    max(0, min(canvas_w - 1, zl)),
                    max(0, min(canvas_h - 1, zt)),
                    max(1, min(canvas_w, zr)),
                    max(1, min(canvas_h, zb)),
                )
                lz, tz, rz, bz = z_fit_rect
                draw.rectangle((lz, tz, rz, bz), outline=(220, 20, 20, 255), width=max(6, ss * 2))
                draw.text((lz + 12 * ss, max(0, tz - 24 * ss)), "Zoom Area", fill=(220, 20, 20, 255))

            if title_text:
                draw.rectangle(
                    (20 * ss, 16 * ss, min(canvas_w - 20 * ss, 1800 * ss), 90 * ss),
                    fill=(255, 255, 255, 240),
                    outline=(60, 60, 60, 220),
                    width=max(1, ss),
                )
                draw.text((32 * ss, 36 * ss), title_text, fill=(30, 30, 30, 255))

            fit_final = fit_img.resize((width, height), resample=Image.Resampling.LANCZOS).convert("RGB")
            fit_final.save(str(fit_view_path), quality=100, subsampling=0, optimize=True)

            # Zoom around highlighted nets; if none found, duplicate fit image.
            if z_fit_rect:
                zl, zt, zr, zb = z_fit_rect
                zr = max(zl + 1, zr)
                zb = max(zt + 1, zb)
                zoom_img = fit_img.crop((zl, zt, zr, zb)).resize((width, height), resample=Image.Resampling.LANCZOS).convert("RGB")
                # Add zoom border for readability in reports.
                zdraw = ImageDraw.Draw(zoom_img)
                zdraw.rectangle((8, 8, width - 8, height - 8), outline=(120, 145, 165), width=4)
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
        model_safety_filter_enabled = bool(cut_cfg.get("model_safety_filter_enabled", True))
        sweep_force_recreate = bool(cut_cfg.get("sweep_force_recreate", True))
        safe_minimal_retry_enabled = bool(cut_cfg.get("safe_minimal_retry_enabled", True))
        seed_global = self._dedupe_keep_order(cut_cfg.get("model_substitute_seed", []) or [])
        if not seed_global:
            # Default safeguard for recurring low-compatibility embedded model in this project.
            seed_global = ["C9903"]
        seed_by_net = cut_cfg.get("model_substitute_seed_by_net", {}) or {}
        try:
            max_model_sub_retries = int(cut_cfg.get("model_substitute_max_retries", 2))
        except Exception:
            max_model_sub_retries = 2
        max_model_sub_retries = max(0, min(max_model_sub_retries, 12))
        bom_info = conf_data.get("__BOM_INFO__", {}) if isinstance(conf_data, dict) else {}
        bom_designator_tokens = self._extract_bom_designator_tokens(bom_info)
        if bom_designator_tokens:
            self._log(
                f"[AEDT][CUTOUT] BOM designator tokens loaded: {len(bom_designator_tokens)}",
                level=LogLevel.DETAIL1,
            )

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
                    {
                        "Case_Index": idx,
                        "IC": ic,
                        "Net": net,
                        "PCB_Net": pcb_net,
                        "Status": "Skipped",
                        "Reason": "No signal nets",
                        "Setup_Name": "",
                        "Sweep_Name": "",
                        "Ports": [],
                        "Failure_Label": "NO_SIGNAL_NETS",
                        "Failure_Detail": "No signal nets were resolved for this case.",
                    }
                )
                continue

            cutout_name = f"{model_name}_CUTOUT_{idx:03d}_{self._safe(ic)}_{self._safe(net)}.aedb"
            cutout_path = output_dir / cutout_name
            aedt_proj = cutout_path.with_suffix(".aedt")
            case_src_edb = output_dir / f"{model_name}_CUTSRC_{idx:03d}.aedb"
            active_cutout_path = cutout_path
            active_aedt_proj = aedt_proj

            edb = None
            h3dl = None
            ensured_ports = []
            try:
                self._log(
                    f"[AEDT][CUTOUT] Case#{idx}: IC={ic}, Net={net}, PCB_Net={pcb_net}, signal_nets={signal_nets}, gnd={gnd}",
                    level=LogLevel.INFO,
                )
                safe_case = f"{self._safe(ic)}_{self._safe(net)}"
                net_seed = self._dedupe_keep_order(seed_by_net.get(str(net), []) if isinstance(seed_by_net, dict) else [])
                case_seed_substitute = self._filter_substitute_targets(self._dedupe_keep_order(seed_global + net_seed), protected_tokens=None, skip_vrm=True)
                solve_ok = False
                mesh_related_fail = False
                retry_worthy_fail = False
                model_freq_fail = False
                solve_fail_detail = ""
                setup = None
                solve_profile = "base"
                solve_setup_name = ""
                solve_sweep_name = ""
                solve_model_substitute = []
                retry_profiles = [
                    {
                        "extent": extent_type,
                        "expand": expansion_size,
                        "include_pg": include_pingroups,
                        "check_terms": check_terminals,
                        "tag": "base",
                        "model_substitute": case_seed_substitute,
                        "safety_aggressive": False,
                    },
                    {
                        "extent": extent_type,
                        "expand": max(expansion_size * 1.8, expansion_size + 0.0015),
                        "include_pg": False,  # mesh-safe: reduce inherited pin-groups
                        "check_terms": False,  # mesh-safe: avoid terminal carry-over complexity
                        "tag": "mesh_retry_expand",
                        "model_substitute": case_seed_substitute,
                        "safety_aggressive": False,
                    },
                ]
                if safe_minimal_retry_enabled:
                    retry_profiles.append(
                        {
                            "extent": "Conforming",
                            "expand": max(expansion_size * 2.0, expansion_size + 0.0020),
                            "include_pg": False,
                            "check_terms": False,
                            "tag": "safe_minimal",
                            "model_substitute": case_seed_substitute,
                            "safety_aggressive": True,
                        }
                    )
                model_sub_retry_count = 0
                model_sub_changes = {"disabled": [], "deleted": [], "missing": [], "errors": []}

                for prof in retry_profiles:
                    prof_extent = prof.get("extent", extent_type)
                    prof_expand = float(prof.get("expand", expansion_size))
                    prof_include_pg = bool(prof.get("include_pg", include_pingroups))
                    prof_check_terms = bool(prof.get("check_terms", check_terminals))
                    prof_tag = str(prof.get("tag", "base"))
                    prof_model_substitute = self._dedupe_keep_order(prof.get("model_substitute", []))
                    prof_safety_aggressive = bool(prof.get("safety_aggressive", False))
                    profile_case_src = output_dir / f"{model_name}_CUTSRC_{idx:03d}_{prof_tag}.aedb"
                    profile_cutout = (
                        cutout_path
                        if prof_tag == "base"
                        else output_dir / f"{model_name}_CUTOUT_{idx:03d}_{self._safe(ic)}_{self._safe(net)}_{prof_tag}.aedb"
                    )
                    profile_aedt = profile_cutout.with_suffix(".aedt")
                    active_cutout_path = profile_cutout
                    active_aedt_proj = profile_aedt

                    if profile_case_src.exists():
                        shutil.rmtree(profile_case_src, ignore_errors=True)
                    if profile_cutout.exists():
                        shutil.rmtree(profile_cutout, ignore_errors=True)
                    try:
                        if profile_aedt.exists():
                            profile_aedt.unlink()
                    except Exception:
                        pass
                    shutil.copytree(ref_edb_path, profile_case_src)

                    cutout_build = self._create_cutout_with_validation(
                        source_edb_path=profile_case_src,
                        output_aedb_path=profile_cutout,
                        signal_nets=signal_nets,
                        gnd_net=gnd,
                        extent_type=prof_extent,
                        expansion_size=prof_expand,
                        include_pingroups=prof_include_pg,
                        check_terminals=prof_check_terms,
                        preserve_models=preserve_models,
                        case_idx=idx,
                        profile_tag=prof_tag,
                    )
                    if not cutout_build.get("ok"):
                        raise RuntimeError(
                            f"Cutout validation failed after remediation: {cutout_build.get('detail', 'unknown')}"
                        )
                    # keep the effective cutout knobs for downstream diagnostics and failure details
                    prof_extent = cutout_build.get("extent", prof_extent)
                    prof_expand = float(cutout_build.get("expand", prof_expand))
                    prof_include_pg = bool(cutout_build.get("include_pg", prof_include_pg))
                    prof_check_terms = bool(cutout_build.get("check_terms", prof_check_terms))

                    ensured_ports = self._ensure_case_port_in_cutout_edb(
                        cutout_path=profile_cutout,
                        case=case,
                        gnd_net=gnd,
                        safe_case=safe_case,
                    )
                    _bom_cap_changes = self._apply_bom_capacitor_policy(
                        cutout_path=profile_cutout,
                        signal_nets=signal_nets,
                        bom_designator_tokens=bom_designator_tokens,
                        case_idx=idx,
                        profile_tag=prof_tag,
                    )
                    case_protected_tokens, case_protected_detail = self._collect_case_protected_component_tokens(
                        cutout_path=profile_cutout,
                        case=case,
                        signal_nets=signal_nets,
                        gnd_net=gnd,
                        bom_designator_tokens=bom_designator_tokens,
                    )
                    direct_signal_cap_tokens, direct_signal_cap_detail = self._collect_direct_signal_capacitor_tokens(
                        cutout_path=profile_cutout,
                        signal_nets=signal_nets,
                        gnd_net=gnd,
                    )
                    prof_model_substitute = self._filter_substitute_targets(
                        prof_model_substitute,
                        protected_tokens=case_protected_tokens,
                        skip_vrm=True,
                    )
                    self._log(
                        f"[AEDT][CUTOUT] Protected components for case#{idx} ({prof_tag}): "
                        f"caps={len(case_protected_detail.get('caps', []))}, "
                        f"vrm={len(case_protected_detail.get('vrm', []))}, "
                        f"bom_blocked={len(case_protected_detail.get('bom_blocked', []))}, "
                        f"errors={case_protected_detail.get('errors', [])[:2]}",
                        level=LogLevel.DETAIL1,
                    )
                    self._log(
                        f"[AEDT][CUTOUT] Direct-net decap force-keep for case#{idx} ({prof_tag}): "
                        f"caps={len(direct_signal_cap_detail.get('caps', []))}, "
                        f"errors={direct_signal_cap_detail.get('errors', [])[:2]}",
                        level=LogLevel.DETAIL1,
                    )
                    if model_safety_filter_enabled:
                        nets_for_filter = signal_nets[:1] if prof_safety_aggressive else signal_nets
                        safety_changes = self._sanitize_cutout_capacitor_models(
                            cutout_path=profile_cutout,
                            gnd_net=gnd,
                            signal_nets=nets_for_filter,
                            protected_tokens=case_protected_tokens,
                            force_keep_tokens=direct_signal_cap_tokens,
                        )
                        self._log(
                            f"[AEDT][CUTOUT] Applied model safety filter for case#{idx} ({prof_tag}): "
                            f"disabled={len(safety_changes.get('disabled', []))}, "
                            f"kept={len(safety_changes.get('kept', []))}, "
                            f"force_kept={len(safety_changes.get('force_kept', []))}, "
                            f"unsafe_pin_count={len(safety_changes.get('unsafe_pin_count', []))}, "
                            f"unsafe_no_gnd={len(safety_changes.get('unsafe_no_gnd', []))}, "
                            f"unsafe_multi_net={len(safety_changes.get('unsafe_multi_net', []))}, "
                            f"errors={safety_changes.get('errors', [])[:3]}",
                            level=LogLevel.DETAIL1 if not safety_changes.get("errors") else LogLevel.WARNING,
                        )
                    model_sub_changes = {"disabled": [], "deleted": [], "missing": [], "errors": []}
                    if prof_model_substitute:
                        model_sub_changes = self._deactivate_components_in_cutout(
                            cutout_path=profile_cutout,
                            component_names=prof_model_substitute,
                            protected_tokens=case_protected_tokens,
                        )
                        self._log(
                            f"[AEDT][CUTOUT] Applied model substitution for case#{idx} ({prof_tag}): "
                            f"requested={model_sub_changes.get('requested', [])}, "
                            f"disabled={model_sub_changes.get('disabled', [])}, "
                            f"deleted={model_sub_changes.get('deleted', [])}, "
                            f"missing={model_sub_changes.get('missing', [])}, "
                            f"errors={model_sub_changes.get('errors', [])}",
                            level=LogLevel.WARNING if model_sub_changes.get("errors") else LogLevel.DETAIL1,
                        )

                    repair_ok, repair_detail = self._repair_and_validate_cutout_before_import(
                        cutout_path=profile_cutout,
                        signal_nets=signal_nets,
                        gnd_net=gnd,
                        case_idx=idx,
                        profile_tag=prof_tag,
)
                    if not repair_ok:
                        solve_fail_detail = (
                            f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                            f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                            f"model_substitute={prof_model_substitute}, "
                            f"analyze_ret=SKIPPED_REPAIR_VALIDATE, probe=SKIPPED_REPAIR_VALIDATE, "
                            f"messages={repair_detail}"
                        )
                        mesh_related_fail = self._is_mesh_related_text(solve_fail_detail)
                        retry_worthy_fail = True
                        model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                        self._log(
                            f"[AEDT][CUTOUT][WARNING] Repair/validation failed for case#{idx} ({prof_tag}). "
                            f"retry_worthy={retry_worthy_fail}. detail={solve_fail_detail}",
                            level=LogLevel.WARNING,
                        )
                        continue

                    h3dl = Hfss3dLayout(version=self.version, non_graphical=non_graphical)
                    imported_ok, imported_detail = self._import_cutout_with_retry(
                        h3dl=h3dl,
                        cutout_path=profile_cutout,
                        case_idx=idx,
                        profile_tag=prof_tag,
                        max_retry=3,
                    )
                    if not imported_ok:
                        solve_fail_detail = (
                            f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                            f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                            f"model_substitute={prof_model_substitute}, "
                            f"analyze_ret=SKIPPED_IMPORT, probe=SKIPPED_IMPORT, messages={imported_detail}"
                        )
                        mesh_related_fail = self._is_mesh_related_text(solve_fail_detail)
                        retry_worthy_fail = True
                        model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                        self._log(
                            f"[AEDT][CUTOUT][WARNING] Import precheck failed for case#{idx} ({prof_tag}). "
                            f"retry_worthy={retry_worthy_fail}. detail={solve_fail_detail}",
                            level=LogLevel.WARNING,
                        )
                        try:
                            h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                        h3dl = None
                        continue

                    # Import succeeded; run a lightweight mesh-warning precheck before solve.
                    pre_msgs = self._collect_design_messages(h3dl)
                    pre_mesh_msgs = [m for m in (pre_msgs or []) if self._is_mesh_related_text(m)]
                    if pre_mesh_msgs:
                        solve_fail_detail = (
                            f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                            f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                            f"model_substitute={prof_model_substitute}, "
                            f"analyze_ret=SKIPPED_PRE_MESH_VALIDATE, probe=SKIPPED_PRE_MESH_VALIDATE, "
                            f"messages={' | '.join(pre_mesh_msgs[-8:])}"
                        )
                        mesh_related_fail = True
                        retry_worthy_fail = True
                        model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                        self._log(
                            f"[AEDT][CUTOUT][WARNING] Mesh precheck failed for case#{idx} ({prof_tag}). "
                            f"retry_worthy={retry_worthy_fail}. detail={solve_fail_detail}",
                            level=LogLevel.WARNING,
                        )
                        try:
                            h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                        h3dl = None
                        continue
                    precheck_ok, precheck_ports, precheck_detail, _precheck_catalog = self._validate_case_excitation(
                        h3dl=h3dl,
                        cutout_path=profile_cutout,
                        case=case,
                        safe_case=safe_case,
                        ensured_ports=ensured_ports,
                    )
                    if not precheck_ok:
                        solve_fail_detail = (
                            f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                            f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                            f"model_substitute={prof_model_substitute}, "
                            f"analyze_ret=SKIPPED_PRECHECK, probe=SKIPPED_PRECHECK, messages={precheck_detail}"
                        )
                        mesh_related_fail = self._is_mesh_related_text(solve_fail_detail)
                        retry_worthy_fail = self._is_retry_worthy_solve_text(solve_fail_detail)
                        model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                        self._log(
                            f"[AEDT][CUTOUT][WARNING] Excitation precheck failed for case#{idx} ({prof_tag}). "
                            f"retry_worthy={retry_worthy_fail}. detail={solve_fail_detail}",
                            level=LogLevel.WARNING,
                        )
                        try:
                            h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                        h3dl = None
                        if not retry_worthy_fail:
                            break
                        continue
                    # Robust port bundle validation before solve.
                    chosen_port = str(precheck_ports[0]) if precheck_ports else ""
                    bundle_ok, bundle_detail = self._validate_case_port_bundle(_precheck_catalog, chosen_port)
                    if not bundle_ok:
                        solve_fail_detail = (
                            f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                            f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                            f"model_substitute={prof_model_substitute}, analyze_ret=SKIPPED_PORT_BUNDLE, "
                            f"probe=no_port_data, messages={bundle_detail}"
                        )
                        mesh_related_fail = self._is_mesh_related_text(solve_fail_detail)
                        retry_worthy_fail = self._is_retry_worthy_solve_text(solve_fail_detail)
                        model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                        self._log(
                            f"[AEDT][CUTOUT][WARNING] Port bundle validation failed for case#{idx} ({prof_tag}): {bundle_detail}",
                            level=LogLevel.WARNING,
                        )
                        try:
                            h3dl.release_desktop(close_projects=True, close_desktop=True)
                        except Exception:
                            pass
                        h3dl = None
                        if not retry_worthy_fail:
                            break
                        continue
                    setup = h3dl.create_setup(name=f"SYZ_CUTOUT_{idx}")
                    self._force_setup_adaptive_1ghz(h3dl, setup)
                    probe_setup, probe_sweep = self._ensure_sweep_for_touchstone_with_mode(
                        h3dl, setup, idx, force_recreate=sweep_force_recreate
                    )
                    analyze_ret = h3dl.analyze(setup=setup.name if setup else None)
                    h3dl.save_project(file_name=str(profile_aedt))
                    if (not probe_sweep) and sweep_force_recreate:
                        probe_setup, probe_sweep = self._ensure_sweep_for_touchstone_with_mode(
                            h3dl, setup, idx, force_recreate=True
                        )
                    probe_port = str(precheck_ports[0]) if precheck_ports else ""
                    probe_ok, probe_detail = self._probe_solve_success(
                        h3dl,
                        probe_setup,
                        probe_sweep,
                        probe_port,
                    )

                    solve_ok = (analyze_ret is not False) and probe_ok
                    if solve_ok:
                        cutout_path = profile_cutout
                        aedt_proj = profile_aedt
                        solve_profile = prof_tag
                        solve_setup_name = str(probe_setup or "")
                        solve_sweep_name = str(probe_sweep or "")
                        solve_model_substitute = list(prof_model_substitute or [])
                        self._log(
                            f"[AEDT][CUTOUT] Solve success for case#{idx} ({prof_tag}): "
                            f"setup={probe_setup}, sweep={probe_sweep}, port={probe_port}, detail={probe_detail}",
                            level=LogLevel.DETAIL1,
                        )
                        break

                    msg_list = self._collect_design_messages(h3dl)
                    combined = " | ".join(msg_list[-12:]) if msg_list else ""
                    solve_fail_detail = (
                        f"profile={prof_tag}, extent={prof_extent}, expansion={prof_expand}, "
                        f"include_pingroups={prof_include_pg}, check_terminals={prof_check_terms}, "
                        f"model_substitute={prof_model_substitute}, "
                        f"analyze_ret={analyze_ret}, probe={probe_detail}, messages={combined}"
                    )
                    mesh_related_fail = self._is_mesh_related_text(solve_fail_detail)
                    retry_worthy_fail = self._is_retry_worthy_solve_text(solve_fail_detail)
                    model_freq_fail = self._is_model_freq_coverage_text(solve_fail_detail)
                    failed_comps = self._extract_interpolation_failed_components(combined)
                    filtered_failed_comps = self._filter_substitute_targets(
                        failed_comps,
                        protected_tokens=case_protected_tokens,
                        skip_vrm=True,
                    )
                    if model_freq_fail and filtered_failed_comps:
                        merged_substitute = self._filter_substitute_targets(
                            self._dedupe_keep_order((prof_model_substitute or []) + filtered_failed_comps),
                            protected_tokens=case_protected_tokens,
                            skip_vrm=True,
                        )
                        new_added = [c for c in merged_substitute if c not in (prof_model_substitute or [])]
                        if len(merged_substitute) > len(prof_model_substitute):
                            allow_retry = model_sub_retry_count < max_model_sub_retries
                            if allow_retry:
                                model_sub_retry_count += 1
                                retry_tag = f"model_retry_substitute_{model_sub_retry_count}"
                                retry_profiles.append(
                                    {
                                        "extent": prof_extent,
                                        "expand": max(prof_expand, max(expansion_size * 1.8, expansion_size + 0.0015)),
                                        "include_pg": False,
                                        "check_terms": False,
                                        "tag": retry_tag,
                                        "model_substitute": merged_substitute,
                                    }
                                )
                                self._log(
                                    f"[AEDT][CUTOUT] Scheduled model substitution retry for case#{idx}: "
                                    f"retry={model_sub_retry_count}/{max_model_sub_retries}, "
                                    f"added={new_added}, cumulative={merged_substitute}",
                                    level=LogLevel.WARNING,
                                )
                            else:
                                self._log(
                                    f"[AEDT][CUTOUT][WARNING] Model substitution retry limit reached for case#{idx}: "
                                    f"limit={max_model_sub_retries}, pending_added={new_added}",
                                    level=LogLevel.WARNING,
                                )
                        else:
                            self._log(
                                f"[AEDT][CUTOUT] Model substitution candidate has no new components for case#{idx}: "
                                f"candidates={filtered_failed_comps}",
                                level=LogLevel.DETAIL1,
                            )
                    elif model_freq_fail and failed_comps:
                        self._log(
                            f"[AEDT][CUTOUT] Model substitution candidates were blocked by protection rules for case#{idx}: "
                            f"blocked={failed_comps}",
                            level=LogLevel.WARNING,
                        )
                    self._log(
                        f"[AEDT][CUTOUT][WARNING] Solve failed for case#{idx} ({prof_tag}). "
                        f"mesh_related={mesh_related_fail}, model_freq_related={model_freq_fail}, "
                        f"retry_worthy={retry_worthy_fail}. detail={solve_fail_detail}",
                        level=LogLevel.WARNING,
                    )
                    try:
                        h3dl.release_desktop(close_projects=True, close_desktop=True)
                    except Exception:
                        pass
                    h3dl = None
                    if not retry_worthy_fail:
                        break

                # Best-effort extraction of impedance artifacts for result payload.
                safe_case = f"{self._safe(ic)}_{self._safe(net)}"
                z_plot = output_dir / f"Z_Param_{safe_case}.jpg"
                z_csv = output_dir / f"Z_Param_{safe_case}.csv"
                fit_view = output_dir / f"{safe_case}_FitView.jpg"
                zoom_view = output_dir / f"{safe_case}_ZoomView.jpg"
                touchstone = ""
                msg = "OK"
                ports = []
                setup_name = ""
                sweep_name = ""
                failure_label = ""
                failure_detail = ""
                port_catalog = {}
                try:
                    if not solve_ok:
                        fail_lower = str(solve_fail_detail or "").lower()
                        if ("no_trace_data" in fail_lower) or ("no trace data for quantity" in fail_lower):
                            failure_label = "NO_TRACE_DATA"
                        elif ("no_valid_sweep_or_port" in fail_lower) or ("setup/sweep/port missing" in fail_lower):
                            failure_label = "NO_VALID_SWEEP"
                        elif ("no_port_data" in fail_lower) or ("skipped_port_bundle" in fail_lower):
                            failure_label = "NO_PORT_DATA"
                        elif model_freq_fail:
                            failure_label = "MODEL_FREQ_COVERAGE_MISSING"
                        elif mesh_related_fail:
                            failure_label = "MESH_FAILED"
                        elif ("no excitations are defined" in fail_lower) or ("precheck=no_case_excitation" in fail_lower):
                            failure_label = "EXCITATION_MISSING"
                        else:
                            failure_label = "SOLVE_FAILED"
                        sub_summary = (
                            f"substitute_disabled={model_sub_changes.get('disabled', [])}, "
                            f"substitute_deleted={model_sub_changes.get('deleted', [])}, "
                            f"substitute_missing={model_sub_changes.get('missing', [])}, "
                            f"substitute_errors={model_sub_changes.get('errors', [])}"
                        )
                        base_detail = solve_fail_detail or "Solve did not produce valid solution data."
                        failure_detail = f"{base_detail} | {sub_summary}"
                        msg = "OK (solve failed, export skipped)"
                        self._log(
                            f"[AEDT][ART][WARNING] Export skipped for case#{idx}: {failure_label} | {failure_detail}",
                            level=LogLevel.WARNING,
                        )
                        raise RuntimeError(failure_detail)

                    port_catalog = self._build_port_catalog(
                        h3dl=h3dl,
                        cutout_path=active_cutout_path,
                        ensured_ports=ensured_ports,
                    )
                    ports = self._pick_case_ports(port_catalog, case, safe_case)
                    self._log(
                        f"[AEDT][ART] Ports detected for case#{idx}: usable={ports}, "
                        f"sources={port_catalog.get('by_source', {})}",
                        level=LogLevel.DETAIL1,
                    )
                    setup_name = str(solve_setup_name or "")
                    sweep_name = str(solve_sweep_name or "")
                    if not setup_name:
                        setup_name, sweep_name = self._ensure_sweep_for_touchstone(h3dl, setup, idx)
                    self._log(
                        f"[AEDT][ART] Setup/Sweep selected for case#{idx}: setup={setup_name}, sweep={sweep_name or '<none>'}",
                        level=LogLevel.DETAIL1,
                    )
                    solution_name = f"{setup_name} : {sweep_name}" if (setup_name and sweep_name) else setup_name

                    if setup_name and ports:
                        plot_name = f"Z_Param_{safe_case}"
                        # Export touchstone first so report API issues do not block solver artifacts.
                        ts_file = output_dir / f"{plot_name}.s1p"
                        ts_errors = []
                        touchstone, ts_err = self._export_touchstone_best_effort(
                            h3dl,
                            setup_name,
                            sweep_name,
                            ts_file,
                            debug_case=safe_case,
                            debug_output_dir=output_dir,
                            export_port=(ports[0] if ports else None),
                        )
                        if (not touchstone) and ts_err:
                            ts_errors.append(f"{ts_file}: {ts_err}")
                        if (not touchstone) and ts_errors:
                            failure_label = "EXPORT_API_NO_OUTPUT"
                            failure_detail = " | ".join(ts_errors)
                            self._log(
                                f"[AEDT][ART][WARNING] Touchstone export failed for case#{idx}: {' | '.join(ts_errors)}",
                                level=LogLevel.WARNING,
                            )
                        try:
                            expressions = [f"mag(Z({p},{p}))" for p in ports]
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
                        except Exception as report_err:
                            self._log(
                                f"[AEDT][ART][WARNING] Report generation failed for case#{idx}: {report_err}",
                                level=LogLevel.WARNING,
                            )
                            if not failure_label:
                                failure_label = "REPORT_EXPORT_EXCEPTION"
                                failure_detail = str(report_err)
                    else:
                        if not setup_name:
                            failure_label = "NO_SETUP"
                            failure_detail = "Setup name is empty; report/export step skipped."
                        elif not ports:
                            failure_label = "NO_PORTS"
                            failure_detail = (
                                f"No usable ports after catalog normalization. "
                                f"all_ports={port_catalog.get('all_ports', [])}, "
                                f"errors={port_catalog.get('errors', {})}, "
                                f"cutout={cutout_path}"
                            )
                        else:
                            failure_label = "REPORT_EXPORT_SKIPPED"
                            failure_detail = f"Report export skipped for setup={setup_name}, ports={ports}"
                        self._log(
                            f"[AEDT][ART][WARNING] Report export skipped for case#{idx}: setup={setup_name}, ports={ports}",
                            level=LogLevel.WARNING,
                        )

                    if not touchstone:
                        extra_roots = []
                        for attr_name in ("project_path", "working_directory"):
                            try:
                                v = getattr(h3dl, attr_name, None)
                            except Exception:
                                v = None
                            if v:
                                extra_roots.append(v)
                        ts_found = self._find_touchstone_artifact(
                            output_dir,
                            active_aedt_proj,
                            safe_case,
                            extra_roots=extra_roots,
                        )
                        if ts_found:
                            touchstone = str(ts_found)
                    if touchstone:
                        failure_label = ""
                        failure_detail = ""
                    elif not failure_label:
                        failure_label = "TOUCHSTONE_NOT_FOUND"
                        failure_detail = (
                            "Touchstone was not generated by export APIs and not found by artifact scan."
                        )

                    if touchstone and (not z_plot.exists() or not z_csv.exists()):
                        self._write_impedance_artifacts_from_touchstone(
                            ts_path=Path(touchstone),
                            z_csv=z_csv,
                            z_plot=z_plot,
                        )

                    # Image policy: DCIR-style PyVista capture first.
                    # Prefer reference EDB first to avoid "same EDB opened twice" conflicts
                    # while AEDT/H3DL session keeps cutout EDB handles alive.
                    dcir_primary_edb = Path(ref_edb_path).resolve()
                    dcir_fallback_edb = Path(active_cutout_path).resolve()
                    dcir_style_ok = self._render_dcir_style_net_path_images(
                        edb_path=dcir_primary_edb,
                        target_nets=signal_nets,
                        fit_view_path=fit_view,
                        zoom_view_path=zoom_view,
                        ic_name=str(case.get("IC", "")).strip(),
                        source_name=str(case.get("Source_name", "")).strip(),
                    )
                    if (not dcir_style_ok) and (dcir_fallback_edb != dcir_primary_edb):
                        self._log(
                            f"[AEDT][IMG] DCIR-style retry from cutout EDB for case={safe_case}",
                            level=LogLevel.DETAIL1,
                        )
                        dcir_style_ok = self._render_dcir_style_net_path_images(
                            edb_path=dcir_fallback_edb,
                            target_nets=signal_nets,
                            fit_view_path=fit_view,
                            zoom_view_path=zoom_view,
                            ic_name=str(case.get("IC", "")).strip(),
                            source_name=str(case.get("Source_name", "")).strip(),
                        )
                    if not dcir_style_ok:
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
                    if solve_ok:
                        self._log(
                            f"[AEDT][ART][WARNING] Artifact export partial for case#{idx}: {artifact_err}",
                            level=LogLevel.WARNING,
                        )
                    if not failure_label:
                        failure_label = "ARTIFACT_EXPORT_EXCEPTION"
                        failure_detail = str(artifact_err)
                    if solve_ok:
                        msg = f"OK (solve done, artifact export partial: {artifact_err})"
                    else:
                        msg = "OK (solve failed, export skipped)"

                summary["Done"] += 1
                reduced_model = bool(solve_profile != "base")
                reliability = "Reduced" if reduced_model else "Nominal"
                reliability_reason = (
                    f"solve_profile={solve_profile}, model_substitute={solve_model_substitute}"
                    if reduced_model
                    else "solve_profile=base"
                )
                summary["Records"].append(
                    {
                        "Case_Index": idx,
                        "IC": ic,
                        "Net": net,
                        "PCB_Net": pcb_net,
                        "Status": "Done",
                        "Cutout_Edb": str(active_cutout_path),
                        "Aedt_Project": str(active_aedt_proj),
                        "Impedance_Plot": str(z_plot) if z_plot.exists() else "",
                        "Impedance_CSV": str(z_csv) if z_csv.exists() else "",
                        "Touchstone": touchstone,
                        "FitView": str(fit_view) if fit_view.exists() else "",
                        "ZoomView": str(zoom_view) if zoom_view.exists() else "",
                        "Setup_Name": setup_name,
                        "Sweep_Name": sweep_name,
                        "Ports": ports,
                        "Port_Catalog": port_catalog.get("by_source", {}) if isinstance(port_catalog, dict) else {},
                        "Solve_Profile": solve_profile,
                        "Model_Substitute": solve_model_substitute,
                        "Result_Reliability": reliability,
                        "Result_Reliability_Reason": reliability_reason,
                        "Failure_Label": failure_label,
                        "Failure_Detail": failure_detail,
                        "Message": msg,
                    }
                )
            except Exception as e:
                summary["Skipped"] += 1
                summary["Records"].append(
                    {
                        "Case_Index": idx,
                        "IC": ic,
                        "Net": net,
                        "PCB_Net": pcb_net,
                        "Status": "Skipped",
                        "Reason": str(e),
                        "Setup_Name": "",
                        "Sweep_Name": "",
                        "Ports": [],
                        "Failure_Label": "CUTOUT_CASE_EXCEPTION",
                        "Failure_Detail": str(e),
                    }
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
                try:
                    for temp_src in output_dir.glob(f"{model_name}_CUTSRC_{idx:03d}*.aedb"):
                        shutil.rmtree(temp_src, ignore_errors=True)
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

            for net_name, net_obj in (edb.nets.nets or {}).items():
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
                        net_key = str(net_name).strip().upper()
                        boxes.append((x1, y1, x2, y2, net_key))
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
            ss = 4
            canvas_w, canvas_h = width * ss, height * ss
            margin = 0.04
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

            base = Image.new("RGBA", (canvas_w, canvas_h), (238, 240, 239, 255))
            draw = ImageDraw.Draw(base)
            board_area = max(dx * dy, 1e-12)
            # Pseudo PCB palette for richer context (deterministic by net name).
            copper_palette = [
                ((123, 109, 40, 155), (111, 95, 28, 185)),
                ((115, 128, 88, 145), (86, 98, 62, 175)),
                ((106, 128, 126, 145), (74, 95, 94, 175)),
                ((138, 120, 88, 145), (110, 92, 62, 175)),
                ((120, 108, 133, 145), (95, 82, 108, 175)),
            ]
            for x1, y1, x2, y2, net_key in boxes:
                rect = _map_rect(x1, y1, x2, y2)
                rw = max(1.0, x2 - x1)
                rh = max(1.0, y2 - y1)
                elong = max(rw, rh) / max(min(rw, rh), 1e-9)
                area_ratio = ((x2 - x1) * (y2 - y1)) / board_area
                pi = (sum(ord(c) for c in net_key) if net_key else 0) % len(copper_palette)
                fill_c, edge_c = copper_palette[pi]
                if area_ratio > 0.50:
                    draw.rectangle(rect, outline=(120, 128, 122, 120), width=max(1, ss))
                else:
                    if area_ratio < 0.0015 and elong > 6.0:
                        # Thin routes should stay visible and crisp.
                        draw.rectangle(rect, fill=(167, 202, 214, 180), outline=(116, 144, 156, 195), width=max(1, ss))
                    else:
                        draw.rectangle(rect, fill=fill_c, outline=edge_c, width=max(1, ss))

            draw.rectangle(
                (12 * ss, 12 * ss, canvas_w - 12 * ss, canvas_h - 12 * ss),
                outline=(152, 159, 154, 180),
                width=max(2, ss),
            )

            board_rect = _map_rect(min_x, min_y, max_x, max_y)
            base_final = base.resize((width, height), resample=Image.Resampling.LANCZOS).convert("RGB")
            # Add dimension overlay to match DCIR output readability.
            br = (
                int(board_rect[0] / ss),
                int(board_rect[1] / ss),
                int(board_rect[2] / ss),
                int(board_rect[3] / ss),
            )
            base_final = self._draw_board_dimensions_overlay(base_final, br, dx, dy)

            base_final.save(str(top_jpg), quality=98, subsampling=0, optimize=True)
            base_final.save(str(top_img))
            # Bottom image is mirrored for quick visual discrimination.
            btm = base_final.transpose(Image.FLIP_LEFT_RIGHT)
            btm.save(str(btm_jpg), quality=98, subsampling=0, optimize=True)
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


















































