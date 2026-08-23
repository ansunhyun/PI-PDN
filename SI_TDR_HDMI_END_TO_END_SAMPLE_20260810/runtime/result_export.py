"""Export DCIR-style Web JSON files from SI-TDR execution artifacts.

The customer-confirmed runtime boundary keeps DCIR and SI-TDR independent.
This module therefore follows DCIR's five public Web JSON filenames without
importing DCIR code or reusing DCIR-specific voltage-drop result fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


WEB_RESULT_FILENAMES = (
    "title.json",
    "request.json",
    "setting.json",
    "result_detail.json",
    "result.json",
)


@dataclass(frozen=True)
class BatchExecutionInput:
    config_path: Path
    run_dir: Path | None
    status_code: int
    error: str | None = None


@dataclass(frozen=True)
class EdenResultExport:
    output_dir: Path
    title: Path
    request: Path
    result: Path
    setting: Path
    result_detail: Path
    result_dirs: tuple[Path, ...]


def _json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _input_name(value: Any) -> str | None:
    normalized = _text(value)
    return Path(normalized).name if normalized else None


def _request_relative_path(
    value: Any,
    *,
    request_dir: Path,
    source_dir: Path | None = None,
    require_exists: bool = True,
) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    path = Path(normalized)
    if not path.is_absolute():
        path = ((source_dir or request_dir) / path).resolve()
    else:
        path = path.resolve()
    if require_exists and not path.exists():
        return None
    try:
        return path.relative_to(request_dir).as_posix()
    except ValueError:
        return None


def _batch_name(config_path: Path, config: Mapping[str, Any]) -> str:
    segment = config.get("segment") or config.get("interface") or {}
    if isinstance(segment, Mapping):
        value = _text(segment.get("name") or segment.get("interface"))
        if value:
            return value
    stem = config_path.stem
    suffix = "_run_config"
    return stem[: -len(suffix)] if stem.endswith(suffix) else stem


def _default_run_dir(
    config_path: Path,
    config: Mapping[str, Any],
    *,
    runtime_root: Path,
) -> Path:
    segment = config.get("segment") or config.get("interface") or {}
    if not isinstance(segment, Mapping):
        segment = {}
    segment_name = _text(segment.get("name") or segment.get("interface")) or "default"
    strategy = _text(
        segment.get("strategy")
        or config.get("strategy")
        or (config.get("ports") or {}).get("mode")
    ) or "default"
    run_name = f"{segment_name}__{strategy.replace('-', '_')}"
    return (runtime_root / "work" / run_name).resolve()


def _generation_manifest_path(config_path: Path) -> Path:
    suffix = "_run_config"
    stem = config_path.stem
    prefix = stem[: -len(suffix)] if stem.endswith(suffix) else stem
    return config_path.with_name(f"{prefix}_manifest.json")


def _detailed_batch_metadata(
    generation_manifest: Mapping[str, Any],
    generation_manifest_path: Path,
    batch_name: str,
) -> dict[str, Any]:
    report_value = generation_manifest.get("detailedInputReport")
    report_path = Path(str(report_value)) if report_value else None
    if report_path is not None and not report_path.is_absolute():
        report_path = generation_manifest_path.parent / report_path
    report = _json_object(report_path)
    batches = report.get("snpBatches") or []
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        if str(batch.get("snpFile") or "").casefold() == batch_name.casefold():
            return dict(batch)
    if len(batches) == 1 and isinstance(batches[0], Mapping):
        return dict(batches[0])
    return {}


def _endpoint_refdes(channel: Mapping[str, Any], role: str) -> str | None:
    metadata = channel.get("measurementEndpoints") or {}
    if not isinstance(metadata, Mapping):
        return None
    endpoint = metadata.get(role) or {}
    return _text(endpoint.get("refdes")) if isinstance(endpoint, Mapping) else None


def _path_start_refdes(channel: Mapping[str, Any]) -> str | None:
    metadata = channel.get("measurementEndpoints") or {}
    if not isinstance(metadata, Mapping):
        return None
    for role in ("start", "end"):
        endpoint = metadata.get(role) or {}
        if (
            isinstance(endpoint, Mapping)
            and endpoint.get("channelPathRole") == "start"
        ):
            return _text(endpoint.get("refdes"))
    return None


def _grouping_metadata(
    config: Mapping[str, Any],
    detailed_batch: Mapping[str, Any],
) -> dict[str, Any]:
    grouping = detailed_batch.get("groupingKey") or {}
    if not isinstance(grouping, Mapping):
        grouping = {}
    tdr = config.get("tdr") or {}
    channels = tdr.get("channels") or [] if isinstance(tdr, Mapping) else []
    first_channel = channels[0] if channels and isinstance(channels[0], Mapping) else {}
    report_groups = tdr.get("reportGroups") or [] if isinstance(tdr, Mapping) else []
    segment = config.get("segment") or config.get("interface") or {}
    if not isinstance(segment, Mapping):
        segment = {}
    group = grouping.get("group")
    if not group and report_groups and isinstance(report_groups[0], Mapping):
        group = report_groups[0].get("name")
    return {
        "Function": _text(grouping.get("function") or segment.get("interface")),
        "Version": _text(grouping.get("version")),
        "Designator": _text(grouping.get("designator")) or _path_start_refdes(first_channel),
        "Group": _text(group or detailed_batch.get("tdrReportName")),
        "Direction": _text(grouping.get("direction")),
    }


def _artifact_paths(
    values: Sequence[Any],
    *,
    request_dir: Path,
    source_dir: Path,
) -> list[str]:
    paths: list[str] = []
    for value in values:
        path = _request_relative_path(
            value,
            request_dir=request_dir,
            source_dir=source_dir,
        )
        if path and path not in paths:
            paths.append(path)
    return paths


def _pcb_image_paths(
    pcb_manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    request_dir: Path,
    channel_name: str | None = None,
) -> list[str]:
    records: list[Mapping[str, Any]] = []
    if channel_name is None:
        records.extend(
            item
            for item in pcb_manifest.get("overviewCaptures") or []
            if isinstance(item, Mapping)
        )
        records.extend(
            item
            for item in pcb_manifest.get("captures") or []
            if isinstance(item, Mapping)
        )
    else:
        requested = channel_name.casefold()
        records.extend(
            item
            for item in pcb_manifest.get("captures") or []
            if isinstance(item, Mapping)
            and str(item.get("channel") or "").casefold() == requested
        )
    return _artifact_paths(
        [item.get("image") for item in records],
        request_dir=request_dir,
        source_dir=manifest_path.parent,
    )


def _overview_image(
    pcb_manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    request_dir: Path,
    position: str,
) -> str | None:
    requested = position.casefold()
    for item in pcb_manifest.get("overviewCaptures") or []:
        if not isinstance(item, Mapping):
            continue
        evidence = " ".join(
            str(item.get(key) or "") for key in ("kind", "view", "captureId", "image")
        ).casefold()
        if requested not in evidence:
            continue
        return _request_relative_path(
            item.get("image"),
            request_dir=request_dir,
            source_dir=manifest_path.parent,
        )
    return None


def _requested_stage(requested_operations: Mapping[str, Any]) -> str:
    if requested_operations.get("runTdr") or requested_operations.get("tdrOnly"):
        return "completed"
    if requested_operations.get("solveTouchstone"):
        return "snp_solved"
    if requested_operations.get("setupSyz"):
        return "syz_configured"
    if requested_operations.get("applyPorts"):
        return "ports_configured"
    return "prepared"


def _marker_by_channel(marker: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    mapped: dict[str, Mapping[str, Any]] = {}
    for item in marker.get("channels") or []:
        if not isinstance(item, Mapping):
            continue
        for key in (item.get("channel"), item.get("trace")):
            if key:
                mapped[str(key).casefold()] = item
    return mapped


def _batch_payload(
    batch: BatchExecutionInput,
    *,
    request_dir: Path,
    runtime_root: Path,
    requested_operations: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], Path, dict[str, Any]]:
    config_path = batch.config_path.resolve()
    config = _json_object(config_path)
    batch_name = _batch_name(config_path, config)
    run_dir = (
        batch.run_dir.resolve()
        if batch.run_dir is not None
        else _default_run_dir(config_path, config, runtime_root=runtime_root)
    )
    context = _json_object(run_dir / "run_context.json")
    if context:
        workspace = context.get("workspace") or {}
        context_run_dir = workspace.get("runDir") if isinstance(workspace, Mapping) else None
        if context_run_dir:
            run_dir = Path(str(context_run_dir)).resolve()

    generation_manifest_path = _generation_manifest_path(config_path)
    generation_manifest = _json_object(generation_manifest_path)
    detailed_batch = _detailed_batch_metadata(
        generation_manifest,
        generation_manifest_path,
        batch_name,
    )
    grouping = _grouping_metadata(config, detailed_batch)
    solve_path = run_dir / "channel_solve.json"
    transient_path = run_dir / "tdr_transient.json"
    image_path = run_dir / "tdr_image.json"
    marker_path = run_dir / "tdr_minmax_markers.json"
    pcb_manifest_path = run_dir / "pcb_capture" / "pcb_capture_manifest.json"
    solve = _json_object(solve_path)
    transient = _json_object(transient_path)
    image = _json_object(image_path)
    marker = _json_object(marker_path)
    pcb = _json_object(pcb_manifest_path)
    marker_channels = _marker_by_channel(marker)

    touchstone = _artifact_paths(
        solve.get("exportedTouchstoneFiles") or [],
        request_dir=request_dir,
        source_dir=run_dir,
    )
    tdr_images = _artifact_paths(
        [
            image.get("runImagePath") or image.get("imagePath"),
            transient.get("reportImagePath"),
            *[
                item.get("imagePath")
                for item in transient.get("reports") or []
                if isinstance(item, Mapping)
            ],
        ],
        request_dir=request_dir,
        source_dir=run_dir,
    )
    tdr_csv = _request_relative_path(
        image.get("waveformCsvPath"),
        request_dir=request_dir,
        source_dir=run_dir,
    )
    pcb_images = _pcb_image_paths(
        pcb,
        manifest_path=pcb_manifest_path,
        request_dir=request_dir,
    )
    unresolved = (generation_manifest.get("summary") or {}).get("combinedUnresolved")
    analysis_status = (
        "failed" if int(batch.status_code) != 0 else _requested_stage(requested_operations)
    )
    batch_summary = {
        **grouping,
        "Batch": batch_name,
        "ChannelCount": len((config.get("tdr") or {}).get("channels") or []),
        "is_done": int(batch.status_code) == 0,
        "analysisStatus": analysis_status,
        "evaluationStatus": "not_evaluated",
        "sNp": touchstone,
        "TDRImage": tdr_images,
        "TDRCsv": tdr_csv,
        "PCBImages": pcb_images,
        "Unresolved": int(unresolved or 0),
        "Error": batch.error,
    }

    channel_details: list[dict[str, Any]] = []
    tdr = config.get("tdr") or {}
    for channel in tdr.get("channels") or []:
        if not isinstance(channel, Mapping):
            continue
        channel_id = _text(channel.get("name"))
        display_name = _text(channel.get("displayName")) or channel_id
        marker_item = marker_channels.get(str(display_name or "").casefold()) or marker_channels.get(
            str(channel_id or "").casefold()
        ) or {}
        target_range = channel.get("targetRangeOhm") or channel.get("targetBandOhm") or {}
        if not isinstance(target_range, Mapping):
            target_range = {}
        evaluation = marker_item.get("evaluation") or {}
        if not isinstance(evaluation, Mapping):
            evaluation = {}
        min_marker = marker_item.get("minMarker") or {}
        max_marker = marker_item.get("maxMarker") or {}
        channel_details.append(
            {
                **grouping,
                "Batch": batch_name,
                "Channel": display_name,
                "ChannelId": channel_id,
                "MeasurementDirection": channel.get("measurementDirection"),
                "NearRefdes": _endpoint_refdes(channel, "start"),
                "FarRefdes": _endpoint_refdes(channel, "end"),
                "TargetImpedanceOhm": channel.get("referenceImpedanceOhm"),
                "MinSpecOhm": target_range.get("lower"),
                "MaxSpecOhm": target_range.get("upper"),
                "analysisStatus": analysis_status,
                "markerStatus": marker_item.get("status") or "not_available",
                "markerReason": marker_item.get("reason"),
                "evaluationStatus": evaluation.get("status") or "not_evaluated",
                "evaluationReason": evaluation.get("reason") or "pass_fail_rule_out_of_scope",
                "MinOhm": min_marker.get("impedanceOhm") if isinstance(min_marker, Mapping) else None,
                "MinTimePs": min_marker.get("timePs") if isinstance(min_marker, Mapping) else None,
                "MaxOhm": max_marker.get("impedanceOhm") if isinstance(max_marker, Mapping) else None,
                "MaxTimePs": max_marker.get("timePs") if isinstance(max_marker, Mapping) else None,
                "TDRImage": tdr_images,
                "TDRCsv": tdr_csv,
                "PCBImages": _pcb_image_paths(
                    pcb,
                    manifest_path=pcb_manifest_path,
                    request_dir=request_dir,
                    channel_name=channel_id,
                ),
            }
        )

    setting = {
        **grouping,
        "Batch": batch_name,
        "aedtVersion": config.get("aedtVersion") or context.get("aedtVersion"),
        "syzTemplateId": (config.get("syz") or {}).get("templateId"),
        "tdrTemplateId": (config.get("tdr") or {}).get("templateId"),
        "referenceNet": (config.get("nets") or {}).get("reference"),
        "referenceLayer": (config.get("ports") or {}).get("referenceLayer"),
        "portImpedanceOhm": (config.get("ports") or {}).get("singleEndedImpedanceOhm"),
        "frequencySweep": (config.get("syz") or {}).get("frequencySweep"),
        "tdr": {
            "mode": (config.get("tdr") or {}).get("mode"),
            "riseTimePs": (config.get("tdr") or {}).get("riseTimePs"),
            "transient": (config.get("tdr") or {}).get("transient"),
            "view": (config.get("tdr") or {}).get("view"),
        },
    }
    supporting = {
        "runDirectory": _request_relative_path(
            run_dir,
            request_dir=request_dir,
            require_exists=False,
        ),
        "runConfig": _request_relative_path(
            config_path,
            request_dir=request_dir,
            require_exists=False,
        ),
        "generationManifest": _request_relative_path(
            generation_manifest_path,
            request_dir=request_dir,
        ),
        "runContext": _request_relative_path(
            run_dir / "run_context.json",
            request_dir=request_dir,
        ),
        "channelSolveRecord": _request_relative_path(
            solve_path,
            request_dir=request_dir,
        ),
        "tdrTransientRecord": _request_relative_path(
            transient_path,
            request_dir=request_dir,
        ),
        "tdrImageRecord": _request_relative_path(image_path, request_dir=request_dir),
        "tdrMarkersRecord": _request_relative_path(marker_path, request_dir=request_dir),
        "pcbCaptureManifest": _request_relative_path(
            pcb_manifest_path,
            request_dir=request_dir,
        ),
    }
    return batch_summary, channel_details, setting, run_dir, {
        "supportingArtifacts": supporting,
        "topImage": _overview_image(
            pcb,
            manifest_path=pcb_manifest_path,
            request_dir=request_dir,
            position="top",
        ),
        "bottomImage": _overview_image(
            pcb,
            manifest_path=pcb_manifest_path,
            request_dir=request_dir,
            position="bottom",
        ),
    }


def export_eden_web_results(
    *,
    request_path: Path,
    output_dir: Path,
    runtime_root: Path,
    batches: Sequence[BatchExecutionInput],
    exit_code: int,
    started_at: datetime,
    completed_at: datetime,
    requested_operations: Mapping[str, Any],
    generation_manifest: Path | None = None,
    full_log_path: Path | None = None,
    progress_log_path: Path | None = None,
    error: str | None = None,
) -> EdenResultExport:
    """Write the five EDEN/DCIR-style Web JSON files for one SI-TDR request."""

    request_path = request_path.resolve()
    request_dir = request_path.parent
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    request_payload = _json_object(request_path)
    base_config_path: Path | None = None
    if request_payload.get("baseConfig"):
        candidate = Path(str(request_payload["baseConfig"]))
        base_config_path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (request_dir / candidate).resolve()
        )
    elif request_path.is_file():
        base_config_path = request_path
    base_config = _json_object(base_config_path)
    project = base_config.get("project") or {}
    if not isinstance(project, Mapping):
        project = {}
    preprocessing = base_config.get("preprocessing") or {}
    if not isinstance(preprocessing, Mapping):
        preprocessing = {}
    preprocessing_inputs = preprocessing.get("inputs") or {}
    if not isinstance(preprocessing_inputs, Mapping):
        preprocessing_inputs = {}

    batch_summaries: list[dict[str, Any]] = []
    channel_details: list[dict[str, Any]] = []
    settings: list[dict[str, Any]] = []
    result_dirs: list[Path] = []
    top_image = None
    bottom_image = None
    detailed_batches: list[dict[str, Any]] = []
    for batch in batches:
        summary, channels, setting, run_dir, supporting = _batch_payload(
            batch,
            request_dir=request_dir,
            runtime_root=runtime_root.resolve(),
            requested_operations=requested_operations,
        )
        batch_summaries.append(summary)
        channel_details.extend(channels)
        settings.append(setting)
        detailed_batches.append({**summary, **supporting["supportingArtifacts"]})
        if run_dir not in result_dirs:
            result_dirs.append(run_dir)
        top_image = top_image or supporting.get("topImage")
        bottom_image = bottom_image or supporting.get("bottomImage")

    overall_status = (
        "failed" if int(exit_code) != 0 else _requested_stage(requested_operations)
    )
    model_name = _text(project.get("name") or request_payload.get("name")) or request_path.stem
    revision = _text(project.get("revision") or project.get("rev"))
    bom_config = base_config.get("BOM") or {}
    if not isinstance(bom_config, Mapping):
        bom_config = {}
    title = {
        "model": model_name,
        "revision": revision,
        "date": completed_at.date().isoformat(),
    }
    request = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "modelInfo": {
            "name": model_name,
            "year": _text(project.get("year")),
            "requestDate": _text(project.get("requestDate")),
            "targetDate": _text(project.get("targetDate")),
            "event": _text(project.get("event")),
        },
        "requestData": {
            "socName": _text(project.get("socName")),
            "pcbPartNo": _text(project.get("pcbPartNo")),
            "pcbRevision": revision,
            "design": _input_name(preprocessing_inputs.get("design")),
            "Stackup": _input_name(preprocessing_inputs.get("stackup")),
            "bom": _input_name(preprocessing_inputs.get("bom") or bom_config.get("path")),
            "channelCsv": _input_name(request_payload.get("csv")),
            "purpose": _text(project.get("purpose")) or "SI-TDR analysis",
        },
        "Image": {
            "pcbTopImage": top_image,
            "pcbBtmImage": bottom_image,
        },
    }
    setting = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "tool": {
            "comp": "ANSYS",
            "name": "SIwave / AEDT Circuit",
            "version": _text(
                request_payload.get("aedtVersion")
                or preprocessing.get("version")
                or base_config.get("aedtVersion")
            ),
        },
        "stackup": _input_name(preprocessing_inputs.get("stackup")),
        "setting": settings,
    }
    result_detail = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "status": overall_status,
        "exitCode": int(exit_code),
        "result": channel_details,
        "batches": detailed_batches,
        "generationManifest": _request_relative_path(
            generation_manifest,
            request_dir=request_dir,
        ),
        "logs": {
            "full": _request_relative_path(full_log_path, request_dir=request_dir),
            "progress": _request_relative_path(progress_log_path, request_dir=request_dir),
        },
        "error": error,
    }
    result = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "status": overall_status,
        "exitCode": int(exit_code),
        "simSchedule": {
            "startDate": started_at.isoformat(timespec="seconds"),
            "endDate": completed_at.isoformat(timespec="seconds"),
            "endData": completed_at.isoformat(timespec="seconds"),
        },
        "summary": batch_summaries,
        "counts": {
            "batches": len(batch_summaries),
            "channels": len(channel_details),
            "completedBatches": sum(bool(item.get("is_done")) for item in batch_summaries),
            "failedBatches": sum(not bool(item.get("is_done")) for item in batch_summaries),
            "unresolved": sum(int(item.get("Unresolved") or 0) for item in batch_summaries),
        },
        "error": error,
    }

    title_path = output_dir / "title.json"
    request_output_path = output_dir / "request.json"
    setting_path = output_dir / "setting.json"
    result_detail_path = output_dir / "result_detail.json"
    result_path = output_dir / "result.json"
    _write_json_atomic(title_path, title)
    _write_json_atomic(request_output_path, request)
    _write_json_atomic(setting_path, setting)
    _write_json_atomic(result_detail_path, result_detail)
    # result.json is written last and acts as the public completion marker.
    _write_json_atomic(result_path, result)
    return EdenResultExport(
        output_dir=output_dir,
        title=title_path,
        request=request_output_path,
        result=result_path,
        setting=setting_path,
        result_detail=result_detail_path,
        result_dirs=tuple(result_dirs),
    )
