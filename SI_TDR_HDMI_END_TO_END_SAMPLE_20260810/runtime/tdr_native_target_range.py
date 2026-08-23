from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

try:
    from SI_TDR.channel.target_band import (
        build_tdr_impedance_metadata,
        build_tdr_impedance_overlay_records,
    )
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from channel.target_band import (  # type: ignore[no-redef]
        build_tdr_impedance_metadata,
        build_tdr_impedance_overlay_records,
    )


SCHEMA = "si-tdr-aedt-native-target-range/v1"
BAND_COLORS_RGB = [
    (224, 159, 62),
    (42, 157, 143),
    (155, 93, 229),
    (241, 91, 181),
]
NOTE_X = 650
NOTE_TOP_Y = 650
NOTE_ROW_STEP_Y = 520
NOTE_FONT_SIZE_PT = 18


def analyze_native_target_ranges(
    tdr: Mapping[str, Any],
    trace_names: Sequence[str],
    channels: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Resolve the configured Target Range for one AEDT native report."""

    normalized_traces = [str(item) for item in trace_names]
    raw_target_ranges = [tdr.get("targetRangeOhm")]
    raw_target_ranges.extend(
        item.get("targetRangeOhm")
        for item in tdr.get("channels") or []
        if isinstance(item, Mapping)
    )
    if not any(item is not None for item in raw_target_ranges):
        return {
            "schema": SCHEMA,
            "status": "not-configured",
            "reason": "no configured Target Range applies to this report",
            "traceNames": normalized_traces,
            "channels": [str(item) for item in channels or []],
            "referenceLinesOhm": [],
            "targetBandsOhm": [],
        }
    if channels is None:
        configured_channels = [
            str(item.get("name") or "").strip()
            for item in tdr.get("channels") or []
            if isinstance(item, Mapping) and str(item.get("name") or "").strip()
        ]
        if len(configured_channels) == 1:
            normalized_channels: list[str | None] = [configured_channels[0]] * len(normalized_traces)
        elif configured_channels:
            raise ValueError(
                "AEDT native Target Range needs an explicit trace/channel mapping "
                "when tdr.channels contains multiple channels"
            )
        else:
            normalized_channels = [None] * len(normalized_traces)
    else:
        normalized_channels = [str(item) for item in channels]
        if len(normalized_channels) != len(normalized_traces):
            raise ValueError(
                "AEDT native Target Range trace/channel mapping length mismatch"
            )

    transient = {
        "traceNames": normalized_traces,
        "manualTopology": {
            "tdrProbes": [
                {"traceName": trace_name, "channel": channel_name}
                for trace_name, channel_name in zip(
                    normalized_traces,
                    normalized_channels,
                )
                if channel_name
            ]
        },
    }
    metadata = build_tdr_impedance_metadata(dict(tdr), transient)
    overlays = build_tdr_impedance_overlay_records(metadata)
    target_bands = overlays["targetBandsOhm"]
    return {
        "schema": SCHEMA,
        "status": "configured" if target_bands else "not-configured",
        "reason": (
            "configured Target Range is ready for AEDT native rendering"
            if target_bands
            else "no configured Target Range applies to this report"
        ),
        "traceNames": normalized_traces,
        "channels": [item for item in normalized_channels if item],
        "referenceLinesOhm": overlays["referenceLinesOhm"],
        "targetBandsOhm": target_bands,
    }


def _report_lines(report: Any) -> list[Any]:
    try:
        return list(report.limit_lines)
    except Exception:
        return []


def _report_notes(report: Any) -> list[Any]:
    try:
        return list(report.notes)
    except Exception:
        return []


def _scope_label(band: Mapping[str, Any]) -> str:
    channel_names = list(band.get("channels") or [])
    trace_names = list(band.get("traceNames") or [])
    names = channel_names or trace_names
    if not names:
        return ""
    kind = "channels" if channel_names else "traces"
    if len(names) <= 3:
        return ", ".join(str(item) for item in names)
    return f"{len(names)} {kind}"


def _cleanup_failed_report(report: Any) -> tuple[bool, str | None]:
    try:
        return bool(report.delete()), None
    except Exception as exc:
        return False, str(exc)


def add_native_report_target_ranges(
    report: Any,
    analysis: Mapping[str, Any],
    *,
    x_min_ps: float,
    x_max_ps: float,
) -> dict[str, Any]:
    """Add labeled Target Range lower/upper bounds to an AEDT report."""

    report_name = str(getattr(report, "plot_name", "") or "").strip()
    target_bands = list(analysis.get("targetBandsOhm") or [])
    base = {
        "schema": SCHEMA,
        "status": analysis.get("status"),
        "reason": analysis.get("reason"),
        "reportName": report_name or None,
        "xAxisPs": {"min": x_min_ps, "max": x_max_ps},
        "targetBandsOhm": [],
        "limitLineCount": 0,
        "labelCount": 0,
        "semantics": {
            "representation": "labeled lower/upper bounds",
            "insideBounds": "configured Target Range",
            "passFailCalculated": False,
        },
    }
    if not target_bands:
        return base

    try:
        x_min = float(x_min_ps)
        x_max = float(x_max_ps)
    except (TypeError, ValueError) as exc:
        raise ValueError("AEDT native Target Range x-axis bounds must be numeric") from exc
    if not math.isfinite(x_min) or not math.isfinite(x_max) or x_max <= x_min:
        raise ValueError(
            "AEDT native Target Range requires finite x-axis bounds with max greater than min"
        )
    if not report_name:
        return {
            **base,
            "status": "render_failed",
            "reason": "created AEDT report object has no plot name",
        }

    rendered_bands: list[dict[str, Any]] = []
    label_count = 0
    limit_line_count = 0
    try:
        for index, band in enumerate(target_bands):
            lower = float(band["lower"])
            upper = float(band["upper"])
            color = BAND_COLORS_RGB[index % len(BAND_COLORS_RGB)]
            rendered_lines: list[dict[str, Any]] = []
            for bound_name, value in (
                ("lower", lower),
                ("upper", upper),
            ):
                lines_before = _report_lines(report)
                created = bool(
                    report.add_limit_line_from_points(
                        [x_min, x_max],
                        [value, value],
                        x_units="ps",
                        y_units="ohm",
                        y_axis="Y1",
                    )
                )
                if not created:
                    raise RuntimeError(
                        f"PyAEDT did not create the {bound_name} Target Range limit line"
                    )
                lines_after = _report_lines(report)
                new_lines = lines_after[len(lines_before) :]
                line = new_lines[-1] if new_lines else None
                if line is None:
                    raise RuntimeError(
                        f"PyAEDT did not expose the created {bound_name} Target Range limit line"
                    )
                line_name = str(getattr(line, "line_name", "") or "") or None
                styled = bool(
                    line.set_line_properties(
                        style="Dash",
                        width=3,
                        color=color,
                    )
                )
                if not styled:
                    raise RuntimeError(
                        f"PyAEDT did not style the {bound_name} Target Range limit line"
                    )
                violation_emphasis_disabled = bool(
                    line._change_property(
                        [
                            "NAME:ChangedProps",
                            ["NAME:Violation Emphasis", "Value:=", False],
                        ]
                    )
                )
                if not violation_emphasis_disabled:
                    raise RuntimeError(
                        "PyAEDT did not disable automatic limit-line violation hatching"
                    )
                rendered_lines.append(
                    {
                        "bound": bound_name,
                        "valueOhm": value,
                        "lineName": line_name,
                        "styled": styled,
                        "violationEmphasis": False,
                    }
                )
                limit_line_count += 1

            scope = _scope_label(band)
            label = f"Target Range: {lower:g}-{upper:g} ohm"
            if len(target_bands) > 1 and scope:
                label += f" ({scope})"
            notes_before = _report_notes(report)
            note_created = bool(
                report.add_note(
                    label,
                    x_position=NOTE_X,
                    y_position=NOTE_TOP_Y + NOTE_ROW_STEP_Y * index,
                )
            )
            if not note_created:
                raise RuntimeError("PyAEDT did not create the Target Range label")
            notes_after = _report_notes(report)
            new_notes = notes_after[len(notes_before) :]
            note = new_notes[-1] if new_notes else None
            note_name = None
            note_styled = None
            if note is not None:
                note_name = str(getattr(note, "plot_note_name", "") or "") or None
                note_styled = bool(
                    note.set_note_properties(
                        background_visibility=False,
                        border_visibility=False,
                        font="Arial",
                        font_size=NOTE_FONT_SIZE_PT,
                        bold=True,
                        color=color,
                    )
                )
                if not note_styled:
                    raise RuntimeError("PyAEDT did not style the Target Range label")
            label_count += 1
            rendered_bands.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "channels": list(band.get("channels") or []),
                    "traceNames": list(band.get("traceNames") or []),
                    "colorRgb": list(color),
                    "limitLines": rendered_lines,
                    "label": {
                        "text": label,
                        "noteName": note_name,
                        "styled": note_styled,
                        "x": NOTE_X,
                        "y": NOTE_TOP_Y + NOTE_ROW_STEP_Y * index,
                    },
                }
            )
    except Exception as exc:
        cleanup_succeeded, cleanup_error = _cleanup_failed_report(report)
        return {
            **base,
            "status": "render_failed",
            "reason": f"AEDT native Target Range rendering failed: {exc}",
            "targetBandsOhm": rendered_bands,
            "limitLineCount": limit_line_count,
            "labelCount": label_count,
            "reportDeleted": cleanup_succeeded,
            "cleanupError": cleanup_error,
        }

    return {
        **base,
        "status": "rendered",
        "reason": "Target Range bounds and labels were added before AEDT image export",
        "targetBandsOhm": rendered_bands,
        "limitLineCount": limit_line_count,
        "labelCount": label_count,
    }
