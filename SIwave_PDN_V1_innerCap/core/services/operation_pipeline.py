import json
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from EBU_lib.DCIR import DCIR
from EBU_lib.SIwave import SIwave
from core.logger import LogLevel
from core.post_stage import PostStageError


def handle_pre_stage_exit(
    *,
    stage: str,
    step: int,
    input_valchk: Any,
    input_json: Path,
    output_dir: Path,
    pre_edb_file_path: Path,
    dcir_cases_info: list[dict[str, Any]],
    inner_cap_audit: list[dict[str, Any]],
    logger: Any,
    export_pre_stage_reports: Callable[..., None],
) -> tuple[bool, str | None]:
    if stage != "pre":
        return False, None
    try:
        logger.log(
            f"Step {step}. PRE report export (DCIR setup and simulation steps are skipped by design)",
            level=LogLevel.INFO,
        )
        spec_file_for_report = Path(input_valchk._default_inputFiles["Spec"]) if input_valchk else input_json
        export_pre_stage_reports(
            output_dir,
            spec_file_for_report,
            pre_edb_file_path,
            dcir_cases_info,
            inner_cap_audit,
            logger,
        )
    except Exception:
        logger.fatal(f"Failed to export pre-stage reports: {traceback.format_exc()}")
        raise SystemExit(1)
    return True, time.strftime("%Y.%m.%d, %H:%M:%S")


def run_step5_cad_modification(
    *,
    step: int,
    pre_edb_file_path: Path,
    edb_file_path: Path,
    aedt_version: str,
    logger: Any,
    conf_manager: Any,
    del_comp: Any,
    short_correction: Any,
    input_dir: Path,
    settings_manager: Any,
    working_dir: Path,
    siwave_file_path: Path,
    input_cad_file: Path,
    output_dir: Path,
    gnd_net: str,
    wait_for_edb_ready: Callable[..., bool],
    ensure_pre_edb_saved: Callable[..., None],
    is_edb_ready: Callable[[Path], bool],
) -> tuple[int, Path | None, Path | None]:
    ref_siwave_file_path = None
    final_edb_file_path = None
    try:
        logger.log(f"Step {step}. CAD Modification", level=LogLevel.INFO)

        logger.log("Waiting for EDB file I/O completion...", level=LogLevel.DETAIL2)
        max_wait_time = 300.0
        if not wait_for_edb_ready(pre_edb_file_path, timeout=max_wait_time, check_interval=3.0):
            logger.log(
                "[WARNING] PRE EDB not ready in time. Trying one recovery save from source EDB...",
                level=LogLevel.WARNING,
            )
            recovery_app = None
            try:
                recovery_app = SIwave(version=aedt_version, logger=logger)
                recovery_app.set_cad_file(edb_file_path)
                ensure_pre_edb_saved(
                    app=recovery_app,
                    source_edb_path=edb_file_path,
                    pre_edb_path=pre_edb_file_path,
                    max_retries=2,
                    timeout=max_wait_time,
                )
            finally:
                if recovery_app:
                    recovery_app.quit_application()

        if not is_edb_ready(pre_edb_file_path):
            raise FileNotFoundError(
                f"Target EDB path or edb.def is not ready after retries: {pre_edb_file_path}"
            )

        app = None
        image_app = None
        try:
            app = SIwave(version=aedt_version, logger=logger)
            time.sleep(3.0)

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.log(
                        f"Importing EDB to SIwave (Attempt {attempt + 1}/{max_retries}): {pre_edb_file_path.name}",
                        level=LogLevel.DETAIL1,
                    )
                    app.import_edb(str(pre_edb_file_path))
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.log(f"[WARNING] Failed to import EDB. Retrying in 5 seconds... ({e})", level=LogLevel.WARNING)
                        time.sleep(5.0)
                    else:
                        logger.log(f"[ERROR] Failed to import EDB after {max_retries} attempts.", level=LogLevel.ERROR)
                        raise

            dcir_logic = DCIR(logger=logger)
            dcir_logic.apply_dc_shorts(
                app=app,
                shorted_comp_defs=conf_manager.data["DCIR"]["dcShort"]["shortedComp"],
                del_comps=del_comp,
                short_correction=short_correction,
            )

            pmap_file = input_dir / settings_manager.data["CAE"]["PCB"]["Pmap"] if settings_manager.data["CAE"]["PCB"]["Pmap"] else None
            sws_file = working_dir / "core" / conf_manager.data["DCIR"]["sws"]
            app.setup_simulation(pmap_file, sws_file)

            ref_siwave_file_path = siwave_file_path.parent / f"{siwave_file_path.stem}_ref{siwave_file_path.suffix}"
            app.save_project_as(ref_siwave_file_path)

            base_cad_name = input_cad_file.stem.split("-")[0]
            final_edb_file_path = output_dir / f"{base_cad_name}_ref.aedb"
            app.export_edb(final_edb_file_path)
        finally:
            if app:
                app.quit_application()

        try:
            image_app = SIwave(version=aedt_version, logger=logger)
            image_app.set_cad_file(str(final_edb_file_path))
            image_app.export_layer_images(ref_siwave_file_path, output_dir, gnd_net)
            image_app.close_edb()
        finally:
            if image_app:
                image_app.quit_application()

        step += 1
    except Exception:
        logger.fatal(f"An error occurred while CAD modification process : {traceback.format_exc()}")
    return step, ref_siwave_file_path, final_edb_file_path


def run_step6_simulation(
    *,
    step: int,
    logger: Any,
    input_cad_file: Path,
    aedt_version: str,
    pre_edb_file_path: Path,
    working_dir: Path,
    dcir_cases_info: list[dict[str, Any]],
    mode: int,
    output_dir: Path,
    ref_siwave_file_path: Path | None,
    gnd_net: str,
    conf_manager: Any,
    bom_info: dict[str, Any],
    stage: str,
    run_dcir_case: Callable[..., dict[str, Any] | None],
    resolve_siwave_executable: Callable[[str], Path],
    edb_file_path: Path,
) -> tuple[int, str]:
    app = None
    try:
        logger.log(f"Step {step}. Generate Files and Run DCIR Simulation", level=LogLevel.INFO)
        preprocessing_data = []
        model_name = input_cad_file.stem.split("-")[0]

        app = SIwave(version=aedt_version, logger=logger)
        app.set_cad_file(str(pre_edb_file_path))
        signal_layers = list(app.edb.stackup.signal_layers.keys())

        siw_execute_file = resolve_siwave_executable(aedt_version)
        exec_file = working_dir / "core" / "DCIR.exec"

        for idx, case in enumerate(dcir_cases_info):
            case_record = run_dcir_case(
                case=case,
                idx=idx,
                total_cases=len(dcir_cases_info),
                mode=mode,
                model_name=model_name,
                output_dir=output_dir,
                ref_siwave_file_path=ref_siwave_file_path,
                gnd_net=gnd_net,
                aedt_version=aedt_version,
                case_data_app=app,
                signal_layers=signal_layers,
                conf_data=conf_manager.data,
                siw_execute_file=siw_execute_file,
                exec_file=exec_file,
                bulk_inductor_list=bom_info.get("bulkInd", []),
                run_solve=(stage != "pre"),
            )
            if case_record:
                preprocessing_data.append(case_record)

        app.close_edb()

        with open(output_dir / "preprocessing_result.json", "w", encoding="utf-8") as f:
            json.dump(preprocessing_data, f, indent=4, ensure_ascii=False)
        logger.log(f"Exported preprocessing result to: {output_dir / 'preprocessing_result.json'}", level=LogLevel.DETAIL1)

        try:
            if edb_file_path.exists():
                shutil.rmtree(edb_file_path)
            if pre_edb_file_path.exists():
                shutil.rmtree(pre_edb_file_path)
            logger.log("Cleaned up intermediate EDB files to save disk space.", level=LogLevel.DETAIL1)
        except Exception as e:
            logger.log(f"Failed to clean up intermediate files: {e}", level=LogLevel.WARNING)

        step += 1
    except Exception:
        logger.fatal(f"An error occurred while generating files and running simulation : {traceback.format_exc()}")
    finally:
        if app:
            app.quit_application()
    return step, time.strftime("%Y.%m.%d, %H:%M:%S")


def run_step8_post_processing(
    *,
    stage: str,
    step: int,
    logger: Any,
    conf_manager: Any,
    input_json: Path,
    output_dir: Path,
    analysis_start: str | None,
    analysis_end: str | None,
    run_standalone_post: Callable[..., Any],
) -> None:
    if stage == "pre":
        logger.log(f"Step {step}. Post-processing skipped (stage=pre)", level=LogLevel.INFO)
        return
    try:
        logger.log(f"Step {step}. Post-processing : Extracting DCIR results", level=LogLevel.INFO)
        full_state = run_standalone_post(
            conf_manager,
            input_json,
            output_dir,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
        )
        complete_count = sum(1 for case in full_state.summary if case.get("is_done"))
        if complete_count == 0:
            raise PostStageError(
                f"FullBatch Post failed: no completed result was detected "
                f"(0/{len(full_state.summary)} cases)"
            )
    except Exception:
        logger.fatal(f"An error occurred while performing DCIR results extracting : {traceback.format_exc()}")
