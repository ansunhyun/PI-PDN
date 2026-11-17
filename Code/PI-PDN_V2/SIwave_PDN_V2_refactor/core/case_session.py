from __future__ import annotations

import time
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path


def detect_system_cpu_counts(cpu_count_fn=None):
    """Return physical/logical CPU counts without treating SMT threads as cores."""
    if cpu_count_fn is None:
        import psutil
        cpu_count_fn = psutil.cpu_count
    logical = max(1, int(cpu_count_fn(logical=True) or 1))
    physical = cpu_count_fn(logical=False)
    if physical:
        return {
            "physical": max(1, int(physical)),
            "logical": logical,
            "source": "physical",
        }
    return {
        "physical": max(1, logical // 2),
        "logical": logical,
        "source": "half_logical_fallback",
    }


def resolve_solver_cpu_count(physical_cpus: int, config=None) -> int:
    """Resolve a high-throughput physical-core count with OS headroom."""
    physical_cpus = max(1, int(physical_cpus or 1))
    config = dict(config or {})
    policy = str(config.get("corePolicy", "safe_max") or "safe_max").strip().lower()
    if policy == "fixed":
        requested = max(1, int(config.get("coreCount", 1) or 1))
        return min(requested, physical_cpus)

    utilization = float(config.get("maxUtilizationPercent", 90) or 90)
    utilization = min(100.0, max(1.0, utilization))
    reserve = max(0, int(config.get(
        "reservePhysicalCores", config.get("reserveLogicalCores", 2)
    ) or 0))
    by_ratio = max(1, int(physical_cpus * utilization / 100.0))
    by_reserve = max(1, physical_cpus - reserve)
    return min(physical_cpus, by_ratio, by_reserve)


def save_isolated_case_snapshots(
    *,
    session,
    reference_project: Path,
    plans,
    cpu_count: int,
    wait_for_edb,
    run_validation=False,
    progress=None,
):
    """Save isolated cases in one SIwave application session.

    Supported circuit elements are deactivated and then restored with the
    SIwave 2025.2 ScrActivateCktElem API.  Unsupported elements use deletion as
    a compatibility fallback; only that path reloads the unchanged reference
    before the following case.
    """
    results = []
    emit = progress or (lambda _event: None)
    reference_path = Path(reference_project).resolve()
    reload_reference = False

    def open_reference():
        session.open_project(str(reference_path))
        session.set_num_cpus_to_use(int(cpu_count))

    try:
        open_reference()
        total = len(plans)
        for case_number, plan in enumerate(plans, start=1):
            error = ""
            deactivated = []
            display_net = str(plan.get("display_net", ""))
            started = time.monotonic()
            emit({
                "phase": "case_start", "display_net": display_net,
                "case_number": case_number, "total": total,
            })
            try:
                if reload_reference:
                    open_reference()
                    reload_reference = False
                element_types = dict(plan.get("remove_element_types", {}) or {})
                for element_name in sorted(plan.get("remove_elements", ())):
                    element_type = element_types.get(element_name)
                    if element_type:
                        session.set_circuit_element_active(element_name, element_type, False)
                        deactivated.append((element_name, element_type))
                    else:
                        # A transistor/FET is not one of ScrActivateCktElem's
                        # supported types.  Deletion opens it for this case.
                        reload_reference = True
                        session.delete_circuit_element(element_name)
                siw_path = Path(plan["siw_path"]).resolve()
                edb_path = Path(plan["edb_path"]).resolve()
                session.save_project_as(siw_path)
                if run_validation:
                    validation_result = session.run_validation_check()
                    if validation_result is False:
                        raise RuntimeError("SIwave validation check failed")
                session.export_edb(edb_path)
                if not wait_for_edb(edb_path):
                    raise FileNotFoundError(f"case EDB export missing: {edb_path}")
                emit({
                    "phase": "case_saved", "display_net": display_net,
                    "case_number": case_number, "total": total,
                    "siw_path": str(siw_path),
                    "elapsed_seconds": time.monotonic() - started,
                })
            except Exception as exc:
                error = str(exc)
            finally:
                restore_errors = []
                for element_name, element_type in reversed(deactivated):
                    try:
                        session.set_circuit_element_active(element_name, element_type, True)
                    except Exception as exc:
                        reload_reference = True
                        restore_errors.append(str(exc))
                if restore_errors:
                    suffix = "state restore failed: " + "; ".join(restore_errors[:3])
                    error = f"{error}; {suffix}" if error else suffix
                emit({
                    "phase": "case_restored", "display_net": display_net,
                    "case_number": case_number, "total": total,
                    "ok": not bool(error), "error": error,
                    "elapsed_seconds": time.monotonic() - started,
                })
            results.append({
                "display_net": display_net,
                "ok": not bool(error),
                "error": error,
            })
    finally:
        session.quit_application()
    return results


def run_ng_case_solves(
    *,
    executable: Path,
    exec_file: Path,
    plans,
    runner=subprocess.run,
    progress=None,
    heartbeat_seconds: float = 60.0,
):
    """Solve independent case projects with local non-graphical SIwave."""
    plans = list(plans or [])
    total = len(plans)
    workers = 1
    emit = progress or (lambda _event: None)
    executable = Path(executable).resolve()
    exec_file = Path(exec_file).resolve()

    def solve_one(index, plan):
        display_net = str(plan.get("display_net", ""))
        siw_path = Path(plan["siw_path"]).resolve()
        started = time.monotonic()
        emit({
            "phase": "solve_start", "display_net": display_net,
            "case_number": index + 1, "total": total,
            "siw_path": str(siw_path),
        })
        base_cmd = [str(executable), str(siw_path), str(exec_file)]
        variants = [
            base_cmd + ["-formatOutput", "-useSubdir"],
            base_cmd + ["-useSubdir"],
            base_cmd,
        ]
        last = None
        error = ""
        attempts = 0
        try:
            for attempts, cmd in enumerate(variants, start=1):
                emit({
                    "phase": "solve_attempt", "display_net": display_net,
                    "case_number": index + 1, "total": total,
                    "attempt": attempts, "command": cmd,
                })
                last = runner(cmd, capture_output=True, text=True)
                if int(last.returncode) == 0:
                    break
            returncode = int(last.returncode) if last is not None else -1
            if returncode != 0:
                detail = str(getattr(last, "stderr", "") or getattr(last, "stdout", "") or "").strip()
                error = f"siwave_ng failed: rc={returncode}, attempts={attempts}"
                if detail:
                    error += f", output={detail[-1000:]}"
        except Exception as exc:
            returncode = -1
            error = str(exc)
        result = {
            "display_net": display_net,
            "ok": not bool(error),
            "error": error,
            "returncode": returncode,
            "attempts": attempts,
            "elapsed_seconds": time.monotonic() - started,
        }
        emit({
            "phase": "solve_complete", "case_number": index + 1,
            "total": total, **result,
        })
        return index, result

    ordered_results = [None] * total
    if not plans:
        return ordered_results
    started_all = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdn-ng") as pool:
        pending = {
            pool.submit(solve_one, index, plan)
            for index, plan in enumerate(plans)
        }
        completed = 0
        while pending:
            done, pending = wait(
                pending,
                timeout=max(0.01, float(heartbeat_seconds)),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                emit({
                    "phase": "solve_heartbeat", "completed": completed,
                    "total": total, "running_or_queued": len(pending),
                    "elapsed_seconds": time.monotonic() - started_all,
                    "max_workers": workers,
                })
                continue
            for future in done:
                index, result = future.result()
                ordered_results[index] = result
                completed += 1
    return ordered_results
