from pathlib import Path
import json
import shutil
import time
from core.logger import LogLevel


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

    sws_file = working_dir / "core" / sws_name

    s2p_dir_conf = conf_data.get("PDN", {}).get("sParameter", {}).get("s2pDirectory", "")
    s2p_dir = Path(s2p_dir_conf) if s2p_dir_conf else None
    if s2p_dir and not s2p_dir.is_absolute():
        s2p_dir = input_dir / s2p_dir

    if conf_data.get("PDN", {}).get("sParameter", {}).get("enableAssign", True):
        innercap_name = settings_data.get("CAE", {}).get("SOC", {}).get("Inner_cap")
        innercap_csv_path = (input_dir / innercap_name) if innercap_name else None
        bom_name = settings_data.get("CAE", {}).get("PCB", {}).get("BOM")
        bom_file_path = (input_dir / bom_name) if bom_name else None
        model_lib_candidates = conf_data.get("PDN", {}).get("sParameter", {}).get("model_library_dir_candidates", [])
        resolved_candidates = [Path(cand) if Path(cand).is_absolute() else (input_dir / Path(cand)) for cand in model_lib_candidates if cand]
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
            search_roots=[input_dir, working_dir, output_dir] + resolved_candidates,
        )

    vrm_setup_conf = conf_data.get("PDN", {}).get("vrmSetup", {})
    vrm_records = configure_ports_and_vrms_fn(
        app=edb_ops_app,
        cases=pdn_cases_info,
        gnd_net=gnd_net,
        bulk_inductor_set=set(bom_info.get("bulkInd", [])),
        output_dir=output_dir,
        logger=logger,
        vrm_setup_conf=vrm_setup_conf,
        port_app=(app if step5_backend == "siwave" else None),
    )
    vrm_done = sum(1 for r in vrm_records if r.get("Status") == "Done")
    vrm_skipped = len(vrm_records) - vrm_done
    logger.log(
        f"[VRM_SETUP] Summary: total={len(vrm_records)}, done={vrm_done}, skipped={vrm_skipped}",
        level=LogLevel.INFO,
    )
    if conf_data.get("__STAGE__") == "pre":
        logger.log(
            f"[PRE][CHECK] Port/VRM detail report: {output_dir / 'vrm_port_setup_result.json'}",
            level=LogLevel.INFO,
        )
    if vrm_done == 0 and len(pdn_cases_info) > 0:
        logger.log(
            "[VRM_SETUP][WARNING] No Port/VRM termination was created. Solver may fail with no terminals/sources.",
            level=LogLevel.WARNING,
        )
    exclude_tokens = conf_data.get("PDN", {}).get("dcShort", {}).get("excludeNet", [])
    analysis_nets = collect_analysis_nets_fn(pdn_cases_info, gnd_net, exclude_tokens=exclude_tokens)
    classify_and_audit_analysis_nets_fn(
        edb_ops_app,
        analysis_nets=analysis_nets,
        gnd_net=gnd_net,
        logger=logger,
        exclude_tokens=exclude_tokens,
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
        app.setup_simulation(None, sws_file, None)
        apply_dynamic_frequency_setup_fn(app, stackup_layer_count, conf_data, logger)
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
):
    base_cad_name = input_cad_file.stem.split("-")[0]
    run_tag_value = run_tag or time.strftime("%Y%m%d_%H%M%S")
    final_edb_file_path = output_dir / f"{base_cad_name}_ref_{run_tag_value}.aedb"
    if step5_backend == "siwave":
        ref_siwave_file_path = siwave_file_path
        app.save_project_as(ref_siwave_file_path)
        if not ref_siwave_file_path.exists():
            raise FileNotFoundError(f"SIwave file was not created at {ref_siwave_file_path}")
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
        try:
            aedt_image = aedt_cls(version=aedt_version, logger=logger)
            aedt_image.export_edb_preview_images(
                ref_edb_path=Path(final_edb_file_path),
                output_dir=output_dir,
            )
        except Exception as img_exc:
            logger.log(
                f"[AEDT][IMG][WARNING] Preview image export failed but workflow will continue: {img_exc}",
                level=LogLevel.WARNING,
            )
            try:
                image_app = siwave_cls(version=aedt_version, logger=logger)
                image_app.set_cad_file(str(final_edb_file_path))
                image_app.export_layer_images(ref_siwave_file_path, output_dir, gnd_net)
                image_app.close_edb()
                logger.log("[AEDT][IMG] Fallback SIwave layer image export succeeded.", level=LogLevel.INFO)
            except Exception as fallback_exc:
                logger.log(f"[AEDT][IMG][WARNING] Fallback SIwave image export failed: {fallback_exc}", level=LogLevel.WARNING)
            finally:
                try:
                    if "image_app" in locals() and image_app:
                        safe_close_edb_session_fn(image_app, logger, "step5-image-fallback")
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
