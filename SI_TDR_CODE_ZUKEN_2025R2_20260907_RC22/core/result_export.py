"""Export DCIR-style Web JSON files from SI-TDR execution artifacts.

The customer-confirmed runtime boundary keeps DCIR and SI-TDR independent.
This module therefore follows DCIR's five public Web JSON filenames without
importing DCIR code or reusing DCIR-specific voltage-drop result fields.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .marker_evaluation import evaluate_center_markers, aggregate_status
except ImportError:
    from marker_evaluation import evaluate_center_markers, aggregate_status


WEB_RESULT_FILENAMES = (
    "title.json",
    "request.json",
    "setting.json",
    "result_detail.json",
    "result.json",
)
# 배치(sNp) PCB 캡처는 그 채널 경로 한 장이다.  보드 전체 앞/뒷면은 Job 단위
# overview가 담당하며 아직 복구되지 않았다.
STRICT_PCB_CAPTURE_VIEW = "route"
STRICT_PCB_CAPTURE_FILENAME = "route.png"


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


class ResultExportContractError(RuntimeError):
    """Raised when a strict customer result references an invalid artifact set."""


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _public_profile_file(value: Any, *, label: str) -> dict[str, str]:
    record = _mapping(value)
    file_name = _input_name(record.get("fileName") or record.get("resolvedPath"))
    sha256 = _text(record.get("sha256"))
    if not file_name or not sha256 or len(sha256) != 64:
        raise ResultExportContractError(f"{label} file/hash evidence is incomplete")
    return {"fileName": file_name, "sha256": sha256.casefold()}


def _tdr_settings_identity(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    rise_time = value.get("riseTimePs")
    if isinstance(rise_time, bool):
        raise ResultExportContractError(f"{label} riseTimePs is invalid")
    try:
        normalized_rise_time = float(rise_time)
    except (TypeError, ValueError) as exc:
        raise ResultExportContractError(f"{label} riseTimePs is invalid") from exc
    if not math.isfinite(normalized_rise_time) or normalized_rise_time <= 0:
        raise ResultExportContractError(f"{label} riseTimePs is invalid")
    normalized: dict[str, Any] = {"riseTimePs": normalized_rise_time}
    for field in ("pulseRepetition", "pulseWidth", "timeDelay"):
        item = _text(value.get(field))
        if item is None:
            raise ResultExportContractError(f"{label} {field} is invalid")
        normalized[field] = item
    return normalized


def _strict_analysis_profile_summary(
    config: Mapping[str, Any],
    transient: Mapping[str, Any],
    *,
    batch_name: str,
    required: bool,
) -> dict[str, Any]:
    """Return public-safe strict Profile provenance without Job-local paths."""

    selection = _mapping(config.get("analysisOptionSelection"))
    syz = _mapping(selection.get("syz"))
    tdr_by_item = _mapping(selection.get("tdr"))
    if not syz or not tdr_by_item:
        if required:
            raise ResultExportContractError(
                f"strict public Batch {batch_name} is missing analysisOptionSelection"
            )
        return {}

    syz_profile_id = _text(syz.get("profileId"))
    syz_source = _text(syz.get("selectionSource"))
    syz_settings = _mapping(syz.get("settings"))
    if not syz_profile_id or not syz_source or not syz_settings:
        raise ResultExportContractError(
            f"strict Batch {batch_name} has incomplete SYZ Profile evidence"
        )
    syz_summary = {
        "profileId": syz_profile_id,
        "selectionSource": syz_source,
        "sws": _public_profile_file(
            syz.get("sws"), label=f"Batch {batch_name} selected SWS"
        ),
        "sfsdf": _public_profile_file(
            syz.get("sfsdf"), label=f"Batch {batch_name} selected SFSDF"
        ),
        "settings": dict(syz_settings),
    }

    tdr_summaries: list[dict[str, Any]] = []
    selected_identities: set[str] = set()
    for item_id in sorted(str(key) for key in tdr_by_item):
        item = _mapping(tdr_by_item.get(item_id))
        profile_id = _text(item.get("profileId"))
        selection_source = _text(item.get("selectionSource"))
        settings = _mapping(item.get("settings"))
        if not profile_id or not selection_source or not settings:
            raise ResultExportContractError(
                f"strict Batch {batch_name} TDR item {item_id!r} has incomplete Profile evidence"
            )
        missing = [
            field
            for field in ("riseTimePs", "pulseRepetition", "pulseWidth", "timeDelay")
            if field not in settings or settings.get(field) is None
        ]
        if missing:
            raise ResultExportContractError(
                f"strict Batch {batch_name} TDR item {item_id!r} is missing {missing}"
            )
        file_record = _public_profile_file(
            item.get("file"), label=f"Batch {batch_name} TDR item {item_id!r}"
        )
        normalized_settings = _tdr_settings_identity(
            settings, label=f"strict Batch {batch_name} TDR item {item_id!r}"
        )
        identity = json.dumps(
            {
                "profileId": profile_id,
                "sha256": file_record["sha256"],
                "settings": normalized_settings,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        selected_identities.add(identity)
        tdr_summaries.append(
            {
                "itemId": item_id,
                "profileId": profile_id,
                "selectionSource": selection_source,
                "file": file_record,
                "settings": dict(settings),
            }
        )

    if required:
        reports = transient.get("reports") or []
        if not isinstance(reports, list) or not reports:
            raise ResultExportContractError(
                f"strict public Batch {batch_name} has no TDR report Profile evidence"
            )
        for index, report in enumerate(reports):
            profile = _mapping(_mapping(report).get("tdrProfile"))
            profile_id = _text(profile.get("profileId"))
            sha256 = _text(profile.get("sha256"))
            settings = _mapping(profile.get("settings"))
            if not profile_id or not sha256 or not settings:
                raise ResultExportContractError(
                    f"strict public Batch {batch_name} report[{index}] has incomplete TDR Profile evidence"
                )
            normalized_settings = _tdr_settings_identity(
                settings,
                label=f"strict public Batch {batch_name} report[{index}] TDR Profile",
            )
            identity = json.dumps(
                {
                    "profileId": profile_id,
                    "sha256": sha256.casefold(),
                    "settings": normalized_settings,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if identity not in selected_identities:
                raise ResultExportContractError(
                    f"strict public Batch {batch_name} report[{index}] TDR Profile differs from Run Config"
                )

    unique_profile_ids = sorted({item["profileId"] for item in tdr_summaries})
    unique_settings = {
        json.dumps(
            item["settings"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for item in tdr_summaries
    }
    return {
        "syz": syz_summary,
        "tdr": tdr_summaries,
        "tdrProfileIds": unique_profile_ids,
        "uniformTdrSettings": (
            dict(tdr_summaries[0]["settings"])
            if len(unique_settings) == 1
            else None
        ),
    }


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
    syz = config.get("syz") or {}
    if isinstance(syz, Mapping):
        value = _text(syz.get("touchstoneBaseName"))
        if value:
            return value
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


def _strict_artifact_path(
    value: Any,
    *,
    request_dir: Path,
    source_dir: Path,
    label: str,
) -> tuple[Path, str]:
    normalized = _text(value)
    if normalized is None:
        raise ResultExportContractError(f"strict result is missing {label}")
    path = Path(normalized)
    if not path.is_absolute():
        path = (source_dir / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ResultExportContractError(
            f"strict result artifact is missing or empty: {label}: {path}"
        )
    relative = _request_relative_path(
        path,
        request_dir=request_dir,
        source_dir=source_dir,
    )
    if relative is None:
        raise ResultExportContractError(
            f"strict result artifact is outside the request tree: {label}: {path}"
        )
    return path, relative


def _strict_tdr_result_artifacts(
    transient: Mapping[str, Any],
    *,
    run_dir: Path,
    request_dir: Path,
) -> tuple[list[str], str]:
    if transient.get("status") != "ok" or transient.get("buildMode") != (
        "customer-strict-native-aedt-tdr"
    ):
        raise ResultExportContractError(
            "strict result requires a successful native AEDT TDR record"
        )
    reports = [item for item in transient.get("reports") or [] if isinstance(item, Mapping)]
    if not reports:
        raise ResultExportContractError("strict TDR result contains no native reports")
    images: list[str] = []
    seen: set[str] = set()
    for index, report in enumerate(reports):
        path, relative = _strict_artifact_path(
            report.get("imagePath"),
            request_dir=request_dir,
            source_dir=run_dir,
            label=f"tdr_transient.reports[{index}].imagePath",
        )
        if path.suffix.casefold() not in {".jpg", ".jpeg"}:
            raise ResultExportContractError(
                f"strict native TDR report must be JPG: {path}"
            )
        key = relative.casefold()
        if key in seen:
            raise ResultExportContractError(
                f"strict native TDR report image is duplicated: {relative}"
            )
        seen.add(key)
        images.append(relative)
    artifacts = transient.get("artifacts") or {}
    waveform = artifacts.get("waveformCsv") or {}
    waveform_path, waveform_relative = _strict_artifact_path(
        waveform.get("path"),
        request_dir=request_dir,
        source_dir=run_dir,
        label="tdr_transient.artifacts.waveformCsv.path",
    )
    if waveform_path.name != "Tdr_waveform.csv":
        raise ResultExportContractError(
            "strict customer CSV must be named exactly Tdr_waveform.csv"
        )
    return images, waveform_relative


def _strict_pcb_result_artifacts(
    pcb_manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    request_dir: Path,
) -> tuple[list[str], dict[str, str]]:
    if (
        pcb_manifest.get("schema") != "si-tdr-strict-pcb-capture-manifest/2"
        or pcb_manifest.get("mode") != "customer-strict-snp-batch-union"
        or pcb_manifest.get("status") != "ok"
    ):
        raise ResultExportContractError(
            "strict result requires a successful sNp-batch PCB capture manifest"
        )
    if pcb_manifest.get("overviewCaptures"):
        raise ResultExportContractError(
            "strict PCB result cannot expose overview/Fit All captures"
        )
    captures = [item for item in pcb_manifest.get("captures") or [] if isinstance(item, Mapping)]
    if len(captures) != 1:
        raise ResultExportContractError(
            "strict PCB result requires exactly one sNp-batch route capture"
        )
    by_side = {str(item.get("view") or ""): item for item in captures}
    if set(by_side) != {STRICT_PCB_CAPTURE_VIEW}:
        raise ResultExportContractError(
            f"strict PCB result capture view must be exactly {STRICT_PCB_CAPTURE_VIEW}"
        )
    relative_by_side: dict[str, str] = {}
    for side, file_name in ((STRICT_PCB_CAPTURE_VIEW, STRICT_PCB_CAPTURE_FILENAME),):
        item = by_side[side]
        if (
            item.get("kind") != "snp-batch-union"
            or item.get("fileName") != file_name
            or item.get("status") != "ok"
        ):
            raise ResultExportContractError(
                f"strict PCB {side} capture contract is invalid"
            )
        path, relative = _strict_artifact_path(
            item.get("image"),
            request_dir=request_dir,
            source_dir=manifest_path.parent,
            label=f"PCB {side} image",
        )
        if path.name != file_name:
            raise ResultExportContractError(
                f"strict PCB {side} image must be named exactly {file_name}"
            )
        relative_by_side[side] = relative
    actual_png = {
        path.name
        for path in (manifest_path.parent / "images").glob("*.png")
        if path.is_file()
    }
    if actual_png != {STRICT_PCB_CAPTURE_FILENAME}:
        raise ResultExportContractError(
            "strict PCB result contains an extra, missing, or legacy PNG: "
            f"{sorted(actual_png)}"
        )
    return [relative_by_side[STRICT_PCB_CAPTURE_VIEW]], relative_by_side


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


def _batch_payload(
    batch: BatchExecutionInput,
    *,
    request_dir: Path,
    runtime_root: Path,
    requested_operations: Mapping[str, Any],
    public_artifacts: Mapping[str, Any] | None = None,
    include_internal_references: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], Path, dict[str, Any]]:
    config_path = batch.config_path.resolve()
    config = _json_object(config_path)
    strict_customer = bool(config.get("customerComponentHandling"))
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
    pcb_manifest_path = run_dir / "pcb_capture" / "pcb_capture_manifest.json"
    solve = _json_object(solve_path)
    transient = _json_object(transient_path)
    pcb = _json_object(pcb_manifest_path)

    touchstone = _artifact_paths(
        solve.get("exportedTouchstoneFiles") or [],
        request_dir=request_dir,
        source_dir=run_dir,
    )
    strict_pcb_by_side: dict[str, str] = {}
    public_folder: str | None = None
    if strict_customer:
        if requested_operations.get("runTdr") or requested_operations.get("tdrOnly"):
            tdr_images, tdr_csv = _strict_tdr_result_artifacts(
                transient,
                run_dir=run_dir,
                request_dir=request_dir,
            )
        else:
            tdr_images, tdr_csv = [], None
        if requested_operations.get("capturePcbRoutes"):
            pcb_images, strict_pcb_by_side = _strict_pcb_result_artifacts(
                pcb,
                manifest_path=pcb_manifest_path,
                request_dir=request_dir,
            )
        else:
            pcb_images = []
    else:
        tdr_images = _artifact_paths(
            [
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
        waveform_record = (transient.get("artifacts") or {}).get("waveformCsv") or {}
        waveform_path = (
            waveform_record.get("path")
            if isinstance(waveform_record, Mapping)
            else None
        ) or transient.get("waveformCsvPath")
        tdr_csv = _request_relative_path(
            waveform_path,
            request_dir=request_dir,
            source_dir=run_dir,
        )
        pcb_images = _pcb_image_paths(
            pcb,
            manifest_path=pcb_manifest_path,
            request_dir=request_dir,
        )
    if public_artifacts is not None:
        if not strict_customer:
            raise ResultExportContractError(
                "public FullBatch artifact overrides require a strict customer batch"
            )
        tdr_images = [str(item) for item in public_artifacts.get("tdrImages") or []]
        tdr_csv = _text(public_artifacts.get("tdrCsv"))
        pcb_images = [str(item) for item in public_artifacts.get("pcbImages") or []]
        route_image = _text(public_artifacts.get("routeImage"))
        strict_pcb_by_side = (
            {STRICT_PCB_CAPTURE_VIEW: route_image}
            if route_image is not None
            else {}
        )
        reproduction = _mapping(public_artifacts.get("reproductionArtifacts"))
        public_touchstone = _text(reproduction.get("touchstone"))
        public_folder = _text(public_artifacts.get("publicFolder"))
        route_requested = bool(requested_operations.get("capturePcbRoutes"))
        if route_requested:
            route_mapping_invalid = (
                len(pcb_images) != 1
                or route_image is None
                or pcb_images != [route_image]
            )
        else:
            route_mapping_invalid = bool(pcb_images) or route_image is not None
        if (
            not tdr_images
            or tdr_csv is None
            or route_mapping_invalid
            or public_touchstone is None
            or public_folder is None
        ):
            raise ResultExportContractError(
                f"public FullBatch artifact mapping is incomplete for batch {batch_name}"
            )
        touchstone = [public_touchstone]
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
    if public_folder is not None:
        batch_summary["PublicFolder"] = public_folder

    channel_details: list[dict[str, Any]] = []
    tdr = config.get("tdr") or {}
    for channel in tdr.get("channels") or []:
        if not isinstance(channel, Mapping):
            continue
        channel_id = _text(channel.get("name"))
        display_name = _text(channel.get("displayName")) or channel_id
        target_range = channel.get("targetRangeOhm") or channel.get("targetBandOhm") or {}
        if not isinstance(target_range, Mapping):
            target_range = {}
        channel_detail = {
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
                "waveformExtremaStatus": "not_evaluated",
                "waveformExtremaReason": "python_minmax_analysis_removed",
                "evaluationStatus": "not_evaluated",
                "evaluationReason": "pass_fail_rule_out_of_scope",
                "MinOhm": None,
                "MinTimePs": None,
                "MaxOhm": None,
                "MaxTimePs": None,
                "TDRImage": tdr_images,
                "TDRCsv": tdr_csv,
                "PCBImages": (
                    list(pcb_images)
                    if strict_customer
                    else _pcb_image_paths(
                        pcb,
                        manifest_path=pcb_manifest_path,
                        request_dir=request_dir,
                        channel_name=channel_id,
                    )
                ),
            }
        if public_folder is not None:
            channel_detail["PublicFolder"] = public_folder
        if strict_customer and public_artifacts is not None:
            if len(tdr_images) != 1 or len(pcb_images) > 1:
                raise ResultExportContractError("each channel result requires one image per image field")
            channel_detail["TDRImage"] = tdr_images[0]
            channel_detail["PCBImages"] = pcb_images[0] if pcb_images else None
        channel_details.append(channel_detail)

    if strict_customer and public_artifacts is not None:
        try:
            csv_evidence = transient["artifacts"]["waveformCsv"]["path"]
            csv_source = Path(csv_evidence)
            if not csv_source.is_absolute():
                csv_source = run_dir / csv_source
            evaluations = evaluate_center_markers(transient, csv_source, tdr.get("channels") or [])
            for row in channel_details:
                row.update(evaluations[row["ChannelId"]])
            batch_summary["evaluationStatus"] = aggregate_status(channel_details)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ResultExportContractError(f"central Marker evaluation failed: {exc}") from exc

    profile_summary = _strict_analysis_profile_summary(
        config,
        transient,
        batch_name=batch_name,
        required=bool(strict_customer and public_artifacts is not None),
    )
    syz_profile = _mapping(profile_summary.get("syz"))
    tdr_profiles = profile_summary.get("tdr") or []
    tdr_profile_ids = profile_summary.get("tdrProfileIds") or []
    uniform_tdr_settings = _mapping(profile_summary.get("uniformTdrSettings"))
    legacy_syz = _mapping(config.get("syz"))
    legacy_tdr = _mapping(config.get("tdr"))
    terminal_layer_policy = _text(
        _mapping(config.get("ports")).get("terminalLayerPolicy")
    )
    setting = {
        **grouping,
        "Batch": batch_name,
        "aedtVersion": config.get("aedtVersion") or context.get("aedtVersion"),
        "syzTemplateId": syz_profile.get("profileId") or legacy_syz.get("templateId"),
        "syzProfileId": syz_profile.get("profileId") or legacy_syz.get("templateId"),
        "syzProfile": dict(syz_profile) if syz_profile else None,
        "tdrTemplateId": (
            tdr_profile_ids[0]
            if len(tdr_profile_ids) == 1
            else legacy_tdr.get("templateId")
        ),
        "tdrProfileId": tdr_profile_ids[0] if len(tdr_profile_ids) == 1 else None,
        "tdrProfileIds": list(tdr_profile_ids),
        "tdrProfiles": list(tdr_profiles),
        "referenceNet": _mapping(config.get("nets")).get("reference"),
        "referenceLayer": _mapping(config.get("ports")).get("referenceLayer"),
        "terminalLayerPolicy": terminal_layer_policy,
        "portImpedanceOhm": _mapping(config.get("ports")).get("singleEndedImpedanceOhm"),
        "tdr": {
            "mode": legacy_tdr.get("mode"),
            "riseTimePs": uniform_tdr_settings.get("riseTimePs") or legacy_tdr.get("riseTimePs"),
            "pulseRepetition": uniform_tdr_settings.get("pulseRepetition"),
            "pulseWidth": uniform_tdr_settings.get("pulseWidth"),
            "timeDelay": uniform_tdr_settings.get("timeDelay"),
            "transient": legacy_tdr.get("transient"),
            "view": legacy_tdr.get("view"),
        },
    }
    supporting_artifacts = {}
    if include_internal_references:
        supporting_artifacts = {
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
            "pcbCaptureManifest": _request_relative_path(
                pcb_manifest_path,
                request_dir=request_dir,
            ),
        }
    supporting = {
        **supporting_artifacts,
    }
    return batch_summary, channel_details, setting, run_dir, {
        "supportingArtifacts": supporting,
        # request.json의 pcbTopImage/pcbBtmImage는 보드 전체 앞/뒷면을 가리킨다.
        # strict FullBatch에는 아직 Job 단위 overview가 없으므로 비워 둔다.
        "topImage": (
            None
            if strict_customer
            else _overview_image(
                pcb,
                manifest_path=pcb_manifest_path,
                request_dir=request_dir,
                position="top",
            )
        ),
        "bottomImage": (
            None
            if strict_customer
            else _overview_image(
                pcb,
                manifest_path=pcb_manifest_path,
                request_dir=request_dir,
                position="bottom",
            )
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
    public_batch_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    public_overview_artifacts: Mapping[str, str] | None = None,
    include_internal_references: bool = True,
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
    project = _mapping(base_config.get("project"))
    preprocessing = _mapping(base_config.get("preprocessing"))
    preprocessing_inputs = _mapping(preprocessing.get("inputs"))
    external_request = _mapping(request_payload.get("Request"))
    external_cae = _mapping(request_payload.get("CAE"))
    external_soc = _mapping(external_cae.get("SOC"))
    external_pcb = _mapping(external_cae.get("PCB"))

    overview_requested = bool(
        requested_operations.get(
            "capturePcbOverview",
            public_batch_artifacts is not None,
        )
    )
    overview_top_image: str | None = None
    overview_bottom_image: str | None = None
    if public_overview_artifacts is not None:
        overview_top_image = _text(public_overview_artifacts.get("top"))
        overview_bottom_image = _text(public_overview_artifacts.get("bottom"))
        if not overview_top_image or not overview_bottom_image:
            raise ResultExportContractError(
                "public whole-board overview references require top and bottom images"
            )
    if overview_requested and public_overview_artifacts is None:
        raise ResultExportContractError(
            "public FullBatch export requires the Job whole-board overview references"
        )
    if not overview_requested and public_overview_artifacts is not None:
        raise ResultExportContractError(
            "public FullBatch export received disabled whole-board overview references"
        )

    batch_summaries: list[dict[str, Any]] = []
    channel_details: list[dict[str, Any]] = []
    settings: list[dict[str, Any]] = []
    result_dirs: list[Path] = []
    top_image = None
    bottom_image = None
    detailed_batches: list[dict[str, Any]] = []
    for batch in batches:
        batch_config = _json_object(batch.config_path.resolve())
        current_batch_name = _batch_name(batch.config_path.resolve(), batch_config)
        public_artifacts = None
        if public_batch_artifacts is not None:
            public_artifacts = public_batch_artifacts.get(current_batch_name)
            if public_artifacts is None:
                raise ResultExportContractError(
                    "public FullBatch artifact mapping is missing batch "
                    f"{current_batch_name}"
                )
        summary, channels, setting, run_dir, supporting = _batch_payload(
            batch,
            request_dir=request_dir,
            runtime_root=runtime_root.resolve(),
            requested_operations=requested_operations,
            public_artifacts=public_artifacts,
            include_internal_references=include_internal_references,
        )
        batch_summaries.append(summary)
        channel_details.extend(channels)
        settings.append(setting)
        detailed_batches.append({**summary, **supporting["supportingArtifacts"]})
        if run_dir not in result_dirs:
            result_dirs.append(run_dir)
        top_image = top_image or supporting.get("topImage")
        bottom_image = bottom_image or supporting.get("bottomImage")
    if public_overview_artifacts is not None:
        # request.json의 pcbTopImage/pcbBtmImage는 Job 전체 보드 overview를
        # 가리킨다(DCIR과 동일 의미).
        top_image = overview_top_image
        bottom_image = overview_bottom_image

    overall_status = (
        "failed" if int(exit_code) != 0 else _requested_stage(requested_operations)
    )
    model_name = _text(
        external_request.get("Model")
        or external_soc.get("Name")
        or project.get("name")
        or request_payload.get("name")
    ) or request_path.stem
    revision = _text(project.get("revision") or project.get("rev"))
    bom_config = _mapping(base_config.get("BOM"))
    batch_versions = sorted(
        {
            str(item.get("aedtVersion"))
            for item in settings
            if _text(item.get("aedtVersion"))
        }
    )
    if len(batch_versions) > 1:
        raise ResultExportContractError(
            f"public batches disagree on aedtVersion: {batch_versions}"
        )
    tool_version = _text(
        batch_versions[0]
        if batch_versions
        else request_payload.get("aedtVersion")
        or preprocessing.get("version")
        or base_config.get("aedtVersion")
    )
    stackup_name = _input_name(
        external_pcb.get("Stackup") or preprocessing_inputs.get("stackup")
    )
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
            "socName": _text(external_soc.get("Name") or project.get("socName")),
            "pcbPartNo": _text(project.get("pcbPartNo")),
            "pcbRevision": revision,
            "design": _input_name(
                external_pcb.get("cadFile") or preprocessing_inputs.get("design")
            ),
            "Stackup": stackup_name,
            "bom": _input_name(
                external_pcb.get("BOM")
                or preprocessing_inputs.get("bom")
                or bom_config.get("path")
            ),
            "channelCsv": _input_name(
                external_soc.get("Spec") or request_payload.get("csv")
            ),
            "purpose": _text(external_cae.get("Purpose") or project.get("purpose"))
            or "SI-TDR analysis",
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
            "version": tool_version,
        },
        "stackup": "stackup.xml" if public_batch_artifacts is not None else stackup_name,
        "setting": settings,
    }
    result_detail = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "status": overall_status,
        "exitCode": int(exit_code),
        "result": channel_details,
        "batches": detailed_batches,
        "generationManifest": (
            _request_relative_path(
                generation_manifest,
                request_dir=request_dir,
            )
            if include_internal_references
            else None
        ),
        "logs": {
            "full": (
                _request_relative_path(full_log_path, request_dir=request_dir)
                if include_internal_references
                else None
            ),
            "progress": (
                _request_relative_path(progress_log_path, request_dir=request_dir)
                if include_internal_references
                else None
            ),
        },
        "error": error,
    }
    result = {
        "schemaVersion": 1,
        "analysisType": "SI-TDR",
        "status": overall_status,
        "exitCode": int(exit_code),
        "simSchedule": {
            # EDEN confirm contract (2026-09-07): retain the legacy key spelling.
            "startDate": started_at.strftime("%Y.%m.%d, %H:%M:%S"),
            "endData": completed_at.strftime("%Y.%m.%d, %H:%M:%S"),
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
    if public_batch_artifacts is not None:
        evaluation = aggregate_status(channel_details)
        result["evaluationStatus"] = evaluation
        result_detail["evaluationStatus"] = evaluation
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
