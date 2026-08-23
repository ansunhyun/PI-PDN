from __future__ import annotations

import csv
import json
import math
import textwrap
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "si-tdr-endpoint-annotation/v1"
SUPPORTED_MODE = "boundary_labels"
DEFAULT_FONT_SIZE_PT = 32.0
DEFAULT_WRAP_WIDTH_CHARS = 24
NATIVE_REPORT_NOTE_X_MIN = 650
NATIVE_REPORT_NOTE_X_MAX = 9640
NATIVE_REPORT_NOTE_BOTTOM_Y = 7600
NATIVE_REPORT_NOTE_ROW_STEP_Y = 1100
NATIVE_REPORT_NOTE_CHAR_WIDTH_AT_32PT = 180


def _not_rendered_result(
    status: str,
    reason: str,
    *,
    config: Mapping[str, Any] | None,
    channels: list[dict[str, Any]] | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": status,
        "reason": reason,
        "config": dict(config or {}),
        "source": dict(source or {}),
        "semantics": {
            "placement": "plot_inside_boundary_labels",
            "measurementDirection": "near_to_far",
            "visibleTextPolicy": "refdes_only",
            "dataCoordinateMarkerRendered": False,
            "actualArrivalTimePositioned": False,
            "statement": (
                "Labels identify measurement start/end components at the chart boundaries; "
                "they do not claim a physical arrival-time coordinate."
            ),
        },
        "channels": channels or [],
        "groups": [],
    }


def _endpoint_config(tdr: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    processing = tdr.get("resultProcessing")
    if processing is None:
        return None, None
    if not isinstance(processing, Mapping):
        return None, "tdr.resultProcessing must be an object"
    config = processing.get("endpointAnnotations")
    if config is None:
        return None, None
    if not isinstance(config, Mapping):
        return None, "tdr.resultProcessing.endpointAnnotations must be an object"
    return dict(config), None


def _layout_config(config: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    layout = config.get("layout") or {}
    if not isinstance(layout, Mapping):
        return {}, "endpointAnnotations.layout must be an object"
    placement = str(layout.get("placement") or "plot_inside")
    if placement != "plot_inside":
        return {}, "endpointAnnotations.layout.placement must be 'plot_inside'"
    try:
        font_size = float(layout.get("fontSizePt", DEFAULT_FONT_SIZE_PT))
        wrap_width = int(layout.get("wrapWidthChars", DEFAULT_WRAP_WIDTH_CHARS))
    except (TypeError, ValueError):
        return {}, "endpointAnnotations layout numeric values are invalid"
    if not math.isfinite(font_size) or not 8.0 <= font_size <= 48.0:
        return {}, "endpointAnnotations.layout.fontSizePt must be between 8 and 48"
    if not 12 <= wrap_width <= 120:
        return {}, "endpointAnnotations.layout.wrapWidthChars must be between 12 and 120"
    return {
        "placement": placement,
        "fontSizePt": font_size,
        "wrapWidthChars": wrap_width,
    }, None


def _channel_settings(tdr: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    settings: dict[str, Mapping[str, Any]] = {}
    for item in tdr.get("channels") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            settings[name] = item
    return settings


def _unresolved_channel(
    *,
    trace: str,
    channel: str | None,
    status: str,
    reason: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace": trace,
        "channel": channel,
        "status": status,
        "reason": reason,
        "direction": None,
        "start": None,
        "end": None,
        "metadataEvidence": dict(metadata or {}),
    }


def _resolved_channel(
    *,
    trace: str,
    channel: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    if metadata.get("schema") != SCHEMA:
        return _unresolved_channel(
            trace=trace,
            channel=channel,
            status="not_rendered_invalid_metadata",
            reason=f"measurementEndpoints.schema must be {SCHEMA!r}",
            metadata=metadata,
        )
    if metadata.get("status") != "resolved":
        return _unresolved_channel(
            trace=trace,
            channel=channel,
            status="not_rendered_unresolved_metadata",
            reason=str(metadata.get("reason") or "measurement endpoint metadata is unresolved"),
            metadata=metadata,
        )
    direction = metadata.get("direction")
    if not isinstance(direction, Mapping) or direction.get("measurement") != "near_to_far":
        return _unresolved_channel(
            trace=trace,
            channel=channel,
            status="not_rendered_direction_conflict",
            reason="measurementEndpoints.direction.measurement must be 'near_to_far'",
            metadata=metadata,
        )

    endpoints: dict[str, dict[str, Any]] = {}
    for role, expected_port_role in (("start", "near"), ("end", "far")):
        endpoint = metadata.get(role)
        if not isinstance(endpoint, Mapping):
            return _unresolved_channel(
                trace=trace,
                channel=channel,
                status="not_rendered_missing_metadata",
                reason=f"measurementEndpoints.{role} is missing",
                metadata=metadata,
            )
        refdes = str(endpoint.get("refdes") or "").strip()
        source_fields = endpoint.get("sourceFields")
        if not refdes:
            return _unresolved_channel(
                trace=trace,
                channel=channel,
                status="not_rendered_missing_metadata",
                reason=f"measurementEndpoints.{role}.refdes is missing",
                metadata=metadata,
            )
        if endpoint.get("portRole") != expected_port_role:
            return _unresolved_channel(
                trace=trace,
                channel=channel,
                status="not_rendered_direction_conflict",
                reason=(
                    f"measurementEndpoints.{role}.portRole must be "
                    f"{expected_port_role!r}"
                ),
                metadata=metadata,
            )
        if not isinstance(source_fields, list) or not source_fields or not all(
            isinstance(item, str) and item.strip() for item in source_fields
        ):
            return _unresolved_channel(
                trace=trace,
                channel=channel,
                status="not_rendered_missing_provenance",
                reason=f"measurementEndpoints.{role}.sourceFields is missing",
                metadata=metadata,
            )
        endpoints[role] = {
            "refdes": refdes,
            "portRole": expected_port_role,
            "channelPathRole": endpoint.get("channelPathRole"),
            "sourceFields": list(source_fields),
            "sourceArtifact": metadata.get("sourceArtifact"),
        }

    return {
        "trace": trace,
        "channel": channel,
        "status": "resolved",
        "reason": "explicit measurement endpoint metadata validated",
        "direction": dict(direction),
        "start": endpoints["start"],
        "end": endpoints["end"],
        "metadataEvidence": {
            "schema": metadata.get("schema"),
            "sourceArtifact": metadata.get("sourceArtifact"),
            "direction": dict(direction),
        },
    }


def analyze_endpoint_annotations(
    tdr: Mapping[str, Any],
    trace_to_channel: Mapping[str, str | None],
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve fail-closed chart-boundary RefDes labels for plotted traces."""

    config, config_error = _endpoint_config(tdr)
    if config_error:
        return _not_rendered_result(
            "invalid_configuration",
            config_error,
            config={},
            source=source,
        )
    if config is None:
        return _not_rendered_result(
            "not_configured",
            "tdr.resultProcessing.endpointAnnotations is absent; legacy image behavior is unchanged",
            config={},
            source=source,
        )
    enabled = config.get("enabled")
    if not isinstance(enabled, bool):
        return _not_rendered_result(
            "invalid_configuration",
            "endpointAnnotations.enabled must be true or false",
            config=config,
            source=source,
        )
    if not enabled:
        return _not_rendered_result(
            "disabled",
            "endpoint annotation is explicitly disabled",
            config=config,
            source=source,
        )
    mode = str(config.get("mode") or SUPPORTED_MODE)
    if mode != SUPPORTED_MODE:
        return _not_rendered_result(
            "invalid_configuration",
            f"endpointAnnotations.mode must be {SUPPORTED_MODE!r}",
            config=config,
            source=source,
        )
    layout, layout_error = _layout_config(config)
    if layout_error:
        return _not_rendered_result(
            "invalid_configuration",
            layout_error,
            config=config,
            source=source,
        )

    configured_channels = _channel_settings(tdr)
    channel_records: list[dict[str, Any]] = []
    for trace, mapped_channel in trace_to_channel.items():
        channel = str(mapped_channel or "").strip()
        if not channel:
            channel_records.append(
                _unresolved_channel(
                    trace=str(trace),
                    channel=None,
                    status="not_rendered_trace_unmapped",
                    reason="plotted trace has no explicit TDR channel mapping",
                )
            )
            continue
        settings = configured_channels.get(channel)
        if settings is None:
            channel_records.append(
                _unresolved_channel(
                    trace=str(trace),
                    channel=channel,
                    status="not_rendered_missing_channel",
                    reason="mapped TDR channel is absent from config",
                )
            )
            continue
        metadata = settings.get("measurementEndpoints")
        if not isinstance(metadata, Mapping):
            channel_records.append(
                _unresolved_channel(
                    trace=str(trace),
                    channel=channel,
                    status="not_rendered_missing_metadata",
                    reason="tdr channel has no measurementEndpoints metadata",
                )
            )
            continue
        channel_records.append(
            _resolved_channel(
                trace=str(trace),
                channel=channel,
                metadata=metadata,
            )
        )

    resolved = [item for item in channel_records if item["status"] == "resolved"]
    omitted = [item for item in channel_records if item["status"] != "resolved"]
    if not resolved:
        result = _not_rendered_result(
            "not_rendered",
            "no plotted trace has complete, conflict-free measurement endpoint metadata",
            config={**config, "mode": mode, "layout": layout},
            channels=channel_records,
            source=source,
        )
        result["summary"] = {
            "plottedTraceCount": len(channel_records),
            "resolvedTraceCount": 0,
            "omittedTraceCount": len(omitted),
        }
        return result
    if omitted:
        result = _not_rendered_result(
            "not_rendered",
            "at least one plotted trace lacks safe endpoint metadata; the complete plot annotation failed closed",
            config={**config, "mode": mode, "layout": layout},
            channels=channel_records,
            source=source,
        )
        result["summary"] = {
            "plottedTraceCount": len(channel_records),
            "resolvedTraceCount": len(resolved),
            "omittedTraceCount": len(omitted),
        }
        return result

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in resolved:
        pair = (item["start"]["refdes"], item["end"]["refdes"])
        group = grouped.setdefault(
            pair,
            {
                "startRefdes": pair[0],
                "endRefdes": pair[1],
                "channels": set(),
                "traceNames": set(),
            },
        )
        group["channels"].add(item["channel"])
        group["traceNames"].add(item["trace"])

    groups: list[dict[str, Any]] = []
    for pair in sorted(grouped):
        group = grouped[pair]
        channels = sorted(group["channels"])
        trace_names = sorted(group["traceNames"])
        groups.append(
            {
                "startRefdes": pair[0],
                "endRefdes": pair[1],
                "channels": channels,
                "traceNames": trace_names,
                "visibleTextPolicy": "refdes_only",
            }
        )

    status = "rendered"
    reason = "all plotted traces have complete, conflict-free measurement endpoint metadata"
    result = _not_rendered_result(
        status,
        reason,
        config={**config, "mode": mode, "layout": layout},
        channels=channel_records,
        source=source,
    )
    result["groups"] = groups
    result["summary"] = {
        "plottedTraceCount": len(channel_records),
        "resolvedTraceCount": len(resolved),
        "omittedTraceCount": len(omitted),
        "uniqueEndpointPairCount": len(groups),
    }
    return result


def _wrap_text(text: str, width: int) -> str:
    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return "\n".join(wrapped) if wrapped else text


def add_endpoint_annotation_overlays(
    axes: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Draw large RefDes-only labels inside the plot, never at a data-time coordinate."""

    if result.get("status") != "rendered":
        return {
            "renderedGroupCount": 0,
            "labelCount": 0,
            "coordinateSystem": "axes_fraction",
            "visibleTextPolicy": "refdes_only",
            "dataCoordinateMarkerRendered": False,
            "actualArrivalTimePositioned": False,
        }

    layout = (result.get("config") or {}).get("layout") or {}
    font_size = float(layout.get("fontSizePt", DEFAULT_FONT_SIZE_PT))
    wrap_width = int(layout.get("wrapWidthChars", DEFAULT_WRAP_WIDTH_CHARS))
    groups = result.get("groups") or []
    y = 0.035
    label_count = 0
    rendered_labels: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        start_text = _wrap_text(str(group["startRefdes"]), wrap_width)
        end_text = _wrap_text(str(group["endRefdes"]), wrap_width)
        line_units = max(start_text.count("\n") + 1, end_text.count("\n") + 1)
        axes.text(
            0.018,
            y,
            start_text,
            transform=axes.transAxes,
            ha="left",
            va="bottom",
            fontsize=font_size,
            fontweight="bold",
            color="black",
            clip_on=True,
            zorder=20,
        )
        axes.text(
            0.982,
            y,
            end_text,
            transform=axes.transAxes,
            ha="right",
            va="bottom",
            fontsize=font_size,
            fontweight="bold",
            color="black",
            clip_on=True,
            zorder=20,
        )
        label_count += 2
        rendered_labels.append(
            {
                "channels": list(group.get("channels") or []),
                "startRefdes": group["startRefdes"],
                "endRefdes": group["endRefdes"],
                "rowIndex": index,
                "color": "black",
                "fontSizePt": font_size,
            }
        )
        y += 0.15 + 0.10 * max(0, line_units - 1)

    return {
        "renderedGroupCount": len(groups),
        "labelCount": label_count,
        "coordinateSystem": "axes_fraction",
        "visibleTextPolicy": "refdes_only",
        "startPlacement": "left_lower_inside_plot",
        "endPlacement": "right_lower_inside_plot",
        "labels": rendered_labels,
        "dataCoordinateMarkerRendered": False,
        "actualArrivalTimePositioned": False,
    }


def _native_note_font_property(font_size: float) -> list[Any]:
    """Build the AEDT ReportSetup font property for a bold black note."""

    return [
        "NAME:Note Font",
        "Height:=",
        -int(round(font_size)) - 2,
        "Width:=",
        0,
        "Escapement:=",
        0,
        "Orientation:=",
        0,
        "Weight:=",
        700,
        "Italic:=",
        0,
        "Underline:=",
        0,
        "StrikeOut:=",
        0,
        "CharSet:=",
        0,
        "OutPrecision:=",
        3,
        "ClipPrecision:=",
        2,
        "Quality:=",
        1,
        "PitchAndFamily:=",
        34,
        "FaceName:=",
        "Arial",
        "R:=",
        0,
        "G:=",
        0,
        "B:=",
        0,
    ]


def _native_note_x(text: str, *, side: str, font_size: float) -> int:
    if side == "start":
        return NATIVE_REPORT_NOTE_X_MIN
    longest_line = max((len(line) for line in text.splitlines()), default=1)
    character_width = NATIVE_REPORT_NOTE_CHAR_WIDTH_AT_32PT * font_size / 32.0
    estimated_width = int(round(longest_line * character_width))
    return max(5200, NATIVE_REPORT_NOTE_X_MAX - estimated_width)


def _add_native_note(
    report_setup: Any,
    *,
    report_name: str,
    note_name: str,
    text: str,
    x_position: int,
    y_position: int,
    font_size: float,
) -> None:
    """Add and style an explicitly positioned AEDT report note.

    AEDT 2024 R2 applies the supplied report-layout coordinates when
    ``HaveDefaultPos=True``. The native API is used so the generated note has a
    stable name that can be styled immediately through ``ChangeProperty``.
    """

    report_setup.AddNote(
        report_name,
        [
            "NAME:NoteDataSource",
            [
                "NAME:NoteDataSource",
                "SourceName:=",
                note_name,
                "HaveDefaultPos:=",
                True,
                "DefaultXPos:=",
                int(x_position),
                "DefaultYPos:=",
                int(y_position),
                "String:=",
                text,
            ],
        ],
    )
    report_setup.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:Note",
                ["NAME:PropServers", f"{report_name}:{note_name}"],
                [
                    "NAME:ChangedProps",
                    ["NAME:Background Visibility", "Value:=", False],
                    ["NAME:Border Visibility", "Value:=", False],
                    _native_note_font_property(font_size),
                ],
            ],
        ]
    )


def add_native_report_endpoint_notes(
    report: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Add RefDes-only notes to the lower corners of an AEDT native report."""

    base = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "reportName": getattr(report, "plot_name", None),
        "coordinateSystem": "aedt_report_layout",
        "visibleTextPolicy": "refdes_only",
        "startPlacement": "left_lower_inside_plot",
        "endPlacement": "right_lower_inside_plot",
        "dataCoordinateMarkerRendered": False,
        "actualArrivalTimePositioned": False,
        "labelCount": 0,
        "labels": [],
    }
    if result.get("status") != "rendered":
        return base

    report_name = str(getattr(report, "plot_name", "") or "").strip()
    report_setup = getattr(getattr(report, "_post", None), "oreportsetup", None)
    if not report_name or report_setup is None:
        return {
            **base,
            "status": "not_rendered",
            "reason": "created AEDT report object does not expose ReportSetup",
        }

    layout = (result.get("config") or {}).get("layout") or {}
    configured_font_size = float(layout.get("fontSizePt", DEFAULT_FONT_SIZE_PT))
    font_size = float(int(round(configured_font_size)))
    wrap_width = int(layout.get("wrapWidthChars", DEFAULT_WRAP_WIDTH_CHARS))
    labels: list[dict[str, Any]] = []
    try:
        for index, group in enumerate(result.get("groups") or []):
            y_position = NATIVE_REPORT_NOTE_BOTTOM_Y - index * NATIVE_REPORT_NOTE_ROW_STEP_Y
            start_text = _wrap_text(str(group["startRefdes"]), wrap_width)
            end_text = _wrap_text(str(group["endRefdes"]), wrap_width)
            for side, text in (("start", start_text), ("end", end_text)):
                note_name = f"Endpoint{side.title()}{index + 1}"
                x_position = _native_note_x(text, side=side, font_size=font_size)
                _add_native_note(
                    report_setup,
                    report_name=report_name,
                    note_name=note_name,
                    text=text,
                    x_position=x_position,
                    y_position=y_position,
                    font_size=font_size,
                )
                labels.append(
                    {
                        "noteName": note_name,
                        "side": side,
                        "text": text,
                        "refdes": group[f"{side}Refdes"],
                        "channels": list(group.get("channels") or []),
                        "rowIndex": index,
                        "x": x_position,
                        "y": y_position,
                        "fontSizePt": font_size,
                        "configuredFontSizePt": configured_font_size,
                        "bold": True,
                        "color": "black",
                        "backgroundVisible": False,
                        "borderVisible": False,
                    }
                )
    except Exception as exc:
        cleanup_succeeded = False
        cleanup_error = None
        try:
            cleanup_succeeded = bool(report.delete())
        except Exception as cleanup_exc:
            cleanup_error = str(cleanup_exc)
        return {
            **base,
            "status": "render_failed",
            "reason": f"AEDT native report note creation failed: {exc}",
            "labelCount": len(labels),
            "labels": labels,
            "reportDeleted": cleanup_succeeded,
            "cleanupError": cleanup_error,
        }

    return {
        **base,
        "status": "rendered",
        "reason": "RefDes-only AEDT report notes were added before image export",
        "labelCount": len(labels),
        "labels": labels,
    }


CSV_FIELDS = [
    "trace_name",
    "channel",
    "annotation_status",
    "reason",
    "direction",
    "start_refdes",
    "start_port_role",
    "start_channel_path_role",
    "start_source_fields",
    "end_refdes",
    "end_port_role",
    "end_channel_path_role",
    "end_source_fields",
    "source_artifact",
]


def _csv_row(channel: Mapping[str, Any]) -> dict[str, Any]:
    start = channel.get("start") or {}
    end = channel.get("end") or {}
    direction = channel.get("direction") or {}
    source_artifact = start.get("sourceArtifact") or end.get("sourceArtifact")
    return {
        "trace_name": channel.get("trace"),
        "channel": channel.get("channel"),
        "annotation_status": channel.get("status"),
        "reason": channel.get("reason"),
        "direction": direction.get("measurement"),
        "start_refdes": start.get("refdes"),
        "start_port_role": start.get("portRole"),
        "start_channel_path_role": start.get("channelPathRole"),
        "start_source_fields": " | ".join(start.get("sourceFields") or []),
        "end_refdes": end.get("refdes"),
        "end_port_role": end.get("portRole"),
        "end_channel_path_role": end.get("channelPathRole"),
        "end_source_fields": " | ".join(end.get("sourceFields") or []),
        "source_artifact": source_artifact,
    }


def write_endpoint_annotation_results(
    result: Mapping[str, Any],
    *,
    json_path: Path,
    csv_path: Path,
) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as fp:
        json.dump(result, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for channel in result.get("channels") or []:
            writer.writerow(_csv_row(channel))
    return json_path, csv_path
