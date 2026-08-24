from pathlib import Path
import json
import time

from core.database import ErrorCode, PDNSessionException
from core.logger import LogLevel


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
    exclude_prefixes = tuple(conf_manager.data["PDN"]["dcShort"].get("excludePrefixes", ["AR", "JK", "P", "IC", "X", "D"]))
    delete_types = conf_manager.data["PDN"]["dcShort"].get("deleteCompTypes", ["IC", "IO", "Other"])

    for comp_name, comp_inst in app.edb._components.components.items():
        if str(comp_name).upper().startswith("C"):
            continue
        if comp_name in bom_info["Designators"]:
            continue
        elif (
            not comp_name.startswith(exclude_prefixes)
            and comp_inst.component_def in conf_manager.data["PDN"]["dcShort"]["shortedComp"]
        ):
            continue
        else:
            if comp_inst.type in delete_types:
                del_comp[comp_name] = comp_inst
                missing_in_bom.append(comp_name)
            else:
                comp_inst.enabled = False

    short_correction, del_comp_set = {}, set()
    for comp_name, comp_inst in app.edb._components.components.items():
        if comp_name.startswith(exclude_prefixes) or comp_inst.component_def not in conf_manager.data["PDN"]["dcShort"]["shortedComp"]:
            continue
        target_nets = app.edb.nets.nets_by_components[comp_name]
        if len(target_nets) != 2:
            continue
        net1, net2 = target_nets
        short_key = conf_manager.data["PDN"]["dcShort"]["shortKey"]
        primary, secondary = (net2, net1) if short_key in net1 or (short_key not in net2 and len(net1) > len(net2)) else (net1, net2)
        short_correction.setdefault(primary, []).append(secondary)
        del_comp_set.add(comp_name)

    spec_file = input_valchk._default_inputFiles["Spec"]
    settings_manager.parse_spec(spec_file)
    spec_info = settings_manager.get_spec()
    logger.log(f"[SPEC] Parsed rows: {len(spec_info)}", level=LogLevel.DETAIL1)
    target_designators = {str(row.get("Designator", "")).strip() for row in spec_info if row.get("Designator")}
    cmp_pin_records = parse_cmp_pin_records(cmp_file_path, target_designators=target_designators)
    ndf_pin_records = parse_ndf_pin_records(ndf_file_path, target_designators=target_designators)
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
        if settings_manager.parse_inner_cap(inner_cap_path):
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
                    "status": "pending",
                    "message": "",
                }

                norm_ic_name = normalize_name(ic_refdes)
                ic_inst = get_component_by_normalized(norm_ic_name)
                if not ic_inst:
                    continue

                pin_inst = ic_inst.pins.get(pin_no)
                if not pin_inst:
                    continue

                pin_loc = pin_inst.position

                try:
                    gnd_pin_inst = find_nearest_gnd_pin(app.edb, pin_loc, gnd_net)
                    if not gnd_pin_inst:
                        continue

                    created = app.create_rlc_component(
                        pins=[pin_inst, gnd_pin_inst],
                        comp_name=cap_name,
                        part_name=icap.get("Part_Number", "INNER_CAP"),
                        r_value=1e9,
                    )
                    if created:
                        new_comp = app.edb._components.components.get(cap_name)
                        if new_comp:
                            new_comp.type = "Capacitor"
                            new_comp.value = cap_val
                            audit_item["status"] = "created"
                    inner_cap_audit.append(audit_item)
                except Exception as e:
                    logger.log(f"[ERROR] Inner Cap {cap_name} 생성 중 오류 발생: {e}", level=LogLevel.ERROR)

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
            ov_pin_name = str(pin_override_map.get(ov_key, "")).strip()
            ov_inst, ov_resolved_name = find_component_pin_by_name_or_display_fn(
                comp_inst=comp_inst,
                pin_name=ov_pin_name,
                excluded=used_component_pins.get(comp_name, set()),
            )
            if ov_inst is not None:
                pin_inst = ov_inst
                resolved_pin_name = ov_resolved_name or ov_pin_name
                resolve_mode = "pin_override"
                logger.log(
                    f"[SPEC][OVERRIDE] {comp_name}: {target_pin_name} -> {resolved_pin_name}",
                    level=LogLevel.WARNING,
                )
            else:
                logger.log(
                    f"[SPEC][OVERRIDE][WARNING] Override target not found: {comp_name}:{target_pin_name} -> {ov_pin_name}",
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
    target_nets = {gnd_net} | {n for case in pdn_cases_info for n in case.get("Full_Net_Chain", [])}
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

    full_chain = []
    for n in (net_chain + [target_net]):
        if n not in full_chain:
            full_chain.append(n)

    vmag_default = extract_voltage(target_net) or 1.0
    vmag = parse_numeric(case_row.get("Voltage_(V)"), vmag_default)
    imag = parse_numeric(case_row.get("Current_(A)"), 1.0)
    min_spec = parse_numeric(case_row.get("Min_Spec_(V)"), 0.0)
    max_spec = parse_numeric(case_row.get("Max_Spec_(V)"), max(vmag * 1.2, vmag + 0.1))
    spec_net_preferred = spec_nets_row[0] if spec_nets_row else target_net

    pdn_cases_info.append(
        {
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
    )
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
