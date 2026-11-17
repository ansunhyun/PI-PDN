from pathlib import Path
import json
import time

from core.database import ErrorCode, PDNSessionException
from core.logger import LogLevel
from core.services.net_search_service import export_net_search_results, resolve_case_power_path
from core.vrm_setup import (
    _create_temporary_pad_host,
    _delete_independent_pad,
    _delete_temporary_pad_host,
    _place_independent_vrm_pad,
)


def _normalize_identity_token(value):
    import re
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _padstack_component_name(pad):
    try:
        component = getattr(pad, "component", None)
        return str(getattr(component, "name", "") or "").strip()
    except Exception:
        return ""


def _padstack_position(pad):
    for attr in ("position", "center"):
        try:
            value = getattr(pad, attr, None)
            if value is not None and len(value) >= 2:
                return float(value[0]), float(value[1])
        except Exception:
            pass
    return None


def resolve_inner_cap_global_padstack(
    *,
    edbapp,
    comp_name,
    spec_pin,
    cmp_pin_record,
    coordinate_tolerance_m=1e-6,
    diagnostics=None,
):
    """Find an InnerCap target directly in board-global padstack instances."""
    instances = getattr(getattr(edbapp, "padstacks", None), "instances", {}) or {}
    target_comp = _normalize_identity_token(comp_name)
    target_pin = _normalize_identity_token(spec_pin)
    pin_id = _normalize_identity_token((cmp_pin_record or {}).get("pin_id", ""))

    target_position = None
    try:
        # CMP text coordinates are millimetres; PyEDB positions are metres.
        target_position = (
            float(cmp_pin_record["x"]) * 1e-3,
            float(cmp_pin_record["y"]) * 1e-3,
        )
    except (KeyError, TypeError, ValueError):
        pass

    matches = []
    nearest = []
    total_instances = 0
    positioned_instances = 0
    component_matches = 0
    pin_matches = 0
    pin_id_matches = 0
    iterable = instances.items() if hasattr(instances, "items") else enumerate(instances)
    for instance_key, pad in iterable:
        total_instances += 1
        component_name = _normalize_identity_token(_padstack_component_name(pad))
        component_match = bool(component_name and component_name == target_comp)
        if component_match:
            component_matches += 1

        tokens = set()
        for value in (
            instance_key,
            getattr(pad, "name", ""),
            getattr(pad, "aedt_name", ""),
            getattr(pad, "component_pin", ""),
            getattr(pad, "pin_name", ""),
        ):
            token = _normalize_identity_token(value)
            if token:
                tokens.add(token)

        pin_match = bool(target_pin and target_pin in tokens)
        id_match = bool(pin_id and pin_id in tokens)
        if pin_match:
            pin_matches += 1
        if id_match:
            pin_id_matches += 1
        position = _padstack_position(pad)
        distance2 = None
        coordinate_match = False
        if target_position and position:
            positioned_instances += 1
            distance2 = (
                (position[0] - target_position[0]) ** 2
                + (position[1] - target_position[1]) ** 2
            )
            coordinate_match = distance2 <= float(coordinate_tolerance_m) ** 2
            nearest.append(
                (
                    distance2,
                    str(instance_key),
                    str(getattr(pad, "name", "") or ""),
                    _padstack_component_name(pad),
                    position,
                )
            )

        if component_name and not component_match:
            continue

        if component_match and pin_match:
            priority = 0
            mode = "global_padstack_pin"
        elif component_match and id_match:
            priority = 1
            mode = "global_padstack_pin_id"
        elif component_match and coordinate_match:
            priority = 2
            mode = "global_padstack_coord"
        elif not component_name and pin_match and coordinate_match:
            priority = 3
            mode = "global_padstack_pin_coord"
        elif not component_name and coordinate_match:
            priority = 4
            mode = "global_padstack_coord_unowned"
        else:
            continue

        matches.append((priority, distance2 if distance2 is not None else float("inf"), str(instance_key), pad, mode))

    if diagnostics is not None:
        nearest.sort(key=lambda item: (item[0], item[1]))
        diagnostics.update(
            {
                "total": total_instances,
                "positioned": positioned_instances,
                "component_matches": component_matches,
                "pin_matches": pin_matches,
                "pin_id_matches": pin_id_matches,
                "target_position_m": target_position,
                "pin_id": pin_id,
                "nearest": [
                    {
                        "key": item[1],
                        "name": item[2],
                        "component": item[3],
                        "position_m": item[4],
                        "distance_um": round((item[0] ** 0.5) * 1e6, 3),
                    }
                    for item in nearest[:5]
                ],
            }
        )

    if not matches:
        return None, "", ""
    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    best = matches[0]
    if len(matches) > 1 and matches[1][0:2] == best[0:2]:
        raise ValueError(
            f"InnerCap global padstack ambiguous: {comp_name}/{spec_pin} "
            f"(candidates={best[2]},{matches[1][2]})"
        )
    return best[3], best[2], best[4]


def create_inner_cap_rlc_boundary(
    *,
    edbapp,
    positive_pin,
    negative_pin,
    boundary_name,
    capacitance,
    resistance=None,
    inductance=None,
):
    """Create one uniquely named RLC branch without re-parenting either padstack."""
    positive_terminal = None
    negative_terminal = None
    try:
        positive_terminal = positive_pin.create_terminal(f"{boundary_name}_P")
        negative_terminal = negative_pin.create_terminal(f"{boundary_name}_N")
        positive_terminal.boundary_type = "RlcBoundary"
        negative_terminal.boundary_type = "RlcBoundary"
        positive_terminal.is_circuit_port = False
        negative_terminal.is_circuit_port = False
        positive_terminal.reference_terminal = negative_terminal

        rlc = edbapp.edb_api.utility.utility.Rlc()
        rlc.IsParallel = False
        rlc.REnabled = resistance is not None
        rlc.LEnabled = inductance is not None
        rlc.CEnabled = True
        if resistance is not None:
            rlc.R = edbapp.edb_value(resistance)
        if inductance is not None:
            rlc.L = edbapp.edb_value(inductance)
        rlc.C = edbapp.edb_value(capacitance)
        result = positive_terminal._edb_object.SetRlcBoundaryParameters(rlc)
        if result is False:
            raise RuntimeError("SetRlcBoundaryParameters returned False")
        return positive_terminal, negative_terminal
    except Exception:
        # Only roll back terminals created by this call.
        for terminal in (positive_terminal, negative_terminal):
            if terminal is None:
                continue
            try:
                terminal._edb_object.Delete()
            except Exception:
                pass
        raise


def create_inner_cap_rlc_component(
    *,
    app,
    positive_pin,
    negative_pin,
    component_name,
    capacitance,
):
    """Create a serializable 2-pin RLC component on independent cloned pads.

    EDB RlcBoundary terminals survive an EDB reopen but are not serialized as
    SIwave circuit elements.  Independent pads avoid re-parenting the SoC/GND
    pins while producing a real component that Save As preserves.
    """
    positive_pad = None
    negative_pad = None
    temporary_host = None
    try:
        positive_net = str(getattr(positive_pin, "net_name", "") or "")
        negative_net = str(getattr(negative_pin, "net_name", "") or "")
        if not positive_net or not negative_net:
            raise RuntimeError(
                f"InnerCap source pin net is missing: positive={positive_net}, negative={negative_net}"
            )
        positive_position = _padstack_position(positive_pin)
        negative_position = _padstack_position(negative_pin)
        if positive_position is None or negative_position is None:
            raise RuntimeError(
                f"InnerCap source pin position is missing: {component_name}"
            )
        separation_m = (
            (positive_position[0] - negative_position[0]) ** 2
            + (positive_position[1] - negative_position[1]) ** 2
        ) ** 0.5
        if separation_m <= 1e-9:
            raise RuntimeError(
                f"InnerCap P/N pads overlap: {component_name}, separation={separation_m}m"
            )
        positive_pad = _place_independent_vrm_pad(
            app.edb, positive_pin, f"{component_name}_P", positive_net
        )
        negative_pad = _place_independent_vrm_pad(
            app.edb, negative_pin, f"{component_name}_N", negative_net
        )
        temporary_host = _create_temporary_pad_host(
            app.edb, [positive_pad, negative_pad], f"{component_name}__HOST"
        )
        created = app.create_rlc_component(
            pins=[positive_pad, negative_pad],
            comp_name=component_name,
            part_name=f"{component_name}__PART",
            # A zero-ohm R branch would shunt the capacitor and produced the
            # observed ~9 uOhm result.  This is a capacitor-only component.
            r_value=None,
            c_value=capacitance,
        )
        if not created:
            raise RuntimeError("create_rlc_component returned False")
    except Exception:
        _delete_independent_pad(positive_pad)
        _delete_independent_pad(negative_pad)
        raise
    finally:
        _delete_temporary_pad_host(temporary_host)

    component = app.edb._components.components.get(component_name)
    if component is None:
        _delete_independent_pad(positive_pad)
        _delete_independent_pad(negative_pad)
        raise RuntimeError(f"InnerCap component not found after creation: {component_name}")
    pins = list(component.pins.values())
    pin_nets = [str(getattr(pin, "net_name", "") or "") for pin in pins]
    if len(pins) != 2 or positive_net not in pin_nets or negative_net not in pin_nets:
        try:
            component.delete()
        except Exception:
            pass
        raise RuntimeError(
            f"InnerCap component topology invalid: {component_name}, pins={len(pins)}, nets={pin_nets}"
        )
    return component, positive_pad, negative_pad


def resolve_inner_cap_pin(
    *,
    comp_name,
    comp_inst,
    spec_pin,
    expected_net,
    net_alias_map,
    pin_override_map,
    pin_crosswalk_cache,
    cmp_pin_records,
    ndf_pin_records,
    build_component_pin_crosswalk_fn,
    find_component_pin_by_name_or_display_fn,
    collect_component_pin_identity_candidates_fn,
    net_matches_spec_fn,
    edbapp=None,
):
    """Resolve an InnerCap pin conservatively through override/direct/crosswalk mappings."""

    ndf_record = (ndf_pin_records.get(comp_name, {}) or {}).get(spec_pin) or {}
    authoritative_board_net = str(ndf_record.get("net", "") or "").strip()

    def matching_net_source(pin_inst, required_net, *, allow_ndf_alias=True):
        resolved_net = str(getattr(pin_inst, "net_name", "") or "").strip()
        if not required_net and not authoritative_board_net:
            return "unverified"
        if required_net and net_matches_spec_fn(required_net, resolved_net, net_alias_map):
            return "expected_net"
        if (
            allow_ndf_alias
            and authoritative_board_net
            and net_matches_spec_fn(authoritative_board_net, resolved_net, net_alias_map)
        ):
            return "ndf_board_net"
        return ""

    override_key = (str(comp_name).upper(), str(spec_pin).upper())
    override = pin_override_map.get(override_key)
    if override is not None:
        if isinstance(override, dict):
            candidate_name = str(override.get("edb_pin", "") or "").strip()
            override_net = str(override.get("expected_net", "") or "").strip()
        else:
            candidate_name = str(override or "").strip()
            override_net = ""
        candidate, resolved_name = find_component_pin_by_name_or_display_fn(
            comp_inst=comp_inst,
            pin_name=candidate_name,
        )
        required_net = override_net or expected_net
        if candidate is None:
            raise ValueError(
                f"InnerCap pin override target not found: {comp_name}/{spec_pin} -> {candidate_name}"
            )
        net_source = matching_net_source(
            candidate,
            required_net,
            allow_ndf_alias=not bool(override_net),
        )
        if not net_source:
            mode = "pin_override_net_warning"
        elif net_source == "ndf_board_net":
            mode = "pin_override_ndf_net"
        else:
            mode = "pin_override"
        return candidate, resolved_name or candidate_name, mode

    if comp_name not in pin_crosswalk_cache:
        pin_crosswalk_cache[comp_name] = build_component_pin_crosswalk_fn(
            comp_inst=comp_inst,
            cmp_component_record=(cmp_pin_records.get(comp_name, {}) or {}),
            ndf_component_record=(ndf_pin_records.get(comp_name, {}) or {}),
            net_alias_map=net_alias_map,
        )
    crosswalk_record = (pin_crosswalk_cache.get(comp_name, {}) or {}).get(spec_pin) or {}
    identity_candidates = collect_component_pin_identity_candidates_fn(
        comp_inst=comp_inst,
        pin_name=spec_pin,
        crosswalk_pin_record=crosswalk_record,
        excluded=None,
    )
    for identity in identity_candidates:
        candidate = identity["pin_inst"]
        resolved_name = identity["pin_key"]
        evidence = identity["evidence"]
        net_source = matching_net_source(candidate, expected_net)

        if evidence == "component_key":
            mode = "direct" if net_source else "direct_net_warning"
            return candidate, resolved_name, mode

        if evidence == "component_display":
            if not net_source:
                mode = "display_net_warning"
            elif net_source == "ndf_board_net":
                mode = "display_ndf_net"
            else:
                mode = "display"
            return candidate, resolved_name, mode

        if evidence == "crosswalk":
            meta = identity.get("metadata", {}) or {}
            method = str(meta.get("method", "") or "")
            confidence = float(meta.get("confidence", 0.0) or 0.0)
            d2 = meta.get("d2")
            coordinate_identity = (
                method in {"coord_net", "coord_only", "coord_net_validated"}
                and d2 is not None
                and float(d2) < 0.0025
                and confidence >= 0.70
            )
            if method == "pin_exact" or coordinate_identity:
                if coordinate_identity and not net_source:
                    mode = "crosswalk_coord_warning"
                elif net_source == "ndf_board_net":
                    mode = "crosswalk_ndf_net"
                else:
                    mode = "crosswalk"
                return candidate, resolved_name, mode

    cmp_pin_record = (cmp_pin_records.get(comp_name, {}) or {}).get(spec_pin) or {}
    global_diagnostics = {}
    global_pad, global_name, global_mode = resolve_inner_cap_global_padstack(
        edbapp=edbapp,
        comp_name=comp_name,
        spec_pin=spec_pin,
        cmp_pin_record=cmp_pin_record,
        diagnostics=global_diagnostics,
    )
    if global_pad is not None:
        resolved_net = str(getattr(global_pad, "net_name", "") or "").strip()
        net_source = matching_net_source(global_pad, expected_net)
        mode = global_mode if net_source else f"{global_mode}_net_warning"
        return global_pad, global_name or str(spec_pin), mode

    available = ",".join(str(name) for name in list(comp_inst.pins.keys())[:20])
    suffix = "..." if len(comp_inst.pins) > 20 else ""
    raise ValueError(
        f"InnerCap pin not found: {comp_name}/{spec_pin} "
        f"(expected_net={expected_net or '-'}, ndf_board_net={authoritative_board_net or '-'}, "
        f"available_pins={available}{suffix}, global_padstack={global_diagnostics})"
    )


def initialize_step4_context(
    *,
    aedt_version: str,
    logger,
    edb_file_path,
    conf_manager,
    input_valchk,
    settings_manager,
    cmp_file_path,
    ndf_file_path,
    output_dir: Path,
    input_dir: Path,
    base_name: str,
    parse_cmp_pin_records,
    parse_ndf_pin_records,
    discover_siw_for_ui_crosswalk,
    parse_siw_pin_records,
    load_pin_overrides,
    log_signal_layer_thicknesses,
    siwave_cls,
):
    app = siwave_cls(version=aedt_version, logger=logger)
    app.set_cad_file(edb_file_path)
    log_signal_layer_thicknesses(app, logger, tag="[STACKUP][Step4]")
    try:
        stackup_layer_count = len(app.edb.stackup.signal_layers.keys())
    except Exception:
        stackup_layer_count = None

    power_net_areas = {
        net_name: sum(p.area() for p in net_inst.primitives if p.type != "Path")
        for net_name, net_inst in app.edb._nets.power.items()
        if conf_manager.data["PDN"]["dcShort"]["shortKey"] not in net_name and "+" not in net_name
    }
    if power_net_areas:
        gnd_net = max(power_net_areas, key=power_net_areas.get)
    else:
        raise PDNSessionException(ErrorCode.GND_NET_DETECT_FAIL)

    bom_file = input_valchk._default_inputFiles["BOM"]
    settings_manager.parse_bom_and_partlist(bom_file)
    bom_info = settings_manager.get_bom()

    del_comp = {}
    missing_in_bom = []
    pdn_conf = conf_manager.data.get("PDN", {})
    dc_short_conf = pdn_conf.get("dcShort", {})
    bom_strict_conf = pdn_conf.get("bomStrict", {})

    strict_enabled = bool(bom_strict_conf.get("enabled", False))
    strict_disable_only = bool(bom_strict_conf.get("disableOnly", True))
    strict_delete_fallback = bool(bom_strict_conf.get("deleteFallbackOnUnsupported", True))
    strict_log_max_details = int(bom_strict_conf.get("logMaxDetails", 20) or 20)
    strict_keep_prefixes = tuple(str(x or "") for x in bom_strict_conf.get("keepDesignatorPrefixes", []) if str(x or ""))
    strict_keep_component_defs = {str(x or "") for x in bom_strict_conf.get("keepComponentDefs", []) if str(x or "")}
    strict_disable_skipped = []
    deferred_disable = []

    exclude_prefixes = tuple(dc_short_conf.get("excludePrefixes", ["AR", "JK", "P", "IC", "X", "D"]))
    delete_types = dc_short_conf.get("deleteCompTypes", ["IC", "IO", "Other"])
    shorted_comp_defs = set(dc_short_conf.get("shortedComp", []))

    for comp_name, comp_inst in app.edb._components.components.items():
        if str(comp_name).upper().startswith("C"):
            continue
        if comp_name in bom_info["Designators"]:
            continue

        if strict_enabled:
            keep_by_prefix = bool(strict_keep_prefixes) and str(comp_name).startswith(strict_keep_prefixes)
            keep_by_def = str(comp_inst.component_def) in strict_keep_component_defs
            if keep_by_prefix or keep_by_def:
                continue
            missing_in_bom.append(comp_name)
            if strict_disable_only or comp_inst.type not in delete_types:
                deferred_disable.append(comp_name)
            else:
                del_comp[comp_name] = comp_inst
            continue

        if (
            not comp_name.startswith(exclude_prefixes)
            and comp_inst.component_def in shorted_comp_defs
        ):
            continue

        if comp_inst.type in delete_types:
            del_comp[comp_name] = comp_inst
            missing_in_bom.append(comp_name)
        else:
            deferred_disable.append(comp_name)

    # Net Search must see the original electrical topology.  Apply this list
    # only in finalize_step4_outputs after every target path has been resolved.
    app._pdn_deferred_disable = sorted(set(deferred_disable))
    bom_info["__deferred_disable__"] = list(app._pdn_deferred_disable)
    logger.log(
        f"[BOM] Deferred component deactivate until Net Search completion: {len(app._pdn_deferred_disable)}",
        level=LogLevel.INFO,
    )

    short_correction, del_comp_set = {}, set()
    if not strict_enabled:
        for comp_name, comp_inst in app.edb._components.components.items():
            if comp_name.startswith(exclude_prefixes) or comp_inst.component_def not in shorted_comp_defs:
                continue
            target_nets = app.edb.nets.nets_by_components[comp_name]
            if len(target_nets) != 2:
                continue
            net1, net2 = target_nets
            short_key = dc_short_conf.get("shortKey", "")
            primary, secondary = (net2, net1) if short_key in net1 or (short_key not in net2 and len(net1) > len(net2)) else (net1, net2)
            short_correction.setdefault(primary, []).append(secondary)
            del_comp_set.add(comp_name)

    if strict_enabled:
        logger.log(
            f"[BOM][STRICT] enabled=True, disable_only={strict_disable_only}, delete_fallback={strict_delete_fallback}, "
            f"missing_nonbom={len(missing_in_bom)}, delete_queue={len(del_comp)}, disable_skip={len(strict_disable_skipped)}",
            level=LogLevel.INFO,
        )
        for comp_name, comp_type, reason in strict_disable_skipped[:max(0, strict_log_max_details)]:
            logger.log(
                f"[BOM][STRICT][SKIP] Disable unsupported -> queued delete: {comp_name} (type={comp_type}, reason={reason})",
                level=LogLevel.WARNING,
            )
        extra = len(strict_disable_skipped) - max(0, strict_log_max_details)
        if extra > 0:
            logger.log(f"[BOM][STRICT] ... {extra} more disable-skip entries omitted", level=LogLevel.WARNING)

    spec_file = input_valchk._default_inputFiles["Spec"]
    settings_manager.parse_spec(spec_file)
    spec_info = settings_manager.get_spec()
    logger.log(f"[SPEC] Parsed rows: {len(spec_info)}", level=LogLevel.DETAIL1)
    target_designators = {str(row.get("Designator", "")).strip() for row in spec_info if row.get("Designator")}
    # InnerCap designators are not necessarily present in the PDN Spec rows.
    # Parse CMP/NDF without the Spec-only filter so their pin/net aliases remain available.
    cmp_pin_records = parse_cmp_pin_records(cmp_file_path, target_designators=None)
    ndf_pin_records = parse_ndf_pin_records(ndf_file_path, target_designators=None)
    siw_crosswalk_source = discover_siw_for_ui_crosswalk(output_dir, input_dir, base_name)
    siw_pin_records = parse_siw_pin_records(siw_crosswalk_source, target_designators=target_designators)
    if siw_crosswalk_source:
        logger.log(f"[SPEC] SIW source selected for UI/API crosswalk: {siw_crosswalk_source}", level=LogLevel.DETAIL1)
    else:
        logger.log("[SPEC][WARNING] SIW source not found. SIW-assisted UI/API crosswalk is disabled.", level=LogLevel.WARNING)
    pin_crosswalk_cache = {}
    edb_truth_cache = {}
    ui_api_crosswalk_cache = {}
    pin_override_map, pin_override_path = load_pin_overrides(input_dir)
    if pin_override_path:
        logger.log(
            f"[SPEC] Pin override table loaded: {pin_override_path} (rows={len(pin_override_map)})",
            level=LogLevel.DETAIL1,
        )
    else:
        logger.log("[SPEC] Pin override table not found. Continue without manual overrides.", level=LogLevel.DETAIL2)

    return {
        "app": app,
        "STACKUP_LAYER_COUNT": stackup_layer_count,
        "GND_NET": gnd_net,
        "BOM_FILE": bom_file,
        "bom_info": bom_info,
        "delComp": del_comp,
        "missing_in_bom": missing_in_bom,
        "exclude_prefixes": exclude_prefixes,
        "delete_types": delete_types,
        "SHORT_CORRECTION": short_correction,
        "DEL_COMP": del_comp_set,
        "SPEC_FILE": spec_file,
        "spec_info": spec_info,
        "cmp_pin_records": cmp_pin_records,
        "ndf_pin_records": ndf_pin_records,
        "siw_crosswalk_source": siw_crosswalk_source,
        "siw_pin_records": siw_pin_records,
        "pin_crosswalk_cache": pin_crosswalk_cache,
        "edb_truth_cache": edb_truth_cache,
        "ui_api_crosswalk_cache": ui_api_crosswalk_cache,
        "pin_override_map": pin_override_map,
        "pin_override_path": pin_override_path,
    }


def prepare_step4_case_state(
    *,
    app,
    settings_manager,
    input_dir: Path,
    gnd_net: str,
    short_correction,
    spec_info,
    conf_manager,
    sanitize_name_token,
    find_nearest_gnd_pin,
    pin_override_map,
    pin_crosswalk_cache,
    cmp_pin_records,
    ndf_pin_records,
    build_component_pin_crosswalk_fn,
    find_component_pin_by_name_or_display_fn,
    collect_component_pin_identity_candidates_fn,
    net_matches_spec_fn,
    logger,
):
    def normalize_name(name):
        import re
        return re.sub(r"[^A-Za-z0-9]", "", str(name)).upper()

    def normalize_pin_name(pin_name):
        import re
        return re.sub(r"[^A-Za-z0-9]", "", str(pin_name or "")).upper()

    default_net_aliases = {
        "+1.8V": ["EMMC1V8", "VCC_1V8", "+1V8"],
        "PIF1V5": ["+VTERM", "VCC_1V5", "SIGN003116"],
    }

    def build_net_alias_map(sc):
        alias = {}
        for primary, secondaries in (sc or {}).items():
            all_nets = [str(primary)] + [str(s) for s in secondaries]
            for n in all_nets:
                alias.setdefault(n, set()).update(x for x in all_nets if x != n)
        for spec_net, edb_nets in default_net_aliases.items():
            alias.setdefault(spec_net, set()).update(edb_nets)
            for edb_net in edb_nets:
                alias.setdefault(edb_net, set()).add(spec_net)
        return alias

    net_alias_map = build_net_alias_map(short_correction)
    normalized_edb_component_names = {normalize_name(c_name): c_name for c_name in app.edb._components.components.keys()}

    def get_component_by_normalized(norm_name):
        comp_name = normalized_edb_component_names.get(norm_name)
        return app.edb._components.components.get(comp_name) if comp_name else None

    inner_cap_audit = []
    inner_cap_net_lookup = {}
    inner_cap_file = settings_manager.data.get("CAE", {}).get("SOC", {}).get("Inner_cap")
    if inner_cap_file:
        inner_cap_path = input_dir / inner_cap_file
        if not settings_manager.parse_inner_cap(inner_cap_path):
            raise ValueError(f"InnerCap parsing failed: {inner_cap_path}")
        if settings_manager.get_inner_cap():
            inner_caps = settings_manager.get_inner_cap()
            for icap_item in inner_caps:
                lk = (normalize_name(icap_item.get("Designator", "")), normalize_pin_name(icap_item.get("Pin_Number", "")))
                if lk not in inner_cap_net_lookup:
                    inner_cap_net_lookup[lk] = {
                        "PCB_Net": (icap_item.get("PCB_Net") or "").strip(),
                        "SoC_Net": (icap_item.get("SoC_Net") or "").strip(),
                    }

            cap_name_counter = {}
            for idx, icap in enumerate(inner_caps):
                ic_refdes = icap["Designator"]
                pin_no = icap["Pin_Number"]
                cap_val = icap["Cap_Value"]
                if not cap_val:
                    raise ValueError(
                        f"InnerCap row {icap.get('Row_Number')}: {ic_refdes}/{pin_no} matched input has no capacitance; "
                        "provide Cap. to create an electrical capacitor (no default value is assumed)"
                    )
                base_cap_name = f"C_{sanitize_name_token(ic_refdes)}_{sanitize_name_token(pin_no)}"
                seq = cap_name_counter.get(base_cap_name, 0) + 1
                cap_name_counter[base_cap_name] = seq
                cap_name = base_cap_name if seq == 1 else f"{base_cap_name}_Q{seq}"

                audit_item = {
                    "index": idx + 1,
                    "component_name": cap_name,
                    "designator": ic_refdes,
                    "pin_number": pin_no,
                    "cap_value": cap_val,
                    "vendor": str(icap.get("Vendor", "") or ""),
                    "part_number": str(icap.get("Part_Number", "") or "").strip(),
                    "maker_part_number": str(
                        icap.get("maker_part_number") or icap.get("Part_Number") or ""
                    ).strip(),
                    "electrical_model": "nominal_capacitance_from_innercap",
                    "resistance": None,
                    "inductance": None,
                    "status": "pending",
                    "message": "",
                }
                inner_cap_audit.append(audit_item)

                norm_ic_name = normalize_name(ic_refdes)
                ic_inst = get_component_by_normalized(norm_ic_name)
                if not ic_inst:
                    raise ValueError(f"InnerCap IC not found: {ic_refdes}")

                expected_net = str(icap.get("PCB_Net") or icap.get("SoC_Net") or "").strip()
                pin_inst, resolved_pin_name, resolve_mode = resolve_inner_cap_pin(
                    comp_name=normalized_edb_component_names[norm_ic_name],
                    comp_inst=ic_inst,
                    spec_pin=pin_no,
                    expected_net=expected_net,
                    net_alias_map=net_alias_map,
                    pin_override_map=pin_override_map,
                    pin_crosswalk_cache=pin_crosswalk_cache,
                    cmp_pin_records=cmp_pin_records,
                    ndf_pin_records=ndf_pin_records,
                    build_component_pin_crosswalk_fn=build_component_pin_crosswalk_fn,
                    find_component_pin_by_name_or_display_fn=find_component_pin_by_name_or_display_fn,
                    collect_component_pin_identity_candidates_fn=collect_component_pin_identity_candidates_fn,
                    net_matches_spec_fn=net_matches_spec_fn,
                    edbapp=app.edb,
                )
                audit_item["resolved_pin_number"] = resolved_pin_name
                audit_item["resolve_mode"] = resolve_mode
                audit_item["resolved_net"] = str(getattr(pin_inst, "net_name", "") or "")
                if resolve_mode != "direct":
                    logger.log(
                        f"[INNER_CAP][PIN_MAP] {ic_refdes}:{pin_no} -> {resolved_pin_name} "
                        f"(mode={resolve_mode}, net={audit_item['resolved_net']}, expected_net={expected_net or '-'})",
                        level=LogLevel.WARNING,
                    )

                pin_loc = pin_inst.position

                try:
                    gnd_pin_inst = find_nearest_gnd_pin(app.edb, pin_loc, gnd_net)
                    if not gnd_pin_inst:
                        raise ValueError(f"InnerCap GND not found: {ic_refdes}/{pin_no}")

                    component, positive_pad, negative_pad = create_inner_cap_rlc_component(
                        app=app,
                        positive_pin=pin_inst,
                        negative_pin=gnd_pin_inst,
                        component_name=cap_name,
                        capacitance=cap_val,
                    )
                    audit_item["status"] = "created"
                    audit_item["representation"] = "rlc_component"
                    audit_item["positive_pin"] = str(getattr(positive_pad, "name", f"{cap_name}_P"))
                    audit_item["negative_pin"] = str(getattr(negative_pad, "name", f"{cap_name}_N"))
                    audit_item["positive_net"] = str(getattr(pin_inst, "net_name", "") or "")
                    audit_item["negative_net"] = str(getattr(gnd_pin_inst, "net_name", "") or "")
                    logger.log(
                        f"[INNER_CAP][RLC] Created {cap_name}: pin={ic_refdes}/{pin_no}, "
                        f"C={cap_val} (source=InnerCap), R=disabled, L=disabled, "
                        f"part={audit_item['part_number'] or '-'}, representation=component, "
                        f"pins={audit_item['positive_pin']}/{audit_item['negative_pin']}",
                        level=LogLevel.INFO,
                    )
                except Exception as e:
                    audit_item["status"] = "error"
                    audit_item["message"] = str(e)
                    logger.log(f"[ERROR] Inner Cap {cap_name} 생성 중 오류 발생: {e}", level=LogLevel.ERROR)
                    raise

    pdn_cases_info = []
    designator_list = {case["Designator"] for case in spec_info}
    spec_skip_missing_comp = 0
    spec_skip_missing_pin = 0
    spec_fallback_resolved = 0
    strict_refdes_pin = bool(conf_manager.data.get("PDN", {}).get("pinMapping", {}).get("strictRefdesPin", True))

    from collections import defaultdict
    pin_resolution_stats = defaultdict(int)
    pin_mapping_quality_stats = defaultdict(int)
    pin_mapping_records = []
    used_component_pins = defaultdict(set)
    logger.log(
        f"[SPEC] Pin matching policy: strict_refdes_pin={strict_refdes_pin}",
        level=LogLevel.INFO,
    )

    return {
        "pdn_cases_info": pdn_cases_info,
        "inner_cap_audit": inner_cap_audit,
        "inner_cap_net_lookup": inner_cap_net_lookup,
        "designator_list": designator_list,
        "normalize_name": normalize_name,
        "normalize_pin_name": normalize_pin_name,
        "net_alias_map": net_alias_map,
        "normalized_edb_component_names": normalized_edb_component_names,
        "get_component_by_normalized": get_component_by_normalized,
        "spec_skip_missing_comp": spec_skip_missing_comp,
        "spec_skip_missing_pin": spec_skip_missing_pin,
        "spec_fallback_resolved": spec_fallback_resolved,
        "strict_refdes_pin": strict_refdes_pin,
        "pin_resolution_stats": pin_resolution_stats,
        "pin_mapping_quality_stats": pin_mapping_quality_stats,
        "pin_mapping_records": pin_mapping_records,
        "used_component_pins": used_component_pins,
    }


def process_step4_cases(
    *,
    app,
    spec_info,
    normalize_name,
    get_component_by_normalized,
    strict_refdes_pin: bool,
    net_alias_map,
    cmp_pin_records,
    ndf_pin_records,
    siw_pin_records,
    pin_crosswalk_cache,
    edb_truth_cache,
    ui_api_crosswalk_cache,
    pin_override_map,
    gnd_net: str,
    used_component_pins,
    spec_skip_missing_comp: int,
    spec_skip_missing_pin: int,
    spec_fallback_resolved: int,
    add_pin_mapping_record_fn,
    append_case_from_target_net_fn,
    trace_ic_local_power_pin_fn,
    build_component_pin_crosswalk_fn,
    build_component_edb_truth_table_fn,
    build_component_ui_api_crosswalk_fn,
    normalize_spec_pin_for_strict_mode_fn,
    find_component_pin_by_name_or_display_fn,
    resolve_spec_pin_designator_primary_fn,
    resolve_spec_pin_via_spec_api_gui_fn,
    resolve_spec_pin_to_edb_pin_fn,
    remap_pin_by_net_then_ui_fn,
    is_power_like_net_fn,
    is_signal_like_net_fn,
    net_matches_spec_fn,
    spec_net_candidates_fn,
    resolve_board_net_by_spec_fn,
    normalize_pin_token_fn,
    iter_all_padstack_instances_fn,
    pad_component_name_fn,
    find_component_pin_by_global_padstack_scan_fn,
    find_component_pin_by_spatial_query_fn,
    iter_dotnet_pins_from_component_fn,
    collect_dotnet_pin_tokens_fn,
    dotnet_pin_net_name_fn,
    dotnet_pin_position_fn,
    logger,
    conf_manager=None,
):
    for case in spec_info:
        comp_name = case["Designator"]
        norm_comp_name = normalize_name(comp_name)
        comp_inst = get_component_by_normalized(norm_comp_name)
        target_pin_name = str(case.get("Pin_number", "")).strip()
        spec_nets = spec_net_candidates_fn(case)
        if comp_inst is None:
            fallback_candidates = []
            for cand_name, cand_comp in app.edb._components.components.items():
                cand_pin = cand_comp.pins.get(target_pin_name)
                if not cand_pin:
                    continue
                if not spec_nets:
                    fallback_candidates.append((cand_name, cand_comp))
                    continue
                for spec_net in spec_nets:
                    if net_matches_spec_fn(spec_net, cand_pin.net_name, net_alias_map):
                        fallback_candidates.append((cand_name, cand_comp))
                        break
            if fallback_candidates:
                chosen_name, chosen_comp = fallback_candidates[0]
                comp_name = chosen_name
                comp_inst = chosen_comp
                spec_fallback_resolved += 1
                logger.log(
                    f"[SPEC][FALLBACK] Designator remapped: {case.get('Designator')} -> {comp_name}, pin={target_pin_name}, nets={spec_nets}",
                    level=LogLevel.WARNING,
                )
            else:
                spec_skip_missing_comp += 1
                logger.log(
                    f"[SPEC][SKIP] Component not found: designator={case.get('Designator')}, pin={target_pin_name}, nets={spec_nets}",
                    level=LogLevel.WARNING,
                )
                continue

        cmp_pin_info = (cmp_pin_records.get(comp_name, {}) or {}).get(target_pin_name)
        if comp_name not in pin_crosswalk_cache:
            pin_crosswalk_cache[comp_name] = build_component_pin_crosswalk_fn(
                comp_inst=comp_inst,
                cmp_component_record=(cmp_pin_records.get(comp_name, {}) or {}),
                ndf_component_record=(ndf_pin_records.get(comp_name, {}) or {}),
                net_alias_map=net_alias_map,
            )
        if comp_name not in edb_truth_cache:
            edb_truth_cache[comp_name] = build_component_edb_truth_table_fn(comp_inst)
        if comp_name not in ui_api_crosswalk_cache:
            ui_api_crosswalk_cache[comp_name] = build_component_ui_api_crosswalk_fn(
                comp_inst=comp_inst,
                components_api=getattr(app.edb, "components", None),
                siw_component_record=(siw_pin_records.get(comp_name, {}) or {}),
                ndf_component_record=(ndf_pin_records.get(comp_name, {}) or {}),
                net_alias_map=net_alias_map,
            )
        crosswalk_pin_info = (pin_crosswalk_cache.get(comp_name, {}) or {}).get(target_pin_name)
        truth_component_rows = edb_truth_cache.get(comp_name, [])
        ui_api_component_map = ui_api_crosswalk_cache.get(comp_name, {})
        siw_pin_info = (siw_pin_records.get(comp_name, {}) or {}).get(target_pin_name)
        lookup_pin_name = target_pin_name
        strict_norm_reason = None
        if strict_refdes_pin:
            lookup_pin_name, strict_norm_reason = normalize_spec_pin_for_strict_mode_fn(
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                crosswalk_pin_record=crosswalk_pin_info,
                net_alias_map=net_alias_map,
            )
            if strict_norm_reason:
                logger.log(
                    f"[SPEC][STRICT][NORMALIZE] {comp_name}:{target_pin_name} -> {lookup_pin_name} "
                    f"(method={strict_norm_reason.get('method')}, conf={strict_norm_reason.get('confidence')}, "
                    f"net_match={strict_norm_reason.get('net_match')})",
                    level=LogLevel.WARNING,
                )

        pin_inst = None
        resolve_mode = "none"
        resolved_pin_name = None
        pin_trace_meta = {}
        ov_key = (str(comp_name).upper(), str(target_pin_name).upper())
        if ov_key in pin_override_map:
            ov_raw = pin_override_map.get(ov_key, {})
            if isinstance(ov_raw, dict):
                ov_pin_name = str(ov_raw.get("edb_pin", "")).strip()
                ov_expected_net = str(ov_raw.get("expected_net", "")).strip()
            else:
                # Backward compatibility for legacy override format (string value only).
                ov_pin_name = str(ov_raw or "").strip()
                ov_expected_net = ""
            ov_inst, ov_resolved_name = find_component_pin_by_name_or_display_fn(
                comp_inst=comp_inst,
                pin_name=ov_pin_name,
                excluded=used_component_pins.get(comp_name, set()),
            )
            if ov_inst is None and ov_pin_name and ov_expected_net:
                # Conservative bridge: interpret override pin as UI/API label and map to EDB pin.
                ui_api_by_ui = (ui_api_component_map or {}).get("by_ui", {}) if isinstance(ui_api_component_map, dict) else {}
                ui_api_by_token = (ui_api_component_map or {}).get("by_token", {}) if isinstance(ui_api_component_map, dict) else {}
                cand_records = []
                cand_records.extend(ui_api_by_ui.get(ov_pin_name, []) or [])
                ov_tok = normalize_pin_token_fn(ov_pin_name)
                if ov_tok:
                    cand_records.extend(ui_api_by_token.get(ov_tok, []) or [])
                dedup = {}
                for rec in cand_records:
                    if not isinstance(rec, dict):
                        continue
                    edb_pin = str(rec.get("edb_pin", "")).strip()
                    if not edb_pin:
                        continue
                    if edb_pin in dedup:
                        continue
                    dedup[edb_pin] = rec
                ranked = []
                for edb_pin, rec in dedup.items():
                    if edb_pin not in comp_inst.pins:
                        continue
                    if edb_pin in used_component_pins.get(comp_name, set()):
                        continue
                    rec_net = str(getattr(comp_inst.pins[edb_pin], "net_name", "") or "").strip()
                    net_score = 0
                    if ov_expected_net:
                        net_score = 1 if net_matches_spec_fn(ov_expected_net, rec_net, net_alias_map) else -1
                    confidence = float(rec.get("confidence", 0.0) or 0.0)
                    ranked.append((net_score, confidence, edb_pin, rec_net, str(rec.get("source", ""))))
                if ranked:
                    ranked.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                    net_score, _, best_pin, best_net, best_source = ranked[0]
                    if net_score >= 0:
                        ov_inst = comp_inst.pins.get(best_pin)
                        ov_resolved_name = best_pin
                        logger.log(
                            f"[SPEC][OVERRIDE][MAP] {comp_name}:{target_pin_name} UI/API '{ov_pin_name}' -> EDB '{best_pin}' "
                            f"(source={best_source}, net={best_net})",
                            level=LogLevel.WARNING,
                        )
            if ov_inst is not None:
                if ov_expected_net and (not net_matches_spec_fn(ov_expected_net, getattr(ov_inst, "net_name", ""), net_alias_map)):
                    logger.log(
                        f"[SPEC][OVERRIDE][WARNING] Override net mismatch: {comp_name}:{target_pin_name} -> {ov_resolved_name} "
                        f"(resolved_net={getattr(ov_inst, 'net_name', '')}, expected_net={ov_expected_net})",
                        level=LogLevel.WARNING,
                    )
                    ov_inst = None
                    ov_resolved_name = None
                else:
                    pin_inst = ov_inst
                    resolved_pin_name = ov_resolved_name or ov_pin_name
                    resolve_mode = "pin_override"
                    logger.log(
                        f"[SPEC][OVERRIDE] {comp_name}: {target_pin_name} -> {resolved_pin_name}",
                        level=LogLevel.WARNING,
                    )
            if ov_inst is None:
                exp_msg = f", expected_net={ov_expected_net}" if ov_expected_net else ""
                logger.log(
                    f"[SPEC][OVERRIDE][WARNING] Override target not found: {comp_name}:{target_pin_name} -> {ov_pin_name}{exp_msg}",
                    level=LogLevel.WARNING,
                )

        if pin_inst is None:
            dsg_pin, dsg_mode, dsg_key, dsg_meta = resolve_spec_pin_designator_primary_fn(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                net_alias_map=net_alias_map,
                ui_api_component_map=ui_api_component_map,
                crosswalk_pin_record=crosswalk_pin_info,
                cmp_pin_record=cmp_pin_info,
                siw_pin_record=siw_pin_info,
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            if dsg_pin is not None:
                pin_inst = dsg_pin
                resolved_pin_name = dsg_key or target_pin_name
                resolve_mode = dsg_mode
                pin_trace_meta = {
                    "status": "direct",
                    "reason": resolve_mode,
                    "dsg_source": (dsg_meta or {}).get("source", ""),
                    "dsg_d2": (dsg_meta or {}).get("d2", ""),
                }
                logger.log(
                    f"[SPEC][PINMAP][D+P] {comp_name}: {target_pin_name} -> {resolved_pin_name} "
                    f"(mode={resolve_mode}, net={getattr(pin_inst, 'net_name', '')}, source={(dsg_meta or {}).get('source', '')})",
                    level=LogLevel.WARNING,
                )

        if pin_inst is None:
            tri_pin, tri_name, tri_trace = resolve_spec_pin_via_spec_api_gui_fn(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                crosswalk_pin_record=crosswalk_pin_info,
                ui_api_component_map=ui_api_component_map,
                truth_component_rows=truth_component_rows,
                net_alias_map=net_alias_map,
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            tri_status = str((tri_trace or {}).get("status", "")).strip().lower()
            tri_selected = (tri_trace or {}).get("selected", {}) if isinstance(tri_trace, dict) else {}
            tri_relation_ok = bool(tri_selected.get("ui_hit", False))
            if tri_pin is not None and ((not strict_refdes_pin) or tri_relation_ok):
                pin_inst = tri_pin
                resolved_pin_name = tri_name
                resolve_mode = "spec_api_gui"
                pin_trace_meta = {
                    "status": "direct",
                    "reason": resolve_mode,
                    "tri_status": tri_status,
                    "tri_reason": tri_selected.get("reason"),
                    "tri_ui_hit": tri_relation_ok,
                }
                logger.log(
                    f"[SPEC][PINMAP][3SRC] {comp_name}: {target_pin_name} -> {resolved_pin_name} "
                    f"(net={getattr(pin_inst, 'net_name', '')}, tri_status={tri_status}, "
                    f"reason={tri_selected.get('reason')}, ui_hit={tri_selected.get('ui_hit')}, "
                    f"net_match={tri_selected.get('net_match')}, conf={tri_selected.get('confidence')})",
                    level=LogLevel.WARNING,
                )
            elif tri_pin is not None and strict_refdes_pin and not tri_relation_ok:
                logger.log(
                    f"[SPEC][PINMAP][3SRC][STRICT-SKIP] {comp_name}:{target_pin_name} "
                    f"spec-api candidate exists but api-ui relation is missing. tri_status={tri_status}",
                    level=LogLevel.WARNING,
                )

        if pin_inst is None:
            pin_inst, resolve_mode, resolved_pin_name = resolve_spec_pin_to_edb_pin_fn(
                comp_inst=comp_inst,
                spec_pin=lookup_pin_name,
                spec_nets=spec_nets,
                net_alias_map=net_alias_map,
                cmp_pin_record=cmp_pin_info,
                cmp_component_record=(cmp_pin_records.get(comp_name, {}) or {}),
                crosswalk_pin_record=crosswalk_pin_info,
                components_api=getattr(app.edb, "components", None),
                excluded_pin_names=used_component_pins.get(comp_name, set()),
                edbapp=getattr(app, "edb", None),
                truth_component_rows=truth_component_rows,
                strict_refdes_pin=strict_refdes_pin,
            )

        try:
            fixed_inst, fixed_name, fixed_meta = remap_pin_by_net_then_ui_fn(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                current_pin_inst=pin_inst,
                current_pin_name=resolved_pin_name,
                crosswalk_pin_record=crosswalk_pin_info,
                truth_component_rows=truth_component_rows,
                cmp_pin_record=cmp_pin_info,
                net_alias_map=net_alias_map,
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            fixed_mode = str((fixed_meta or {}).get("mode", "")).strip()
            if fixed_inst is None:
                if pin_inst is not None:
                    logger.log(
                        f"[SPEC][NET-FIX][WARNING] Drop invalid mapping after net/ui verification: "
                        f"{comp_name}:{target_pin_name} -> {resolved_pin_name} (mode={resolve_mode}, fix_mode={fixed_mode})",
                        level=LogLevel.WARNING,
                    )
                pin_inst = None
                resolved_pin_name = None
                resolve_mode = "net_fix_unresolved"
            else:
                if (pin_inst is None) or (resolved_pin_name != fixed_name) or bool((fixed_meta or {}).get("changed", False)):
                    logger.log(
                        f"[SPEC][NET-FIX] {comp_name}:{target_pin_name} remap -> {fixed_name} "
                        f"(prev={resolved_pin_name}, fix_mode={fixed_mode}, ui_verified={fixed_meta.get('ui_verified')})",
                        level=LogLevel.WARNING,
                    )
                    pin_inst = fixed_inst
                    resolved_pin_name = fixed_name
                    if fixed_mode and fixed_mode not in {"net_valid_keep", "no_spec_net"}:
                        resolve_mode = fixed_mode
        except Exception as net_fix_exc:
            logger.log(
                f"[SPEC][NET-FIX][WARNING] Net/UI correction failed for {comp_name}:{target_pin_name}: {net_fix_exc}",
                level=LogLevel.WARNING,
            )

        if pin_inst is not None and resolve_mode in ("coord_only", "coord_only_net_mismatch", "coord_only_power_net_unvalidated"):
            spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
            if spec_nets_local and any(is_power_like_net_fn(n) for n in spec_nets_local):
                net_matched = any(net_matches_spec_fn(n, pin_inst.net_name, net_alias_map) for n in spec_nets_local)
                signal_like = is_signal_like_net_fn(pin_inst.net_name)
                if (not net_matched) or signal_like:
                    logger.log(
                        f"[SPEC][SAFETY] Reject coord-only power pin mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name or target_pin_name} "
                        f"(resolved_net={pin_inst.net_name}, spec_nets={spec_nets_local}, signal_like={signal_like})",
                        level=LogLevel.WARNING,
                    )
                    try:
                        rec_inst, rec_key = find_component_pin_by_global_padstack_scan_fn(
                            comp_inst=comp_inst,
                            spec_pin=target_pin_name,
                            spec_nets=spec_nets_local,
                            net_alias_map=net_alias_map,
                            excluded_pin_names=used_component_pins.get(comp_name, set()),
                            edbapp=getattr(app, "edb", None),
                        )
                        if rec_inst is not None:
                            pin_inst = rec_inst
                            resolved_pin_name = rec_key or target_pin_name
                            resolve_mode = "global_padstack_scan_recover"
                            logger.log(
                                f"[SPEC][RECOVER] Global padstack scan recovered mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name} (net={pin_inst.net_name})",
                                level=LogLevel.WARNING,
                            )
                        else:
                            sq_inst, sq_key = find_component_pin_by_spatial_query_fn(
                                comp_inst=comp_inst,
                                spec_pin=target_pin_name,
                                spec_nets=spec_nets_local,
                                net_alias_map=net_alias_map,
                                cmp_pin_record=cmp_pin_info,
                                excluded_pin_names=used_component_pins.get(comp_name, set()),
                                edbapp=getattr(app, "edb", None),
                            )
                            if sq_inst is not None:
                                pin_inst = sq_inst
                                resolved_pin_name = sq_key or target_pin_name
                                resolve_mode = "spatial_query_recover"
                                logger.log(
                                    f"[SPEC][RECOVER] Spatial query recovered mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name} (net={pin_inst.net_name})",
                                    level=LogLevel.WARNING,
                                )
                    except Exception as rec_exc:
                        logger.log(
                            f"[SPEC][RECOVER][WARNING] Recovery attempt failed for {comp_name}:{target_pin_name}: {rec_exc}",
                            level=LogLevel.WARNING,
                        )

                    if pin_inst is not None and resolve_mode in ("global_padstack_scan_recover", "spatial_query_recover"):
                        pass
                    else:
                        try:
                            gcnt = 0
                            gtok = normalize_pin_token_fn(target_pin_name)
                            gspec = [str(n).strip() for n in (spec_nets_local or []) if str(n).strip()]
                            for pad in iter_all_padstack_instances_fn(getattr(app, "edb", None)):
                                pname = str(getattr(pad, "name", "") or "")
                                pnet = str(getattr(pad, "net_name", "") or "")
                                pcname = pad_component_name_fn(pad)
                                ptok = normalize_pin_token_fn(pname)
                                if gtok and gtok not in ptok:
                                    continue
                                if pcname and str(pcname).upper() != str(comp_name).upper():
                                    continue
                                if gspec and not any(net_matches_spec_fn(sn, pnet, net_alias_map) for sn in gspec):
                                    continue
                                gcnt += 1
                            logger.log(
                                f"[SPEC][DEBUG] global scan candidates for {comp_name}:{target_pin_name} => {gcnt}",
                                level=LogLevel.DETAIL1,
                            )
                        except Exception:
                            pass
                    try:
                        helper = getattr(app.edb, "components", None)
                        hp = helper.get_pin_from_component(comp_name, pin_name=target_pin_name) if helper else []
                        hp = hp or []
                        hp_brief = []
                        for p in hp[:10]:
                            hp_brief.append(
                                {
                                    "component_pin": str(getattr(p, "component_pin", "")),
                                    "aedt_name": str(getattr(p, "aedt_name", "")),
                                    "name": str(getattr(p, "name", "")),
                                    "net": str(getattr(p, "net_name", "")),
                                }
                            )
                        logger.log(
                            f"[SPEC][DEBUG] helper pins for {comp_name}:{target_pin_name} => {hp_brief}",
                            level=LogLevel.DETAIL1,
                        )
                    except Exception:
                        pass
                    try:
                        raw_comp = getattr(comp_inst, "_edb_object", None) or getattr(comp_inst, "edbcomponent", None)
                        dpins = iter_dotnet_pins_from_component_fn(raw_comp)
                        ttok = normalize_pin_token_fn(target_pin_name)
                        dbrief = []
                        for dp in dpins:
                            toks = sorted(
                                list(
                                    collect_dotnet_pin_tokens_fn(
                                        dp,
                                        components_api=getattr(app.edb, "components", None),
                                    )
                                )
                            )
                            if ttok and ttok not in toks:
                                continue
                            dbrief.append(
                                {
                                    "tokens": toks[:6],
                                    "net": dotnet_pin_net_name_fn(dp),
                                    "pos": dotnet_pin_position_fn(dp),
                                }
                            )
                            if len(dbrief) >= 10:
                                break
                        logger.log(
                            f"[SPEC][DEBUG] dotnet pins for {comp_name}:{target_pin_name} => {dbrief}",
                            level=LogLevel.DETAIL1,
                        )
                    except Exception:
                        pass
                    if resolve_mode not in ("global_padstack_scan_recover", "spatial_query_recover"):
                        pin_inst = None
                        resolve_mode = "coord_rejected_power_net_mismatch"
                        resolved_pin_name = None

        actual_pin_name = resolved_pin_name or target_pin_name
        if not pin_inst:
            if strict_refdes_pin:
                trace_meta = {
                    "status": "skip",
                    "reason": "strict_refdes_pin_no_exact_match",
                }
                add_pin_mapping_record_fn(
                    case_row=case,
                    resolved_comp=comp_name,
                    spec_pin=target_pin_name,
                    resolved_pin=actual_pin_name,
                    mode=resolve_mode or "strict_no_exact_pin",
                    spec_nets_row=spec_nets,
                    resolved_net="",
                    trace_meta=trace_meta,
                )
                spec_skip_missing_pin += 1
                logger.log(
                    f"[SPEC][SKIP][STRICT] Pin exact match not found: designator={comp_name}, pin={target_pin_name}, "
                    f"lookup_pin={lookup_pin_name}, nets={spec_nets}. Net/coord fallback disabled by policy.",
                    level=LogLevel.WARNING,
                )
                continue
            try:
                board_nets = list((getattr(app.edb, "nets", None).nets or {}).keys())
            except Exception:
                board_nets = []
            fallback_net = resolve_board_net_by_spec_fn(spec_nets, board_nets, net_alias_map)
            trace_meta = {
                "start_net": fallback_net or "",
                "reached_net": "",
                "hops": "",
                "status": "skip",
                "reason": "",
            }
            if fallback_net:
                traced_pin_name, traced_pin_net, traced_hops, trace_reason, trace_diag = trace_ic_local_power_pin_fn(
                    comp_inst_local=comp_inst,
                    start_net=fallback_net,
                    gnd_net=gnd_net,
                    spec_nets=spec_nets,
                    max_hops=10,
                )
                diag_hits = trace_diag.get("ic_hits", []) if isinstance(trace_diag, dict) else []
                diag_visited = trace_diag.get("visited_nets", []) if isinstance(trace_diag, dict) else []
                diag_hit_tokens = [f"{h.get('pin')}@{h.get('net')}(hop={h.get('hop')})" for h in diag_hits if isinstance(h, dict)]
                trace_meta.update(
                    {
                        "start_net": fallback_net,
                        "reached_net": traced_pin_net or "",
                        "hops": traced_hops if traced_hops >= 0 else "",
                        "reason": trace_reason,
                        "candidate_pins": diag_hit_tokens,
                        "visited_nets": diag_visited,
                    }
                )
                if diag_hits:
                    preview = ", ".join(diag_hit_tokens[:30])
                    if len(diag_hit_tokens) > 30:
                        preview += f", ...(+{len(diag_hit_tokens)-30})"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][CANDIDATES] {comp_name}:{target_pin_name} traced_nets={diag_visited} | ic_hits={preview}",
                        level=LogLevel.WARNING,
                    )
                else:
                    logger.log(
                        f"[SPEC][NET-FALLBACK][CANDIDATES] {comp_name}:{target_pin_name} traced_nets={diag_visited} | ic_hits=<none>",
                        level=LogLevel.WARNING,
                    )
                if traced_pin_name:
                    trace_meta["status"] = "ok"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][TRACE] {comp_name}:{target_pin_name} -> IC pin {traced_pin_name} "
                        f"(start_net={fallback_net}, reached_net={traced_pin_net}, hops={traced_hops})",
                        level=LogLevel.WARNING,
                    )
                else:
                    trace_meta["status"] = "skip"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][TRACE][SKIP] Could not trace IC-local power pin from net={fallback_net} "
                        f"for {comp_name}:{target_pin_name} (GND/signal excluded).",
                        level=LogLevel.WARNING,
                    )
                    fallback_net = ""

            if fallback_net:
                if append_case_from_target_net_fn(
                    case_row=case,
                    resolved_comp=comp_name,
                    spec_pin=target_pin_name,
                    resolved_pin=(traced_pin_name or target_pin_name),
                    target_net=fallback_net,
                    spec_nets_row=spec_nets,
                    mapping_mode="net_only_fallback",
                    trace_meta=trace_meta,
                    conf_manager=conf_manager,
                ):
                    logger.log(
                        f"[SPEC][NET-FALLBACK] Pin unresolved. Continue with net mapping: {comp_name}:{target_pin_name} -> net={fallback_net}",
                        level=LogLevel.WARNING,
                    )
                    add_pin_mapping_record_fn(
                        case_row=case,
                        resolved_comp=comp_name,
                        spec_pin=target_pin_name,
                        resolved_pin=(traced_pin_name or target_pin_name),
                        mode="net_only_fallback",
                        spec_nets_row=spec_nets,
                        resolved_net=fallback_net,
                        trace_meta=trace_meta,
                    )
                    continue

            add_pin_mapping_record_fn(
                case_row=case,
                resolved_comp=comp_name,
                spec_pin=target_pin_name,
                resolved_pin=actual_pin_name,
                mode=resolve_mode,
                spec_nets_row=spec_nets,
                resolved_net="",
                trace_meta=trace_meta,
            )
            spec_skip_missing_pin += 1
            logger.log(
                f"[SPEC][SKIP] Pin not resolved: designator={comp_name}, pin={target_pin_name}, mode={resolve_mode}, nets={spec_nets}",
                level=LogLevel.WARNING,
            )
            continue

        add_pin_mapping_record_fn(
            case_row=case,
            resolved_comp=comp_name,
            spec_pin=target_pin_name,
            resolved_pin=actual_pin_name,
            mode=resolve_mode,
            spec_nets_row=spec_nets,
            resolved_net=pin_inst.net_name,
            trace_meta=(pin_trace_meta or {"status": "direct"}),
        )
        used_component_pins[comp_name].add(actual_pin_name)
        if resolve_mode != "exact":
            logger.log(
                f"[SPEC][PINMAP] {comp_name}: {target_pin_name} -> {actual_pin_name} (mode={resolve_mode}, net={pin_inst.net_name})",
                level=LogLevel.WARNING,
            )
        append_case_from_target_net_fn(
            case_row=case,
            resolved_comp=comp_name,
            spec_pin=target_pin_name,
            resolved_pin=actual_pin_name,
            target_net=pin_inst.net.name,
            spec_nets_row=spec_nets,
            mapping_mode=resolve_mode,
            trace_meta=(pin_trace_meta or {"status": "direct", "reason": resolve_mode}),
            conf_manager=conf_manager,
        )

    return {
        "spec_skip_missing_comp": spec_skip_missing_comp,
        "spec_skip_missing_pin": spec_skip_missing_pin,
        "spec_fallback_resolved": spec_fallback_resolved,
    }


def finalize_step4_outputs(
    *,
    app,
    gnd_net: str,
    pdn_cases_info,
    spec_info,
    spec_fallback_resolved: int,
    spec_skip_missing_comp: int,
    spec_skip_missing_pin: int,
    pin_resolution_stats,
    pin_mapping_quality_stats,
    pin_mapping_records,
    output_dir: Path,
    edb_file_path: Path,
    run_tag: str | None,
    stage: str,
    ensure_pre_edb_saved,
    export_pin_crosswalk_reports,
    export_edb_truth_table_reports,
    export_ui_api_crosswalk_reports,
    pin_crosswalk_cache,
    edb_truth_cache,
    ui_api_crosswalk_cache,
    logger,
):
    net_search_json, net_search_csv = export_net_search_results(output_dir, pdn_cases_info)
    logger.log(f"[NET_SEARCH] Exported full path JSON: {net_search_json}", level=LogLevel.INFO)
    logger.log(f"[NET_SEARCH] Exported full path CSV: {net_search_csv}", level=LogLevel.DETAIL1)

    logger.log(
        f"[BOM] Component deactivate remains deferred until verified VRM creation: "
        f"{len(getattr(app, '_pdn_deferred_disable', []) or [])}",
        level=LogLevel.INFO,
    )

    target_nets = {gnd_net} | {
        n
        for case in pdn_cases_info
        for n in case.get("Analysis_Net_Chain", case.get("Full_Net_Chain", []))
    }
    if not app.sanitize_nets(target_nets):
        raise PDNSessionException(ErrorCode.SANITIZE_FAIL)
    logger.log(
        f"[SPEC] Case build summary: total_rows={len(spec_info)}, cases={len(pdn_cases_info)}, "
        f"fallback_resolved={spec_fallback_resolved}, skip_missing_comp={spec_skip_missing_comp}, skip_missing_pin={spec_skip_missing_pin}",
        level=LogLevel.INFO,
    )
    logger.log(f"[SPEC] Pin resolution modes: {dict(pin_resolution_stats)}", level=LogLevel.INFO)
    logger.log(f"[SPEC] Pin mapping quality: {dict(pin_mapping_quality_stats)}", level=LogLevel.INFO)
    cross_csv, cross_json = export_pin_crosswalk_reports(output_dir, pin_crosswalk_cache)
    truth_csv, truth_json = export_edb_truth_table_reports(output_dir, edb_truth_cache)
    ui_api_csv, ui_api_json = export_ui_api_crosswalk_reports(output_dir, ui_api_crosswalk_cache)
    logger.log(f"[SPEC] Exported pin crosswalk CSV: {cross_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported pin crosswalk JSON: {cross_json}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported EDB truth-table CSV: {truth_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported EDB truth-table JSON: {truth_json}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported UI<->API crosswalk CSV: {ui_api_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported UI<->API crosswalk JSON: {ui_api_json}", level=LogLevel.DETAIL1)
    pin_map_report = output_dir / "spec_to_edb_pin_map.json"
    with open(pin_map_report, "w", encoding="utf-8") as f:
        json.dump(
            {
                "Summary": {
                    "TotalRows": len(spec_info),
                    "ResolvedCases": len(pdn_cases_info),
                    "ResolutionModes": dict(pin_resolution_stats),
                    "MappingQuality": dict(pin_mapping_quality_stats),
                },
                "Records": pin_mapping_records,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    logger.log(f"[SPEC] Exported Spec->EDB pin map: {pin_map_report}", level=LogLevel.DETAIL1)
    run_tag_value = run_tag or time.strftime("%Y%m%d_%H%M%S")
    pre_edb_file_path = edb_file_path.parent / f"{edb_file_path.stem}_pre_{run_tag_value}{edb_file_path.suffix}"
    ensure_pre_edb_saved(app=app, source_edb_path=edb_file_path, pre_edb_path=pre_edb_file_path, max_retries=2, timeout=300.0)

    edb_setup_app = None
    if stage == "full":
        app.set_cad_file(pre_edb_file_path)
        edb_setup_app = app
        app = None

    return {
        "PRE_EDB_FILE_PATH": pre_edb_file_path,
        "EDB_SETUP_APP": edb_setup_app,
        "app": app,
    }


def add_pin_mapping_record(
    *,
    case_row,
    resolved_comp,
    spec_pin,
    resolved_pin,
    mode,
    spec_nets_row,
    resolved_net,
    trace_meta,
    evaluate_pin_mapping_quality,
    pin_mapping_quality_stats,
    pin_resolution_stats,
    pin_mapping_records,
):
    spec_net_preferred = ""
    try:
        spec_net_preferred = str((spec_nets_row or [""])[0] or "").strip()
    except Exception:
        spec_net_preferred = ""
    trace_meta = trace_meta or {}
    mapping_status, mapping_confidence, mapping_note = evaluate_pin_mapping_quality(mode, trace_meta)
    pin_mapping_quality_stats[mapping_status] += 1
    pin_resolution_stats[mode] += 1
    pin_mapping_records.append(
        {
            "Spec_Designator": case_row.get("Designator"),
            "Resolved_Designator": resolved_comp,
            "Spec_Pin": spec_pin,
            "Resolved_Pin": resolved_pin,
            "Resolve_Mode": mode,
            "Spec_Nets": spec_nets_row,
            "Resolved_Net": spec_net_preferred or resolved_net,
            "Resolved_Net_PCB": resolved_net,
            "Trace_Start_Net": trace_meta.get("start_net", ""),
            "Trace_Reached_Net": trace_meta.get("reached_net", ""),
            "Trace_Hops": trace_meta.get("hops", ""),
            "Trace_Status": trace_meta.get("status", ""),
            "Trace_Reason": trace_meta.get("reason", ""),
            "Trace_Candidate_Pins": trace_meta.get("candidate_pins", []),
            "Mapping_Status": mapping_status,
            "Mapping_Confidence": mapping_confidence,
            "Mapping_Note": mapping_note,
        }
    )


def append_case_from_target_net(
    *,
    app,
    case_row,
    resolved_comp,
    spec_pin,
    resolved_pin,
    target_net,
    spec_nets_row,
    mapping_mode,
    trace_meta,
    evaluate_pin_mapping_quality,
    db,
    find_power_source,
    designator_list,
    bom_info,
    gnd_net,
    parse_numeric,
    extract_voltage,
    error_code_cls,
    pdn_cases_info,
    conf_manager=None,
):
    try:
        net_obj = app.edb.nets.nets.get(target_net)
    except Exception:
        net_obj = None
    if net_obj is None:
        return False
    trace_meta = trace_meta or {}
    mapping_status, mapping_confidence, mapping_note = evaluate_pin_mapping_quality(mapping_mode, trace_meta)

    db.other_nets[target_net] = []
    result, net_chain = find_power_source(app.edb, net_obj, designator_list, bom_info, gnd_net, target_ic=resolved_comp)
    if isinstance(result, error_code_cls):
        net_chain = []

    source_pin_name = None
    if not isinstance(result, error_code_cls) and result and getattr(result, "pins", None):
        source_pin_name = next(
            (
                p_name
                for s_net in reversed(net_chain + [target_net])
                for p_name, p in result.pins.items()
                if p.net_name == s_net
            ),
            None,
        )

    full_chain_raw = []
    for n in (net_chain + [target_net]):
        if n not in full_chain_raw:
            full_chain_raw.append(n)

    pdn_conf = ((getattr(conf_manager, "data", {}) or {}).get("PDN", {}) if conf_manager else {})
    net_search_conf = dict(pdn_conf.get("netSearch", {}) or {})
    power_keywords = [str(k).upper() for k in net_search_conf.get("powerKeywords", ["+", "VDD", "VCC", "AVDD", "DVDD", "PWR", "POWER"]) if str(k).strip()]
    signal_keywords = [str(k).upper() for k in net_search_conf.get("signalKeywords", ["SIGN", "CLK", "DATA", "GPIO", "TX", "RX"]) if str(k).strip()]
    inductor_prefixes = tuple(str(k).upper() for k in net_search_conf.get("inductorPrefixes", ["L", "B", "FB"]) if str(k).strip())
    inductor_def_keywords = [str(k).upper() for k in net_search_conf.get("inductorDefKeywords", ["IND", "BEAD", "WIRE WOUND", "WOUND"]) if str(k).strip()]
    min_plane_area_mm2 = float(net_search_conf.get("minPlaneAreaMm2", 1.0) or 0.0)
    short_key = str(((pdn_conf.get("dcShort", {}) or {}).get("shortKey", "") or "")).upper().strip()

    if not hasattr(append_case_from_target_net, "_pdn_net_cache"):
        append_case_from_target_net._pdn_net_cache = {}
    app_key = id(app)
    runtime_cache = append_case_from_target_net._pdn_net_cache.get(app_key)
    if runtime_cache is None:
        inductor_touch_nets = set()
        for comp_name, comp_inst in app.edb._components.components.items():
            try:
                comp_u = str(comp_name or "").upper()
                comp_def_u = str(getattr(comp_inst, "component_def", "") or "").upper()
                if not (comp_u.startswith(inductor_prefixes) or any(k in comp_def_u for k in inductor_def_keywords)):
                    continue
                for p in comp_inst.pins.values():
                    pn = str(getattr(p, "net_name", "") or "").strip()
                    if pn:
                        inductor_touch_nets.add(pn)
            except Exception:
                continue
        runtime_cache = {
            "inductor_touch_nets": inductor_touch_nets,
            "plane_area_mm2": {},
        }
        append_case_from_target_net._pdn_net_cache[app_key] = runtime_cache

    def _is_power_net_name(name: str) -> bool:
        up = str(name or "").upper().strip()
        if not up:
            return False
        if up == str(gnd_net or "").upper():
            return False
        if any(tok and tok in up for tok in signal_keywords):
            return False
        return any(tok and tok in up for tok in power_keywords)

    def _net_plane_area_mm2(name: str) -> float:
        cache = runtime_cache.get("plane_area_mm2", {})
        key = str(name or "")
        if key in cache:
            return float(cache[key])
        try:
            net_inst = app.edb.nets.nets.get(name)
            if net_inst is None:
                cache[key] = 0.0
                return 0.0
            total = 0.0
            for prim in getattr(net_inst, "primitives", []) or []:
                try:
                    if getattr(prim, "type", "") == "Path":
                        continue
                    area_m2 = float(prim.area())
                    if area_m2 > 0:
                        total += area_m2 * 1_000_000.0
                except Exception:
                    continue
            cache[key] = total
            runtime_cache["plane_area_mm2"] = cache
            return total
        except Exception:
            cache[key] = 0.0
            runtime_cache["plane_area_mm2"] = cache
            return 0.0

    def _is_plane_power_net(name: str) -> bool:
        if not _is_power_net_name(name):
            return False
        return _net_plane_area_mm2(name) >= min_plane_area_mm2

    def _net_touches_power_inductor(name: str) -> bool:
        target = str(name or "").strip()
        if not target:
            return False
        return target in runtime_cache.get("inductor_touch_nets", set())

    chain_soc_to_source = [n for n in reversed(full_chain_raw) if str(n).strip()]
    if short_key:
        chain_soc_to_source = [n for n in chain_soc_to_source if short_key not in str(n).upper()]

    selected_soc_path = []
    hit_inductor = False
    for net_name in chain_soc_to_source:
        selected_soc_path.append(net_name)
        if _net_touches_power_inductor(net_name):
            hit_inductor = True
            break

    if not hit_inductor:
        fallback_idx = None
        for idx, net_name in enumerate(chain_soc_to_source):
            if _is_plane_power_net(net_name):
                fallback_idx = idx
                break
        if fallback_idx is not None:
            selected_soc_path = chain_soc_to_source[: fallback_idx + 1]
        elif chain_soc_to_source:
            selected_soc_path = [chain_soc_to_source[0]]

    full_chain = []
    for n in reversed(selected_soc_path):
        if n not in full_chain:
            full_chain.append(n)
    if not full_chain and target_net:
        full_chain = [target_net]

    vmag_default = extract_voltage(target_net) or 1.0
    vmag = parse_numeric(case_row.get("Voltage_(V)"), vmag_default)
    imag = parse_numeric(case_row.get("Current_(A)"), 1.0)
    min_spec = parse_numeric(case_row.get("Min_Spec_(V)"), 0.0)
    max_spec = parse_numeric(case_row.get("Max_Spec_(V)"), max(vmag * 1.2, vmag + 0.1))
    spec_net_preferred = spec_nets_row[0] if spec_nets_row else target_net

    case_record = {
            "IC": resolved_comp,
            "Spec_Pin": spec_pin,
            "IC_pin": resolved_pin,
            "Net": target_net,
            "Spec_Net": spec_net_preferred,
            "Display_Net": spec_net_preferred or target_net,
            "Source_name": result.name if not isinstance(result, error_code_cls) else "",
            "Source_pin": source_pin_name,
            "Source_net_chain": net_chain,
            "Full_Net_Chain": full_chain,
            "Vmag": vmag,
            "Imag": imag,
            "MinSpec": min_spec,
            "MaxSpec": max_spec,
            "Mapping_Mode": mapping_mode,
            "Mapping_Trace": trace_meta,
            "Mapping_Status": mapping_status,
            "Mapping_Confidence": mapping_confidence,
            "Mapping_Note": mapping_note,
        }
    path_result = resolve_case_power_path(
        edb=app.edb,
        case=case_record,
        discovered_chain=full_chain_raw,
        source_component=None if isinstance(result, error_code_cls) else result,
        source_pin=source_pin_name or "",
        bom_info=bom_info,
        net_search_conf=net_search_conf,
    )
    pdn_cases_info.append(case_record)
    if path_result.status != "ok":
        # Keep the case for diagnostics, but never invent an IC-side VRM anchor.
        case_record["Setup_Eligible"] = False
    else:
        case_record["Setup_Eligible"] = True
    return True


def trace_ic_local_power_pin(
    *,
    app,
    comp_inst_local,
    start_net,
    gnd_net,
    spec_nets,
    max_hops,
    is_signal_like_net,
    is_forbidden_trace_net,
    is_power_like_net,
    net_matches_spec,
    net_alias_map,
):
    start_net = str(start_net or "").strip()
    if not start_net or comp_inst_local is None:
        return "", "", -1, "invalid_input", {"visited_nets": [], "ic_hits": []}

    from collections import deque
    q = deque([(start_net, 0)])
    visited = {start_net}
    visited_order = [start_net]
    gnd_u = str(gnd_net or "").strip().upper()
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    ic_hits_all = []
    ic_hits_seen = set()

    while q:
        cur_net, hops = q.popleft()
        cur_u = str(cur_net or "").upper()
        cur_signal = is_signal_like_net(cur_net)
        cur_forbidden = is_forbidden_trace_net(cur_net)
        if cur_u == gnd_u or cur_signal or cur_forbidden:
            pass
        else:
            ic_hits = []
            for pn, p in comp_inst_local.pins.items():
                pnet = str(getattr(p, "net_name", "") or "").strip()
                if not pnet:
                    continue
                if pnet == cur_net:
                    is_gnd = str(pnet).upper() == gnd_u
                    is_signal = is_signal_like_net(pnet)
                    is_forbidden = is_forbidden_trace_net(pnet)
                    is_power = is_power_like_net(pnet)
                    hit_key = (str(pn), str(pnet), hops)
                    if hit_key not in ic_hits_seen:
                        ic_hits_all.append(
                            {
                                "pin": str(pn),
                                "net": str(pnet),
                                "hop": hops,
                                "is_gnd": bool(is_gnd),
                                "is_power_like": bool(is_power),
                                "is_signal_like": bool(is_signal),
                                "is_forbidden": bool(is_forbidden),
                            }
                        )
                        ic_hits_seen.add(hit_key)
                    if is_gnd or is_signal or is_forbidden or (not is_power):
                        continue
                    if spec_nets_local and not any(net_matches_spec(sn, pnet, net_alias_map) for sn in spec_nets_local):
                        continue
                    ic_hits.append((pn, pnet))
            if ic_hits:
                ic_hits.sort(key=lambda x: str(x[0]))
                return ic_hits[0][0], ic_hits[0][1], hops, "ok", {
                    "visited_nets": visited_order,
                    "ic_hits": ic_hits_all,
                }

        if hops >= max_hops:
            continue

        for _, comp in app.edb._components.components.items():
            pins = list(comp.pins.values())
            if not pins:
                continue
            if len(pins) > 24 and comp is not comp_inst_local:
                continue
            if not any(str(getattr(p, "net_name", "") or "") == cur_net for p in pins):
                continue
            for p in pins:
                nxt = str(getattr(p, "net_name", "") or "").strip()
                if not nxt or nxt in visited or nxt == cur_net:
                    continue
                if str(nxt).upper() == gnd_u:
                    continue
                if is_signal_like_net(nxt) or is_forbidden_trace_net(nxt):
                    continue
                visited.add(nxt)
                visited_order.append(nxt)
                q.append((nxt, hops + 1))

    return "", "", -1, "no_ic_local_power_pin", {
        "visited_nets": visited_order,
        "ic_hits": ic_hits_all,
    }
