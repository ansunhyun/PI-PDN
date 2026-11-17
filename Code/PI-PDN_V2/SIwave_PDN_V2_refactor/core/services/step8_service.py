import traceback

from core.database import ErrorCode, _message
from core.logger import LogLevel


def run_step8_post_processing(
    *,
    stage: str,
    step: int,
    solver_backend_used: str,
    output_dir,
    logger,
    conf_manager,
    input_json,
    start_time,
    end_time,
    export_aedt_cutout_post_reports_fn,
    run_standalone_post_fn,
):
    if stage == "pre":
        logger.log(f"Step {step}. Post-processing skipped (stage=pre)", level=LogLevel.INFO)
        return
    if solver_backend_used == "aedt_cutout":
        try:
            logger.log(
                f"Step {step}. Post-processing : Export AEDT cutout summary artifacts",
                level=LogLevel.INFO,
            )
            export_aedt_cutout_post_reports_fn(output_dir, logger)
            logger.log(
                "Full completed for AEDT cutout backend.",
                level=LogLevel.DETAIL1,
            )
        except Exception as exc:
            logger.fatal(f"An error occurred while exporting AEDT post artifacts : {traceback.format_exc()}")
            _message(ErrorCode.PDN_RESULT_NOT_FOUND, output_dir, f"AEDT Post: {exc}")
            raise SystemExit(1)
        return
    if solver_backend_used == "siwave":
        try:
            logger.log(f"Step {step}. Post-processing : Extracting PDN results", level=LogLevel.INFO)
            post_state = run_standalone_post_fn(
                conf_manager,
                input_json,
                output_dir,
                analysis_start=start_time,
                analysis_end=end_time,
            )
            summary = list(getattr(post_state, "summary", []) or [])
            complete_count = sum(1 for case in summary if case.get("is_done"))
            total_count = len(summary)
            if total_count > 0 and complete_count == total_count:
                logger.log(
                    f"Full completed: {complete_count}/{total_count} cases",
                    level=LogLevel.DETAIL1,
                )
            else:
                logger.log(
                    f"Full incomplete: {complete_count}/{total_count} cases",
                    level=LogLevel.WARNING,
                )
        except Exception as exc:
            logger.fatal(f"An error occurred while performing PDN results extracting : {traceback.format_exc()}")
            _message(ErrorCode.PDN_RESULT_NOT_FOUND, output_dir, f"SIwave Post: {exc}")
            raise SystemExit(1)
        return
    logger.fatal(f"Unsupported solver backend at post stage: {solver_backend_used}")
    _message(
        ErrorCode.PDN_RESULT_NOT_FOUND,
        output_dir,
        f"Unsupported solver backend: {solver_backend_used}",
    )
    raise SystemExit(1)
