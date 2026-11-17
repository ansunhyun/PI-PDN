import zipfile
import traceback
from pathlib import Path

from core.database import ErrorCode, PDNSessionException
from core.logger import LogLevel


def prepare_ecad_data(
    *,
    step: int,
    logger,
    input_dir: Path,
    output_dir: Path,
    working_dir: Path,
    settings_manager,
    input_valchk,
    conf_manager,
    aedt_version: str,
    resolve_stackup_for_project,
    resolve_temp_preconverted_files,
    is_edb_stackup_valid_for_solve,
    run_external_tool,
    resolve_or_create_anf_cmp,
    build_edb_via_create_project,
    discover_cmp_file,
    discover_ndf_file,
):
    try:
        logger.log(f"Step {step}. Get ECAD Data", level=LogLevel.INFO)
        input_cad_file = input_dir / settings_manager.data["CAE"]["PCB"]["cadFile"]
        edb_file_path = None
        cmp_file_path = None
        stackup_input_file = input_valchk._default_inputFiles.get("Stackup") if input_valchk else None
        stackup_effective_file = resolve_stackup_for_project(input_valchk, input_dir, working_dir, logger)
        stackup_applied_at_project_creation = False

        if input_cad_file.suffix == ".zip":
            with zipfile.ZipFile(input_cad_file, "r") as zip_ref:
                zip_ref.extractall(input_cad_file.parent)
            if conf_manager.data["PDN"]["isZuken"]:
                temp_preconv = resolve_temp_preconverted_files(input_cad_file, logger)
                if temp_preconv:
                    dsgn_file = temp_preconv.get("DSGN_FILE")
                    edb_file_path = temp_preconv.get("EDB_FILE_PATH")
                    if edb_file_path and Path(edb_file_path).exists():
                        valid_preconv, preconv_thickness = is_edb_stackup_valid_for_solve(
                            Path(edb_file_path),
                            aedt_version,
                            logger,
                        )
                        logger.log(
                            f"[STACKUP][CHECK] Preconverted EDB thickness map: {preconv_thickness}",
                            level=LogLevel.DETAIL1,
                        )
                        if not valid_preconv:
                            logger.log(
                                "[STACKUP][CHECK] Preconverted EDB stackup is invalid. Force rebuild via create_project(ANF/CMP/STK).",
                                level=LogLevel.WARNING,
                            )
                            edb_file_path = None
                            temp_preconv = None
                else:
                    dsgn_file = None

                zuken_bin_dir = Path(conf_manager.data["PDN"]["DF_path"])
                pcb_files = list(input_cad_file.parent.glob("*.pcb"))
                if len(pcb_files) == 1:
                    pcb_file = pcb_files[0]
                else:
                    raise PDNSessionException(ErrorCode.INVALID_PCB_FILE_NUM, pcb_files)

                if not temp_preconv:
                    cr5_exec = zuken_bin_dir / "DFevolv.cr5.exe"
                    dsgn_candidate = pcb_file.with_suffix(".dsgn")
                    if dsgn_candidate.exists():
                        dsgn_file = dsgn_candidate
                        logger.log(
                            f"[PRE] Reuse existing DSGN for rebuild path: {dsgn_file}",
                            level=LogLevel.DETAIL1,
                        )
                    else:
                        result = run_external_tool([str(cr5_exec), str(pcb_file.parent)], "DFevolv.cr5.exe")
                        if result.returncode:
                            raise PDNSessionException(ErrorCode.CONVERT_PCB_TO_DSGN_FAIL, result.returncode)
                        dsgn_file = dsgn_candidate

                if edb_file_path is None:
                    base_name_for_project = input_cad_file.stem.split("-")[0]
                    use_create_project_flow = bool(
                        dsgn_file and stackup_effective_file and Path(stackup_effective_file).exists()
                    )
                    if use_create_project_flow:
                        anf_file, cmp_file = resolve_or_create_anf_cmp(Path(dsgn_file), zuken_bin_dir, logger)
                        _, edb_file_path = build_edb_via_create_project(
                            aedt_version=aedt_version,
                            anf_file=Path(anf_file),
                            cmp_file=Path(cmp_file),
                            stackup_file=Path(stackup_effective_file),
                            output_dir=output_dir,
                            base_name=base_name_for_project,
                            logger=logger,
                        )
                        stackup_applied_at_project_creation = True
                    else:
                        dsgn2edb_exec = zuken_bin_dir / "DFaedbout.exe"
                        edb_file_path = dsgn_file.with_suffix(".aedb")
                        result = run_external_tool(
                            [str(dsgn2edb_exec), "-r", str(dsgn_file), "-o", str(edb_file_path)],
                            "DFaedbout.exe",
                        )
                        if result.returncode:
                            raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_EDB_FAIL, result.returncode)
            else:
                edb_file_path = input_cad_file.with_suffix(".aedb")
        else:
            raise PDNSessionException(ErrorCode.INVALID_CAD_FILE, input_cad_file)

        base_name = input_cad_file.stem.split("-")[0]
        cmp_file_path = discover_cmp_file(input_dir, base_name)
        ndf_file_path = discover_ndf_file(input_dir, base_name)
        if cmp_file_path:
            logger.log(f"[SPEC] CMP source selected for pin mapping: {cmp_file_path}", level=LogLevel.DETAIL1)
        else:
            logger.log(
                "[SPEC][WARNING] CMP file not found. Spec->EDB pin coordinate fallback is disabled.",
                level=LogLevel.WARNING,
            )
        if ndf_file_path:
            logger.log(f"[SPEC] NDF source selected for pin/net crosswalk: {ndf_file_path}", level=LogLevel.DETAIL1)
        else:
            logger.log(
                "[SPEC][WARNING] NDF file not found. Spec pin/net crosswalk fallback is disabled.",
                level=LogLevel.WARNING,
            )

        siwave_file_path = output_dir / f"{base_name}_ready_for_solve.siw"

        return {
            "step": step + 1,
            "INPUT_CAD_FILE": input_cad_file,
            "EDB_FILE_PATH": edb_file_path,
            "CMP_FILE_PATH": cmp_file_path,
            "NDF_FILE_PATH": ndf_file_path,
            "STACKUP_INPUT_FILE": stackup_input_file,
            "STACKUP_EFFECTIVE_FILE": stackup_effective_file,
            "STACKUP_APPLIED_AT_PROJECT_CREATION": stackup_applied_at_project_creation,
            "SIwave_FILE_PATH": siwave_file_path,
            "base_name": base_name,
        }
    except Exception:
        logger.fatal(f"An error occurred while getting CAD database: {traceback.format_exc()}")
        raise PDNSessionException(ErrorCode.CAD_IMPORT_FAIL)
