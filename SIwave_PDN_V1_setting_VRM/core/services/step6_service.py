from pathlib import Path
import json

from core.logger import LogLevel


def prepare_step6_runtime(
    *,
    working_dir: Path,
    input_cad_file: Path,
    output_dir: Path,
    final_edb_file_path,
    pre_edb_file_path,
    stackup_layer_count,
    conf_data,
    logger,
    resolve_solver_backend_fn,
):
    model_name = input_cad_file.stem.split("-")[0]
    pdn_setup_conf = conf_data.get("PDN", {}).get("setup", {})
    z_exec_name = str(pdn_setup_conf.get("zExecFile", "PDN.exec")).strip() or "PDN.exec"
    exec_file = working_dir / "core" / z_exec_name
    if not exec_file.exists():
        logger.log(
            f"[WARNING] Z solve exec not found: {exec_file}. Fallback to DC exec.",
            level=LogLevel.WARNING,
        )
        exec_file = working_dir / "core" / "PDN.exec"

    runtime_edb_path = final_edb_file_path if (final_edb_file_path and Path(final_edb_file_path).exists()) else pre_edb_file_path
    logger.log(f"[UNIFIED] Runtime EDB selected: {runtime_edb_path}", level=LogLevel.DETAIL1)
    if not runtime_edb_path or not Path(runtime_edb_path).exists():
        raise FileNotFoundError(f"Runtime EDB not found: {runtime_edb_path}")

    solver_backend = resolve_solver_backend_fn(stackup_layer_count, conf_data, logger)
    if solver_backend not in {"siwave", "aedt_cutout"}:
        raise ValueError(f"Unsupported solver backend: {solver_backend}")

    return {
        "model_name": model_name,
        "exec_file": exec_file,
        "runtime_edb_path": Path(runtime_edb_path),
        "solver_backend": solver_backend,
    }


def run_step6_solver(
    *,
    solver_backend: str,
    pdn_cases_info,
    model_name: str,
    output_dir: Path,
    ref_siwave_file_path: Path,
    runtime_edb_path: Path,
    gnd_net: str,
    aedt_version: str,
    conf_data,
    bom_info,
    logger,
    edb_setup_app,
    app,
    siwave_cls,
    resolve_siwave_executable_fn,
    run_pdn_unified_fn,
    run_pdn_aedt_cutout_solve_fn,
    build_preprocessing_record_fn,
    exec_file: Path,
):
    preprocessing_data = []
    app_ref = app

    if solver_backend == "siwave":
        siw_execute_file = resolve_siwave_executable_fn(aedt_version)
        case_data_app = edb_setup_app if edb_setup_app else app_ref
        if not case_data_app:
            app_ref = siwave_cls(version=aedt_version, logger=logger)
            app_ref.set_cad_file(str(runtime_edb_path))
            case_data_app = app_ref

        signal_layers = list(case_data_app.edb.stackup.signal_layers.keys())
        preprocessing_data = run_pdn_unified_fn(
            cases=pdn_cases_info,
            model_name=model_name,
            output_dir=output_dir,
            ref_siwave_file_path=ref_siwave_file_path,
            ref_edb_path=runtime_edb_path,
            gnd_net=gnd_net,
            aedt_version=aedt_version,
            case_data_app=case_data_app,
            signal_layers=signal_layers,
            conf_data=conf_data,
            siw_execute_file=siw_execute_file,
            exec_file=exec_file,
            bulk_inductor_list=bom_info.get("bulkInd", []),
            run_solve=True,
        )
    elif solver_backend == "aedt_cutout":
        full_siw_file = (output_dir / f"{model_name}_PDN_FULL.siw").resolve()
        for idx, case in enumerate(pdn_cases_info):
            net = str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))))
            ic = str(case.get("IC", ""))
            safe_ic = "".join(c for c in ic if c.isalnum() or c == "_")
            safe_net = "".join(c for c in net if c.isalnum() or c == "_")
            preprocessing_data.append(
                build_preprocessing_record_fn(
                    case=case,
                    idx=idx,
                    net_siw_file=full_siw_file,
                    net_edb_dir=Path(runtime_edb_path),
                    v_port_name=f"V_{safe_ic}_{safe_net}",
                    i_port_name=f"I_{safe_ic}_{safe_net}",
                    gnd_net=gnd_net,
                    solver_backend="aedt_cutout",
                )
            )
        run_pdn_aedt_cutout_solve_fn(
            cases=pdn_cases_info,
            model_name=model_name,
            ref_edb_path=Path(runtime_edb_path),
            output_dir=output_dir,
            aedt_version=aedt_version,
            conf_data=conf_data,
            logger=logger,
        )

    return {"preprocessing_data": preprocessing_data, "app": app_ref}


def write_step6_preprocessing_result(*, output_dir: Path, preprocessing_data, logger):
    with open(output_dir / "preprocessing_result.json", "w", encoding="utf-8") as f:
        json.dump(preprocessing_data, f, indent=4, ensure_ascii=False)
    logger.log(f"Exported preprocessing result to: {output_dir / 'preprocessing_result.json'}", level=LogLevel.DETAIL1)
