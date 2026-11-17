from pathlib import Path
import json
import shutil
import time
from core.logger import LogLevel
from core.model_generation import resolve_generation_options


def _named_edb_objects(raw):
    """Best-effort iterator for Python dictionaries and EDB .NET collections."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        result = []
        for key, value in raw.items():
            name = str(getattr(value, "name", "") or key or "").strip()
            if name:
                result.append((name, value))
        return result
    try:
        values = list(raw)
    except Exception:
        values = []
    result = []
    for value in values:
        name = str(
            getattr(value, "name", "")
            or getattr(value, "Name", "")
            or ""
        ).strip()
        if name:
            result.append((name, value))
    return result


def _collect_edb_terminals(edb):
    """Collect terminals through all PyEDB/.NET access paths used by supported releases."""
    collected = {}
    sources = []
    for attr in ("terminals", "_terminals", "excitations"):
        try:
            sources.append((attr, getattr(edb, attr, None)))
        except Exception:
            pass
    try:
        layout = getattr(edb, "active_layout", None)
        sources.append(("active_layout.Terminals", getattr(layout, "Terminals", None)))
        get_terminals = getattr(layout, "GetTerminals", None)
        if callable(get_terminals):
            sources.append(("active_layout.GetTerminals", get_terminals()))
    except Exception:
        pass

    source_hits = {}
    for source_name, raw in sources:
        hits = 0
        for name, terminal in _named_edb_objects(raw):
            if name not in collected:
                collected[name] = terminal
            hits += 1
        if hits:
            source_hits[source_name] = hits
    return collected, source_hits


def _terminal_boundary_type(terminal):
    for attr in ("boundary_type", "BoundaryType"):
        try:
            value = getattr(terminal, attr, None)
            if value is not None:
                return str(value)
        except Exception:
            pass
    try:
        raw = getattr(terminal, "_edb_object", terminal)
        getter = getattr(raw, "GetBoundaryType", None)
        if callable(getter):
            return str(getter())
    except Exception:
        pass
    return ""


def _terminal_is_rlc(terminal, boundary_type):
    if "RLC" in str(boundary_type or "").upper():
        return True
    try:
        raw = getattr(terminal, "_edb_object", terminal)
        getter = getattr(raw, "GetRlcBoundaryParameters", None)
        if callable(getter):
            result = getter()
            if isinstance(result, tuple):
                return bool(result and result[0])
            return result is not None and result is not False
    except Exception:
        pass
    return False


def _validate_inner_cap_boundaries(*, edb_ops_app, inner_cap_audit, output_dir: Path, logger):
    """Fail closed unless every created InnerCap electrical model survived EDB handoff."""
    expected = [
        item for item in (inner_cap_audit or [])
        if str(item.get("status", "")).lower() == "created"
        and str(item.get("representation", "")).lower() in {"rlc_boundary", "rlc_component"}
    ]
    if not expected:
        return {"expected": 0, "verified": 0, "ok": True, "records": []}

    edb = getattr(edb_ops_app, "edb", None)
    terminals, source_hits = _collect_edb_terminals(edb)
    terminal_names_ci = {str(name).upper(): (name, terminal) for name, terminal in terminals.items()}
    components = getattr(getattr(edb, "_components", None), "components", {}) or {}
    components_ci = {str(name).upper(): component for name, component in components.items()}
    records = []
    verified = 0
    for item in expected:
        cap_name = str(item.get("component_name", "")).strip()
        representation = str(item.get("representation", "")).lower()
        if representation == "rlc_component":
            component = components_ci.get(cap_name.upper())
            try:
                pins = list(component.pins.values()) if component is not None else []
            except Exception:
                pins = []
            pin_nets = [str(getattr(pin, "net_name", "") or "") for pin in pins]
            positive_net = str(item.get("positive_net", "") or "")
            negative_net = str(item.get("negative_net", "") or "")
            ok = bool(
                component is not None
                and len(pins) == 2
                and positive_net in pin_nets
                and negative_net in pin_nets
            )
            if ok:
                verified += 1
            records.append({
                "inner_cap": cap_name,
                "representation": representation,
                "electrical_model": item.get("electrical_model", ""),
                "capacitance_from_innercap": item.get("cap_value", ""),
                "part_number": item.get("part_number", ""),
                "resistance": item.get("resistance"),
                "inductance": item.get("inductance"),
                "component_found": component is not None,
                "pin_count": len(pins),
                "pin_nets": pin_nets,
                "expected_nets": [positive_net, negative_net],
                "rlc_component_verified": ok,
            })
            continue
        pos_expected = str(item.get("positive_terminal", f"{cap_name}_P")).strip()
        neg_expected = str(item.get("negative_terminal", f"{cap_name}_N")).strip()
        pos_hit = terminal_names_ci.get(pos_expected.upper())
        neg_hit = terminal_names_ci.get(neg_expected.upper())
        boundary_type = _terminal_boundary_type(pos_hit[1]) if pos_hit else ""
        type_ok = _terminal_is_rlc(pos_hit[1], boundary_type) if pos_hit else False
        ok = bool(pos_hit and neg_hit and type_ok)
        if ok:
            verified += 1
        records.append({
            "inner_cap": cap_name,
            "representation": representation,
            "positive_terminal": pos_expected,
            "negative_terminal": neg_expected,
            "positive_found": bool(pos_hit),
            "negative_found": bool(neg_hit),
            "positive_boundary_type": boundary_type,
            "rlc_boundary_verified": ok,
        })

    payload = {
        "expected": len(expected),
        "verified": verified,
        "ok": verified == len(expected),
        "terminal_inventory_count": len(terminals),
        "terminal_sources": source_hits,
        "records": records,
    }
    report_path = output_dir / "innercap_boundary_validation.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.log(
        f"[INNER_CAP][VERIFY] RLC boundaries={verified}/{len(expected)}, "
        f"terminal_inventory={len(terminals)}, sources={source_hits}",
        level=LogLevel.INFO if payload["ok"] else LogLevel.ERROR,
    )
    if not payload["ok"]:
        missing = [
            r["inner_cap"] for r in records
            if not (r.get("rlc_boundary_verified") or r.get("rlc_component_verified"))
        ]
        raise RuntimeError(
            f"InnerCap RLC boundary handoff incomplete: verified={verified}/{len(expected)}, "
            f"missing_or_invalid={missing}. Full solve is blocked."
        )
    return payload


def _validate_inner_caps_in_siw(*, siw_path: Path, inner_cap_audit, output_dir: Path, logger):
    """Verify that every InnerCap component was serialized into the SIW project."""
    expected = [
        str(item.get("component_name", "")).strip()
        for item in (inner_cap_audit or [])
        if str(item.get("status", "")).lower() == "created"
        and str(item.get("representation", "")).lower() == "rlc_component"
        and str(item.get("component_name", "")).strip()
    ]
    if not expected:
        return {"expected": 0, "verified": 0, "ok": True, "missing": []}
    text = Path(siw_path).read_text(encoding="utf-8", errors="ignore")
    missing = [name for name in expected if name not in text]
    payload = {
        "siw": str(siw_path),
        "expected": len(expected),
        "verified": len(expected) - len(missing),
        "ok": not missing,
        "missing": missing,
    }
    report_path = Path(output_dir) / "innercap_siw_validation.json"
    report_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.log(
        f"[INNER_CAP][SIW_VERIFY] components={payload['verified']}/{payload['expected']}, "
        f"project={Path(siw_path).name}",
        level=LogLevel.INFO if payload["ok"] else LogLevel.ERROR,
    )
    if missing:
        raise RuntimeError(
            f"InnerCap SIW serialization incomplete: verified={payload['verified']}/{payload['expected']}, "
            f"missing={missing}. Full solve is blocked."
        )
    return payload

def _prune_components_for_analysis_scope(*, edb_ops_app, analysis_nets, gnd_net, bom_info, inner_cap_audit, conf_data, logger, output_dir: Path):
    scope_conf = dict(conf_data.get("PDN", {}).get("componentScope", {}) or {})
    if not bool(scope_conf.get("enabled", False)):
        return {"enabled": False}

    stage = str(conf_data.get("__STAGE__", "")).strip().lower()
    apply_on_stages = [str(s).strip().lower() for s in scope_conf.get("applyOnStages", ["pre", "full"]) if str(s).strip()]
    if apply_on_stages and stage and stage not in apply_on_stages:
        return {"enabled": True, "skipped_by_stage": True, "stage": stage}

    disable_only = bool(scope_conf.get("disableOnly", True))
    delete_fallback = bool(scope_conf.get("deleteFallbackOnUnsupported", True))
    require_bom_match = bool(scope_conf.get("requireBomMatch", True))
    keep_prefixes = tuple(str(x or "") for x in scope_conf.get("alwaysKeepDesignatorPrefixes", ["C_IC", "PORT_", "Rvrm_"]) if str(x or ""))
    keep_component_defs = {str(x or "") for x in scope_conf.get("alwaysKeepComponentDefs", []) if str(x or "")}
    delete_types = set(str(x or "") for x in scope_conf.get("deleteCompTypes", ["IC", "IO", "Other"]) if str(x or ""))
    max_detail = int(scope_conf.get("logMaxDetails", 40) or 40)

    resistor_conf = dict(scope_conf.get("resistorPolicy", {}) or {})
    resistor_policy_enabled = bool(resistor_conf.get("enabled", True))
    resistor_prefixes = tuple(str(x or "").upper() for x in resistor_conf.get("designatorPrefixes", ["R"]) if str(x or ""))
    resistor_require_all_pins_in_scope = bool(resistor_conf.get("requireAllPinsInAnalysisOrGnd", True))

    capacitor_conf = dict(scope_conf.get("capacitorPolicy", {}) or {})
    capacitor_policy_enabled = bool(capacitor_conf.get("enabled", True))
    capacitor_prefixes = tuple(str(x or "").upper() for x in capacitor_conf.get("designatorPrefixes", ["C"]) if str(x or ""))
    capacitor_require_all_pins_in_scope = bool(capacitor_conf.get("requireAllPinsInAnalysisOrGnd", True))
    capacitor_keep_innercap = bool(capacitor_conf.get("keepInnerCap", True))

    analysis_set = {str(n).strip().upper() for n in (analysis_nets or []) if str(n).strip()}
    gnd_u = str(gnd_net or "").strip().upper()
    if gnd_u:
        analysis_set.add(gnd_u)

    bom_designators = {str(x).strip() for x in (bom_info.get("Designators", []) if isinstance(bom_info, dict) else []) if str(x).strip()}
    inner_caps = {str(item.get("component_name", "")).strip() for item in (inner_cap_audit or []) if str(item.get("component_name", "")).strip()}

    all_components = getattr(getattr(edb_ops_app, "edb", None), "_components", None)
    all_components = getattr(all_components, "components", {}) if all_components is not None else {}

    to_delete = {}
    disable_skip = []
    keep_reason = {}
    processed = 0
    kept = 0
    disabled = 0

    for comp_name, comp_inst in all_components.items():
        comp_name_str = str(comp_name)
        processed += 1

        reasons = []
        if keep_prefixes and comp_name_str.startswith(keep_prefixes):
            reasons.append("prefix")
        if str(getattr(comp_inst, "component_def", "")) in keep_component_defs:
            reasons.append("component_def")
        if comp_name_str in inner_caps or comp_name_str.upper().startswith("C_IC"):
            reasons.append("inner_cap")
        if comp_name_str in bom_designators:
            reasons.append("bom")

        pin_nets = set()
        try:
            for pin in getattr(comp_inst, "pins", {}).values():
                net_u = str(getattr(pin, "net_name", "") or "").strip().upper()
                if net_u:
                    pin_nets.add(net_u)
        except Exception:
            pin_nets = set()

        if analysis_set and (pin_nets & analysis_set):
            reasons.append("analysis_net")

        keep_by_special = any(r in {"prefix", "component_def", "inner_cap"} for r in reasons)
        keep_by_net = "analysis_net" in reasons
        keep_by_bom = "bom" in reasons
        keep_it = keep_by_special or (keep_by_bom and keep_by_net if require_bom_match else keep_by_net)

        is_resistor = bool(resistor_prefixes) and comp_name_str.upper().startswith(resistor_prefixes)
        if resistor_policy_enabled and is_resistor and (not keep_by_special):
            if resistor_require_all_pins_in_scope:
                resistor_ok = bool(pin_nets) and not bool(pin_nets - analysis_set)
            else:
                resistor_ok = bool(pin_nets & analysis_set)
            keep_it = bool(keep_it and resistor_ok)

        is_capacitor = bool(capacitor_prefixes) and comp_name_str.upper().startswith(capacitor_prefixes)
        is_innercap = (comp_name_str in inner_caps) or comp_name_str.upper().startswith("C_IC")
        if capacitor_policy_enabled and is_capacitor and (not keep_by_special):
            if capacitor_keep_innercap and is_innercap:
                pass
            else:
                if capacitor_require_all_pins_in_scope:
                    capacitor_ok = bool(pin_nets) and not bool(pin_nets - analysis_set)
                else:
                    capacitor_ok = bool(pin_nets & analysis_set)
                keep_it = bool(keep_it and capacitor_ok)

        if keep_it:
            kept += 1
            keep_reason[comp_name_str] = sorted(set(reasons))
            continue

        disabled_ok = False
        disable_reason = ""
        if disable_only or str(getattr(comp_inst, "type", "")) not in delete_types:
            try:
                cmp_prop = comp_inst.component_property.Clone()
                if hasattr(cmp_prop, "SetEnabled"):
                    cmp_prop.SetEnabled(False)
                    comp_inst.edbcomponent.SetComponentProperty(cmp_prop)
                    disabled_ok = True
                    disabled += 1
                else:
                    disable_reason = f"unsupported:{type(cmp_prop).__name__}"
            except Exception as disable_exc:
                disable_reason = str(disable_exc)

            if (not disabled_ok) and delete_fallback:
                to_delete[comp_name_str] = comp_inst
                disable_skip.append((comp_name_str, str(getattr(comp_inst, "type", "")), disable_reason or "disable_failed"))
        else:
            to_delete[comp_name_str] = comp_inst

    deleted = 0
    delete_failed = 0
    for _, comp_inst in list(to_delete.items()):
        try:
            if str(getattr(comp_inst, "type", "")) in {"Other", "IO"}:
                comp_inst.delete()
            else:
                comp_inst.edbcomponent.Delete()
            deleted += 1
        except Exception:
            delete_failed += 1

    logger.log(
        f"[SCOPE] enabled=True, stage={stage or 'n/a'}, require_bom_match={require_bom_match}, analysis_nets={len(analysis_set)}, "
        f"resistor_policy={{enabled:{resistor_policy_enabled}, all_pins_in_scope:{resistor_require_all_pins_in_scope}}}, "
        f"capacitor_policy={{enabled:{capacitor_policy_enabled}, all_pins_in_scope:{capacitor_require_all_pins_in_scope}, keep_innercap:{capacitor_keep_innercap}}}, "
        f"processed={processed}, kept={kept}, disabled={disabled}, delete_queue={len(to_delete)}, deleted={deleted}, "
        f"delete_failed={delete_failed}, disable_skip={len(disable_skip)}",
        level=LogLevel.INFO,
    )
    for comp_name, comp_type, reason in disable_skip[:max(0, max_detail)]:
        logger.log(f"[SCOPE][SKIP] Disable unsupported -> queued delete: {comp_name} (type={comp_type}, reason={reason})", level=LogLevel.WARNING)
    extra = len(disable_skip) - max(0, max_detail)
    if extra > 0:
        logger.log(f"[SCOPE] ... {extra} more disable-skip entries omitted", level=LogLevel.WARNING)

    try:
        with open(output_dir / "component_scope_result.json", "w", encoding="utf-8") as f:
            json.dump({
                "enabled": True,
                "stage": stage,
                "require_bom_match": require_bom_match,
                "analysis_nets": sorted(analysis_set),
                "resistor_policy": {
                    "enabled": resistor_policy_enabled,
                    "designator_prefixes": list(resistor_prefixes),
                    "all_pins_in_scope": resistor_require_all_pins_in_scope,
                },
                "capacitor_policy": {
                    "enabled": capacitor_policy_enabled,
                    "designator_prefixes": list(capacitor_prefixes),
                    "all_pins_in_scope": capacitor_require_all_pins_in_scope,
                    "keep_innercap": capacitor_keep_innercap,
                },
                "processed": processed,
                "kept": kept,
                "disabled": disabled,
                "delete_queue": len(to_delete),
                "deleted": deleted,
                "delete_failed": delete_failed,
                "disable_skip": len(disable_skip),
                "kept_components": [{"component": n, "reasons": keep_reason.get(n, [])} for n in sorted(keep_reason.keys())],
            }, f, indent=2, ensure_ascii=False)
    except Exception as report_exc:
        logger.log(f"[SCOPE][WARNING] Failed to write scope report: {report_exc}", level=LogLevel.WARNING)

    return {
        "enabled": True,
        "stage": stage,
        "processed": processed,
        "kept": kept,
        "disabled": disabled,
        "deleted": deleted,
        "delete_failed": delete_failed,
        "disable_skip": len(disable_skip),
    }
def initialize_step5_runtime(
    *,
    stage: str,
    pre_edb_file_path: Path,
    aedt_version: str,
    logger,
    stackup_layer_count,
    conf_data,
    wait_for_edb_ready,
    resolve_solver_backend,
    siwave_cls,
    edb_setup_app,
):
    if not wait_for_edb_ready(pre_edb_file_path, timeout=300.0, check_interval=3.0):
        raise FileNotFoundError(f"Target EDB path or edb.def is not ready after retries: {pre_edb_file_path}")

    app = None
    image_app = None
    step5_backend = resolve_solver_backend(stackup_layer_count, conf_data, logger)
    if step5_backend == "siwave":
        app = siwave_cls(version=aedt_version, logger=logger)
        app.import_edb(str(pre_edb_file_path))
        edb_ops_app = edb_setup_app if (edb_setup_app and getattr(edb_setup_app, "edb", None)) else app
        if edb_ops_app is app and not getattr(app, "edb", None):
            app.set_cad_file(str(pre_edb_file_path))
    else:
        edb_ops_app = edb_setup_app if (edb_setup_app and getattr(edb_setup_app, "edb", None)) else None
        if not edb_ops_app:
            edb_ops_app = siwave_cls(version=aedt_version, logger=logger)
            edb_ops_app.set_cad_file(str(pre_edb_file_path))
            app = edb_ops_app

    return {
        "app": app,
        "image_app": image_app,
        "edb_ops_app": edb_ops_app,
        "step5_backend": step5_backend,
    }


def configure_step5_settings(
    *,
    app,
    edb_ops_app,
    step5_backend: str,
    logger,
    stackup_effective_file,
    stackup_input_file,
    stackup_applied_at_project_creation: bool,
    conf_data,
    stackup_layer_count,
    working_dir: Path,
    input_dir: Path,
    output_dir: Path,
    settings_data,
    bom_info,
    inner_cap_audit,
    gnd_net: str,
    pdn_cases_info,
    input_cad_file: Path,
    assign_sparameter_models_fn,
    configure_ports_and_vrms_fn,
    collect_analysis_nets_fn,
    classify_and_audit_analysis_nets_fn,
    sync_edb_changes_to_siw_project_fn,
    resolve_zparam_profile_fn,
    resolve_pdn_setup_asset_fn,
    apply_dynamic_frequency_setup_fn,
):
    log_signal_layer_thicknesses = conf_data.get("__log_signal_layer_thicknesses_fn__")
    if callable(log_signal_layer_thicknesses):
        log_signal_layer_thicknesses(edb_ops_app, logger, tag="[STACKUP][Step5]")

    pdn_logic = conf_data.get("__pdn_logic_factory__")(logger=logger) if callable(conf_data.get("__pdn_logic_factory__")) else None
    if pdn_logic and hasattr(pdn_logic, "apply_dc_shorts"):
        pdn_logic.apply_dc_shorts(
            app=edb_ops_app,
            shorted_comp_defs=conf_data["PDN"]["dcShort"]["shortedComp"],
            del_comps=conf_data.get("__DEL_COMP__"),
            short_correction=conf_data.get("__SHORT_CORRECTION__"),
        )
    else:
        logger.log(
            "[PDN][Short][WARNING] apply_dc_shorts() is not available in PDN class. Skip short replacement.",
            level=LogLevel.WARNING,
        )

    stackup_input = stackup_effective_file if stackup_effective_file else (Path(stackup_input_file) if stackup_input_file else None)
    profile_key, sws_name, sfsdf_name = resolve_zparam_profile_fn(
        conf_data,
        Path(stackup_input) if stackup_input else None,
        stackup_layer_count,
    )
    if step5_backend == "siwave" and stackup_input and not stackup_applied_at_project_creation:
        raw_stackup = Path(stackup_input)
        if app.import_layer_stackup(raw_stackup):
            logger.log(f"[PRE] Raw stackup imported: {raw_stackup}", level=LogLevel.DETAIL1)
        else:
            raise RuntimeError(f"[PRE] Failed to import raw stackup file: {raw_stackup}")
    elif step5_backend == "siwave" and stackup_applied_at_project_creation and stackup_input:
        logger.log(
            f"[PRE] Skip stackup re-import (already applied at create_project): {stackup_input}",
            level=LogLevel.DETAIL1,
        )

    if callable(resolve_pdn_setup_asset_fn):
        sws_file = resolve_pdn_setup_asset_fn(sws_name, working_dir, input_dir, logger)
    else:
        sws_file = working_dir / "core" / sws_name

    s2p_dir_conf = conf_data.get("PDN", {}).get("sParameter", {}).get("s2pDirectory", "")
    s2p_dir = Path(s2p_dir_conf) if s2p_dir_conf else None
    if s2p_dir and not s2p_dir.is_absolute():
        s2p_dir = input_dir / s2p_dir


    exclude_tokens = conf_data.get("PDN", {}).get("dcShort", {}).get("excludeNet", [])
    analysis_nets = collect_analysis_nets_fn(pdn_cases_info, gnd_net, exclude_tokens=exclude_tokens)
    mandatory_seed_nets = conf_data.get("PDN", {}).get("netCoverage", {}).get("mandatorySeeds", [])
    analysis_nets = sorted({*analysis_nets, *mandatory_seed_nets})

    # Inner capacitors are stored as paired RLC terminals, not ordinary EDB
    # components. Verify the reopened Step-5 EDB before any model assignment or
    # solve setup so a stale bootstrap EDB cannot silently omit them.
    _validate_inner_cap_boundaries(
        edb_ops_app=edb_ops_app,
        inner_cap_audit=inner_cap_audit,
        output_dir=output_dir,
        logger=logger,
    )

    if conf_data.get("PDN", {}).get("sParameter", {}).get("enableAssign", True):
        innercap_name = settings_data.get("CAE", {}).get("SOC", {}).get("Inner_cap")
        innercap_csv_path = (input_dir / innercap_name) if innercap_name else None
        bom_name = settings_data.get("CAE", {}).get("PCB", {}).get("BOM")
        bom_file_path = (input_dir / bom_name) if bom_name else None
        model_lib_candidates = conf_data.get("PDN", {}).get("sParameter", {}).get("model_library_dir_candidates", [])
        resolved_candidates = []
        for cand in model_lib_candidates:
            if not cand:
                continue
            p = Path(cand)
            if p.is_absolute():
                resolved_candidates.append(p)
            else:
                # allow flexible relative roots (model/input, workspace root, and current cwd)
                resolved_candidates.append(input_dir / p)
                resolved_candidates.append(working_dir.parent / p)
                resolved_candidates.append(Path.cwd() / p)
        # de-duplicate while preserving order
        _uniq = []
        _seen = set()
        for rp in resolved_candidates:
            key = str(rp).lower()
            if key in _seen:
                continue
            _seen.add(key)
            _uniq.append(rp)
        resolved_candidates = _uniq
        assign_sparameter_models_fn(
            app=edb_ops_app,
            bom_info=bom_info,
            inner_cap_audit=inner_cap_audit,
            pmap_file=None,
            s2p_dir=s2p_dir,
            gnd_net=gnd_net,
            output_dir=output_dir,
            logger=logger,
            innercap_csv_path=innercap_csv_path,
            bom_file_path=bom_file_path,
            search_roots=[input_dir, working_dir] + resolved_candidates,
            analysis_nets=analysis_nets,
            model_aliases=conf_data.get("PDN", {}).get("sParameter", {}).get("modelAliases", {}),
            model_generation=resolve_generation_options(
                conf_data.get("PDN", {}).get("sParameter", {}).get("generation", {}),
                input_dir, resolved_candidates),
        )
    vrm_setup_conf = dict(conf_data.get("PDN", {}).get("vrmSetup", {}) or {})
    vrm_runtime = dict(vrm_setup_conf.get("__runtime__", {}) or {})
    vrm_runtime["solver_backend"] = step5_backend
    vrm_setup_conf["__runtime__"] = vrm_runtime
    vrm_setup_conf["__deferred_disable__"] = list(bom_info.get("__deferred_disable__", []) or [])
    vrm_records = configure_ports_and_vrms_fn(
        app=edb_ops_app,
        cases=pdn_cases_info,
        gnd_net=gnd_net,
        bulk_inductor_set=set(bom_info.get("bulkInd", [])) | set(bom_info.get("beadInd", [])),
        output_dir=output_dir,
        logger=logger,
        vrm_setup_conf=vrm_setup_conf,
        port_app=(app if step5_backend == "siwave" else None),
    )
    vrm_done = sum(1 for r in vrm_records if r.get("Status") == "Done")
    vrm_skipped = len(vrm_records) - vrm_done
    for case, record in zip(pdn_cases_info, vrm_records):
        ready = record.get("Status") == "Done"
        case["Case_Ready_For_Solve"] = ready
        case["Case_Precheck_Error"] = "" if ready else str(record.get("Message", "VRM setup failed"))
    logger.log(
        f"[VRM_SETUP] Summary: total={len(vrm_records)}, done={vrm_done}, skipped={vrm_skipped}",
        level=LogLevel.INFO,
    )
    if conf_data.get("__STAGE__") == "pre":
        logger.log(
            f"[PRE][CHECK] Port/VRM detail report: {output_dir / 'vrm_port_setup_result.json'}",
            level=LogLevel.INFO,
        )
    if vrm_done == 0:
        raise RuntimeError(
            f"Port/VRM setup failed for every case: verified=0/{len(pdn_cases_info)}."
        )
    if vrm_skipped:
        logger.log(
            f"[VRM_SETUP][PARTIAL] Continue valid cases: ready={vrm_done}, failed={vrm_skipped}",
            level=LogLevel.WARNING,
        )

    classify_and_audit_analysis_nets_fn(
        edb_ops_app,
        analysis_nets=analysis_nets,
        gnd_net=gnd_net,
        logger=logger,
        exclude_tokens=exclude_tokens,
    )

    _prune_components_for_analysis_scope(
        edb_ops_app=edb_ops_app,
        analysis_nets=analysis_nets,
        gnd_net=gnd_net,
        bom_info=bom_info,
        inner_cap_audit=inner_cap_audit,
        conf_data=conf_data,
        logger=logger,
        output_dir=output_dir,
    )

    if step5_backend == "siwave" and edb_ops_app is not app:
        base_cad_name = input_cad_file.stem.split("-")[0]
        sync_edb_path = output_dir / f"{base_cad_name}_step5_sync.aedb"
        sync_edb_changes_to_siw_project_fn(
            source_app=edb_ops_app,
            target_app=app,
            sync_edb_path=sync_edb_path,
            logger=logger,
        )

    if step5_backend == "siwave":
        if not sfsdf_name or not callable(resolve_pdn_setup_asset_fn):
            raise RuntimeError("PDN frequency setup asset resolver or filename missing")
        sfsdf_file = resolve_pdn_setup_asset_fn(sfsdf_name, working_dir, input_dir, logger)
        app.setup_simulation(None, sws_file, sfsdf_file)
    else:
        logger.log(
            "[PRE] Skip SIwave setup import for AEDT cutout backend (avoid SIwave-only setup artifacts).",
            level=LogLevel.INFO,
        )

    return {"SWS_FILE": sws_file, "vrm_records": vrm_records, "profile_key": profile_key, "sfsdf_name": sfsdf_name}


def export_step5_design_artifacts(
    *,
    step5_backend: str,
    app,
    edb_ops_app,
    input_cad_file: Path,
    run_tag: str | None,
    output_dir: Path,
    siwave_file_path: Path,
    wait_for_edb_ready,
    create_siw_snapshot_from_edb_fn,
    aedt_version: str,
    logger,
    aedt_cls,
    inner_cap_audit=None,
):
    base_cad_name = input_cad_file.stem.split("-")[0]
    run_tag_value = run_tag or time.strftime("%Y%m%d_%H%M%S")
    final_edb_file_path = output_dir / f"{base_cad_name}_ref_{run_tag_value}.aedb"
    if step5_backend == "siwave":
        ref_siwave_file_path = siwave_file_path
        app.save_project_as(ref_siwave_file_path)
        if not ref_siwave_file_path.exists():
            raise FileNotFoundError(f"SIwave file was not created at {ref_siwave_file_path}")
        _validate_inner_caps_in_siw(
            siw_path=ref_siwave_file_path,
            inner_cap_audit=inner_cap_audit,
            output_dir=output_dir,
            logger=logger,
        )
        app.export_edb(final_edb_file_path)
    else:
        if final_edb_file_path.exists():
            shutil.rmtree(final_edb_file_path, ignore_errors=True)
        edb_ops_app.edb.save_as(str(final_edb_file_path))
        if not wait_for_edb_ready(final_edb_file_path, timeout=300.0, check_interval=3.0):
            raise FileNotFoundError(f"Final EDB was not created at {final_edb_file_path}")
        ref_siwave_file_path = siwave_file_path
        create_siw_snapshot_from_edb_fn(
            aedt_version=aedt_version,
            source_edb_path=final_edb_file_path,
            siw_output_path=ref_siwave_file_path,
            logger=logger,
        )

        try:
            aedt_full = aedt_cls(version=aedt_version, logger=logger)
            aedt_full.export_full_presolve_aedt(
                ref_edb_path=Path(final_edb_file_path),
                output_dir=output_dir,
                project_stem=base_cad_name,
            )
        except Exception as full_aedt_exc:
            logger.log(
                f"[AEDT][FULL][WARNING] Failed to export full pre-solve AEDT project: {full_aedt_exc}",
                level=LogLevel.WARNING,
            )

    return {"FINAL_EDB_FILE_PATH": final_edb_file_path, "REF_SIwave_FILE_PATH": ref_siwave_file_path}


def export_step5_preview_images(
    *,
    step5_backend: str,
    aedt_version: str,
    logger,
    final_edb_file_path: Path,
    ref_siwave_file_path: Path,
    output_dir: Path,
    gnd_net: str,
    siwave_cls,
    aedt_cls,
    safe_close_edb_session_fn,
):
    # Keep Top/Bottom capture quality aligned with legacy DCIR report pipeline:
    # prefer SIwave ScrSaveToPngFile capture for both backends.
    if step5_backend == "siwave":
        image_app = None
        try:
            image_app = siwave_cls(version=aedt_version, logger=logger)
            image_app.set_cad_file(str(final_edb_file_path))
            image_app.export_layer_images(ref_siwave_file_path, output_dir, gnd_net)
            image_app.close_edb()
        finally:
            if image_app:
                safe_close_edb_session_fn(image_app, logger, "step5-image-export")
                image_app.quit_application()
    else:
        image_app = None
        try:
            image_app = siwave_cls(version=aedt_version, logger=logger)
            image_app.set_cad_file(str(final_edb_file_path))
            image_app.export_layer_images(ref_siwave_file_path, output_dir, gnd_net)
            image_app.close_edb()
            logger.log("[AEDT][IMG] SIwave capture mode applied for top/btm images.", level=LogLevel.INFO)
        except Exception as img_exc:
            logger.log(
                f"[AEDT][IMG][WARNING] SIwave capture failed. Fallback to offline EDB renderer: {img_exc}",
                level=LogLevel.WARNING,
            )
            try:
                aedt_image = aedt_cls(version=aedt_version, logger=logger)
                aedt_image.export_edb_preview_images(
                    ref_edb_path=Path(final_edb_file_path),
                    output_dir=output_dir,
                )
                logger.log("[AEDT][IMG] Offline EDB preview fallback succeeded.", level=LogLevel.INFO)
            except Exception as fallback_exc:
                logger.log(f"[AEDT][IMG][WARNING] Offline EDB preview fallback failed: {fallback_exc}", level=LogLevel.WARNING)
        finally:
            try:
                if "image_app" in locals() and image_app:
                    safe_close_edb_session_fn(image_app, logger, "step5-image-export")
                    image_app.quit_application()
            except Exception:
                pass


def emit_step5_pre_stage_records(
    *,
    stage: str,
    resolve_solver_backend_fn,
    stackup_layer_count,
    conf_data,
    logger,
    ref_siwave_file_path: Path | None,
    siwave_file_path: Path,
    final_edb_file_path: Path | None,
    pre_edb_file_path: Path,
    pdn_cases_info,
    build_preprocessing_record_fn,
    gnd_net: str,
    output_dir: Path,
):
    if stage != "pre":
        return
    try:
        pre_solver_backend = resolve_solver_backend_fn(stackup_layer_count, conf_data, logger)
    except Exception:
        pre_solver_backend = "siwave"

    pre_project_path = ref_siwave_file_path if ref_siwave_file_path else siwave_file_path
    pre_project_path = Path(pre_project_path)
    pre_edb_dir = Path(final_edb_file_path) if final_edb_file_path else Path(pre_edb_file_path)

    pre_records = []
    for idx, case in enumerate(pdn_cases_info):
        net = str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))))
        ic = str(case.get("IC", ""))
        safe_ic = "".join(c for c in ic if c.isalnum() or c == "_")
        safe_net = "".join(c for c in net if c.isalnum() or c == "_")
        pre_records.append(
            build_preprocessing_record_fn(
                case=case,
                idx=idx,
                net_siw_file=pre_project_path,
                net_edb_dir=pre_edb_dir,
                v_port_name=f"V_{safe_ic}_{safe_net}",
                i_port_name=f"I_{safe_ic}_{safe_net}",
                gnd_net=gnd_net,
                solver_backend=pre_solver_backend,
            )
        )

    with open(output_dir / "preprocessing_result.json", "w", encoding="utf-8") as f:
        json.dump(pre_records, f, indent=4, ensure_ascii=False)
    logger.log(
        f"[PRE] Exported preprocessing result to: {output_dir / 'preprocessing_result.json'}",
        level=LogLevel.INFO,
    )
    logger.log(
        f"[PRE][CHECK] Pin mapping report: {output_dir / 'spec_to_edb_pin_map.json'}",
        level=LogLevel.INFO,
    )
    logger.log(
        f"[PRE][CHECK] EDB truth table: {output_dir / 'edb_truth_table.csv'}",
        level=LogLevel.INFO,
    )
    logger.log(
        f"[PRE][CHECK] Final setting EDB: {final_edb_file_path}",
        level=LogLevel.INFO,
    )
    logger.log(
        f"[PRE][CHECK] Final setting SIW: {ref_siwave_file_path}",
        level=LogLevel.INFO,
    )
    logger.log(
        "[PRE] Stage pre completed at Setting phase (Port/VRM + setup). Solve and report stages are skipped by design.",
        level=LogLevel.INFO,
    )


