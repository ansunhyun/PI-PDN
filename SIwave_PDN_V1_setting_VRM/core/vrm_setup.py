# coding=utf-8

import json
import re

from core.logger import LogLevel


def _sanitize_name_token(value):
    import re
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")


def _get_component_pins_by_net(comp_inst, net_name):
    return [pin for pin in comp_inst.pins.values() if pin.net_name == net_name]


def _get_component_pin_names_by_net(comp_inst, net_name):
    return [pin_name for pin_name, pin in comp_inst.pins.items() if pin.net_name == net_name]


def _normalize_net_token(net_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(net_name or "").upper())


def _extract_voltage(net_name: str):
    s = str(net_name or "").upper()
    m = re.search(r"(\d+)\s*[\._]?\s*(\d+)\s*V", s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"V\s*(\d+)\s*[\._]?\s*(\d+)", s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _is_signal_like_net(net_name: str) -> bool:
    n = _normalize_net_token(net_name)
    if not n:
        return False
    signal_blacklist = (
        "ADDR", "DATA", "CLK", "TX", "RX", "MISO", "MOSI",
        "SCL", "SDA", "GPIO", "SIGN", "EB",
    )
    return any(k in n for k in signal_blacklist)


def _is_generic_power_alias(net_name: str) -> bool:
    n = _normalize_net_token(net_name)
    if not n:
        return False
    return any(k in n for k in ("VCC", "VDD", "POWER", "PWR", "EMMC", "VTERM"))


def _net_matches_spec(spec_net: str, cand_net: str) -> bool:
    s = str(spec_net or "").strip()
    c = str(cand_net or "").strip()
    if not s or not c:
        return False
    if s == c:
        return True
    sn = _normalize_net_token(s)
    cn = _normalize_net_token(c)
    if sn and (sn in cn or cn in sn):
        return True
    sv = _extract_voltage(s)
    cv = _extract_voltage(c)
    if sv is not None and cv is not None and abs(float(sv) - float(cv)) < 1e-6:
        if _is_signal_like_net(c):
            return False
        spec_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", s.upper()))
        cand_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", c.upper()))
        if cand_has_drail and not spec_has_drail:
            return False
        if _is_generic_power_alias(c) or _is_generic_power_alias(s):
            return True
    return False


def _get_component_pins_by_net_relaxed(comp_inst, target_net, spec_net=""):
    target_net = str(target_net or "").strip()
    spec_net = str(spec_net or "").strip()
    pins = []
    for pin in comp_inst.pins.values():
        pnet = str(pin.net_name or "").strip()
        if not pnet:
            continue
        if pnet == target_net:
            pins.append(pin)
            continue
        if spec_net and _net_matches_spec(spec_net, pnet):
            pins.append(pin)
            continue
        if target_net and _net_matches_spec(target_net, pnet):
            pins.append(pin)
    return pins


def _get_component_pin_names_by_net_relaxed(comp_inst, target_net, spec_net=""):
    target_net = str(target_net or "").strip()
    spec_net = str(spec_net or "").strip()
    names = []
    for pin_name, pin in comp_inst.pins.items():
        pnet = str(pin.net_name or "").strip()
        if not pnet:
            continue
        if pnet == target_net:
            names.append(pin_name)
            continue
        if spec_net and _net_matches_spec(spec_net, pnet):
            names.append(pin_name)
            continue
        if target_net and _net_matches_spec(target_net, pnet):
            names.append(pin_name)
    return names


def _find_nearest_gnd_pin(edb, ref_coord, gnd_net):
    best_pin = None
    min_dist = float("inf")
    for comp in edb._components.components.values():
        for pin in comp.pins.values():
            if pin.net_name != gnd_net:
                continue
            dist = (pin.position[0] - ref_coord[0]) ** 2 + (pin.position[1] - ref_coord[1]) ** 2
            if dist < min_dist:
                min_dist = dist
                best_pin = pin
    return best_pin


def _find_nearest_gnd_pins(edb, ref_coord, gnd_net, limit=8):
    rows = []
    for comp in edb._components.components.values():
        for pin in comp.pins.values():
            if str(pin.net_name or "") != str(gnd_net or ""):
                continue
            try:
                dist = (float(pin.position[0]) - float(ref_coord[0])) ** 2 + (float(pin.position[1]) - float(ref_coord[1])) ** 2
            except Exception:
                continue
            rows.append((dist, pin))
    rows.sort(key=lambda x: x[0])
    out = []
    seen = set()
    for _, pin in rows:
        if id(pin) in seen:
            continue
        seen.add(id(pin))
        out.append(pin)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _find_nearest_shunt_cap_pin(edb, ref_coord, target_net, gnd_net):
    best_pin = None
    min_dist = float("inf")
    for comp_name, comp_inst in edb._components.components.items():
        if not str(comp_name).upper().startswith("C"):
            continue
        target_pins = [p for p in comp_inst.pins.values() if p.net_name == target_net]
        gnd_pins = [p for p in comp_inst.pins.values() if p.net_name == gnd_net]
        if not target_pins or not gnd_pins:
            continue
        for pin in target_pins:
            dist = (pin.position[0] - ref_coord[0]) ** 2 + (pin.position[1] - ref_coord[1]) ** 2
            if dist < min_dist:
                min_dist = dist
                best_pin = pin
    return best_pin


def _resolve_pin_refdes(edb, pin_obj):
    if pin_obj is None:
        return ""
    try:
        pin_name = str(getattr(pin_obj, "name", "") or "")
    except Exception:
        pin_name = ""
    try:
        pin_net = str(getattr(pin_obj, "net_name", "") or "")
    except Exception:
        pin_net = ""
    try:
        pin_x = float(pin_obj.position[0])
        pin_y = float(pin_obj.position[1])
        pin_has_pos = True
    except Exception:
        pin_x, pin_y = 0.0, 0.0
        pin_has_pos = False

    for cname, comp in edb._components.components.items():
        try:
            for p in comp.pins.values():
                # Fast-path identity comparison.
                if p is pin_obj:
                    return str(cname)

                # Fallback for API wrappers/proxies where object identity differs.
                same_name = (pin_name and str(getattr(p, "name", "") or "") == pin_name)
                same_net = (pin_net and str(getattr(p, "net_name", "") or "") == pin_net)
                if same_name and same_net:
                    if pin_has_pos:
                        try:
                            px = float(p.position[0])
                            py = float(p.position[1])
                            if abs(px - pin_x) <= 1e-9 and abs(py - pin_y) <= 1e-9:
                                return str(cname)
                        except Exception:
                            pass
                    else:
                        return str(cname)
        except Exception:
            continue
    return ""

def _find_nearest_non_cap_pin_on_net(edb, ref_coord, target_net, gnd_net, skip_prefixes=None):
    best_pin = None
    best_refdes = ""
    min_dist = float("inf")
    target_net = str(target_net or "")
    skip_prefixes = tuple(str(x or "").upper() for x in (skip_prefixes or ()))
    for comp_name, comp_inst in edb._components.components.items():
        cname = str(comp_name or "")
        cname_u = cname.upper()
        if cname_u.startswith("C"):
            continue
        if skip_prefixes and any(cname_u.startswith(pref) for pref in skip_prefixes if pref):
            continue
        try:
            target_pins = [p for p in comp_inst.pins.values() if str(p.net_name or "") == target_net]
            gnd_pins = [p for p in comp_inst.pins.values() if str(p.net_name or "") == str(gnd_net or "")]
        except Exception:
            continue
        if not target_pins or not gnd_pins:
            continue
        for pin in target_pins:
            try:
                dist = (float(pin.position[0]) - float(ref_coord[0])) ** 2 + (float(pin.position[1]) - float(ref_coord[1])) ** 2
            except Exception:
                continue
            if dist < min_dist:
                min_dist = dist
                best_pin = pin
                best_refdes = cname
    return best_pin, best_refdes


def _component_anchor_point(comp_inst):
    pts = []
    for p in comp_inst.pins.values():
        try:
            pts.append((float(p.position[0]), float(p.position[1])))
        except Exception:
            continue
    if not pts:
        return None
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    return (sx / len(pts), sy / len(pts))


def _pin_distance_sq(pin_a, pin_b):
    try:
        ax, ay = float(pin_a.position[0]), float(pin_a.position[1])
        bx, by = float(pin_b.position[0]), float(pin_b.position[1])
    except Exception:
        return float("inf")
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def _score_vrm_pair(edb, pos_pin, neg_pin):
    # Lower is better. Penalize ambiguous terminal-face candidates.
    d2 = _pin_distance_sq(pos_pin, neg_pin)
    pos_ref = str(_resolve_pin_refdes(edb, pos_pin) or "").upper()
    neg_ref = str(_resolve_pin_refdes(edb, neg_pin) or "").upper()

    penalty = 0.0
    if not (d2 < float("inf")):
        penalty += 1e6
    # Too short edge often causes ambiguous terminal face.
    if d2 < 4e-8:      # ~0.2 mm
        penalty += 600.0
    elif d2 < 2.5e-7:  # ~0.5 mm
        penalty += 180.0
    elif d2 < 1e-6:    # ~1.0 mm
        penalty += 40.0

    if pos_ref and (pos_ref == neg_ref):
        penalty += 300.0
    if pos_ref.startswith("IC"):
        penalty += 140.0
    elif pos_ref.startswith("C"):
        penalty += 90.0
    if neg_ref.startswith("IC"):
        penalty += 80.0
    elif neg_ref.startswith("C"):
        penalty += 35.0

    # Keep reasonable locality after ambiguity penalties.
    locality = d2 * 1e6
    return penalty + locality


def _is_vrm_pair_geometry_safe(edb, pos_pin, neg_pin):
    d2 = _pin_distance_sq(pos_pin, neg_pin)
    if not (d2 < float("inf")):
        return False

    # Avoid very short terminal edges that often trigger ambiguous horizontal-face errors.
    if d2 < 1.6e-6:  # ~1.26 mm
        return False

    pos_ref = str(_resolve_pin_refdes(edb, pos_pin) or "").upper()
    neg_ref = str(_resolve_pin_refdes(edb, neg_pin) or "").upper()

    # Same-component pair on dense packages is risky for termination-port faces.
    if pos_ref and neg_ref and pos_ref == neg_ref:
        return False

    # Avoid IC<->IC termination pair when alternatives exist.
    if pos_ref.startswith("IC") and neg_ref.startswith("IC"):
        return False

    return True


def _build_vrm_pair_candidates(edb, pos_seed_pin, gnd_net, limit_pos=4, limit_gnd=10):
    pos_candidates = []
    seen_pos = set()

    def _push_pos(pin):
        if pin is None:
            return
        k = id(pin)
        if k in seen_pos:
            return
        seen_pos.add(k)
        pos_candidates.append(pin)

    # 1) Keep current behavior first.
    _push_pos(pos_seed_pin)

    # 2) Prefer non-IC/non-cap anchors for robust termination face generation.
    alt_nc, _ = _find_nearest_non_cap_pin_on_net(
        edb,
        pos_seed_pin.position,
        pos_seed_pin.net_name,
        gnd_net,
        skip_prefixes=("IC",),
    )
    _push_pos(alt_nc)

    # 3) Relaxed non-cap candidate as fallback.
    alt_nocap, _ = _find_nearest_non_cap_pin_on_net(
        edb,
        pos_seed_pin.position,
        pos_seed_pin.net_name,
        gnd_net,
        skip_prefixes=(),
    )
    _push_pos(alt_nocap)

    safe = []
    unsafe = []
    for pos_pin in pos_candidates[: max(1, int(limit_pos))]:
        gnd_candidates = _find_nearest_gnd_pins(edb, pos_pin.position, gnd_net, limit=limit_gnd)
        for neg_pin in gnd_candidates:
            row = (pos_pin, neg_pin, _score_vrm_pair(edb, pos_pin, neg_pin))
            if _is_vrm_pair_geometry_safe(edb, pos_pin, neg_pin):
                safe.append(row)
            else:
                unsafe.append(row)

    safe.sort(key=lambda x: x[2])
    unsafe.sort(key=lambda x: x[2])
    # Prefer geometry-safe pairs first, then keep unsafe as last-resort fallback.
    return safe + unsafe


def _delete_component_if_exists(edb, comp_name):
    try:
        comp = edb._components.components.get(comp_name)
        if comp:
            comp.delete()
    except Exception:
        pass


def _find_nearest_pin_on_net_to_component(edb, target_net, ref_comp):
    """
    Select net-connected pin nearest to reference component anchor.
    Used when direct power-pin group on the target IC is unavailable.
    """
    anchor = _component_anchor_point(ref_comp)
    if anchor is None:
        return None, ""
    best_pin = None
    best_comp = ""
    best_dist = float("inf")
    for cname, comp in edb._components.components.items():
        for p in comp.pins.values():
            if p.net_name != target_net:
                continue
            try:
                d = (float(p.position[0]) - anchor[0]) ** 2 + (float(p.position[1]) - anchor[1]) ** 2
            except Exception:
                continue
            if d < best_dist:
                best_dist = d
                best_pin = p
                best_comp = cname
    return best_pin, best_comp


def _find_series_inductor_on_chain(edb, net_chain, bulk_inductor_set, allowed_prefixes=None):
    if not net_chain or len(net_chain) < 2:
        return None, None, None
    allowed_prefixes = tuple(allowed_prefixes or ())
    for idx in range(len(net_chain) - 1):
        net_a, net_b = net_chain[idx], net_chain[idx + 1]
        for comp_name, comp_inst in edb._components.components.items():
            if allowed_prefixes and not str(comp_name).upper().startswith(tuple(p.upper() for p in allowed_prefixes)):
                continue
            if bulk_inductor_set and comp_name not in bulk_inductor_set:
                continue
            comp_nets = {p.net_name: p for p in comp_inst.pins.values() if p.net_name}
            if net_a in comp_nets and net_b in comp_nets:
                return comp_inst, comp_nets[net_a], comp_nets[net_b]
    return None, None, None


def _resolve_case_source_pin(edb, case, full_chain):
    src_name = case.get("Source_name", "")
    src_pin_name = case.get("Source_pin", "")
    src_comp = edb._components.components.get(src_name) if src_name else None
    src_pin = src_comp.pins.get(src_pin_name) if (src_comp and src_pin_name) else None
    if not src_pin and src_comp:
        net_set = set(full_chain or [])
        for p in src_comp.pins.values():
            if p.net_name in net_set:
                src_pin = p
                break
    return src_comp, src_pin


def _trace_ic_local_pin_from_start_net(edb, ic_inst, start_net, max_hops=8):
    """
    Trace connectivity from start_net and return the first net that reaches ic_inst pins.
    Returns (pin_name, pin_obj, reached_net, hops) or (None, None, "", -1).
    """
    start_net = str(start_net or "").strip()
    if not start_net:
        return None, None, "", -1

    from collections import deque

    q = deque([(start_net, 0)])
    visited = {start_net}

    gnd_u = "GND"
    while q:
        net_name, hops = q.popleft()
        net_u = str(net_name or "").upper()
        if net_u == gnd_u or _is_signal_like_net(net_name):
            continue

        ic_hits = []
        for pn, p in ic_inst.pins.items():
            pnet = str(p.net_name or "")
            if not pnet:
                continue
            pu = pnet.upper()
            if pu == gnd_u or _is_signal_like_net(pnet):
                continue
            if pnet == net_name:
                ic_hits.append((pn, p))
        if ic_hits:
            # deterministic: lexical pin name order
            ic_hits.sort(key=lambda x: str(x[0]))
            pin_name, pin_obj = ic_hits[0]
            return pin_name, pin_obj, net_name, hops

        if hops >= max_hops:
            continue

        for comp_name, comp in edb._components.components.items():
            comp_pins = list(comp.pins.values())
            if not comp_pins:
                continue
            # reduce graph explosion on huge BGAs/connectors
            if len(comp_pins) > 16 and comp is not ic_inst:
                continue
            has_current = any(str(p.net_name or "") == net_name for p in comp_pins)
            if not has_current:
                continue
            for p in comp_pins:
                nxt = str(p.net_name or "").strip()
                if not nxt or nxt == net_name or nxt in visited:
                    continue
                if str(nxt).upper() == gnd_u:
                    continue
                visited.add(nxt)
                q.append((nxt, hops + 1))

    return None, None, "", -1


def _create_pin_group_with_compat(edb, pins, pin_names, group_name):
    candidates = []
    if hasattr(edb, "core_components") and hasattr(edb.core_components, "create_pingroup"):
        candidates.append(edb.core_components.create_pingroup)
    if hasattr(edb, "components") and hasattr(edb.components, "create_pingroup"):
        candidates.append(edb.components.create_pingroup)
    if hasattr(edb, "siwave") and hasattr(edb.siwave, "create_pin_group"):
        candidates.append(edb.siwave.create_pin_group)

    last_exc = None
    for creator in candidates:
        for payload in (pins, pin_names):
            for args in ((payload, group_name), (group_name, payload)):
                try:
                    return creator(*args)
                except Exception as exc:
                    last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("No Pin Group API available")


def _normalize_pin_group_result(value):
    if isinstance(value, tuple) and len(value) >= 2:
        return value[1]
    return value


def _create_pin_group_for_component(edb, refdes, pin_names, group_name):
    pin_names = [str(p) for p in (pin_names or [])]
    last_exc = None

    # Preferred path for pyedb>=0.5x
    try:
        if hasattr(edb, "siwave") and hasattr(edb.siwave, "create_pin_group"):
            return _normalize_pin_group_result(edb.siwave.create_pin_group(refdes, pin_names, group_name))
    except Exception as exc:
        last_exc = exc

    # grpc-style components API
    try:
        if hasattr(edb, "components") and hasattr(edb.components, "create_pin_group"):
            return _normalize_pin_group_result(edb.components.create_pin_group(refdes, pin_names, group_name))
    except Exception as exc:
        last_exc = exc

    # Legacy compatibility fallback
    try:
        comp = edb._components.components.get(refdes)
        if comp:
            pins = [comp.pins.get(name) for name in pin_names if comp.pins.get(name)]
            return _create_pin_group_with_compat(edb, pins, pin_names, group_name)
    except Exception as exc:
        last_exc = exc

    if last_exc:
        raise last_exc
    raise RuntimeError("No component pin-group API available")


def _create_port_with_compat(edb, pos_obj, gnd_obj, port_name, is_circuit_port=False):
    # Preferred: allow explicit circuit/non-circuit mode for solver stability tuning.
    try:
        if hasattr(pos_obj, "create_terminal") and hasattr(gnd_obj, "create_terminal") and hasattr(edb, "create_port"):
            pos_term = pos_obj.create_terminal(f"{port_name}_POS")
            neg_term = gnd_obj.create_terminal(f"{port_name}_NEG")
            return edb.create_port(pos_term, neg_term, is_circuit_port=bool(is_circuit_port), name=port_name)
    except Exception:
        pass

    # Fallback: pin-level port with explicit circuit flag
    try:
        if hasattr(pos_obj, "create_port"):
            return pos_obj.create_port(name=port_name, reference=gnd_obj, is_circuit_port=bool(is_circuit_port))
    except Exception:
        pass

    # Legacy compatibility fallbacks (some backends may ignore circuit/non-circuit flag).
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


def _apply_port_reference_impedance(port_obj, impedance_ohm):
    if port_obj is None:
        return False
    imp = float(impedance_ohm)
    # Best-effort compatibility across pyedb versions/wrappers.
    try:
        if hasattr(port_obj, "set_impedance"):
            port_obj.set_impedance(imp)
            return True
    except Exception:
        pass
    for attr in ("impedance", "reference_impedance", "z0"):
        try:
            if hasattr(port_obj, attr):
                setattr(port_obj, attr, imp)
                return True
        except Exception:
            pass
    return False


def _get_terminal_meta(port_obj):
    try:
        boundary = getattr(port_obj, "boundary_type", "")
    except Exception:
        boundary = ""
    try:
        is_circuit = bool(getattr(port_obj, "is_circuit_port", False))
    except Exception:
        is_circuit = False
    return boundary, is_circuit


def _clear_vrm_setup_artifacts(app, logger, prefixes=None):
    prefixes = tuple(prefixes or ("PORT_", "Rvrm_"))
    for comp_name in list(app.edb._components.components.keys()):
        if comp_name.startswith(prefixes):
            try:
                app.edb._components.components[comp_name].delete()
            except Exception as e:
                logger.log(f"[WARNING] Failed to clear previous setup artifact {comp_name}: {e}", level=LogLevel.WARNING)


def configure_ports_and_vrms_from_spec(app, cases, gnd_net, bulk_inductor_set, output_dir, logger, vrm_setup_conf=None, port_app=None):
    vrm_setup_conf = vrm_setup_conf or {}
    naming_conf = vrm_setup_conf.get("naming", {})
    port_conf = vrm_setup_conf.get("port", {})
    vrm_conf = vrm_setup_conf.get("vrm", {})
    shunt_conf = vrm_setup_conf.get("shuntCap", {})
    inductor_conf = vrm_setup_conf.get("inductor", {})

    clear_prefixes = vrm_setup_conf.get("clearPrefixes", ["PORT_", "Rvrm_"])
    port_prefix = naming_conf.get("portPrefix", "PORT_")
    vrm_prefix = naming_conf.get("vrmPrefix", "Rvrm_")
    # PDN Z default target: Port reference impedance = 0.1 ohm
    port_r = float(port_conf.get("resistance_ohm", 0.1))
    port_l = float(port_conf.get("inductance_h", 0.0))
    port_c = float(port_conf.get("capacitance_f", 0.0))
    # PDN target VRM parameters: R=0.005 ohm, L=1e-9 H
    vrm_r = float(vrm_conf.get("resistance_ohm", 0.005))
    vrm_l = float(vrm_conf.get("inductance_h", 1e-9))
    vrm_c = float(vrm_conf.get("capacitance_f", 0.0))
    vrm_ideal_resistor_only = bool(vrm_conf.get("ideal_resistor_only", True))
    if vrm_ideal_resistor_only:
        vrm_l = 0.0
        vrm_c = 0.0
    shunt_enabled = bool(shunt_conf.get("searchEnabled", True))
    allowed_prefixes = inductor_conf.get("allowedPrefixes", ["L", "B", "FB"])

    runtime_conf = vrm_setup_conf.get("__runtime__", {}) if isinstance(vrm_setup_conf, dict) else {}
    solver_backend = str(runtime_conf.get("solver_backend", "")).strip().lower()

    # Termination mode policy:
    # - default: ideal resistor component (more stable than port-face termination)
    # - optional: force termination-port mode by config when explicitly needed
    term_mode = str(vrm_setup_conf.get("termination_mode", "component_ideal") or "component_ideal").strip().lower()
    vrm_termination_as_port = term_mode in ("port", "termination_port", "circuit_port")

    if vrm_termination_as_port:
        vrm_ideal_resistor_only = True
        vrm_l = 0.0
        vrm_c = 0.0
        create_vrm_component = False
    else:
        create_vrm_component_cfg = vrm_conf.get("createComponent", None)
        if create_vrm_component_cfg is None:
            create_vrm_component = True
        else:
            create_vrm_component = bool(create_vrm_component_cfg)
        # component mode still forces ideal-only to avoid interpolation fragility
        vrm_ideal_resistor_only = True
        vrm_l = 0.0
        vrm_c = 0.0

    logger.log(
        f"[VRM_SETUP] Runtime mode: solver_backend={solver_backend or 'unknown'}, "
        f"termination_mode={term_mode}, "
        f"create_vrm_component={create_vrm_component}, "
        f"termination_as_port={vrm_termination_as_port}, "
        f"ideal_resistor_only={vrm_ideal_resistor_only}, "
        f"R/L/C={vrm_r}/{vrm_l}/{vrm_c}",
        level=LogLevel.INFO,
    )
    records = []
    _clear_vrm_setup_artifacts(app, logger, clear_prefixes)
    logger.log(f"[VRM_SETUP] Cleared previous setup artifacts by prefixes: {clear_prefixes}", level=LogLevel.DETAIL1)

    for case in cases:
        ic_name = case.get("IC", "")
        target_net = case.get("Net", "")
        target_net_display = case.get("Display_Net", case.get("Spec_Net", target_net))
        spec_target_net = str(case.get("Spec_Net", "") or "").strip()
        mapped_ic_pin_name = str(case.get("IC_pin", "")).strip()
        full_chain = case.get("Full_Net_Chain", []) or [target_net]
        item = {
            "IC": ic_name,
            "Spec_Pin": case.get("Spec_Pin", ""),
            "Mapped_IC_Pin": mapped_ic_pin_name,
            "Target_Net": target_net_display,
            "PCB_Target_Net": target_net,
            "Spec_Target_Net": case.get("Spec_Net", ""),
            "Full_Net_Chain": full_chain,
            "Port_Name": "",
            "VRM_Name": "",
            "Status": "Pending",
            "Message": "",
        }

        try:
            ic_inst = app.edb._components.components.get(ic_name)
            if not ic_inst:
                item["Status"] = "Skipped"
                item["Message"] = f"IC not found: {ic_name}"
                logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                records.append(item)
                continue

            use_direct_pin_port = False
            preferred_vrm_anchor_pin = None
            # Pin-mapping-first policy:
            # Prefer explicitly resolved IC pin from crosswalk/mapping stage.
            mapped_ic_pin = ic_inst.pins.get(mapped_ic_pin_name) if mapped_ic_pin_name else None
            if mapped_ic_pin:
                target_net = mapped_ic_pin.net_name or target_net
                pos_group = [mapped_ic_pin]
                pos_group_names = [mapped_ic_pin_name]
                preferred_vrm_anchor_pin = mapped_ic_pin
            else:
                # Trace from target/spec net until it meets IC-local pin.
                t_pin_name, t_pin, reached_net, hops = _trace_ic_local_pin_from_start_net(
                    app.edb, ic_inst, target_net, max_hops=8
                )
                if t_pin is None and spec_target_net and spec_target_net != target_net:
                    t_pin_name, t_pin, reached_net, hops = _trace_ic_local_pin_from_start_net(
                        app.edb, ic_inst, spec_target_net, max_hops=8
                    )
                if t_pin is not None:
                    pos_group = [t_pin]
                    pos_group_names = [t_pin_name]
                    preferred_vrm_anchor_pin = t_pin
                    logger.log(
                        f"[VRM_SETUP][TRACE] Resolved IC-local pin by net tracing: {ic_name}:{t_pin_name} "
                        f"(start={target_net or spec_target_net}, reached={reached_net}, hops={hops})",
                        level=LogLevel.WARNING,
                    )
                else:
                    pos_group = []
                    pos_group_names = []

            gnd_group = _get_component_pins_by_net(ic_inst, gnd_net)
            gnd_group_names = _get_component_pin_names_by_net(ic_inst, gnd_net)
            if not pos_group or not gnd_group:
                item["Status"] = "Skipped"
                item["Message"] = (
                    f"Pin group mismatch on {ic_name}. "
                    f"No IC-local power pin for target/spec net (target={target_net}, spec={spec_target_net})."
                )
                logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                records.append(item)
                continue

            clean_net = _sanitize_name_token(target_net_display or target_net)
            port_name = f"{_sanitize_name_token(ic_name)}_{clean_net}"
            port_comp_name = f"{port_prefix}{port_name}"
            item["Port_Name"] = port_name

            pos_group_name = f"PG_{ic_name}_{clean_net}_POS"
            gnd_group_name = f"PG_{ic_name}_{clean_net}_GND"
            try:
                created_port = None
                try:
                    if use_direct_pin_port:
                        created_port = _create_port_with_compat(app.edb, pos_group[0], gnd_group[0], port_comp_name)
                    else:
                        pos_pin_group = _create_pin_group_for_component(app.edb, ic_name, pos_group_names, pos_group_name)
                        gnd_pin_group = _create_pin_group_for_component(app.edb, ic_name, gnd_group_names, gnd_group_name)
                        created_port = _create_port_with_compat(app.edb, pos_pin_group, gnd_pin_group, port_comp_name)
                except Exception as pg_exc:
                    logger.log(
                        f"[VRM_SETUP][WARNING] PinGroup path failed ({pg_exc}). Fallback to single-pin port.",
                        level=LogLevel.WARNING,
                    )
                    try:
                        created_port = _create_port_with_compat(app.edb, pos_group[0], gnd_group[0], port_comp_name)
                    except Exception as pin_exc:
                        logger.log(
                            f"[VRM_SETUP][WARNING] Direct pin-port path failed ({pin_exc}). "
                            f"Fallback to SIwave circuit-port element.",
                            level=LogLevel.WARNING,
                        )
                        ic_layer = getattr(ic_inst, "placement_layer", None)
                        if not ic_layer:
                            raise RuntimeError("Cannot resolve IC placement layer for circuit-port fallback")
                        port_backend = port_app if port_app is not None else app
                        fallback_success = port_backend.place_circuit_port(
                            port_name=port_comp_name,
                            pos_node=pos_group[0].position,
                            pos_layer=ic_layer,
                            neg_node=gnd_group[0].position,
                            neg_layer=ic_layer,
                            impedance=port_r,
                        )
                        if not fallback_success:
                            raise RuntimeError("SIwave circuit-port fallback failed")
                        # COM script path generally returns no handle on success.
                        created_port = None
                if created_port is not None:
                    if not _apply_port_reference_impedance(created_port, port_r):
                        logger.log(
                            f"[VRM_SETUP][WARNING] Could not set reference impedance to {port_r} ohm for {port_comp_name} (API unsupported).",
                            level=LogLevel.WARNING,
                        )
                    boundary, is_circuit = _get_terminal_meta(created_port)
                    logger.log(
                        f"[VRM_SETUP][PORT_META] {port_comp_name}: boundary={boundary or 'unknown'}, "
                        f"is_circuit_port={is_circuit}",
                        level=LogLevel.DETAIL1,
                    )
            except Exception as e:
                item["Status"] = "Skipped"
                item["Message"] = f"Failed to create Pin Group or Port: {e}"
                logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                records.append(item)
                continue

            ind_comp, ind_target_pin, _ = _find_series_inductor_on_chain(app.edb, full_chain, bulk_inductor_set, allowed_prefixes)
            vrm_pos_pin = None
            vrm_force_inductor_anchor = False
            if ind_comp and ind_target_pin:
                try:
                    ind_comp.enabled = False
                except Exception as e:
                    logger.log(f"[VRM_SETUP][WARN] Failed to deactivate inductor {ind_comp.name}: {e}", level=LogLevel.WARNING)
                vrm_pos_pin = ind_target_pin
                # Requested policy: in termination mode, prioritize the inductor-side pad for VRM+ anchor.
                if vrm_termination_as_port:
                    vrm_force_inductor_anchor = True
                    logger.log(
                        f"[VRM_SETUP][TERM_SAFE] Force inductor-anchor for {target_net}: {ind_comp.name}",
                        level=LogLevel.DETAIL1,
                    )
            else:
                # Fallback: when no series inductor exists, place VRM on source pin directly.
                if preferred_vrm_anchor_pin is not None:
                    vrm_pos_pin = preferred_vrm_anchor_pin
                    logger.log(
                        f"[VRM_SETUP][INFO] No inductor on chain. Using preferred fallback anchor pin on net {vrm_pos_pin.net_name}.",
                        level=LogLevel.DETAIL1,
                    )
                else:
                    item["Status"] = "Skipped"
                    item["Message"] = (
                        f"No inductor and no preferred fallback anchor pin for VRM placement: chain={full_chain}. "
                        f"Source-pin fallback disabled."
                    )
                    logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                    records.append(item)
                    continue

            if shunt_enabled and (not vrm_termination_as_port):
                shunt_pin = _find_nearest_shunt_cap_pin(app.edb, vrm_pos_pin.position, vrm_pos_pin.net_name, gnd_net)
                if shunt_pin:
                    vrm_pos_pin = shunt_pin
            elif shunt_enabled and vrm_termination_as_port:
                logger.log(
                    "[VRM_SETUP][TERM_SAFE] Skip shunt-cap anchor for termination-port mode.",
                    level=LogLevel.DETAIL1,
                )

            vrm_pair_candidates = []
            if vrm_termination_as_port:
                if vrm_force_inductor_anchor:
                    gnd_candidates = _find_nearest_gnd_pins(app.edb, vrm_pos_pin.position, gnd_net, limit=12)
                    safe_rows = []
                    unsafe_rows = []
                    for gpin in gnd_candidates:
                        row = (vrm_pos_pin, gpin, _score_vrm_pair(app.edb, vrm_pos_pin, gpin))
                        if _is_vrm_pair_geometry_safe(app.edb, vrm_pos_pin, gpin):
                            safe_rows.append(row)
                        else:
                            unsafe_rows.append(row)
                    safe_rows.sort(key=lambda x: x[2])
                    unsafe_rows.sort(key=lambda x: x[2])
                    vrm_pair_candidates = safe_rows + unsafe_rows
                else:
                    vrm_pair_candidates = _build_vrm_pair_candidates(
                        app.edb,
                        vrm_pos_pin,
                        gnd_net,
                        limit_pos=4,
                        limit_gnd=10,
                    )

                if vrm_pair_candidates:
                    best_pos, best_neg, best_score = vrm_pair_candidates[0]
                    vrm_pos_pin = best_pos
                    vrm_neg_pin = best_neg
                    safe_count = 0
                    for cp, cn, _cs in vrm_pair_candidates:
                        if _is_vrm_pair_geometry_safe(app.edb, cp, cn):
                            safe_count += 1
                    logger.log(
                        f"[VRM_SETUP][TERM_SAFE] Candidate pairs for {target_net}: total={len(vrm_pair_candidates)}, safe={safe_count}, best_score={best_score:.3f}",
                        level=LogLevel.DETAIL1,
                    )
                else:
                    vrm_neg_pin = _find_nearest_gnd_pin(app.edb, vrm_pos_pin.position, gnd_net)
            else:
                vrm_neg_pin = _find_nearest_gnd_pin(app.edb, vrm_pos_pin.position, gnd_net)

            if not vrm_neg_pin:
                item["Status"] = "Skipped"
                item["Message"] = f"No nearby GND pin for VRM placement: {target_net}"
                logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                records.append(item)
                continue

            vrm_name = f"{vrm_prefix}{clean_net}"
            item["VRM_Name"] = vrm_name
            if vrm_termination_as_port:
                pos_refdes = _resolve_pin_refdes(app.edb, vrm_pos_pin)
                neg_refdes = _resolve_pin_refdes(app.edb, vrm_neg_pin)
                logger.log(
                    f"[VRM_SETUP][TERM_SAFE] Pair selected for {vrm_name}: pos={pos_refdes}:{getattr(vrm_pos_pin, 'name', '?')}, "
                    f"neg={neg_refdes}:{getattr(vrm_neg_pin, 'name', '?')}",
                    level=LogLevel.DETAIL1,
                )
                term_port = None
                last_term_exc = None
                force_circuit = (term_mode == "circuit_port")
                pair_attempts = vrm_pair_candidates if vrm_pair_candidates else [(vrm_pos_pin, vrm_neg_pin, 0.0)]
                for attempt_idx, (cand_pos, cand_neg, cand_score) in enumerate(pair_attempts[:8], start=1):
                    try:
                        _delete_component_if_exists(app.edb, vrm_name)
                        term_port = _create_port_with_compat(app.edb, cand_pos, cand_neg, vrm_name, is_circuit_port=force_circuit)
                        vrm_pos_pin, vrm_neg_pin = cand_pos, cand_neg
                        pos_refdes = _resolve_pin_refdes(app.edb, vrm_pos_pin)
                        neg_refdes = _resolve_pin_refdes(app.edb, vrm_neg_pin)
                        logger.log(
                            f"[VRM_SETUP][TERM_SAFE] Created {vrm_name} on attempt#{attempt_idx}: "
                            f"score={cand_score:.3f}, pos={pos_refdes}, neg={neg_refdes}",
                            level=LogLevel.DETAIL1,
                        )
                        break
                    except Exception as term_exc:
                        last_term_exc = term_exc
                        logger.log(
                            f"[VRM_SETUP][TERM_SAFE][WARNING] Attempt#{attempt_idx} failed for {vrm_name}: {term_exc}",
                            level=LogLevel.WARNING,
                        )
                        term_port = None
                if term_port is None and last_term_exc is not None:
                    item["Status"] = "Skipped"
                    item["Message"] = f"Failed to create VRM termination port: {vrm_name} ({last_term_exc})"
                    logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                    records.append(item)
                    continue
                if term_port is not None and (not _apply_port_reference_impedance(term_port, vrm_r)):
                    logger.log(
                        f"[VRM_SETUP][WARNING] Could not set termination impedance to {vrm_r} ohm for {vrm_name}.",
                        level=LogLevel.WARNING,
                    )
                t_boundary, t_is_circuit = _get_terminal_meta(term_port)
                logger.log(
                    f"[VRM_SETUP][TERM_META] {vrm_name}: boundary={t_boundary or 'unknown'}, "
                    f"is_circuit_port={t_is_circuit}, Z={vrm_r}",
                    level=LogLevel.DETAIL1,
                )
            elif create_vrm_component:
                created_vrm = app.create_rlc_component(
                    pins=[vrm_pos_pin, vrm_neg_pin],
                    comp_name=vrm_name,
                    part_name="VRM_RLC",
                    r_value=vrm_r,
                    l_value=vrm_l,
                    c_value=vrm_c,
                )
                if not created_vrm:
                    item["Status"] = "Skipped"
                    item["Message"] = f"Failed to create VRM element: {vrm_name}"
                    logger.log(f"[VRM_SETUP][SKIP] {item['Message']}", level=LogLevel.WARNING)
                    records.append(item)
                    continue
            else:
                logger.log(
                    f"[VRM_SETUP][INFO] Skip VRM element creation for backend={solver_backend or 'unknown'}: {vrm_name}",
                    level=LogLevel.INFO,
                )
            try:
                target_idx = full_chain.index(vrm_pos_pin.net_name)
                item["Analysis_Target_Nets"] = full_chain[:target_idx + 1]
            except ValueError:
                item["Analysis_Target_Nets"] = [target_net]

            item["Status"] = "Done"
            item["Message"] = "Port/VRM setup completed."
            logger.log(
                f"[VRM_SETUP][OK] {ic_name}:{target_net} => Port={port_name}, VRM={vrm_name}, "
                f"R/L/C={vrm_r}/{vrm_l}/{vrm_c}",
                level=LogLevel.DETAIL1,
            )
            records.append(item)

        except Exception as e:
            item["Status"] = "Skipped"
            item["Message"] = f"Unhandled error: {e}"
            logger.log(f"[VRM_SETUP][SKIP] {ic_name}:{target_net} -> {e}", level=LogLevel.WARNING)
            records.append(item)

    report = {
        "Schema_Version": 1,
        "Summary": {
            "Total": len(records),
            "Done": sum(1 for r in records if r["Status"] == "Done"),
            "Skipped": sum(1 for r in records if r["Status"] != "Done"),
        },
        "Records": records,
    }
    report_file = output_dir / "vrm_port_setup_result.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4, ensure_ascii=False)
    logger.log(f"[VRM_SETUP] Exported setup report: {report_file}", level=LogLevel.DETAIL1)
    return records











