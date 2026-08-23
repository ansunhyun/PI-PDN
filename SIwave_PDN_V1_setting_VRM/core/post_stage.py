# coding=utf-8
from __future__ import annotations

import copy
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class PostStageError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunArtifacts:
    name: str
    folder: Path
    siw: Path
    finished: Path


@dataclass
class PostStageState:
    summary: list[dict[str, Any]]
    change_history: list[dict[str, Any]]
    gnd_net: str
    analysis_start: str
    analysis_end: str
    viewer_artifacts: list[dict[str, Any]] = field(default_factory=list)


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y.%m.%d, %H:%M:%S")


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise PostStageError(f"Required Post input does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise PostStageError(f"Failed to read Post input {path}: {exc}") from exc


def remove_artifact_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def wait_for_edb(edb_path: Path, timeout: float = 120.0, poll_interval: float = 0.5) -> bool:
    """Wait until SIWave has produced a non-empty EDB definition file."""
    deadline = time.monotonic() + timeout
    edb_def = Path(edb_path) / "edb.def"
    while time.monotonic() < deadline:
        try:
            if edb_def.is_file() and edb_def.stat().st_size > 0:
                return True
        except OSError:
            pass
        time.sleep(poll_interval)
    return False


def export_post_edb(
    siw_file: Path,
    edb_path: Path,
    version: str,
    siwave_factory,
    timeout: float = 120.0,
) -> Path:
    """Create a fresh Post-owned AEDB from the selected completed-run SIW."""
    siw_file = Path(siw_file)
    edb_path = Path(edb_path)
    if not siw_file.is_file():
        raise PostStageError(f"Latest completed SIW does not exist: {siw_file}")

    edb_path.parent.mkdir(parents=True, exist_ok=True)
    remove_artifact_path(edb_path)
    siw_inst = None
    try:
        siw_inst = siwave_factory(version=version)
        siw_inst.open_project(str(siw_file))
        siw_inst.oproject.ScrExportEDB(str(edb_path))
        if not wait_for_edb(edb_path, timeout=timeout):
            raise PostStageError(
                f"Timed out after {timeout:g}s waiting for Post AEDB: {edb_path}"
            )
        return edb_path
    except Exception as exc:
        remove_artifact_path(edb_path)
        if isinstance(exc, PostStageError):
            raise
        raise PostStageError(
            f"Failed to export Post AEDB from {siw_file} to {edb_path}: {exc}"
        ) from exc
    finally:
        if siw_inst is not None:
            try:
                siw_inst.quit_application()
            except Exception:
                pass


def _resolve_case_project(record: dict[str, Any], output_dir: Path) -> Path:
    project_file = record.get("Project_File")
    if not project_file:
        project_path = record.get("Project_Path")
        if project_path:
            project_file = Path(str(project_path).replace("\\", "/")).name
    if not project_file:
        raise PostStageError("Preprocessing record has no Project_File or Project_Path")
    return output_dir / str(project_file)


def _resolve_case_edb(record: dict[str, Any], project: Path, output_dir: Path) -> Path:
    edb_folder = record.get("Edb_Folder")
    if not edb_folder:
        edb_path = record.get("Edb_Path")
        if edb_path:
            edb_folder = Path(str(edb_path).replace("\\", "/")).name
    if not edb_folder:
        edb_folder = project.with_suffix(".aedb").name
    return output_dir / str(edb_folder)


def _resolve_run_artifact(folder: Path, run_name: str, suffix: str) -> Path:
    expected = folder / f"{run_name}.{suffix}"
    if expected.is_file():
        return expected

    candidates = sorted(path for path in folder.rglob(f"*.{suffix}") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return expected


def find_run_candidates(project: Path) -> list[RunArtifacts]:
    result_root = project.with_suffix(".siwaveresults")
    if not result_root.is_dir():
        return []

    runs = []
    for folder in result_root.iterdir():
        if not folder.is_dir():
            continue
        match = re.match(r"^(\d+)(?:$|_)", folder.name)
        if not match:
            continue
        run_name = match.group(1)
        runs.append(RunArtifacts(
            name=run_name,
            folder=folder,
            siw=_resolve_run_artifact(folder, run_name, "siw"),
            finished=_resolve_run_artifact(folder, run_name, "finished"),
        ))
    return sorted(runs, key=lambda item: (int(item.name), item.folder.name))


def find_completed_runs(project: Path) -> list[RunArtifacts]:
    # AC PDN에서는 ced 파일 대신 finished 파일과 siw 파일 존재 여부로 완료 판단
    return [
        run for run in find_run_candidates(project)
        if run.siw.is_file() and run.finished.is_file()
    ]


def _no_completed_run_error(project: Path) -> PostStageError:
    result_root = project.with_suffix(".siwaveresults")
    if not result_root.is_dir():
        return PostStageError(f"Result folder does not exist: {result_root}")

    candidates = find_run_candidates(project)
    if not candidates:
        folder_names = sorted(path.name for path in result_root.iterdir() if path.is_dir())
        detail = ", ".join(folder_names) if folder_names else "no run folders"
        return PostStageError(
            f"No supported result run folder found for {project.name}: {detail}"
        )

    details = []
    for run in candidates:
        missing = [
            suffix for suffix, path in (
                ("siw", run.siw),
                ("finished", run.finished),
            )
            if not path.is_file()
        ]
        details.append(f"{run.folder.name} (missing: {', '.join(missing)})")
    return PostStageError(
        f"No completed result run found for {project.name}: {'; '.join(details)}"
    )


def _display_name(project: Path, ic_designator: str, target_net: str) -> str:
    marker = f"_{ic_designator}_"
    if ic_designator and marker in project.stem:
        return f"{ic_designator}_{project.stem.split(marker, 1)[1]}"
    safe_net = "".join(char for char in target_net if char.isalnum() or char == "_")
    return f"{ic_designator}_{safe_net}".strip("_")


def _base_summary(record: dict[str, Any], edb_path: Path) -> dict[str, Any]:
    return {
        "IC": record.get("IC_Designator", ""),
        "IC_pin": record.get("IC_Pin", ""),
        "Net": record.get("Target_Net", ""),
        "Source_name": record.get("Source_Component", ""),
        "Source_pin": record.get("Source_Pin", ""),
        "Source_net": record.get("Net_Chain", []),
        "Full_Net_Chain": record.get("Full_Net_Chain", []),
        "is_done": False,
        "edb": edb_path,
        "_viewer_siw": None,
        "Status": "Pending",
        "Impedance_Plot": "",
        "Impedance_CSV": "",
        "Touchstone": "",
        "FitView": "",
        "ZoomView": "",
    }


def _run_history_entry(run: RunArtifacts, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    entry = {
        "Run": run.name,
        "Folder": run.folder.name,
        "Completed_At": _format_time(run.finished.stat().st_mtime),
        "Status": "Complete",
    }
    gnd_net = str(record.get("GND_Net", "GND"))
    return entry, gnd_net


def _build_case(record: dict[str, Any], output_dir: Path) -> tuple[dict[str, Any], dict[str, Any], str | None, list[float]]:
    project = _resolve_case_project(record, output_dir)
    edb_path = _resolve_case_edb(record, project, output_dir)
    summary = _base_summary(record, edb_path)
    history = {
        "Case_Index": record.get("Case_Index"),
        "IC": summary["IC"],
        "Net": summary["Net"],
        "Project_File": project.name,
        "Result_Folder": project.with_suffix(".siwaveresults"),
        "Latest_Run": None,
        "Runs": [],
        "Status": "Error",
    }
    run_times: list[float] = []
    gnd_net = record.get("GND_Net")

    try:
        runs = find_completed_runs(project)
        if not runs:
            raise _no_completed_run_error(project)

        for run in runs:
            entry, run_gnd = _run_history_entry(run, record)
            history["Runs"].append(entry)
            gnd_net = gnd_net or run_gnd
            run_times.extend([run.siw.stat().st_mtime, run.finished.stat().st_mtime])

        latest = history["Runs"][-1]
        latest_run = runs[-1]
        summary["_viewer_siw"] = latest_run.siw
        summary.update({
            "is_done": True,
            "Status": latest["Status"],
        })

        display_name = _display_name(project, summary["IC"], summary["Net"])
        
        # AC PDN Artifacts
        plot_name = f"Z_Param_{display_name}"
        summary["Impedance_Plot"] = output_dir / f"{plot_name}.jpg"
        summary["Impedance_CSV"] = output_dir / f"{plot_name}.csv"
        # Touchstone 포트 수는 동적이므로 여기서는 기본 확장자 없이 경로만 지정
        # post_processing에서 실제 생성된 파일명으로 덮어씌워짐
        summary["Touchstone"] = output_dir / f"{plot_name}.sNp" 
        
        summary["FitView"] = output_dir / f"{display_name}_FitView.jpg"
        summary["ZoomView"] = output_dir / f"{display_name}_ZoomView.jpg"

        history.update({
            "Latest_Run": latest["Run"],
            "Latest_Siw": latest_run.siw,
            "Status": "Complete",
        })
    except (OSError, ValueError, PostStageError) as exc:
        history["Error"] = str(exc)

    return summary, history, str(gnd_net) if gnd_net else None, run_times


def _load_existing_case_metadata(output_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result_path = output_dir / "result.json"
    if not result_path.is_file():
        return {}
    try:
        result_data = _load_json(result_path)
    except PostStageError:
        return {}
    summary = result_data.get("summary", []) if isinstance(result_data, dict) else []
    return {
        (str(case.get("IC", "")), str(case.get("Net", ""))): case
        for case in summary
        if isinstance(case, dict)
    }


def _merge_legacy_metadata(record: dict[str, Any], existing: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    merged = dict(record)
    previous = existing.get(
        (str(record.get("IC_Designator", "")), str(record.get("Target_Net", ""))),
        {},
    )
    if not merged.get("IC_Pin"):
        merged["IC_Pin"] = previous.get("IC_pin", "")
    if not merged.get("Source_Pin"):
        merged["Source_Pin"] = previous.get("Source_pin", "")
    return merged


def reconstruct_post_state(output_dir: Path) -> PostStageState:
    output_dir = Path(output_dir)
    records = _load_json(output_dir / "preprocessing_result.json")
    if not isinstance(records, list) or not records:
        raise PostStageError("preprocessing_result.json must contain at least one case record")

    summary = []
    change_history = []
    gnd_nets = set()
    run_times: list[float] = []
    existing_metadata = _load_existing_case_metadata(output_dir)
    for record in records:
        if not isinstance(record, dict):
            raise PostStageError("Invalid preprocessing case record")
        record = _merge_legacy_metadata(record, existing_metadata)
        case, history, gnd_net, case_times = _build_case(record, output_dir)
        summary.append(case)
        change_history.append(history)
        if gnd_net:
            gnd_nets.add(gnd_net)
        run_times.extend(case_times)

    if len(gnd_nets) > 1:
        raise PostStageError(f"Inconsistent ground nets in Post results: {sorted(gnd_nets)}")

    now = datetime.now().strftime("%Y.%m.%d, %H:%M:%S")
    return PostStageState(
        summary=summary,
        change_history=change_history,
        gnd_net=next(iter(gnd_nets), "GND"),
        analysis_start=_format_time(min(run_times)) if run_times else now,
        analysis_end=_format_time(max(run_times)) if run_times else now,
    )


def prepare_post_settings(settings: dict[str, Any]) -> dict[str, Any]:
    prepared = copy.deepcopy(settings)
    pcb = prepared.get("CAE", {}).get("PCB", {})
    for key in ("Stackup", "BOM"):
        value = pcb.get(key)
        if value and not isinstance(value, Path):
            pcb[key] = Path(str(value))
    return prepared


def append_post_detail(output_dir: Path, state: PostStageState) -> None:
    result_detail_path = Path(output_dir) / "result_detail.json"
    result_detail = _load_json(result_detail_path)
    if not isinstance(result_detail, dict):
        raise PostStageError("result_detail.json must contain a JSON object")

    result_detail["changeHistory"] = state.change_history
    result_detail["postInfo"] = {
        "resultBasis": "latest_completed_local_run",
        "viewerBasis": "latest_completed_local_siw",
        "viewerReflectsLocalSettings": True,
        "artifactOwnership": {
            "preprocessing_result.json": "Pre",
            "case_siw": "Pre/Local",
            "case_aedb": "Post",
            "web_json_and_viewer": "Post",
        },
        "viewerArtifacts": state.viewer_artifacts,
    }
    temporary_path = result_detail_path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(result_detail, stream, indent=2, ensure_ascii=False, default=str)
    temporary_path.replace(result_detail_path)