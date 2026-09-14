from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shutil
import struct
import sys
import time
import traceback
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .admin_config import normalize_pcb_capture_policy
except ImportError:  # Support direct execution from the customer core folder.
    from admin_config import normalize_pcb_capture_policy  # type: ignore[no-redef]


ROOT_DIR = Path(__file__).resolve().parent
DCIR_VENV_SITE_PACKAGES = (
    ROOT_DIR.parent / "DCIR" / "SIwave_DCIR-1p4p1" / ".venv" / "Lib" / "site-packages"
)
CAPTURE_MANIFEST_SCHEMA = "si-tdr-pcb-capture-manifest/1"
CAPTURE_EVIDENCE_SCHEMA = "si-tdr-pcb-capture-evidence/1"
CAPTURE_PACKAGE_SCHEMA = "si-tdr-pcb-capture-package/1"
STRICT_CAPTURE_MANIFEST_SCHEMA = "si-tdr-strict-pcb-capture-manifest/2"
STRICT_CAPTURE_EVIDENCE_SCHEMA = "si-tdr-pcb-capture-evidence/2"
STRICT_CAPTURE_CONTRACT = "customer-strict-snp-batch/2"
# 배치 캡처는 sNp 단위 1장이다. PCB 전체 앞/뒷면은 Job 단위 overview가 담당한다.
STRICT_BATCH_CAPTURE_FILENAME = "route.png"
STRICT_BATCH_CAPTURE_VIEW = "route"
STRICT_CAPTURE_FILENAMES = {STRICT_BATCH_CAPTURE_VIEW: STRICT_BATCH_CAPTURE_FILENAME}
# Job 단위 전체 보드 overview: 앞/뒷면 각 1장, Job당 1쌍(9.4b).
JOB_OVERVIEW_MANIFEST_SCHEMA = "si-tdr-pcb-job-overview-manifest/2"
JOB_OVERVIEW_DIRNAME = "overview_capture"
JOB_OVERVIEW_MANIFEST_FILENAME = "overview_manifest.json"
JOB_OVERVIEW_FILENAMES = {"top": "top.png", "bottom": "bottom.png"}
STRICT_SELECTED_COLOR = "0xFF0000"
STRICT_CONTEXT_COLOR = "0x808080"
COMPONENT_PRESENTATION_SET_SCHEMA = "si-tdr-pcb-component-presentation-set/2"
PCB_VISUAL_CONFIRMATION_SCHEMA = "si-tdr-pcb-visual-confirmation/1"
PCB_CAPTURE_WORKER_RESULT_SCHEMA = "si-tdr-pcb-capture-worker-result/1"
IMAGE_ANALYSIS_SCHEMA = "si-tdr-pcb-image-analysis/2"
VISIBLE_SELECTED_MIN_RED = 140
VISIBLE_SELECTED_MIN_DOMINANCE = 32
VISIBLE_CONTEXT_MIN_CHANNEL = 70
VISIBLE_CONTEXT_MAX_CHANNEL = 220
VISIBLE_CONTEXT_MAX_SPREAD = 8
WINDOWS_RESERVED_FILE_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

DEFAULT_CAPTURE_OPTIONS: dict[str, Any] = {
    "aedtVersion": "2024.2",
    "channels": ["*"],
    "overview": {
        "enabled": True,
        "topLayer": "",
        "bottomLayer": "",
        "fileNames": {
            "top": "pcb_top.png",
            "bottom": "pcb_bottom.png",
        },
    },
    "layers": {
        "mode": "occupied-signal-layers",
        "include": [],
    },
    "region": {
        "mode": "path-primitive-bbox",
        "marginRatio": 0.08,
        "minimumMarginM": 0.001,
    },
    "highlight": {
        "mode": "context",
        "selectedColor": "0x00A5FF",
        "contextColor": "0xB8B8B8",
        "includeReferenceNet": False,
        "referenceColor": "0x004000",
    },
    "view": {
        "mode": "fit-selection",
        "fallback": "fit-all",
        "showDimensionMarkers": False,
        "showGrid": False,
        "showPinNames": False,
    },
    "image": {
        "format": "png",
        "widthPx": 1920,
        "heightPx": 1080,
        "resizeMode": "contain-white",
        "fileNameTemplate": "pcb_{channel}__{layer}.png",
    },
}

DECISION_PENDING_DEFAULTS = [
    {
        "id": "pcb-capture-channel-granularity",
        "status": "decision-pending",
        "default": "one image per logical channel and occupied signal layer",
        "configPath": "pcbCapture.channels / pcbCapture.layers",
    },
    {
        "id": "pcb-capture-overview-layers",
        "status": "decision-pending",
        "default": (
            "first and last PyEDB signal layers as PCB Top and Bottom; "
            "explicit layer overrides are supported"
        ),
        "configPath": "pcbCapture.overview.topLayer / bottomLayer",
    },
    {
        "id": "pcb-capture-region-and-view",
        "status": "decision-pending",
        "default": "path primitive bounding box evidence with SIWave fit-selection",
        "configPath": "pcbCapture.region / pcbCapture.view",
    },
    {
        "id": "pcb-capture-highlight-style",
        "status": "decision-pending",
        "default": "highlight selected route nets with surrounding nets in gray context",
        "configPath": "pcbCapture.highlight",
    },
    {
        "id": "pcb-capture-resolution",
        "status": "decision-pending",
        "default": "1920x1080 PNG, aspect-preserving white padding",
        "configPath": "pcbCapture.image",
    },
]


class PcbCaptureConfigurationError(ValueError):
    """Raised when the PCB capture contract is invalid before Ansys starts."""


class PcbCaptureContractError(RuntimeError):
    """Raised when a completed strict capture package is not trustworthy."""


def ensure_capture_dependencies() -> None:
    """Expose the repository DCIR environment only as a last-resort fallback.

    Prepending that environment can replace the caller's compiled Pillow/PyEDB
    packages with binaries built for a different interpreter.  Strict capture
    evidence must describe the actual caller runtime, so an already configured
    customer environment always wins.
    """

    if DCIR_VENV_SITE_PACKAGES.exists():
        site_packages = str(DCIR_VENV_SITE_PACKAGES.resolve())
        if site_packages not in sys.path:
            sys.path.append(site_packages)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        merged[key] = _deep_merge(value, {}) if isinstance(value, dict) else value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict):
        raise PcbCaptureConfigurationError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=2, ensure_ascii=False)
        fp.write("\n")


def _resolve_si_tdr_path(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    safe = safe.strip("._")
    return safe or "unnamed"


def _casefold_duplicates(values: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            duplicates.append(value)
        else:
            seen[key] = value
    return duplicates


def _path_value(record: dict[str, Any], snake_name: str, camel_name: str | None = None) -> Any:
    if snake_name in record:
        return record[snake_name]
    if camel_name and camel_name in record:
        return record[camel_name]
    return None


def _unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _unique_casefold_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        folded = text.casefold()
        if not text or folded in seen:
            continue
        seen.add(folded)
        result.append(text)
    return result


def _unquote_siw_token(value: str) -> str:
    return str(value).strip("\"'`")


def is_customer_strict_capture(context: dict[str, Any]) -> bool:
    return bool(context.get("customerComponentHandling"))


def _deterministic_casefold_strings(values: Iterable[Any]) -> list[str]:
    return sorted(
        _unique_casefold_strings(values), key=lambda value: (value.casefold(), value)
    )


def _normalized_target_layers(value: Any) -> list[str]:
    """Accept one layer name or an ordered list and return the unique names."""

    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return _unique_strings(values)


def _entry_target_layers(entry: dict[str, Any]) -> list[str]:
    """Return the signal layers a capture entry must make visible."""

    layers = _normalized_target_layers(entry.get("layers"))
    if layers:
        return layers
    return _normalized_target_layers(entry.get("layer"))


def _strict_component_presentation_set(
    *, batch_id: str, start_components: list[str], end_components: list[str]
) -> dict[str, Any]:
    return {
        "schema": COMPONENT_PRESENTATION_SET_SCHEMA,
        "status": "pending" if not start_components or not end_components else "planned",
        "target": {
            "batchId": batch_id,
            "startComponents": list(start_components),
            "endComponents": list(end_components),
        },
        "pathComponentVisibility": {
            "status": "deferred-follow-up",
            "releaseBlocking": False,
            "implementationClaimed": False,
            "reason": (
                "per-component path-only visibility is outside this FullBatch release; "
                "no PyEDB delete or global VIEW_* surrogate is used"
            ),
        },
        "visualConfirmation": {
            "status": "live-review-pending",
            "schema": PCB_VISUAL_CONFIRMATION_SCHEMA,
            "reason": (
                "SIWave 2025.2 human review of the final route capture Zoom, "
                "selected red, and gray context is pending"
            ),
        },
    }


def _normalize_strict_capture_options(
    context: dict[str, Any], raw: dict[str, Any]
) -> dict[str, Any]:
    if raw.get("contract") != STRICT_CAPTURE_CONTRACT:
        raise PcbCaptureConfigurationError(
            f"strict customer PCB capture requires contract={STRICT_CAPTURE_CONTRACT!r}"
        )
    try:
        capture_policy = normalize_pcb_capture_policy(
            raw, allow_extra_fields=True
        )
    except ValueError as exc:
        raise PcbCaptureConfigurationError(str(exc)) from exc
    required = {
        "captureUnit": "snp-batch-union",
        "fileNames": STRICT_CAPTURE_FILENAMES,
        "highlight": {
            "mode": "context",
            "selectedColor": STRICT_SELECTED_COLOR,
            "contextColor": STRICT_CONTEXT_COLOR,
            "includeReferenceNet": False,
        },
    }
    for key, expected in required.items():
        if raw.get(key) != expected:
            raise PcbCaptureConfigurationError(
                f"strict customer PCB capture requires pcbCapture.{key}={expected!r}"
            )
    view = raw.get("view") or {}
    if view.get("mode") != "fit-selection" or view.get("fallback") != "none":
        raise PcbCaptureConfigurationError(
            "strict customer PCB capture requires fit-selection with no fallback"
        )
    image = raw.get("image") or {}
    if image.get("format") != "png" or image.get("resizeMode") != "contain-white":
        raise PcbCaptureConfigurationError(
            "strict customer PCB capture requires contain-white PNG output"
        )
    width = int(image.get("widthPx") or 0)
    height = int(image.get("heightPx") or 0)
    if width <= 0 or height <= 0 or width > 32768 or height > 32768:
        raise PcbCaptureConfigurationError(
            "strict PCB capture dimensions must be between 1 and 32768 pixels"
        )
    component_presentation = raw.get("componentPresentation")
    if not isinstance(component_presentation, dict):
        raise PcbCaptureConfigurationError(
            "strict customer PCB capture requires componentPresentation policy"
        )
    visibility_policy = component_presentation.get("pathComponentVisibility")
    if set(component_presentation) != {"pathComponentVisibility"}:
        raise PcbCaptureConfigurationError(
            "strict componentPresentation accepts only pathComponentVisibility; "
            "the fixed top endpoint banner was removed from the customer contract"
        )
    if visibility_policy != {
        "status": "deferred-follow-up",
        "releaseBlocking": False,
    }:
        raise PcbCaptureConfigurationError(
            "path component visibility must remain a non-blocking deferred follow-up"
        )
    channel_path_report = (
        raw.get("channelPathReport")
        or (context.get("channelPath") or {}).get("report")
        or (context.get("customerComponentHandling") or {}).get("channelPathReport")
    )
    if not channel_path_report:
        raise PcbCaptureConfigurationError(
            "strict customer PCB capture requires the generated Channel Path report"
        )
    aedt_version = str(raw.get("aedtVersion") or context.get("aedtVersion") or "").strip()
    if not re.fullmatch(r"\d{4}\.\d", aedt_version):
        raise PcbCaptureConfigurationError(
            "strict customer PCB capture requires top-level AEDT YYYY.R version"
        )
    region = raw.get("region") or {}
    margin_ratio = float(region.get("marginRatio", 0.08))
    minimum_margin_m = float(region.get("minimumMarginM", 0.001))
    if not all(math.isfinite(value) and value >= 0 for value in (margin_ratio, minimum_margin_m)):
        raise PcbCaptureConfigurationError("strict PCB capture region margins are invalid")
    layers = raw.get("layers") or {}
    return {
        **raw,
        **capture_policy,
        "aedtVersion": aedt_version,
        "channelPathReport": str(_resolve_si_tdr_path(channel_path_report)),
        "fileNames": dict(STRICT_CAPTURE_FILENAMES),
        "highlight": dict(required["highlight"]),
        "view": {
            "mode": "fit-selection",
            "fallback": "none",
            "showDimensionMarkers": bool(view.get("showDimensionMarkers", False)),
            "showGrid": bool(view.get("showGrid", False)),
            "showPinNames": bool(view.get("showPinNames", False)),
        },
        "image": {
            "format": "png",
            "widthPx": width,
            "heightPx": height,
            "resizeMode": "contain-white",
        },
        "componentPresentation": {
            "pathComponentVisibility": dict(visibility_policy),
        },
        "layers": {
            "top": str(layers.get("top") or ""),
            "bottom": str(layers.get("bottom") or ""),
        },
        "region": {
            "mode": "path-primitive-bbox",
            "marginRatio": margin_ratio,
            "minimumMarginM": minimum_margin_m,
        },
    }


def normalize_capture_options(context: dict[str, Any]) -> dict[str, Any]:
    raw = context.get("pcbCapture") or {}
    if not isinstance(raw, dict):
        raise PcbCaptureConfigurationError("pcbCapture must be a JSON object")
    if is_customer_strict_capture(context):
        return _normalize_strict_capture_options(context, raw)

    options = _deep_merge(DEFAULT_CAPTURE_OPTIONS, raw)
    inferred_report = (
        (context.get("channelPath") or {}).get("report")
        or (context.get("seriesModels") or {}).get("channelPathReport")
    )
    channel_path_report = options.get("channelPathReport") or inferred_report
    if not channel_path_report:
        raise PcbCaptureConfigurationError(
            "pcbCapture.channelPathReport is required when "
            "channelPath.report and seriesModels.channelPathReport are not configured"
        )
    options["channelPathReport"] = str(_resolve_si_tdr_path(channel_path_report))

    channels = options.get("channels")
    if channels == "*":
        channels = ["*"]
    if not isinstance(channels, list) or not channels:
        raise PcbCaptureConfigurationError("pcbCapture.channels must be a non-empty list or '*'")
    normalized_channels = [str(item).strip() for item in channels]
    if any(not item for item in normalized_channels):
        raise PcbCaptureConfigurationError(
            "pcbCapture.channels must not contain empty channel names"
        )
    duplicate_channels = _casefold_duplicates(normalized_channels)
    if duplicate_channels:
        raise PcbCaptureConfigurationError(
            "pcbCapture.channels contains duplicate names: "
            + ", ".join(duplicate_channels)
        )
    options["channels"] = normalized_channels
    if "*" in normalized_channels and len(normalized_channels) != 1:
        raise PcbCaptureConfigurationError(
            "pcbCapture.channels '*' must be used alone"
        )

    aedt_version = str(options.get("aedtVersion") or "").strip()
    if not re.fullmatch(r"\d{4}\.\d", aedt_version):
        raise PcbCaptureConfigurationError(
            "pcbCapture.aedtVersion must use YYYY.R form, for example '2024.2'"
        )
    options["aedtVersion"] = aedt_version

    overview = options.get("overview") or {}
    if not isinstance(overview.get("enabled"), bool):
        raise PcbCaptureConfigurationError(
            "pcbCapture.overview.enabled must be a boolean"
        )
    for key in ("topLayer", "bottomLayer"):
        value = overview.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise PcbCaptureConfigurationError(
                f"pcbCapture.overview.{key} must be a layer name or an empty string"
            )
        overview[key] = value.strip()
    overview_file_names = overview.get("fileNames") or {}
    if not isinstance(overview_file_names, dict):
        raise PcbCaptureConfigurationError(
            "pcbCapture.overview.fileNames must be a JSON object"
        )
    normalized_overview_file_names: dict[str, str] = {}
    for view_name in ("top", "bottom"):
        raw_file_name = str(overview_file_names.get(view_name) or "").strip()
        file_name = _safe_name(Path(raw_file_name).name)
        if not raw_file_name or not file_name.casefold().endswith(".png"):
            raise PcbCaptureConfigurationError(
                f"pcbCapture.overview.fileNames.{view_name} must be a PNG filename"
            )
        if Path(file_name).stem.upper() in WINDOWS_RESERVED_FILE_STEMS:
            raise PcbCaptureConfigurationError(
                "pcbCapture.overview.fileNames produced a Windows reserved filename: "
                f"{file_name}"
            )
        normalized_overview_file_names[view_name] = file_name
    duplicate_overview_names = _casefold_duplicates(
        normalized_overview_file_names.values()
    )
    if duplicate_overview_names:
        raise PcbCaptureConfigurationError(
            "pcbCapture.overview Top and Bottom filenames must be different"
        )
    overview["fileNames"] = normalized_overview_file_names
    options["overview"] = overview

    layers = options.get("layers") or {}
    layer_mode = str(layers.get("mode") or "")
    if layer_mode not in {"occupied-signal-layers", "explicit"}:
        raise PcbCaptureConfigurationError(
            "pcbCapture.layers.mode must be 'occupied-signal-layers' or 'explicit'"
        )
    include_layers = [str(item).strip() for item in layers.get("include") or []]
    if layer_mode == "explicit" and not include_layers:
        raise PcbCaptureConfigurationError(
            "pcbCapture.layers.include must not be empty when layers.mode='explicit'"
        )
    if any(not item for item in include_layers):
        raise PcbCaptureConfigurationError(
            "pcbCapture.layers.include must not contain empty layer names"
        )
    duplicate_layers = _casefold_duplicates(include_layers)
    if duplicate_layers:
        raise PcbCaptureConfigurationError(
            "pcbCapture.layers.include contains duplicate names: "
            + ", ".join(duplicate_layers)
        )
    layers["include"] = include_layers

    region = options.get("region") or {}
    if str(region.get("mode")) != "path-primitive-bbox":
        raise PcbCaptureConfigurationError(
            "only pcbCapture.region.mode='path-primitive-bbox' is supported"
        )
    margin_ratio = float(region.get("marginRatio"))
    minimum_margin_m = float(region.get("minimumMarginM"))
    if (
        not math.isfinite(margin_ratio)
        or not math.isfinite(minimum_margin_m)
        or margin_ratio < 0
        or minimum_margin_m < 0
    ):
        raise PcbCaptureConfigurationError(
            "PCB capture region margins must be finite and non-negative"
        )

    highlight = options.get("highlight") or {}
    if str(highlight.get("mode")) not in {"isolate", "context"}:
        raise PcbCaptureConfigurationError(
            "pcbCapture.highlight.mode must be 'isolate' or 'context'"
        )
    for color_key in ("selectedColor", "contextColor", "referenceColor"):
        color = str(highlight.get(color_key) or "")
        if not re.fullmatch(r"0x[0-9A-Fa-f]{6}", color):
            raise PcbCaptureConfigurationError(
                f"pcbCapture.highlight.{color_key} must use 0xRRGGBB format"
            )
    if not isinstance(highlight.get("includeReferenceNet"), bool):
        raise PcbCaptureConfigurationError(
            "pcbCapture.highlight.includeReferenceNet must be a boolean"
        )
    highlight["referenceNet"] = str(
        highlight.get("referenceNet")
        or (context.get("nets") or {}).get("reference")
        or ""
    )
    if highlight["includeReferenceNet"] and not highlight["referenceNet"]:
        raise PcbCaptureConfigurationError(
            "pcbCapture.highlight.referenceNet or nets.reference is required when "
            "includeReferenceNet=true"
        )

    view = options.get("view") or {}
    if str(view.get("mode")) != "fit-selection":
        raise PcbCaptureConfigurationError(
            "only pcbCapture.view.mode='fit-selection' is supported; "
            "no unverified coordinate zoom API is used"
        )
    if str(view.get("fallback")) != "fit-all":
        raise PcbCaptureConfigurationError("only pcbCapture.view.fallback='fit-all' is supported")
    for key in ("showDimensionMarkers", "showGrid", "showPinNames"):
        if not isinstance(view.get(key), bool):
            raise PcbCaptureConfigurationError(f"pcbCapture.view.{key} must be a boolean")

    image = options.get("image") or {}
    if str(image.get("format")).casefold() != "png":
        raise PcbCaptureConfigurationError("only PNG PCB capture output is supported")
    width = int(image.get("widthPx"))
    height = int(image.get("heightPx"))
    if width <= 0 or height <= 0 or width > 32768 or height > 32768:
        raise PcbCaptureConfigurationError(
            "PCB capture image dimensions must be between 1 and 32768 pixels"
        )
    if str(image.get("resizeMode")) != "contain-white":
        raise PcbCaptureConfigurationError(
            "only pcbCapture.image.resizeMode='contain-white' is supported"
        )
    if not str(image.get("fileNameTemplate") or "").strip():
        raise PcbCaptureConfigurationError(
            "pcbCapture.image.fileNameTemplate must not be empty"
        )

    return options


def build_channel_capture_targets(
    path_report: dict[str, Any],
    *,
    selected_channels: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = path_report.get("paths") or []
    if not isinstance(paths, list):
        raise PcbCaptureConfigurationError("channel path report paths must be a list")

    include_all = "*" in selected_channels
    selected = set(selected_channels)
    grouped: dict[str, list[dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    for raw_path in paths:
        if not isinstance(raw_path, dict):
            continue
        channel = str(raw_path.get("channel") or "")
        if not channel or (not include_all and channel not in selected):
            continue
        status = str(raw_path.get("status") or "")
        if not status.startswith("resolved"):
            unresolved.append(
                {
                    "channel": channel,
                    "polarity": raw_path.get("polarity"),
                    "stage": "channel-path-selection",
                    "code": status or "missing-status",
                    "message": raw_path.get("error") or "channel path is not resolved",
                }
            )
            continue
        polarity = str(raw_path.get("polarity") or "")
        if polarity not in {"positive", "negative"}:
            unresolved.append(
                {
                    "channel": channel,
                    "polarity": polarity or None,
                    "stage": "channel-path-selection",
                    "code": "invalid-resolved-path-polarity",
                    "message": (
                        "resolved differential Channel Path must use positive or negative polarity"
                    ),
                }
            )
            continue
        grouped.setdefault(channel, []).append(raw_path)

    targets: list[dict[str, Any]] = []
    for channel in sorted(grouped):
        channel_paths = grouped[channel]
        polarity_counts: dict[str, int] = {}
        for path in channel_paths:
            polarity = str(path.get("polarity") or "")
            polarity_counts[polarity] = polarity_counts.get(polarity, 0) + 1
        duplicate_polarities = sorted(
            polarity for polarity, count in polarity_counts.items() if count > 1
        )
        if duplicate_polarities:
            unresolved.append(
                {
                    "channel": channel,
                    "stage": "channel-path-selection",
                    "code": "duplicate-resolved-polarity-path",
                    "message": (
                        "multiple resolved paths exist for the same channel/polarity; "
                        "capture target was not guessed"
                    ),
                    "polarities": duplicate_polarities,
                }
            )
            continue
        nets: list[str] = []
        start_components: list[str] = []
        endpoint_components: list[str] = []
        path_evidence: list[dict[str, Any]] = []
        polarities: list[str] = []
        for path in channel_paths:
            polarity = str(path.get("polarity") or "unknown")
            polarities.append(polarity)
            start_components.append(
                str(_path_value(path, "start_component", "startComponent") or "")
            )
            endpoint_components.append(
                str(_path_value(path, "endpoint_component", "endpointComponent") or "")
            )
            nets.extend(
                [
                    _path_value(path, "start_net", "startNet"),
                    _path_value(path, "end_net", "endNet"),
                ]
            )
            for step in path.get("steps") or []:
                if isinstance(step, dict):
                    nets.append(step.get("net"))
            path_evidence.append(
                {
                    "polarity": polarity,
                    "status": path.get("status"),
                    "startComponent": _path_value(path, "start_component", "startComponent"),
                    "startPin": _path_value(path, "start_pin", "startPin"),
                    "startNet": _path_value(path, "start_net", "startNet"),
                    "endpointComponent": _path_value(
                        path, "endpoint_component", "endpointComponent"
                    ),
                    "endpointPin": _path_value(path, "endpoint_pin", "endpointPin"),
                    "endNet": _path_value(path, "end_net", "endNet"),
                }
            )

        unique_nets = _unique_strings(nets)
        if not unique_nets:
            unresolved.append(
                {
                    "channel": channel,
                    "stage": "channel-path-selection",
                    "code": "resolved-path-without-route-nets",
                    "message": "resolved Channel Path did not contain a start/end/step net",
                }
            )
            continue
        path_completeness = (
            "complete-differential"
            if {"positive", "negative"}.issubset(set(polarities))
            else "partial"
        )
        if path_completeness == "partial":
            unresolved.append(
                {
                    "channel": channel,
                    "stage": "channel-path-selection",
                    "code": "partial-differential-channel-path",
                    "message": (
                        "only one differential polarity is resolved; the available "
                        "route may be planned but the channel is not complete"
                    ),
                    "resolvedPolarities": _unique_strings(polarities),
                }
            )
        targets.append(
            {
                "channel": channel,
                "polarities": _unique_strings(polarities),
                "nets": unique_nets,
                "startComponents": _unique_strings(start_components),
                "endpointComponents": _unique_strings(endpoint_components),
                "pathEvidence": path_evidence,
                "pathCompleteness": path_completeness,
            }
        )

    if not include_all:
        discovered = set(grouped)
        for missing in sorted(selected - discovered):
            unresolved.append(
                {
                    "channel": missing,
                    "stage": "channel-path-selection",
                    "code": "requested-channel-not-resolved-or-not-found",
                    "message": "requested channel has no resolved path in the channel path report",
                }
            )
    return targets, unresolved


def build_strict_batch_capture_target(
    path_report: dict[str, Any], *, batch_id: str
) -> dict[str, Any]:
    """Union every resolved P/N Channel Path in one generated sNp batch."""

    raw_paths = path_report.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise PcbCaptureConfigurationError(
            f"strict batch {batch_id!r} has no Channel Path records"
        )
    paths = [item for item in raw_paths if isinstance(item, dict)]
    unresolved = [
        {
            "channel": item.get("channel"),
            "polarity": item.get("polarity"),
            "status": item.get("status"),
        }
        for item in paths
        if not str(item.get("status") or "").startswith("resolved")
    ]
    if unresolved:
        raise PcbCaptureConfigurationError(
            f"strict batch {batch_id!r} contains unresolved Channel Paths: {unresolved}"
        )

    by_channel: dict[str, list[dict[str, Any]]] = {}
    for item in paths:
        channel = str(item.get("channel") or "").strip()
        polarity = str(item.get("polarity") or "").strip()
        if not channel or polarity not in {"positive", "negative"}:
            raise PcbCaptureConfigurationError(
                f"strict batch {batch_id!r} has an invalid resolved path role: "
                f"channel={channel!r}, polarity={polarity!r}"
            )
        by_channel.setdefault(channel, []).append(item)
    for channel, channel_paths in by_channel.items():
        polarities = [str(item.get("polarity")) for item in channel_paths]
        if sorted(polarities) != ["negative", "positive"]:
            raise PcbCaptureConfigurationError(
                f"strict batch {batch_id!r} channel {channel!r} requires exactly "
                f"one positive and one negative path; got {polarities}"
            )

    nets: list[Any] = []
    selected_components: list[Any] = []
    endpoint_refdes: list[Any] = []
    evidence: list[dict[str, Any]] = []
    for item in paths:
        start = _path_value(item, "start_component", "startComponent")
        endpoint = _path_value(item, "endpoint_component", "endpointComponent")
        endpoint_refdes.extend((start, endpoint))
        selected_components.extend((start, endpoint))
        nets.extend(
            (
                _path_value(item, "start_net", "startNet"),
                _path_value(item, "end_net", "endNet"),
            )
        )
        step_records: list[dict[str, Any]] = []
        for step in item.get("steps") or []:
            if not isinstance(step, dict):
                continue
            nets.append(step.get("net"))
            selected_components.append(step.get("component"))
            step_records.append(
                {
                    "kind": step.get("kind"),
                    "net": step.get("net"),
                    "component": step.get("component"),
                }
            )
        evidence.append(
            {
                "channel": str(item.get("channel")),
                "polarity": str(item.get("polarity")),
                "status": item.get("status"),
                "startComponent": start,
                "endpointComponent": endpoint,
                "steps": step_records,
            }
        )
    union_nets = _unique_casefold_strings(nets)
    if not union_nets:
        raise PcbCaptureConfigurationError(
            f"strict batch {batch_id!r} contains no resolved path nets"
        )
    start_components = _deterministic_casefold_strings(
        _path_value(item, "start_component", "startComponent") for item in paths
    )
    end_components = _deterministic_casefold_strings(
        _path_value(item, "endpoint_component", "endpointComponent") for item in paths
    )
    if not start_components or not end_components:
        raise PcbCaptureConfigurationError(
            f"strict batch {batch_id!r} requires non-empty start/end component roles"
        )
    return {
        "channel": batch_id,
        "batchId": batch_id,
        "channels": sorted(by_channel, key=str.casefold),
        "polarities": ["positive", "negative"],
        "pathCompleteness": "complete-batch-union",
        "nets": union_nets,
        "selectedComponents": _deterministic_casefold_strings(selected_components),
        "endpointRefdes": _deterministic_casefold_strings(endpoint_refdes),
        "startComponents": start_components,
        "endComponents": end_components,
        "pathEvidence": evidence,
        "pathCount": len(paths),
    }


def build_strict_batch_capture_entries(
    target: dict[str, Any],
    geometry: dict[str, Any],
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    channel_geometry = (geometry.get("channels") or {}).get(target["batchId"]) or {}
    missing_nets = [str(item) for item in channel_geometry.get("missingNets") or []]
    nets_without_geometry = [
        str(item.get("net"))
        for item in channel_geometry.get("netEvidence") or []
        if not int(item.get("usablePrimitiveCount") or 0)
    ]
    if missing_nets or nets_without_geometry:
        raise PcbCaptureConfigurationError(
            "strict batch union geometry is incomplete: "
            f"missing={missing_nets}, noPrimitives={nets_without_geometry}"
        )
    signal_layers = [str(item) for item in geometry.get("signalLayers") or []]
    if not signal_layers:
        raise PcbCaptureConfigurationError(
            "strict PCB capture requires a non-empty signal-layer inventory"
        )
    requested_layers = {
        "top": options["layers"]["top"] or signal_layers[0],
        "bottom": options["layers"]["bottom"] or signal_layers[-1],
    }
    for side, layer in requested_layers.items():
        if layer not in signal_layers:
            raise PcbCaptureConfigurationError(
                f"strict PCB {side} layer is not a signal layer: {layer!r}"
            )
    layer_records = channel_geometry.get("layers") or {}
    bboxes = [
        list(record["bboxM"])
        for record in layer_records.values()
        if isinstance(record, dict) and record.get("bboxM")
    ]
    raw_bbox = _bbox_union(bboxes)
    if raw_bbox is None:
        raise PcbCaptureConfigurationError(
            "strict batch union has no conductive primitive bounding box"
        )
    expanded_bbox, margin_m = _expanded_bbox(
        raw_bbox,
        margin_ratio=float(options["region"]["marginRatio"]),
        minimum_margin_m=float(options["region"]["minimumMarginM"]),
    )
    # 배치 넷이 실제로 점유한 signal 층만 켜고 한 장으로 캡처한다.
    occupied_layers = [
        name
        for name in signal_layers
        if isinstance(layer_records.get(name), dict)
        and layer_records[name].get("bboxM")
    ]
    if not occupied_layers:
        raise PcbCaptureConfigurationError(
            "strict batch union has no occupied signal layer"
        )
    entries: list[dict[str, Any]] = []
    for side in (STRICT_BATCH_CAPTURE_VIEW,):
        entries.append(
            {
                "captureId": "pcb_batch_route",
                "kind": "snp-batch-union",
                "view": side,
                "viewMode": "fit-selection",
                "batchId": target["batchId"],
                "channel": None,
                "channels": list(target["channels"]),
                "polarities": list(target["polarities"]),
                "pathCompleteness": target["pathCompleteness"],
                "nets": list(target["nets"]),
                "netsOnLayer": list(target["nets"]),
                "selectedComponents": list(target["selectedComponents"]),
                "endpointRefdes": list(target["endpointRefdes"]),
                "startComponents": list(target["startComponents"]),
                "endComponents": list(target["endComponents"]),
                "pathEvidence": list(target["pathEvidence"]),
                "layer": None,
                "layers": list(occupied_layers),
                "layerSelection": "occupied-signal-layers",
                "region": {
                    "mode": "path-primitive-bbox",
                    "coordinateUnit": "m",
                    "geometryScope": "union-of-all-resolved-batch-path-net-primitives",
                    "appliedToRender": True,
                    "renderViewControl": "SIWave ScrFitSelection",
                    "rawBboxM": raw_bbox,
                    "expandedBboxM": expanded_bbox,
                    "marginM": margin_m,
                },
                "fileName": STRICT_BATCH_CAPTURE_FILENAME,
                "status": "planned",
            }
        )
    return entries


def _bbox_values(value: Any) -> list[float] | None:
    try:
        values = [float(item) for item in value]
    except Exception:
        return None
    if len(values) != 4 or not all(math.isfinite(item) for item in values):
        return None
    x1, y1, x2, y2 = values
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def _bbox_union(boxes: Iterable[list[float]]) -> list[float] | None:
    materialized = list(boxes)
    if not materialized:
        return None
    return [
        min(item[0] for item in materialized),
        min(item[1] for item in materialized),
        max(item[2] for item in materialized),
        max(item[3] for item in materialized),
    ]


def _expanded_bbox(
    bbox: list[float],
    *,
    margin_ratio: float,
    minimum_margin_m: float,
) -> tuple[list[float], float]:
    width = max(0.0, bbox[2] - bbox[0])
    height = max(0.0, bbox[3] - bbox[1])
    margin = max(minimum_margin_m, max(width, height) * margin_ratio)
    return [
        bbox[0] - margin,
        bbox[1] - margin,
        bbox[2] + margin,
        bbox[3] + margin,
    ], margin


def inspect_channel_geometry(
    edb_path: Path,
    targets: list[dict[str, Any]],
    *,
    aedt_version: str,
) -> dict[str, Any]:
    ensure_capture_dependencies()
    from pyedb import Edb  # noqa: PLC0415

    edb = Edb(
        edbpath=str(edb_path),
        edbversion=aedt_version,
        isreadonly=True,
    )
    try:
        signal_layers = list(getattr(edb.stackup, "signal_layers", {}).keys())
        geometry_by_channel: dict[str, Any] = {}
        for target in targets:
            layers: dict[str, dict[str, Any]] = {}
            missing_nets: list[str] = []
            net_evidence: list[dict[str, Any]] = []
            for net_name in target["nets"]:
                try:
                    net = edb.nets.nets[net_name]
                except Exception:
                    missing_nets.append(net_name)
                    continue
                net_layers: dict[str, int] = {}
                usable_bbox_count = 0
                for primitive in getattr(net, "primitives", []) or []:
                    layer_name = str(getattr(primitive, "layer_name", "") or "")
                    bbox = _bbox_values(getattr(primitive, "bbox", None))
                    if not layer_name or bbox is None:
                        continue
                    usable_bbox_count += 1
                    net_layers[layer_name] = net_layers.get(layer_name, 0) + 1
                    record = layers.setdefault(
                        layer_name,
                        {
                            "primitiveCount": 0,
                            "nets": [],
                            "primitiveBboxesM": [],
                        },
                    )
                    record["primitiveCount"] += 1
                    record["nets"].append(net_name)
                    record["primitiveBboxesM"].append(bbox)
                net_evidence.append(
                    {
                        "net": net_name,
                        "usablePrimitiveCount": usable_bbox_count,
                        "layers": net_layers,
                    }
                )

            for layer_name, record in layers.items():
                record["nets"] = _unique_strings(record["nets"])
                record["bboxM"] = _bbox_union(record.pop("primitiveBboxesM"))
                record["isSignalLayer"] = layer_name in signal_layers

            geometry_by_channel[target["channel"]] = {
                "layers": layers,
                "missingNets": missing_nets,
                "netEvidence": net_evidence,
            }
        return {
            "sourceEdb": str(edb_path),
            "aedtVersion": aedt_version,
            "api": "pyedb.Edb -> nets.nets[net].primitives[].layer_name/bbox",
            "openMode": "read-only",
            "coordinateUnit": "m",
            "geometryScope": (
                "all conductive primitives belonging to each resolved path net; "
                "not clipped between endpoint pins"
            ),
            "signalLayers": signal_layers,
            "channels": geometry_by_channel,
        }
    finally:
        close = getattr(edb, "close_edb", None) or getattr(edb, "close", None)
        if callable(close):
            close()


def expand_targets_with_geometry(
    targets: list[dict[str, Any]],
    geometry: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    configured_layers = options["layers"]
    region_config = options["region"]
    used_ids: set[str] = set()
    used_file_names: dict[str, str] = {}
    signal_layers = [str(item) for item in geometry.get("signalLayers") or []]
    signal_layer_set = set(signal_layers)

    for target in targets:
        channel = target["channel"]
        channel_geometry = (geometry.get("channels") or {}).get(channel) or {}
        layer_records = channel_geometry.get("layers") or {}
        missing_nets = [str(item) for item in channel_geometry.get("missingNets") or []]
        nets_without_geometry = [
            str(record.get("net"))
            for record in channel_geometry.get("netEvidence") or []
            if not int(record.get("usablePrimitiveCount") or 0)
        ]
        if missing_nets or nets_without_geometry:
            unresolved.append(
                {
                    "channel": channel,
                    "stage": "geometry",
                    "code": "route-net-geometry-incomplete",
                    "message": (
                        "one or more resolved path nets are absent from the reference AEDB "
                        "or have no usable conductive primitive bbox"
                    ),
                    "missingNets": missing_nets,
                    "netsWithoutUsablePrimitives": nets_without_geometry,
                }
            )
            continue
        if configured_layers["mode"] == "explicit":
            layer_names = configured_layers["include"]
        else:
            layer_names = [
                name
                for name in signal_layers
                if name in layer_records and bool(layer_records[name].get("isSignalLayer"))
            ]

        if not layer_names:
            unresolved.append(
                {
                    "channel": channel,
                    "stage": "geometry",
                    "code": "no-target-signal-layer",
                    "message": "no occupied signal layer was found for the resolved route nets",
                    "missingNets": channel_geometry.get("missingNets") or [],
                }
            )
            continue

        for layer_name in layer_names:
            layer_geometry = layer_records.get(layer_name)
            if layer_name not in signal_layer_set or not bool(
                (layer_geometry or {}).get("isSignalLayer")
            ):
                unresolved.append(
                    {
                        "channel": channel,
                        "layer": layer_name,
                        "stage": "geometry",
                        "code": "selected-layer-is-not-signal-layer",
                        "message": "selected layer is not a PyEDB stackup signal layer",
                    }
                )
                continue
            if not layer_geometry or not layer_geometry.get("bboxM"):
                unresolved.append(
                    {
                        "channel": channel,
                        "layer": layer_name,
                        "stage": "geometry",
                        "code": "no-route-primitive-on-layer",
                        "message": "selected layer has no route primitive bbox for this channel",
                    }
                )
                continue

            raw_bbox = list(layer_geometry["bboxM"])
            expanded_bbox, margin_m = _expanded_bbox(
                raw_bbox,
                margin_ratio=float(region_config["marginRatio"]),
                minimum_margin_m=float(region_config["minimumMarginM"]),
            )
            base_capture_id = f"pcb_{_safe_name(channel)}__{_safe_name(layer_name)}"
            capture_id = base_capture_id
            if capture_id.casefold() in used_ids:
                suffix = 2
                while f"{base_capture_id}_{suffix}".casefold() in used_ids:
                    suffix += 1
                capture_id = f"{base_capture_id}_{suffix}"
            used_ids.add(capture_id.casefold())

            filename_template = str(options["image"]["fileNameTemplate"])
            try:
                filename = filename_template.format(
                    channel=_safe_name(channel),
                    layer=_safe_name(layer_name),
                    capture_id=capture_id,
                )
            except (KeyError, ValueError) as exc:
                raise PcbCaptureConfigurationError(
                    f"invalid pcbCapture.image.fileNameTemplate: {exc}"
                ) from exc
            filename = _safe_name(Path(filename).name)
            if not filename.casefold().endswith(".png"):
                filename += ".png"
            if Path(filename).stem.upper() in WINDOWS_RESERVED_FILE_STEMS:
                raise PcbCaptureConfigurationError(
                    "pcbCapture.image.fileNameTemplate produced a Windows reserved "
                    f"filename: {filename}"
                )
            if len(filename) > 240:
                raise PcbCaptureConfigurationError(
                    "pcbCapture.image.fileNameTemplate produced a filename longer "
                    "than 240 characters"
                )
            filename_key = filename.casefold()
            previous_capture = used_file_names.get(filename_key)
            if previous_capture is not None:
                raise PcbCaptureConfigurationError(
                    "pcbCapture.image.fileNameTemplate produces a duplicate output "
                    f"filename for {previous_capture} and {capture_id}: {filename}"
                )
            used_file_names[filename_key] = capture_id

            entries.append(
                {
                    "captureId": capture_id,
                    "channel": channel,
                    "polarities": target["polarities"],
                    "pathCompleteness": target["pathCompleteness"],
                    "nets": target["nets"],
                    "netsOnLayer": layer_geometry.get("nets") or [],
                    "startComponents": target["startComponents"],
                    "endpointComponents": target["endpointComponents"],
                    "pathEvidence": target["pathEvidence"],
                    "layer": layer_name,
                    "region": {
                        "mode": "path-primitive-bbox",
                        "coordinateUnit": "m",
                        "geometryScope": "all-primitives-of-resolved-path-nets",
                        "appliedToRender": False,
                        "renderViewControl": "SIWave ScrFitSelection",
                        "rawBboxM": raw_bbox,
                        "expandedBboxM": expanded_bbox,
                        "marginM": margin_m,
                        "primitiveCount": int(layer_geometry.get("primitiveCount") or 0),
                    },
                    "fileName": filename,
                    "status": "planned",
                }
            )
    return entries, unresolved


def build_overview_capture_entries(
    geometry: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    overview = options["overview"]
    if not bool(overview["enabled"]):
        return [], []

    signal_layers = [str(item) for item in geometry.get("signalLayers") or []]
    if not signal_layers:
        return [], [
            {
                "stage": "geometry",
                "code": "no-signal-layer-for-board-overview",
                "message": "PyEDB returned no signal layer for PCB Top/Bottom capture",
            }
        ]

    requested_layers = {
        "top": str(overview.get("topLayer") or signal_layers[0]),
        "bottom": str(overview.get("bottomLayer") or signal_layers[-1]),
    }
    entries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for view_name in ("top", "bottom"):
        layer_name = requested_layers[view_name]
        if layer_name not in signal_layers:
            unresolved.append(
                {
                    "captureId": f"pcb_overview_{view_name}",
                    "stage": "geometry",
                    "code": "overview-layer-is-not-signal-layer",
                    "message": (
                        f"PCB {view_name} layer '{layer_name}' is not present in the "
                        "PyEDB signal-layer inventory"
                    ),
                    "requestedLayer": layer_name,
                    "signalLayers": signal_layers,
                }
            )
            continue
        entries.append(
            {
                "captureId": f"pcb_overview_{view_name}",
                "kind": "board-overview",
                "view": view_name,
                "viewMode": "fit-all",
                "channel": None,
                "polarities": [],
                "pathCompleteness": None,
                "nets": [],
                "netsOnLayer": [],
                "startComponents": [],
                "endpointComponents": [],
                "pathEvidence": None,
                "layer": layer_name,
                "layerSelection": (
                    "explicit"
                    if overview.get(f"{view_name}Layer")
                    else "derived-from-stackup-order"
                ),
                "region": {
                    "mode": "full-board",
                    "appliedToRender": True,
                    "renderViewControl": "SIWave ScrFitAll",
                },
                "fileName": overview["fileNames"][view_name],
                "status": "planned",
            }
        )
    return entries, unresolved


def _planned_overview_entries(options: dict[str, Any]) -> list[dict[str, Any]]:
    overview = options["overview"]
    if not bool(overview["enabled"]):
        return []
    entries: list[dict[str, Any]] = []
    for view_name in ("top", "bottom"):
        explicit_layer = str(overview.get(f"{view_name}Layer") or "")
        entries.append(
            {
                "captureId": f"pcb_overview_{view_name}",
                "kind": "board-overview",
                "view": view_name,
                "viewMode": "fit-all",
                "channel": None,
                "polarities": [],
                "pathCompleteness": None,
                "nets": [],
                "netsOnLayer": [],
                "startComponents": [],
                "endpointComponents": [],
                "pathEvidence": None,
                "layer": explicit_layer or None,
                "layerSelection": (
                    "explicit" if explicit_layer else "pending-stackup-inspection"
                ),
                "region": {
                    "mode": "full-board",
                    "status": (
                        "configured"
                        if explicit_layer
                        else "pending-license-required-stackup-inspection"
                    ),
                },
                "fileName": overview["fileNames"][view_name],
                "status": "planned",
            }
        )
    return entries


def _ensure_unique_capture_filenames(entries: list[dict[str, Any]]) -> None:
    used: dict[str, str] = {}
    for entry in entries:
        file_name = entry.get("fileName")
        if not file_name:
            continue
        key = str(file_name).casefold()
        previous = used.get(key)
        if previous is not None:
            raise PcbCaptureConfigurationError(
                "PCB capture entries produce a duplicate output filename for "
                f"{previous} and {entry['captureId']}: {file_name}"
            )
        used[key] = str(entry["captureId"])


def rewrite_siw_view_state(
    source_path: Path,
    output_path: Path,
    *,
    target_layer: str | Iterable[str],
    selected_nets: list[str],
    highlight: dict[str, Any],
    show_dimension_markers: bool = False,
    show_grid: bool = False,
    show_pin_names: bool = False,
    show_all_nets: bool = False,
    preserve_other_net_colors: bool = False,
    net_state_strategy: str = "serialized-colors",
    fill_target_layers: bool = True,
) -> dict[str, Any]:
    if net_state_strategy not in {"serialized-colors", "preserve-net-state"}:
        raise ValueError(
            "unsupported SIWave net-state strategy: "
            f"{net_state_strategy!r}"
        )
    target_layers = _normalized_target_layers(target_layer)
    if not target_layers:
        raise ValueError("SIWave view-state rewrite requires at least one target layer")
    text = source_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    selected = set(selected_nets)
    layer_section = False
    net_section = False
    layers_found: set[str] = set()
    nets_found: set[str] = set()
    net_names: list[str] = []
    net_name_keys: set[str] = set()
    layer_records_changed = 0
    filled_target_layer_records = 0
    preserved_layer_color_records = 0
    net_records_changed = 0
    selected_net_records_changed = 0
    context_net_records_changed = 0
    reference_net_records_changed = 0
    view_directives_found = {
        "VIEW_GRID": False,
        "VIEW_PIN_NAMES": False,
        "VIEW_DIM_MARKER": False,
    }
    output_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped in {"B_LAYERS", "E_LAYERS", "B_NETS", "E_NETS"}:
            layer_section = stripped == "B_LAYERS"
            net_section = stripped == "B_NETS"
            output_lines.append(line)
            continue

        if stripped.startswith("VIEW_GRID") or stripped.startswith("VIEW_PIN_NAMES"):
            key = stripped.split()[0]
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            view_directives_found[key] = True
            visible = show_grid if key == "VIEW_GRID" else show_pin_names
            output_lines.append(f"{key} {1 if visible else 0}{newline}")
            continue
        if stripped.startswith("VIEW_DIM_MARKER"):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            view_directives_found["VIEW_DIM_MARKER"] = True
            output_lines.append(
                f"VIEW_DIM_MARKER {1 if show_dimension_markers else 0}{newline}"
            )
            continue

        values = line.split()
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        if layer_section and len(values) > 15 and values[2] == "METAL":
            layer_name = _unquote_siw_token(values[1])
            layers_found.add(layer_name)
            original_color_token = values[6]
            is_target_layer = layer_name in target_layers
            values[7] = "1" if is_target_layer and fill_target_layers else "0"
            values[11:16] = ["1"] * 5 if is_target_layer else ["0"] * 5
            if values[6] != original_color_token:
                raise RuntimeError(
                    f"SIWave layer color token changed unexpectedly: {layer_name}"
                )
            preserved_layer_color_records += 1
            if is_target_layer and fill_target_layers:
                filled_target_layer_records += 1
            output_lines.append(" ".join(values) + newline)
            layer_records_changed += 1
            continue

        if net_section and len(values) > 5:
            net_name = _unquote_siw_token(values[1])
            net_name_key = net_name.casefold()
            if net_name_key in net_name_keys:
                raise RuntimeError(
                    "duplicate case-insensitive net name in SIWave B_NETS: "
                    f"{net_name}"
                )
            net_name_keys.add(net_name_key)
            net_names.append(net_name)
            nets_found.add(net_name)
            if net_name in selected:
                selected_net_records_changed += 1
            elif (
                bool(highlight.get("includeReferenceNet"))
                and net_name == str(highlight.get("referenceNet") or "")
            ):
                reference_net_records_changed += 1
            else:
                context_net_records_changed += 1

            if net_state_strategy == "preserve-net-state":
                output_lines.append(line)
                continue

            if net_name in selected:
                values[4] = str(highlight["selectedColor"])
                values[5] = "1"
            elif (
                bool(highlight.get("includeReferenceNet"))
                and net_name == str(highlight.get("referenceNet") or "")
            ):
                values[4] = str(highlight["referenceColor"])
                values[5] = "1"
            elif show_all_nets:
                if not preserve_other_net_colors:
                    values[4] = str(highlight["contextColor"])
                values[5] = "1"
            elif str(highlight["mode"]) == "isolate":
                values[5] = "0"
            else:
                values[4] = str(highlight["contextColor"])
                values[5] = "1"
            output_lines.append(" ".join(values) + newline)
            net_records_changed += 1
            continue

        output_lines.append(line)

    missing_layers = [name for name in target_layers if name not in layers_found]
    if missing_layers:
        raise RuntimeError(
            "target layer not found in SIWave view state: "
            + ", ".join(missing_layers)
        )
    missing_nets = sorted(selected - nets_found)
    if selected and len(missing_nets) == len(selected):
        raise RuntimeError("none of the target route nets were found in the SIWave view state")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(output_lines), encoding="utf-8")
    deterministic_net_names = sorted(net_names, key=lambda value: (value.casefold(), value))
    net_inventory_json = json.dumps(
        deterministic_net_names,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "sourcePath": str(source_path),
        "viewStatePath": str(output_path),
        "targetLayer": target_layers[0] if len(target_layers) == 1 else None,
        "targetLayers": list(target_layers),
        "targetNets": selected_nets,
        "missingTargetNets": missing_nets,
        "layerRecordsChanged": layer_records_changed,
        "layerFillState": {
            "mechanism": "Job-local SIWave B_LAYERS fill token",
            "enabled": fill_target_layers,
            "targetLayers": list(target_layers),
            "targetFillToken": "1" if fill_target_layers else "0",
            "nonTargetFillToken": "0",
            "filledTargetLayerRecordCount": filled_target_layer_records,
            "phase": "pre-open-view-state-rewrite",
        },
        "layerColorState": {
            "mechanism": "preserved SIWave B_LAYERS color token",
            "colorTokenIndex": 6,
            "preservedLayerRecordCount": preserved_layer_color_records,
            "changedLayerRecordCount": 0,
            "phase": "pre-open-view-state-rewrite",
        },
        "netRecordsChanged": net_records_changed,
        "netStateStrategy": net_state_strategy,
        "netInventory": {
            "order": "casefold-then-original",
            "count": len(deterministic_net_names),
            "names": deterministic_net_names,
            "sha256": hashlib.sha256(net_inventory_json).hexdigest(),
        },
        "colorState": {
            "mechanism": "package-local SIWave B_NETS view-state records",
            "selectedColor": str(highlight["selectedColor"]),
            "contextColor": str(highlight["contextColor"]),
            "selectedNetRecordCount": selected_net_records_changed,
            "contextNetRecordCount": context_net_records_changed,
            "referenceNetRecordCount": reference_net_records_changed,
            "includeReferenceNet": bool(highlight.get("includeReferenceNet")),
            "serializedColorsApplied": net_state_strategy == "serialized-colors",
            "phase": "pre-open-view-state-rewrite",
        },
        "showDimensionMarkers": show_dimension_markers,
        "showGrid": show_grid,
        "showPinNames": show_pin_names,
        "showAllNets": show_all_nets,
        "preserveOtherNetColors": preserve_other_net_colors,
        "viewDirectivesFound": view_directives_found,
        "mechanism": "SIWave text view-state rewrite; source project remains unchanged",
    }


def _validate_strict_view_state_evidence(
    evidence: dict[str, Any], *, selected_nets: list[str]
) -> None:
    unique_selected = _unique_casefold_strings(selected_nets)
    missing_nets = [str(item) for item in evidence.get("missingTargetNets") or []]
    if missing_nets:
        raise RuntimeError(
            "strict PCB capture view state is missing target nets: "
            + ", ".join(missing_nets)
        )
    color_state = evidence.get("colorState") or {}
    selected_record_count = int(color_state.get("selectedNetRecordCount") or 0)
    if selected_record_count != len(unique_selected):
        raise RuntimeError(
            "strict PCB capture selected net record count mismatch: "
            f"expected {len(unique_selected)}, got {selected_record_count}"
        )
    target_layers = _normalized_target_layers(evidence.get("targetLayers"))
    fill_state = evidence.get("layerFillState") or {}
    if (
        fill_state.get("enabled") is not True
        or fill_state.get("targetLayers") != target_layers
        or fill_state.get("targetFillToken") != "1"
        or fill_state.get("nonTargetFillToken") != "0"
        or int(fill_state.get("filledTargetLayerRecordCount") or 0)
        != len(target_layers)
    ):
        raise RuntimeError(
            "strict PCB capture target layer filled view state was not prepared"
        )
    color_state = evidence.get("layerColorState") or {}
    if (
        color_state.get("colorTokenIndex") != 6
        or int(color_state.get("preservedLayerRecordCount") or 0)
        != int(evidence.get("layerRecordsChanged") or 0)
        or color_state.get("changedLayerRecordCount") != 0
    ):
        raise RuntimeError(
            "strict PCB capture original layer color view state was not preserved"
        )


def detect_siwave_capabilities(project: object) -> dict[str, bool]:
    method_names = [
        "ScrUnselectAll",
        "ScrSelectNet",
        "ScrShowSelectedNetsOnly",
        "ScrFitSelection",
        "ScrFitAll",
        "ScrSetLayerVisibility",
        "ScrEditNetColor",
        "ScrSaveToPngFile",
        "ScrCloseProjectNoSave",
        "GetFilePath",
    ]
    capabilities: dict[str, bool] = {}
    for method_name in method_names:
        try:
            capabilities[method_name] = callable(getattr(project, method_name))
        except Exception:
            capabilities[method_name] = False
    return capabilities


def _result_succeeded(value: Any) -> bool:
    """Interpret documented SIWave BOOL results while accepting None-return APIs."""
    return value is not False and value != 0


def _json_safe_return(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def inspect_siwave_capture_capabilities(
    project_path: Path,
    *,
    aedt_version: str,
) -> dict[str, Any]:
    """Open a package-local SIW read-only in practice and inventory its runtime API.

    SIWave does not expose a separate no-license TypeLib instance through PyEDB.
    This production preflight therefore opens only the already-copied capture
    project, closes it without save, and proves that its bytes and timestamp did
    not change.  It never opens or mutates the customer source SIW.
    """
    ensure_capture_dependencies()
    from pyedb.siwave import Siwave  # noqa: PLC0415

    resolved = project_path.resolve()
    before = project_path.stat()
    before_hash = hashlib.sha256(project_path.read_bytes()).hexdigest()
    app = None
    project = None
    close_return: Any = None
    open_return: Any = None
    active_path: Path | None = None
    capabilities: dict[str, bool] | None = None
    caught: Exception | None = None
    try:
        app = Siwave(specified_version=aedt_version)
        open_project = getattr(app, "open_project", None)
        if not callable(open_project):
            raise RuntimeError("pyedb.siwave.Siwave.open_project is unavailable")
        open_return = open_project(str(project_path))
        project = app.oproject
        if project is None:
            raise RuntimeError("SIWave capability preflight did not expose a project")
        capabilities = detect_siwave_capabilities(project)
        if not capabilities["GetFilePath"]:
            raise RuntimeError("SIWave capability preflight requires GetFilePath")
        active_path = Path(str(project.GetFilePath())).resolve()
        if str(active_path).casefold() != str(resolved).casefold():
            raise RuntimeError(
                "SIWave capability preflight opened a different project: "
                f"{active_path}"
            )
        if not capabilities["ScrCloseProjectNoSave"]:
            raise RuntimeError(
                "SIWave capability preflight requires ScrCloseProjectNoSave"
            )
    except Exception as exc:  # preserve the original error after safe close
        caught = exc
    finally:
        if project is not None:
            close_project = getattr(project, "ScrCloseProjectNoSave", None)
            if callable(close_project):
                try:
                    close_return = close_project()
                    if not _result_succeeded(close_return) and caught is None:
                        caught = RuntimeError(
                            "SIWave capability preflight close-without-save failed"
                        )
                except Exception as exc:
                    if caught is None:
                        caught = RuntimeError(
                            "SIWave capability preflight close-without-save failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
        if app is not None:
            quit_application = getattr(app, "quit_application", None)
            if callable(quit_application):
                try:
                    quit_application()
                except Exception:
                    pass
    if caught is not None:
        raise caught
    after = project_path.stat()
    after_hash = hashlib.sha256(project_path.read_bytes()).hexdigest()
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after_hash != before_hash
    ):
        raise RuntimeError(
            "SIWave capability preflight changed the package-local project"
        )
    assert capabilities is not None and active_path is not None
    return {
        "status": "verified-no-save",
        "aedtVersion": aedt_version,
        "requestedPath": str(resolved),
        "activePath": str(active_path),
        "pathMatch": True,
        "sizeBytes": after.st_size,
        "sha256": after_hash,
        "mtimeNs": after.st_mtime_ns,
        "openProjectReturn": _json_safe_return(open_return),
        "closeWithoutSaveReturn": _json_safe_return(close_return),
        "sourceCustomerProjectOpened": False,
        "capabilities": capabilities,
    }


def prepare_strict_siw_capture_view_state(
    source_path: Path,
    output_path: Path,
    *,
    target_layer: str | Iterable[str],
    selected_nets: list[str],
    highlight: dict[str, Any],
    view: dict[str, Any],
    aedt_version: str,
) -> dict[str, Any]:
    """Choose the measured strict color path without mutating the source SIW."""
    rewrite = rewrite_siw_view_state(
        source_path,
        output_path,
        target_layer=target_layer,
        selected_nets=selected_nets,
        highlight=highlight,
        show_dimension_markers=bool(view["showDimensionMarkers"]),
        show_grid=bool(view["showGrid"]),
        show_pin_names=bool(view["showPinNames"]),
        show_all_nets=False,
        preserve_other_net_colors=True,
        net_state_strategy="preserve-net-state",
    )
    _validate_strict_view_state_evidence(rewrite, selected_nets=selected_nets)
    preflight = inspect_siwave_capture_capabilities(
        output_path,
        aedt_version=aedt_version,
    )
    supports_api = bool(
        (preflight.get("capabilities") or {}).get("ScrEditNetColor")
    )
    strategy = "api-all-nets-gray-then-target-red"
    if not supports_api:
        rewrite = rewrite_siw_view_state(
            source_path,
            output_path,
            target_layer=target_layer,
            selected_nets=selected_nets,
            highlight=highlight,
            show_dimension_markers=bool(view["showDimensionMarkers"]),
            show_grid=bool(view["showGrid"]),
            show_pin_names=bool(view["showPinNames"]),
            show_all_nets=False,
            preserve_other_net_colors=False,
            net_state_strategy="serialized-colors",
        )
        _validate_strict_view_state_evidence(rewrite, selected_nets=selected_nets)
        strategy = "serialized-b-nets-fallback"
    inventory = rewrite.get("netInventory") or {}
    net_names = list(inventory.get("names") or [])
    color_state = rewrite.get("colorState") or {}
    if (
        color_state.get("selectedColor") != STRICT_SELECTED_COLOR
        or color_state.get("contextColor") != STRICT_CONTEXT_COLOR
        or color_state.get("includeReferenceNet") is not False
        or int(color_state.get("selectedNetRecordCount") or 0) <= 0
        or int(color_state.get("contextNetRecordCount") or 0) <= 0
        or int(inventory.get("count") or 0) != len(net_names)
        or (
            supports_api
            and (
                rewrite.get("netStateStrategy") != "preserve-net-state"
                or color_state.get("serializedColorsApplied") is not False
                or int(rewrite.get("netRecordsChanged") or 0) != 0
            )
        )
        or (
            not supports_api
            and (
                rewrite.get("netStateStrategy") != "serialized-colors"
                or color_state.get("serializedColorsApplied") is not True
            )
        )
    ):
        raise RuntimeError(
            "strict red-selected/gray-context SIWave view state was not prepared"
        )
    return {
        "strategy": strategy,
        "allNetNames": net_names,
        "rewrite": rewrite,
        "capabilityPreflight": preflight,
    }


def _normalize_png(path: Path, *, width: int, height: int) -> dict[str, Any]:
    from PIL import Image, ImageOps  # noqa: PLC0415

    with Image.open(path) as image:
        before = [int(image.width), int(image.height)]
        normalized = ImageOps.pad(
            image.convert("RGB"),
            (width, height),
            method=Image.Resampling.LANCZOS,
            color="white",
            centering=(0.5, 0.5),
        )
        normalized.save(path, format="PNG")
    with Image.open(path) as image:
        after = [int(image.width), int(image.height)]
    return {
        "sourceResolutionPx": before,
        "actualResolutionPx": after,
        "resizeMode": "contain-white",
    }


def _rgb_from_hex(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"0x[0-9A-Fa-f]{6}", value):
        raise RuntimeError(f"invalid strict PCB RGB color: {value!r}")
    packed = int(value[2:], 16)
    return ((packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF)


def analyze_strict_capture_png(
    path: Path,
    *,
    selected_color: str = STRICT_SELECTED_COLOR,
    context_color: str = STRICT_CONTEXT_COLOR,
) -> dict[str, Any]:
    """Re-open the PNG and measure visible red/gray render classes.

    SIWave can blend the requested net colors with its layer/theme colors before
    writing the native PNG.  The exact requested RGB counts are retained as
    diagnostic evidence, while strict success uses fixed, recorded visible-color
    predicates that survive that renderer-owned blending.
    """
    selected_rgb = _rgb_from_hex(selected_color)
    context_rgb = _rgb_from_hex(context_color)
    raw = path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"strict PCB capture is not a valid PNG: {path}")
    offset = 8
    ihdr: bytes | None = None
    compressed = bytearray()
    saw_iend = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise RuntimeError(f"strict PCB capture PNG chunk is truncated: {path}")
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(raw):
            raise RuntimeError(f"strict PCB capture PNG payload is truncated: {path}")
        chunk_data = raw[data_start:data_end]
        recorded_crc = struct.unpack(">I", raw[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if recorded_crc != actual_crc:
            raise RuntimeError(f"strict PCB capture PNG CRC differs: {path}")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise RuntimeError(f"strict PCB capture PNG IHDR differs: {path}")
            ihdr = chunk_data
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            saw_iend = True
            offset = crc_end
            break
        offset = crc_end
    if ihdr is None or not compressed or not saw_iend or offset != len(raw):
        raise RuntimeError(f"strict PCB capture PNG structure is incomplete: {path}")
    width, height, bit_depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if (
        width <= 0
        or height <= 0
        or bit_depth != 8
        or color_type != 2
        or compression != 0
        or filter_method != 0
        or interlace != 0
    ):
        raise RuntimeError(
            "strict PCB capture must be a normalized 8-bit non-interlaced RGB PNG"
        )
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size != (width, height):
                raise RuntimeError(
                    "strict PCB capture Pillow decode differs from normalized PNG header"
                )
            colors = image.getcolors(maxcolors=width * height)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"strict PCB capture PNG data is invalid: {path}") from exc
    if colors is None:
        raise RuntimeError(f"strict PCB capture color inventory is unavailable: {path}")
    counts = {tuple(color): int(count) for count, color in colors}
    exact_selected_count = counts.get(selected_rgb, 0)
    exact_context_count = counts.get(context_rgb, 0)
    selected_count = sum(
        count
        for (red, green, blue), count in counts.items()
        if red >= VISIBLE_SELECTED_MIN_RED
        and red - green >= VISIBLE_SELECTED_MIN_DOMINANCE
        and red - blue >= VISIBLE_SELECTED_MIN_DOMINANCE
    )
    context_count = sum(
        count
        for (red, green, blue), count in counts.items()
        if VISIBLE_CONTEXT_MIN_CHANNEL <= red <= VISIBLE_CONTEXT_MAX_CHANNEL
        and max(red, green, blue) - min(red, green, blue)
        <= VISIBLE_CONTEXT_MAX_SPREAD
    )
    return {
        "schema": IMAGE_ANALYSIS_SCHEMA,
        "status": "verified",
        "analysisMethod": "visible-red-gray-class-count",
        "imageSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": "PNG",
        "colorMode": "RGB",
        "widthPx": width,
        "heightPx": height,
        "selectedColor": selected_color,
        "selectedPixelCount": selected_count,
        "selectedExactPixelCount": exact_selected_count,
        "selectedVisibleClass": {
            "minimumRed": VISIBLE_SELECTED_MIN_RED,
            "minimumDominance": VISIBLE_SELECTED_MIN_DOMINANCE,
            "predicate": "r>=140 and r-g>=32 and r-b>=32",
        },
        "contextColor": context_color,
        "contextPixelCount": context_count,
        "contextExactPixelCount": exact_context_count,
        "contextVisibleClass": {
            "minimumChannel": VISIBLE_CONTEXT_MIN_CHANNEL,
            "maximumChannel": VISIBLE_CONTEXT_MAX_CHANNEL,
            "maximumChannelSpread": VISIBLE_CONTEXT_MAX_SPREAD,
            "predicate": "70<=r<=220 and max(r,g,b)-min(r,g,b)<=8",
        },
    }


def _deterministic_role_values(value: Any, *, label: str) -> list[str]:
    result = _strict_sequence(value, label=label)
    expected = _deterministic_casefold_strings(result)
    if result != expected:
        raise PcbCaptureContractError(
            f"{label} must use deterministic case-insensitive order"
        )
    return result


def _render_siwave_capture(
    *,
    project_path: Path,
    image_path: Path,
    entry: dict[str, Any],
    all_signal_layers: list[str],
    aedt_version: str,
    image_options: dict[str, Any],
    highlight_options: dict[str, Any],
    require_runtime_filtering: bool,
    strict_customer: bool = False,
) -> dict[str, Any]:
    ensure_capture_dependencies()
    from pyedb.siwave import Siwave  # noqa: PLC0415

    app = None
    project = None
    requested_view = str(entry.get("viewMode") or "fit-selection")
    if requested_view not in {"fit-selection", "fit-all"}:
        raise RuntimeError(f"unsupported SIWave capture view: {requested_view}")
    if strict_customer:
        if requested_view != "fit-selection":
            raise RuntimeError("strict customer PCB capture forbids Fit All")
        if highlight_options != {
            "mode": "context",
            "selectedColor": STRICT_SELECTED_COLOR,
            "contextColor": STRICT_CONTEXT_COLOR,
            "includeReferenceNet": False,
        }:
            raise RuntimeError("strict customer PCB capture highlight policy drifted")
        net_color_strategy = str(entry.get("netColorStrategy") or "")
        if net_color_strategy not in {
            "api-all-nets-gray-then-target-red",
            "serialized-b-nets-fallback",
        }:
            raise RuntimeError("strict PCB capture net color strategy is missing")
        all_net_names = [str(value) for value in entry.get("allNetNames") or []]
        expected_inventory = sorted(
            all_net_names, key=lambda value: (value.casefold(), value)
        )
        if (
            not all_net_names
            or all_net_names != expected_inventory
            or len({value.casefold() for value in all_net_names}) != len(all_net_names)
        ):
            raise RuntimeError(
                "strict PCB capture all-net inventory is empty, duplicated, or unordered"
            )
        inventory_by_key = {value.casefold(): value for value in all_net_names}
        requested_target_nets = _unique_casefold_strings(entry["netsOnLayer"])
        missing_inventory_targets = [
            value
            for value in requested_target_nets
            if value.casefold() not in inventory_by_key
        ]
        if missing_inventory_targets:
            raise RuntimeError(
                "strict PCB capture target nets are absent from the all-net inventory: "
                + ", ".join(missing_inventory_targets)
            )
        target_color_names = sorted(
            [inventory_by_key[value.casefold()] for value in requested_target_nets],
            key=lambda value: (value.casefold(), value),
        )
        net_inventory_sha256 = hashlib.sha256(
            json.dumps(
                all_net_names,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    else:
        net_color_strategy = "non-strict"
        all_net_names = []
        target_color_names = []
        net_inventory_sha256 = None
    render: dict[str, Any] = {
        "backend": "pyedb.siwave.Siwave + SIWave scripting",
        "aedtVersion": aedt_version,
        "executionMode": "graphical-windows-com",
        "headlessSupported": False,
        "sourceProjectFileName": project_path.name,
        "requestedView": requested_view,
        "requestedLayer": entry["layer"],
        "requestedLayers": _entry_target_layers(entry),
        "requestedHighlight": highlight_options,
        "requestedResolutionPx": [
            int(image_options["widthPx"]),
            int(image_options["heightPx"]),
        ],
        "apiEvidence": {
            "localRepository": "DCIR/source/EBU_lib/SIwave.py export_layer_images",
            "officialGuide": (
                "https://ansyshelp.ansys.com/public/Views/Secured/Electronics/"
                "v242/en/PDFs/SIwaveScriptingGuide.pdf"
            ),
        },
    }
    requested_project_stat = project_path.stat()
    requested_project_sha256 = hashlib.sha256(project_path.read_bytes()).hexdigest()
    try:
        app = Siwave(specified_version=aedt_version)
        open_project = getattr(app, "open_project", None)
        if not callable(open_project):
            raise RuntimeError("pyedb.siwave.Siwave.open_project is unavailable")
        open_result = open_project(str(project_path))
        render["openProjectReturn"] = _json_safe_return(open_result)
        project = app.oproject
        if project is None:
            raise RuntimeError("SIWave did not expose an active project after open_project")
        capabilities = detect_siwave_capabilities(project)
        render["capabilities"] = capabilities
        if not capabilities["GetFilePath"]:
            raise RuntimeError("required SIWave capability missing: GetFilePath")
        active_project_path = Path(str(project.GetFilePath())).resolve()
        active_project_matches = (
            str(active_project_path).casefold()
            == str(project_path.resolve()).casefold()
        )
        render["activeProjectFileName"] = active_project_path.name
        render["activeProjectMatchedRequested"] = active_project_matches
        if not active_project_matches:
            raise RuntimeError(
                "SIWave active project does not match the requested capture copy: "
                f"{active_project_path}"
            )
        render["viewStateProjectOpened"] = True
        render["viewStateProjectEvidence"] = {
            "path": entry.get("viewStateProject"),
            "requestedPath": str(project_path.resolve()),
            "activePath": str(active_project_path),
            "pathMatch": True,
            "sizeBytes": requested_project_stat.st_size,
            "sha256": requested_project_sha256,
            "mtimeNs": requested_project_stat.st_mtime_ns,
            "colorStateSource": (
                "original B_NETS color/mode preserved; ScrEditNetColor all-net "
                "gray then target red"
                if net_color_strategy == "api-all-nets-gray-then-target-red"
                else "package-local serialized B_NETS red/gray view-state records"
            ),
            "visualColorConfirmation": "siwave-2025.2-live-pending",
        }
        if not capabilities["ScrSaveToPngFile"]:
            raise RuntimeError("required SIWave capability missing: ScrSaveToPngFile")

        layer_visibility_results: dict[str, bool] = {}
        target_layers = _entry_target_layers(entry)
        if (require_runtime_filtering or strict_customer) and (
            not target_layers
            or any(layer not in all_signal_layers for layer in target_layers)
        ):
            raise RuntimeError(
                "runtime layer filtering cannot verify the target against the "
                "PyEDB signal-layer inventory"
            )
        if (require_runtime_filtering or strict_customer) and capabilities["ScrSetLayerVisibility"]:
            for layer_name in all_signal_layers:
                visible = layer_name in target_layers
                try:
                    result = project.ScrSetLayerVisibility(
                        layer_name,
                        visible,
                        visible,
                        visible,
                        visible,
                        visible,
                    )
                    layer_visibility_results[layer_name] = _result_succeeded(result)
                except Exception:
                    layer_visibility_results[layer_name] = False
        render["layerVisibilityResults"] = layer_visibility_results
        if require_runtime_filtering or strict_customer:
            if not capabilities["ScrSetLayerVisibility"]:
                raise RuntimeError(
                    "view-state rewrite failed and runtime ScrSetLayerVisibility is unavailable"
                )
            failed_layers = [
                layer_name
                for layer_name, succeeded in layer_visibility_results.items()
                if not succeeded
            ]
            if failed_layers:
                raise RuntimeError(
                    "runtime layer filtering failed for: " + ", ".join(failed_layers)
                )

        if strict_customer:
            color_override: dict[str, Any] = {
                "api": "ScrEditNetColor",
                "strategy": net_color_strategy,
                "capability": (
                    "available" if capabilities["ScrEditNetColor"] else "unavailable"
                ),
                "optionalRuntimeEnhancement": True,
                "netInventory": {
                    "order": "casefold-then-original",
                    "count": len(all_net_names),
                    "names": all_net_names,
                    "sha256": net_inventory_sha256,
                },
                "targetNets": target_color_names,
                "netColorMode": 1,
                "contextRgb": [128, 128, 128],
                "selectedRgb": [255, 0, 0],
                "calls": [],
                "intent": (
                    "preserve serialized source net state, set every discovered net "
                    "gray, then override target nets red before selection/fit"
                ),
                "authoritativeColorValidation": (
                    "final strict PNG exact 0xFF0000/0x808080 pixel analysis; "
                    "API return does not prove rendered color"
                ),
                "visualConfirmation": "siwave-2025.2-live-pending",
            }
            if net_color_strategy == "api-all-nets-gray-then-target-red":
                if not capabilities["ScrEditNetColor"]:
                    raise RuntimeError(
                        "ScrEditNetColor capability changed after strict color preflight"
                    )
                color_plan = [
                    ("context-gray", net_name, [net_name, 1, 128, 128, 128])
                    for net_name in all_net_names
                ] + [
                    ("target-red", net_name, [net_name, 1, 255, 0, 0])
                    for net_name in target_color_names
                ]
                for phase, net_name, arguments in color_plan:
                    try:
                        color_return = project.ScrEditNetColor(*arguments)
                    except Exception as exc:
                        color_override["calls"].append(
                            {
                                "phase": phase,
                                "net": net_name,
                                "arguments": arguments,
                                "result": False,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        color_override["status"] = "failed"
                        render["selectedNetColorOverride"] = color_override
                        raise RuntimeError(
                            f"ScrEditNetColor failed for {phase} net {net_name!r}"
                        ) from exc
                    succeeded = _result_succeeded(color_return)
                    color_override["calls"].append(
                        {
                            "phase": phase,
                            "net": net_name,
                            "arguments": arguments,
                            "return": _json_safe_return(color_return),
                            "result": succeeded,
                        }
                    )
                    if not succeeded:
                        color_override["status"] = "failed"
                        render["selectedNetColorOverride"] = color_override
                        raise RuntimeError(
                            "ScrEditNetColor reported failure for "
                            f"{phase} net {net_name!r}"
                        )
                color_override["status"] = "api-call-applied-visual-not-claimed"
                color_override["allCallsSucceeded"] = True
            else:
                if capabilities["ScrEditNetColor"]:
                    raise RuntimeError(
                        "ScrEditNetColor became available after serialized fallback "
                        "was selected"
                    )
                color_override["status"] = "unavailable-view-state-fallback"
                color_override["allCallsSucceeded"] = None
                color_override["fallback"] = (
                    "package-local B_NETS red/gray view-state plus final exact PNG "
                    "pixel validation"
                )
            render["selectedNetColorOverride"] = color_override

        selected_results: dict[str, bool] = {}
        fallbacks: list[dict[str, str]] = []
        if requested_view == "fit-all":
            if not capabilities["ScrFitAll"]:
                raise RuntimeError("required SIWave capability missing: ScrFitAll")
            if capabilities["ScrShowSelectedNetsOnly"]:
                try:
                    project.ScrShowSelectedNetsOnly(0)
                except Exception as exc:
                    if require_runtime_filtering:
                        raise RuntimeError(
                            "runtime all-net visibility could not be established"
                        ) from exc
                    fallbacks.append(
                        {
                            "code": "show-all-nets-failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
            elif require_runtime_filtering:
                raise RuntimeError(
                    "view-state rewrite failed and runtime all-net visibility API "
                    "is unavailable"
                )
            project.ScrFitAll()
            render["actualView"] = "fit-all"
            render["actualHighlight"] = {
                "mode": "all-nets-board-context",
                "color": "source SIWave net colors",
            }
            can_fit_selection = False
        else:
            can_fit_selection = all(
                capabilities[name]
                for name in ("ScrUnselectAll", "ScrSelectNet", "ScrFitSelection")
            )
            if strict_customer and not can_fit_selection:
                raise RuntimeError(
                    "strict customer PCB capture requires ScrUnselectAll, "
                    "ScrSelectNet, and ScrFitSelection"
                )
        if requested_view == "fit-selection" and can_fit_selection:
            try:
                unselect_result = project.ScrUnselectAll()
                render["unselectAllResult"] = _result_succeeded(unselect_result)
                render["unselectAllReturn"] = _json_safe_return(unselect_result)
                if not render["unselectAllResult"]:
                    raise RuntimeError("ScrUnselectAll reported failure")
                for net_name in entry["netsOnLayer"]:
                    selected_results[net_name] = _result_succeeded(
                        project.ScrSelectNet(net_name, 1)
                    )
                if selected_results and all(selected_results.values()):
                    fit_selection_return = project.ScrFitSelection()
                    render["fitSelectionReturn"] = _json_safe_return(
                        fit_selection_return
                    )
                    render["fitSelectionResult"] = _result_succeeded(
                        fit_selection_return
                    )
                    if not render["fitSelectionResult"]:
                        raise RuntimeError("ScrFitSelection reported failure")
                    render["actualView"] = "fit-selection"
                    if str(highlight_options["mode"]) == "isolate":
                        include_reference = bool(
                            highlight_options.get("includeReferenceNet")
                        )
                        if include_reference and not require_runtime_filtering:
                            render["actualHighlight"] = {
                                "mode": "selected-route-with-reference-context",
                                "color": (
                                    "SIWave selection overlay plus text view-state colors"
                                ),
                            }
                        elif capabilities["ScrShowSelectedNetsOnly"]:
                            try:
                                if include_reference:
                                    reference_net = str(
                                        highlight_options.get("referenceNet") or ""
                                    )
                                    if not reference_net:
                                        raise RuntimeError(
                                            "includeReferenceNet requires referenceNet"
                                        )
                                    reference_selected = _result_succeeded(
                                        project.ScrSelectNet(reference_net, 1)
                                    )
                                    render["referenceNetSelection"] = {
                                        "net": reference_net,
                                        "succeeded": reference_selected,
                                    }
                                    if not reference_selected:
                                        raise RuntimeError(
                                            "ScrSelectNet reported failure for reference net"
                                        )
                                project.ScrShowSelectedNetsOnly(1)
                                render["actualHighlight"] = {
                                    "mode": (
                                        "selected-route-and-reference-only"
                                        if include_reference
                                        else "selected-nets-only"
                                    ),
                                    "color": "SIWave selection overlay",
                                }
                            except Exception as exc:
                                if require_runtime_filtering:
                                    raise RuntimeError(
                                        "runtime selected-net isolation failed"
                                    ) from exc
                                render["actualHighlight"] = {
                                    "mode": "selected-net-highlight-with-context",
                                    "color": "SIWave selection overlay",
                                }
                                fallbacks.append(
                                    {
                                        "code": "show-selected-nets-only-failed",
                                        "message": f"{type(exc).__name__}: {exc}",
                                    }
                                )
                        else:
                            if require_runtime_filtering:
                                raise RuntimeError(
                                    "view-state rewrite failed and runtime "
                                    "ScrShowSelectedNetsOnly is unavailable"
                                )
                            render["actualHighlight"] = {
                                "mode": "isolated-by-text-view-state",
                                "color": "text view-state colors plus SIWave selection overlay",
                            }
                            fallbacks.append(
                                {
                                    "code": "show-selected-nets-only-unavailable",
                                    "message": (
                                        "route nets remain isolated by the generated text "
                                        "view-state"
                                    ),
                                }
                            )
                    else:
                        if capabilities["ScrShowSelectedNetsOnly"]:
                            try:
                                show_context_return = project.ScrShowSelectedNetsOnly(0)
                                render["showContextReturn"] = _json_safe_return(
                                    show_context_return
                                )
                                render["showContextResult"] = _result_succeeded(
                                    show_context_return
                                )
                                if not render["showContextResult"]:
                                    raise RuntimeError(
                                        "ScrShowSelectedNetsOnly(0) reported failure"
                                    )
                            except Exception as exc:
                                if strict_customer:
                                    raise RuntimeError(
                                        "strict customer gray PCB context could not be shown"
                                    ) from exc
                                fallbacks.append(
                                    {
                                        "code": "show-all-nets-failed",
                                        "message": f"{type(exc).__name__}: {exc}",
                                    }
                                )
                        elif strict_customer:
                            raise RuntimeError(
                                "strict customer gray PCB context requires "
                                "ScrShowSelectedNetsOnly(0)"
                            )
                        render["actualHighlight"] = (
                            {
                                "mode": "strict-red-selected-gray-context",
                                "selectedColor": STRICT_SELECTED_COLOR,
                                "contextColor": STRICT_CONTEXT_COLOR,
                                "referenceNetIncluded": False,
                                "stateSource": net_color_strategy,
                                "selectionOverlayRemovedBeforeSave": False,
                                "customRedAfterPostFitUnselectIntent": True,
                                "runtimeColorOverrideStatus": render[
                                    "selectedNetColorOverride"
                                ]["status"],
                                "visualConfirmation": "siwave-2025.2-live-pending",
                            }
                            if strict_customer
                            else {
                                "mode": "selected-net-highlight-with-context",
                                "color": "SIWave selection overlay",
                            }
                        )
                    if strict_customer:
                        post_fit_unselect_return = project.ScrUnselectAll()
                        render["postFitUnselectAllReturn"] = _json_safe_return(
                            post_fit_unselect_return
                        )
                        render["postFitUnselectAllResult"] = _result_succeeded(
                            post_fit_unselect_return
                        )
                        if not render["postFitUnselectAllResult"]:
                            raise RuntimeError(
                                "post-fit ScrUnselectAll reported failure"
                            )
                        render["actualHighlight"][
                            "selectionOverlayRemovedBeforeSave"
                        ] = True
                else:
                    can_fit_selection = False
            except Exception as exc:
                render["fitSelectionError"] = f"{type(exc).__name__}: {exc}"
                if strict_customer or require_runtime_filtering:
                    raise
                can_fit_selection = False

        if requested_view == "fit-selection" and not can_fit_selection:
            if strict_customer:
                raise RuntimeError(
                    "strict customer PCB capture does not allow a Fit All fallback"
                )
            if require_runtime_filtering:
                raise RuntimeError(
                    "view-state rewrite failed and runtime net selection/Fit Selection failed"
                )
            if not capabilities["ScrFitAll"]:
                raise RuntimeError(
                    "SIWave fit-selection capability failed and fallback ScrFitAll is unavailable"
                )
            project.ScrFitAll()
            render["actualView"] = "fit-all"
            render.setdefault(
                "actualHighlight",
                {
                    "mode": "text-view-state",
                    "color": "configured text view-state colors",
                },
            )
            fallbacks.append(
                {
                    "code": "fit-selection-unavailable",
                    "message": "generated with the documented fit-all fallback",
                }
            )
        render["selectedNetResults"] = selected_results

        image_path.parent.mkdir(parents=True, exist_ok=True)
        save_png_return = project.ScrSaveToPngFile(str(image_path))
        render["savePngReturn"] = _json_safe_return(save_png_return)
        render["savePngResult"] = _result_succeeded(save_png_return)
        if not render["savePngResult"]:
            raise RuntimeError("ScrSaveToPngFile reported failure")
        if not image_path.exists() or image_path.stat().st_size <= 0:
            raise RuntimeError("ScrSaveToPngFile returned without writing a non-empty PNG")
        # SIWave가 쓴 PNG가 그대로 최종본이다. raw/base 중간 단계는 두지 않는다.
        render["imageNormalization"] = _normalize_png(
            image_path,
            width=int(image_options["widthPx"]),
            height=int(image_options["heightPx"]),
        )
        render["fallbacks"] = fallbacks
        render["status"] = "generated-with-fallback" if fallbacks else "ok"
        return render
    finally:
        if app is not None:
            if project is not None:
                try:
                    close_no_save = getattr(project, "ScrCloseProjectNoSave", None)
                    if callable(close_no_save):
                        close_no_save()
                        render["closedWithoutSave"] = True
                    else:
                        render["closedWithoutSave"] = False
                except Exception as exc:
                    render["closeWithoutSaveError"] = f"{type(exc).__name__}: {exc}"
            try:
                app.quit_application()
            except Exception:
                pass


def _planned_entry(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "captureId": f"pcb_{_safe_name(target['channel'])}__layers_pending",
        "channel": target["channel"],
        "polarities": target["polarities"],
        "pathCompleteness": target["pathCompleteness"],
        "nets": target["nets"],
        "startComponents": target["startComponents"],
        "endpointComponents": target["endpointComponents"],
        "layer": None,
        "region": {
            "mode": "path-primitive-bbox",
            "status": "pending-license-required-geometry-inspection",
        },
        "fileName": None,
        "status": "planned",
    }


def _package_status(
    entries: list[dict[str, Any]],
    *,
    plan_only: bool,
    unresolved: list[dict[str, Any]],
) -> str:
    if plan_only:
        return "planned-with-unresolved" if unresolved else "planned"
    statuses = [str(entry.get("status") or "") for entry in entries]
    generated = sum(status in {"ok", "generated-with-fallback"} for status in statuses)
    if unresolved:
        return "partial" if generated else "unresolved"
    if generated == len(statuses) and generated:
        return "ok" if all(status == "ok" for status in statuses) else "ok-with-fallback"
    if generated:
        return "partial"
    return "unresolved"


def _write_evidence(
    evidence_dir: Path,
    *,
    entry: dict[str, Any],
    source: dict[str, Any],
    options: dict[str, Any],
    render: dict[str, Any] | None,
    image_relative_path: str | None,
    view_state_relative_path: str | None,
    error: dict[str, Any] | None = None,
) -> Path:
    evidence_path = evidence_dir / f"{entry['captureId']}.json"
    payload = {
        "schema": (
            STRICT_CAPTURE_EVIDENCE_SCHEMA
            if entry.get("kind") == "snp-batch-union"
            else CAPTURE_EVIDENCE_SCHEMA
        ),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": entry["status"],
        "captureId": entry["captureId"],
        "source": source,
        "target": {
            "kind": entry.get("kind", "channel-net-zoom"),
            "view": entry.get("view"),
            "channel": entry.get("channel"),
            "batchId": entry.get("batchId"),
            "channels": entry.get("channels") or [],
            "polarities": entry.get("polarities") or [],
            "pathCompleteness": entry.get("pathCompleteness"),
            "nets": entry.get("nets") or [],
            "netsOnLayer": entry.get("netsOnLayer"),
            "startComponents": entry.get("startComponents") or [],
            "endpointComponents": entry.get("endpointComponents") or [],
            "endComponents": entry.get("endComponents") or [],
            "selectedComponents": entry.get("selectedComponents") or [],
            "endpointRefdes": entry.get("endpointRefdes") or [],
            "layer": entry.get("layer"),
            "layers": entry.get("layers"),
            "layerSelection": entry.get("layerSelection"),
            "region": entry.get("region"),
            "pathEvidence": entry.get("pathEvidence"),
        },
        "requestedPresentation": {
            "overview": (
                options["overview"]
                if entry.get("kind") == "board-overview"
                else None
            ),
            "highlight": options["highlight"],
            "view": options["view"],
            "image": options["image"],
        },
        "render": render,
        "artifacts": {
            "image": image_relative_path,
            "viewStateProject": view_state_relative_path,
        },
        "error": error,
    }
    _write_json(evidence_path, payload)
    return evidence_path


def validate_strict_capture_artifacts(
    package_dir: Path,
    entries: list[dict[str, Any]],
    *,
    started_at_ns: int,
) -> dict[str, Any]:
    expected_names = {STRICT_BATCH_CAPTURE_FILENAME}
    actual_entries = {
        str(entry.get("fileName") or ""): entry for entry in entries
    }
    if set(actual_entries) != expected_names or len(entries) != 1:
        raise RuntimeError(
            f"strict PCB capture must contain exactly {STRICT_BATCH_CAPTURE_FILENAME}"
        )
    images_dir = package_dir / "images"
    actual_png = {
        path.name
        for path in images_dir.rglob("*.png")
        if path.is_file()
    }
    if actual_png != expected_names:
        raise RuntimeError(
            f"strict PCB capture image set differs from {STRICT_BATCH_CAPTURE_FILENAME}: "
            f"{sorted(actual_png)}"
        )
    evidence: dict[str, Any] = {}
    for file_name in sorted(expected_names):
        path = images_dir / file_name
        stat = path.stat()
        if stat.st_size <= 0:
            raise RuntimeError(f"strict PCB capture is empty: {path}")
        if stat.st_mtime_ns < started_at_ns:
            raise RuntimeError(f"strict PCB capture is stale: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        analysis = analyze_strict_capture_png(path)
        if (
            analysis["selectedPixelCount"] <= 0
            or analysis["contextPixelCount"] <= 0
        ):
            raise RuntimeError(
                f"strict PCB capture lacks visible red/gray pixel classes: {path}"
            )
        evidence[Path(file_name).stem] = {
            "path": f"images/{file_name}",
            "fileName": file_name,
            "size": stat.st_size,
            "sha256": digest,
            "mtimeNs": stat.st_mtime_ns,
            "fresh": True,
            "imageAnalysis": analysis,
        }
    return evidence


def validate_job_overview_package(
    overview_dir: Path, *, started_at_ns: int | None = None
) -> dict[str, Path]:
    """Validate the Job-level whole-board top/bottom overview package.

    존재하는 패키지는 재사용 전에 여기서 검증한다.  불일치·손상은 재생성이
    아니라 실패다(fail-closed).
    """

    overview_dir = overview_dir.resolve()
    manifest_path = overview_dir / JOB_OVERVIEW_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise PcbCaptureContractError(
            f"Job PCB overview manifest is missing: {manifest_path}"
        )
    manifest = _json_object(manifest_path)
    if (
        manifest.get("schema") != JOB_OVERVIEW_MANIFEST_SCHEMA
        or manifest.get("mode") != "job-board-overview"
        or manifest.get("status") != "ok"
        or manifest.get("layerFillPolicy")
        != "disabled-preserve-rc9-overview-style"
    ):
        raise PcbCaptureContractError(
            "Job PCB overview manifest schema/mode/status differs"
        )
    if started_at_ns is not None:
        _created_after_start(
            manifest.get("createdAt"),
            label="Job PCB overview manifest",
            started_at_ns=started_at_ns,
        )
    captures = manifest.get("captures")
    if not isinstance(captures, list) or len(captures) != len(JOB_OVERVIEW_FILENAMES):
        raise PcbCaptureContractError(
            "Job PCB overview requires exactly one top and one bottom capture"
        )
    capture_by_view: dict[str, dict[str, Any]] = {}
    for capture in captures:
        if not isinstance(capture, dict):
            raise PcbCaptureContractError("Job PCB overview capture entry is invalid")
        view = str(capture.get("view") or "")
        if view not in JOB_OVERVIEW_FILENAMES or view in capture_by_view:
            raise PcbCaptureContractError(
                "Job PCB overview view is missing or duplicated"
            )
        expected_name = JOB_OVERVIEW_FILENAMES[view]
        if (
            capture.get("kind") != "board-overview"
            or capture.get("viewMode") != "fit-all"
            or capture.get("fileName") != expected_name
            or capture.get("image") != expected_name
            or capture.get("status") != "ok"
            or not str(capture.get("layer") or "").strip()
        ):
            raise PcbCaptureContractError(
                f"Job PCB overview {view} capture contract differs"
            )
        render = capture.get("render")
        rewrite = render.get("viewStateRewrite") if isinstance(render, dict) else None
        fill_state = (
            rewrite.get("layerFillState") if isinstance(rewrite, dict) else None
        )
        if (
            not isinstance(fill_state, dict)
            or fill_state.get("enabled") is not False
            or fill_state.get("targetLayers") != [capture.get("layer")]
            or fill_state.get("targetFillToken") != "0"
            or fill_state.get("nonTargetFillToken") != "0"
            or fill_state.get("filledTargetLayerRecordCount") != 0
        ):
            raise PcbCaptureContractError(
                f"Job PCB overview {view} target-layer fill must be disabled"
            )
        capture_by_view[view] = capture
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(JOB_OVERVIEW_FILENAMES):
        raise PcbCaptureContractError("Job PCB overview artifact set is incomplete")
    result: dict[str, Path] = {}
    for view, file_name in JOB_OVERVIEW_FILENAMES.items():
        record = artifacts.get(view)
        if not isinstance(record, dict) or record.get("path") != file_name:
            raise PcbCaptureContractError(
                f"Job PCB overview {view} artifact binding differs"
            )
        path, _ = _validate_file_record(
            record,
            package_dir=overview_dir,
            label=f"Job PCB overview {view} image",
            started_at_ns=started_at_ns if started_at_ns is not None else 0,
        )
        result[view] = path
    actual_pngs = {
        path.name for path in overview_dir.glob("*.png") if path.is_file()
    }
    if actual_pngs != set(JOB_OVERVIEW_FILENAMES.values()):
        raise PcbCaptureContractError(
            f"Job PCB overview image allowlist differs: {sorted(actual_pngs)}"
        )
    return result


def ensure_job_board_overview(
    context: dict[str, Any],
    *,
    options: dict[str, Any],
    signal_layers: list[str],
    run_dir: Path,
) -> dict[str, Any]:
    """Create or reuse the Job-level whole-board overview package.

    첫 배치가 생성하고 이후 배치는 기존 manifest를 검증한 뒤 재사용한다.
    top은 첫 signal layer, bottom은 마지막 signal layer이며 전체 넷을
    원래 색 그대로 보여 주고 ScrFitAll로 캡처한다.  배너는 없다.
    """

    normalized_layers = [str(item) for item in signal_layers if str(item or "").strip()]
    if not normalized_layers:
        raise PcbCaptureConfigurationError(
            "Job PCB overview requires a non-empty signal-layer inventory"
        )
    overview_root = run_dir.resolve().parent.parent
    overview_dir = overview_root / JOB_OVERVIEW_DIRNAME
    manifest_path = overview_dir / JOB_OVERVIEW_MANIFEST_FILENAME
    if overview_dir.exists():
        validate_job_overview_package(overview_dir)
        return {
            "status": "reused",
            "path": str(overview_dir),
            "manifest": str(manifest_path),
        }
    staging_dir = overview_root / f".ovw.{uuid.uuid4().hex[:8]}.tmp"
    view_states_dir = staging_dir / "view_states"
    view_states_dir.mkdir(parents=True, exist_ok=False)
    source_siw = Path(context["reference"]["siw"])
    manifest: dict[str, Any] = {
        "schema": JOB_OVERVIEW_MANIFEST_SCHEMA,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "mode": "job-board-overview",
        "layerFillPolicy": "disabled-preserve-rc9-overview-style",
        "aedtVersion": str(options["aedtVersion"]),
        "source": {
            "referenceSiw": str(context["reference"]["siw"]),
            "referenceEdb": str(context["reference"]["aedb"]),
        },
        "signalLayers": list(normalized_layers),
        "captures": [],
        "artifacts": {},
    }
    try:
        requested_layers = {
            "top": normalized_layers[0],
            "bottom": normalized_layers[-1],
        }
        for view, file_name in JOB_OVERVIEW_FILENAMES.items():
            layer = requested_layers[view]
            image_path = staging_dir / file_name
            view_state_path = view_states_dir / f"pcb_overview_{view}.siw"
            entry: dict[str, Any] = {
                "captureId": f"pcb_overview_{view}",
                "kind": "board-overview",
                "view": view,
                "viewMode": "fit-all",
                "layer": layer,
                "layers": [layer],
                "layerSelection": "derived-from-stackup-order",
                "netsOnLayer": [],
                "region": {
                    "mode": "full-board",
                    "appliedToRender": True,
                    "renderViewControl": "SIWave ScrFitAll",
                },
                "fileName": file_name,
                "image": file_name,
                "viewStateProject": f"view_states/pcb_overview_{view}.siw",
                "status": "planned",
            }
            rewrite = rewrite_siw_view_state(
                source_siw,
                view_state_path,
                target_layer=layer,
                selected_nets=[],
                highlight=options["highlight"],
                show_dimension_markers=bool(
                    options["view"].get("showDimensionMarkers", False)
                ),
                show_grid=bool(options["view"].get("showGrid", False)),
                show_pin_names=bool(options["view"].get("showPinNames", False)),
                show_all_nets=True,
                preserve_other_net_colors=True,
                fill_target_layers=False,
            )
            render = _render_siwave_capture(
                project_path=view_state_path,
                image_path=image_path,
                entry=entry,
                all_signal_layers=list(normalized_layers),
                aedt_version=str(options["aedtVersion"]),
                image_options=options["image"],
                highlight_options={"mode": "board-overview-source-colors"},
                require_runtime_filtering=False,
                strict_customer=False,
            )
            render["viewStateRewrite"] = rewrite
            if (
                render.get("status") != "ok"
                or render.get("actualView") != "fit-all"
                or render.get("savePngResult") is not True
            ):
                raise PcbCaptureContractError(
                    f"Job PCB overview {view} render is not a clean fit-all capture"
                )
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                raise PcbCaptureContractError(
                    f"Job PCB overview {view} image is missing or empty"
                )
            stat = image_path.stat()
            entry["status"] = "ok"
            entry["render"] = render
            manifest["captures"].append(entry)
            manifest["artifacts"][view] = {
                "path": file_name,
                "fileName": file_name,
                "sizeBytes": stat.st_size,
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "mtimeNs": stat.st_mtime_ns,
            }
        _write_json(staging_dir / JOB_OVERVIEW_MANIFEST_FILENAME, manifest)
        # Windows에서는 SIWave COM이 staging 안 파일 핸들을 늦게 놓아
        # 일시적으로 rename이 거부될 수 있다 (2026-08-19 R16 실행에서 관측).
        # 대상이 이미 있으면 즉시 실패하고, 잠금성 오류만 짧게 재시도한다.
        rename_error: OSError | None = None
        for attempt_index in range(10):
            if overview_dir.exists():
                rename_error = FileExistsError(f"target exists: {overview_dir}")
                break
            try:
                staging_dir.rename(overview_dir)
                rename_error = None
                break
            except OSError as exc:
                rename_error = exc
                time.sleep(0.5)
        if rename_error is not None:
            raise PcbCaptureContractError(
                "Job PCB overview publish rename failed; an overview package "
                f"already exists or the target is blocked (fail-closed): {overview_dir}: "
                f"{type(rename_error).__name__}: {rename_error}"
            ) from rename_error
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    validate_job_overview_package(overview_dir)
    return {
        "status": "generated",
        "path": str(overview_dir),
        "manifest": str(manifest_path),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_sequence(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PcbCaptureContractError(f"{label} must be a non-empty list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        folded = text.casefold()
        if not text or folded in seen:
            raise PcbCaptureContractError(
                f"{label} contains an empty or duplicate value"
            )
        seen.add(folded)
        result.append(text)
    return result


def _package_file(package_dir: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PcbCaptureContractError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PcbCaptureContractError(f"{label} path is unsafe: {value!r}")
    path = (package_dir / relative).resolve()
    try:
        path.relative_to(package_dir.resolve())
    except ValueError as exc:
        raise PcbCaptureContractError(f"{label} leaves the capture package") from exc
    return path


def _validate_file_record(
    record: Any,
    *,
    package_dir: Path,
    label: str,
    started_at_ns: int,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(record, dict):
        raise PcbCaptureContractError(f"{label} artifact record is missing")
    path = _package_file(package_dir, record.get("path"), label=label)
    if not path.is_file() or path.stat().st_size <= 0:
        raise PcbCaptureContractError(f"{label} is missing or empty: {path}")
    stat = path.stat()
    size = record.get("sizeBytes", record.get("size"))
    if isinstance(size, bool) or not isinstance(size, int) or size != stat.st_size:
        raise PcbCaptureContractError(f"{label} size evidence differs")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if record.get("sha256") != digest:
        raise PcbCaptureContractError(f"{label} SHA-256 evidence differs")
    if record.get("mtimeNs") != stat.st_mtime_ns:
        raise PcbCaptureContractError(f"{label} mtime evidence differs")
    if stat.st_mtime_ns < started_at_ns:
        raise PcbCaptureContractError(f"{label} predates the strict FullBatch run")
    return path, {
        "path": Path(record["path"]).as_posix(),
        "sizeBytes": stat.st_size,
        "sha256": digest,
        "mtimeNs": stat.st_mtime_ns,
    }


def _created_after_start(value: Any, *, label: str, started_at_ns: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PcbCaptureContractError(f"{label} createdAt is missing")
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PcbCaptureContractError(f"{label} createdAt is invalid") from exc
    if created.tzinfo is None:
        raise PcbCaptureContractError(f"{label} createdAt must include a timezone")
    if int(created.timestamp() * 1_000_000_000) < started_at_ns:
        raise PcbCaptureContractError(f"{label} createdAt predates the strict run")


def _validate_image_analysis_record(
    value: Any,
    *,
    actual: dict[str, Any],
    expected_width: int,
    expected_height: int,
    label: str,
) -> None:
    if not isinstance(value, dict) or _canonical_json(value) != _canonical_json(actual):
        raise PcbCaptureContractError(f"{label} image analysis evidence differs")
    if (
        actual.get("schema") != IMAGE_ANALYSIS_SCHEMA
        or actual.get("status") != "verified"
        or actual.get("widthPx") != expected_width
        or actual.get("heightPx") != expected_height
        or actual.get("selectedPixelCount", 0) <= 0
        or actual.get("contextPixelCount", 0) <= 0
    ):
        raise PcbCaptureContractError(
            f"{label} PNG dimensions or visible red/gray pixel classes differ"
        )


def validate_strict_capture_package(
    manifest_path: Path,
    *,
    job_root: Path,
    started_at_ns: int,
    expected_batch_id: str | None = None,
    expected_capture_policy: Mapping[str, Any] | None = None,
) -> dict[str, Path]:
    """Validate the complete strict package before any public publish."""

    manifest_path = manifest_path.resolve()
    job_root = job_root.resolve()
    try:
        manifest_path.relative_to(job_root)
    except ValueError as exc:
        raise PcbCaptureContractError("PCB manifest is outside the Job root") from exc
    manifest = _json_object(manifest_path)
    package_dir = manifest_path.parent
    if (
        manifest.get("schema") != STRICT_CAPTURE_MANIFEST_SCHEMA
        or manifest.get("mode") != "customer-strict-snp-batch-union"
        or manifest.get("planOnly") is not False
        or manifest.get("overviewCaptures")
    ):
        raise PcbCaptureContractError("strict PCB manifest schema/mode/status differs")
    policy: dict[str, dict[str, bool]] | None = None
    if expected_capture_policy is not None:
        try:
            policy = normalize_pcb_capture_policy(expected_capture_policy)
        except ValueError as exc:
            raise PcbCaptureContractError(str(exc)) from exc
        route_enabled = bool(policy["route"]["enabled"])
        overview_enabled = bool(policy["overview"]["enabled"])
        contract = manifest.get("contract")
        legacy_enabled_manifest = (
            route_enabled
            and overview_enabled
            and isinstance(contract, Mapping)
            and not {"route", "overview"}.intersection(contract)
        )
        if not legacy_enabled_manifest:
            try:
                recorded_policy = normalize_pcb_capture_policy(
                    contract, allow_extra_fields=True
                )
            except ValueError as exc:
                raise PcbCaptureContractError(str(exc)) from exc
            if recorded_policy != policy:
                raise PcbCaptureContractError("strict PCB capture policy differs")
        expected_status = "ok" if route_enabled or overview_enabled else "skipped"
        if manifest.get("status") != expected_status:
            raise PcbCaptureContractError("strict PCB manifest status differs from policy")
        if not legacy_enabled_manifest:
            expected_skipped = {
                "status": "skipped",
                "reason": "disabled-by-administrator-config",
            }
            route_state = manifest.get("routeCapture")
            overview_state = manifest.get("jobOverview")
            if (
                not isinstance(route_state, Mapping)
                or route_state.get("enabled") is not route_enabled
            ):
                raise PcbCaptureContractError("strict PCB route execution state differs")
            if route_enabled:
                if route_state.get("status") != "ok":
                    raise PcbCaptureContractError("strict PCB route was not completed")
            elif {
                key: route_state.get(key) for key in ("status", "reason")
            } != expected_skipped:
                raise PcbCaptureContractError("strict PCB route skip state differs")
            if (
                not isinstance(overview_state, Mapping)
                or overview_state.get("enabled") is not overview_enabled
            ):
                raise PcbCaptureContractError("strict PCB overview execution state differs")
            if overview_enabled:
                if overview_state.get("status") not in {"generated", "reused"}:
                    raise PcbCaptureContractError("strict PCB overview was not completed")
            elif {
                key: overview_state.get(key) for key in ("status", "reason")
            } != expected_skipped:
                raise PcbCaptureContractError("strict PCB overview skip state differs")
        if not route_enabled:
            if manifest.get("captures") or manifest.get("artifacts") not in (None, {}):
                raise PcbCaptureContractError(
                    "disabled strict PCB route contains capture artifacts"
                )
            images_dir = package_dir / "images"
            if images_dir.is_dir() and any(images_dir.iterdir()):
                raise PcbCaptureContractError(
                    "disabled strict PCB route package contains stale images"
                )
    elif manifest.get("status") != "ok":
        raise PcbCaptureContractError("strict PCB manifest schema/mode/status differs")
    _created_after_start(
        manifest.get("createdAt"), label="strict PCB manifest", started_at_ns=started_at_ns
    )
    batch_id = str(manifest.get("batchId") or "").strip()
    if not batch_id or (expected_batch_id is not None and batch_id != expected_batch_id):
        raise PcbCaptureContractError("strict PCB batchId differs")
    if policy is not None and not bool(policy["route"]["enabled"]):
        return {}
    capture_set = manifest.get("captureSet")
    if not isinstance(capture_set, dict) or capture_set != {
        **capture_set,
        "unit": "snp-batch",
        "count": 1,
        "batchId": batch_id,
        "referenceNetIncluded": False,
    }:
        raise PcbCaptureContractError("strict PCB captureSet identity differs")
    selected_nets = _strict_sequence(
        capture_set.get("selectedNets"), label="captureSet.selectedNets"
    )
    selected_components = _strict_sequence(
        capture_set.get("selectedComponents"),
        label="captureSet.selectedComponents",
    )
    endpoint_refdes = _strict_sequence(
        capture_set.get("endpointRefdes"), label="captureSet.endpointRefdes"
    )
    start_components = _deterministic_role_values(
        capture_set.get("startComponents"), label="captureSet.startComponents"
    )
    end_components = _deterministic_role_values(
        capture_set.get("endComponents"), label="captureSet.endComponents"
    )
    if _deterministic_casefold_strings(start_components + end_components) != endpoint_refdes:
        raise PcbCaptureContractError("captureSet endpoint roles/refdes differ")
    presentation_set = manifest.get("componentPresentation")
    if not isinstance(presentation_set, dict) or (
        presentation_set.get("schema") != COMPONENT_PRESENTATION_SET_SCHEMA
        or presentation_set.get("status") != "verified"
    ):
        raise PcbCaptureContractError("PCB component presentation set is not verified")
    expected_presentation_target = {
        "batchId": batch_id,
        "startComponents": start_components,
        "endComponents": end_components,
    }
    if _canonical_json(presentation_set.get("target")) != _canonical_json(
        expected_presentation_target
    ):
        raise PcbCaptureContractError("PCB component presentation target differs")
    if "endpointRefdesLabels" in presentation_set:
        raise PcbCaptureContractError(
            "PCB component presentation must not declare endpoint RefDes labels"
        )
    visibility = presentation_set.get("pathComponentVisibility")
    if not isinstance(visibility, dict) or any(
        (
            visibility.get("status") != "deferred-follow-up",
            visibility.get("releaseBlocking") is not False,
            visibility.get("implementationClaimed") is not False,
        )
    ):
        raise PcbCaptureContractError("path component visibility deferral differs")
    visual_confirmation = presentation_set.get("visualConfirmation")
    if not isinstance(visual_confirmation, dict) or (
        visual_confirmation.get("status") not in {"live-review-pending", "verified"}
        or visual_confirmation.get("schema") != PCB_VISUAL_CONFIRMATION_SCHEMA
    ):
        raise PcbCaptureContractError("PCB live visual confirmation state differs")
    captures = manifest.get("captures")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(artifacts, dict)
        or set(artifacts) != {STRICT_BATCH_CAPTURE_VIEW}
    ):
        raise PcbCaptureContractError("strict PCB batch route capture set is incomplete")
    capture_by_view: dict[str, dict[str, Any]] = {}
    for capture in captures:
        if not isinstance(capture, dict):
            raise PcbCaptureContractError("strict PCB capture entry is not an object")
        view = str(capture.get("view") or "")
        if view != STRICT_BATCH_CAPTURE_VIEW or view in capture_by_view:
            raise PcbCaptureContractError("strict PCB route view is missing or duplicated")
        capture_by_view[view] = capture
    result: dict[str, Path] = {}
    contract_image = (manifest.get("contract") or {}).get("image") or {}
    try:
        expected_width = int(contract_image["widthPx"])
        expected_height = int(contract_image["heightPx"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PcbCaptureContractError("strict PCB requested resolution is missing") from exc
    if expected_width <= 0 or expected_height <= 0:
        raise PcbCaptureContractError("strict PCB requested resolution is invalid")
    for view in (STRICT_BATCH_CAPTURE_VIEW,):
        capture = capture_by_view[view]
        if (
            capture.get("kind") != "snp-batch-union"
            or capture.get("batchId") != batch_id
            or capture.get("viewMode") != "fit-selection"
            or capture.get("fileName") != STRICT_BATCH_CAPTURE_FILENAME
            or capture.get("layerSelection") != "occupied-signal-layers"
            or not _strict_sequence(
                capture.get("layers"), label=f"strict PCB {view} layers"
            )
            or capture.get("status") != "ok"
        ):
            raise PcbCaptureContractError(f"strict PCB {view} capture contract differs")
        if list(capture.get("netsOnLayer") or []) != selected_nets:
            raise PcbCaptureContractError(f"strict PCB {view} net target differs")
        if list(capture.get("selectedComponents") or []) != selected_components:
            raise PcbCaptureContractError(f"strict PCB {view} component target differs")
        if list(capture.get("endpointRefdes") or []) != endpoint_refdes:
            raise PcbCaptureContractError(f"strict PCB {view} endpoint target differs")
        if list(capture.get("startComponents") or []) != start_components:
            raise PcbCaptureContractError(f"strict PCB {view} start role differs")
        if list(capture.get("endComponents") or []) != end_components:
            raise PcbCaptureContractError(f"strict PCB {view} end role differs")
        image, image_record = _validate_file_record(
            artifacts[view],
            package_dir=package_dir,
            label=f"PCB {view} image",
            started_at_ns=started_at_ns,
        )
        if (
            image.name != STRICT_BATCH_CAPTURE_FILENAME
            or capture.get("image") != image_record["path"]
        ):
            raise PcbCaptureContractError(f"strict PCB {view} image binding differs")
        actual_analysis = analyze_strict_capture_png(image)
        _validate_image_analysis_record(
            artifacts[view].get("imageAnalysis"),
            actual=actual_analysis,
            expected_width=expected_width,
            expected_height=expected_height,
            label=f"PCB {view}",
        )
        evidence_path = _package_file(
            package_dir, capture.get("evidence"), label=f"PCB {view} evidence"
        )
        if not evidence_path.is_file() or evidence_path.stat().st_mtime_ns < started_at_ns:
            raise PcbCaptureContractError(f"PCB {view} evidence is missing or stale")
        evidence = _json_object(evidence_path)
        if (
            evidence.get("schema") != STRICT_CAPTURE_EVIDENCE_SCHEMA
            or evidence.get("status") != "ok"
            or evidence.get("captureId") != capture.get("captureId")
        ):
            raise PcbCaptureContractError(f"PCB {view} evidence schema/status differs")
        _created_after_start(
            evidence.get("createdAt"), label=f"PCB {view} evidence", started_at_ns=started_at_ns
        )
        target = evidence.get("target")
        if not isinstance(target, dict) or any(
            (
                target.get("kind") != "snp-batch-union",
                target.get("batchId") != batch_id,
                target.get("view") != view,
                list(target.get("netsOnLayer") or []) != selected_nets,
                list(target.get("selectedComponents") or []) != selected_components,
                list(target.get("endpointRefdes") or []) != endpoint_refdes,
                list(target.get("startComponents") or []) != start_components,
                list(target.get("endComponents") or []) != end_components,
            )
        ):
            raise PcbCaptureContractError(f"PCB {view} evidence target differs")
        evidence_artifacts = evidence.get("artifacts")
        if not isinstance(evidence_artifacts, dict) or evidence_artifacts.get("image") != image_record["path"]:
            raise PcbCaptureContractError(f"PCB {view} evidence image binding differs")
        if {"nativeRawImage", "nativeBaseImage"} & set(evidence_artifacts):
            raise PcbCaptureContractError(
                f"PCB {view} evidence still declares a raw/base image stage"
            )
        view_state_path = evidence_artifacts.get("viewStateProject")
        if capture.get("viewStateProject") != view_state_path:
            raise PcbCaptureContractError(f"PCB {view} view-state path binding differs")
        render = evidence.get("render")
        if not isinstance(render, dict):
            raise PcbCaptureContractError(f"PCB {view} render evidence is missing")
        view_state_record = render.get("viewStateProjectEvidence")
        view_state, canonical_view_state_record = _validate_file_record(
            view_state_record,
            package_dir=package_dir,
            label=f"PCB {view} view-state",
            started_at_ns=started_at_ns,
        )
        if view_state_path != canonical_view_state_record["path"]:
            raise PcbCaptureContractError(f"PCB {view} view-state binding differs")
        if render.get("sourceProjectArtifact") != canonical_view_state_record["path"]:
            raise PcbCaptureContractError(f"PCB {view} source view-state binding differs")
        if {"nativeRawImage", "normalizedBaseImage", "componentPresentation"} & set(render):
            raise PcbCaptureContractError(
                f"PCB {view} render still declares a raw/base/banner composite stage"
            )
        if _canonical_json(render.get("finalImageAnalysis")) != _canonical_json(actual_analysis):
            raise PcbCaptureContractError(f"PCB {view} final image analysis differs")
        normalization = render.get("imageNormalization") or {}
        if (
            render.get("requestedResolutionPx") != [expected_width, expected_height]
            or normalization.get("actualResolutionPx") != [expected_width, expected_height]
            or normalization.get("resizeMode") != "contain-white"
        ):
            raise PcbCaptureContractError(f"PCB {view} normalized resolution differs")
        highlight = render.get("actualHighlight") or {}
        rewrite = render.get("viewStateRewrite") or {}
        color = rewrite.get("colorState") or {}
        if not (
            render.get("status") == "ok"
            and render.get("actualView") == "fit-selection"
            and render.get("fitSelectionResult") is True
            and render.get("showContextResult") is True
            and render.get("postFitUnselectAllResult") is True
            and render.get("savePngResult") is True
            and render.get("viewStateProjectOpened") is True
            and highlight.get("mode") == "strict-red-selected-gray-context"
            and highlight.get("selectedColor") == STRICT_SELECTED_COLOR
            and highlight.get("contextColor") == STRICT_CONTEXT_COLOR
            and highlight.get("referenceNetIncluded") is False
            and highlight.get("selectionOverlayRemovedBeforeSave") is True
            and not rewrite.get("missingTargetNets")
            and color.get("selectedColor") == STRICT_SELECTED_COLOR
            and color.get("contextColor") == STRICT_CONTEXT_COLOR
            and color.get("includeReferenceNet") is False
            and color.get("selectedNetRecordCount") == len(selected_nets)
        ):
            raise PcbCaptureContractError(f"PCB {view} zoom/color evidence differs")
        if view_state.stat().st_mtime_ns > image.stat().st_mtime_ns:
            raise PcbCaptureContractError(
                f"PCB {view} view-state/final freshness order differs"
            )
        result[view] = image
    actual_pngs = {
        path.name for path in (package_dir / "images").glob("*.png") if path.is_file()
    }
    if actual_pngs != {STRICT_BATCH_CAPTURE_FILENAME}:
        raise PcbCaptureContractError("strict PCB image allowlist differs")
    return result


def _verified_package_child(run_dir: Path, package_path: Path) -> None:
    resolved_run_dir = run_dir.resolve()
    resolved_package_path = package_path.resolve()
    if resolved_package_path.parent != resolved_run_dir:
        raise RuntimeError(f"unsafe PCB capture package path: {resolved_package_path}")


def _replace_directory_with_retry(
    source: Path,
    target: Path,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            source.replace(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _prepare_package_directory(staging_dir: Path, ready_dir: Path) -> None:
    """Move a completed package into place, copying if SIWave still holds it open."""
    try:
        _replace_directory_with_retry(staging_dir, ready_dir)
        return
    except PermissionError:
        # SIWave can retain a read handle to a view-state project briefly after
        # quit_application().  The completed package remains readable, but its
        # directory cannot be renamed until the Python process exits.  Copy it
        # to a fresh ready directory so the final publication can still use an
        # atomic directory rename.
        try:
            shutil.copytree(staging_dir, ready_dir)
        except Exception:
            if ready_dir.exists():
                shutil.rmtree(ready_dir, ignore_errors=True)
            raise
        try:
            shutil.rmtree(staging_dir)
        except OSError:
            pass


def _publish_package_directory(
    staging_dir: Path,
    final_dir: Path,
    *,
    preserve_existing_on_failure: bool,
) -> Path:
    _verified_package_child(final_dir.parent, staging_dir)
    _verified_package_child(final_dir.parent, final_dir)
    ready_dir = staging_dir.with_name(
        f".{final_dir.name}.ready.{uuid.uuid4().hex}"
    )
    _verified_package_child(final_dir.parent, ready_dir)
    _prepare_package_directory(staging_dir, ready_dir)
    staging_dir = ready_dir
    if preserve_existing_on_failure and final_dir.exists():
        failed_dir = final_dir.with_name(
            f"{final_dir.name}_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid.uuid4().hex[:8]}"
        )
        _verified_package_child(final_dir.parent, failed_dir)
        _replace_directory_with_retry(staging_dir, failed_dir)
        return failed_dir

    backup_dir: Path | None = None
    if final_dir.exists():
        backup_dir = final_dir.with_name(
            f".{final_dir.name}.previous.{uuid.uuid4().hex}"
        )
        _verified_package_child(final_dir.parent, backup_dir)
        _replace_directory_with_retry(final_dir, backup_dir)
    try:
        _replace_directory_with_retry(staging_dir, final_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not final_dir.exists():
            _replace_directory_with_retry(backup_dir, final_dir)
        raise
    if backup_dir is not None and backup_dir.exists():
        try:
            shutil.rmtree(backup_dir)
        except OSError:
            pass
    return final_dir


def _finalize_capture_package(
    *,
    staging_dir: Path,
    final_dir: Path,
    manifest: dict[str, Any],
    plan_only: bool,
) -> Path:
    manifest_path = staging_dir / "pcb_capture_manifest.json"
    package_index_path = staging_dir / "pcb_capture_package.json"
    _write_json(manifest_path, manifest)
    _write_package_index(package_index_path, manifest)
    successful_statuses = {"planned", "skipped", "ok", "ok-with-fallback"}
    previous_status = None
    previous_manifest_path = final_dir / manifest_path.name
    if previous_manifest_path.is_file():
        try:
            previous_status = _json_object(previous_manifest_path).get("status")
        except Exception:
            previous_status = None
    published_dir = _publish_package_directory(
        staging_dir,
        final_dir,
        preserve_existing_on_failure=(
            not plan_only
            and manifest.get("status") not in successful_statuses
            and previous_status in {"ok", "ok-with-fallback"}
        ),
    )
    manifest["publication"] = {
        "directory": published_dir.name,
        "mode": (
            "preserved-previous-success-and-published-failed-attempt"
            if published_dir != final_dir
            else "atomic-replace"
        ),
    }
    published_manifest_path = published_dir / manifest_path.name
    _write_json(published_manifest_path, manifest)
    _write_package_index(published_dir / package_index_path.name, manifest)
    return published_manifest_path


def _strict_planned_entries(
    target: dict[str, Any], options: dict[str, Any]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for side in (STRICT_BATCH_CAPTURE_VIEW,):
        entries.append(
            {
                "captureId": "pcb_batch_route",
                "kind": "snp-batch-union",
                "view": side,
                "viewMode": "fit-selection",
                "batchId": target["batchId"],
                "channel": None,
                "channels": list(target["channels"]),
                "polarities": list(target["polarities"]),
                "pathCompleteness": target["pathCompleteness"],
                "nets": list(target["nets"]),
                "netsOnLayer": list(target["nets"]),
                "selectedComponents": list(target["selectedComponents"]),
                "endpointRefdes": list(target["endpointRefdes"]),
                "startComponents": list(target["startComponents"]),
                "endComponents": list(target["endComponents"]),
                "pathEvidence": list(target["pathEvidence"]),
                "layer": None,
                "layers": [],
                "layerSelection": "pending-occupied-signal-layers",
                "region": {
                    "mode": "path-primitive-bbox",
                    "status": "pending-license-required-geometry-inspection",
                },
                "fileName": STRICT_BATCH_CAPTURE_FILENAME,
                "image": None,
                "status": "planned",
            }
        )
    return entries


def _run_strict_pcb_capture(
    context: dict[str, Any], *, plan_only: bool
) -> Path:
    run_dir = Path(context["workspace"]["runDir"])
    final_package_dir = run_dir / ("pcb_capture_plan" if plan_only else "pcb_capture")
    staging_prefix = "pcbp" if plan_only else "pcbc"
    package_dir = run_dir / f".{staging_prefix}.{uuid.uuid4().hex[:8]}.tmp"
    _verified_package_child(run_dir, final_package_dir)
    _verified_package_child(run_dir, package_dir)
    images_dir = package_dir / "images"
    evidence_dir = package_dir / "evidence"
    view_states_dir = package_dir / "view_states"
    for path in (
        package_dir,
        images_dir,
        evidence_dir,
        view_states_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now(timezone.utc).isoformat()
    started_at_ns = time.time_ns()
    manifest: dict[str, Any] = {
        "schema": STRICT_CAPTURE_MANIFEST_SCHEMA,
        "createdAt": created_at,
        "status": "unresolved",
        "mode": "customer-strict-snp-batch-union",
        "planOnly": plan_only,
        "batchId": str((context.get("segment") or {}).get("name") or ""),
        "contract": None,
        "captureSet": None,
        "componentPresentation": None,
        "routeCapture": None,
        "jobOverview": None,
        "overviewCaptures": [],
        "captures": [],
        "unresolved": [],
        "packageIndex": "pcb_capture_package.json",
    }
    source = {
        "configPath": context.get("configPath"),
        "referenceSiw": context["reference"]["siw"],
        "referenceEdb": context["reference"]["aedb"],
    }
    manifest["source"] = source
    try:
        options = normalize_capture_options(context)
        manifest["contract"] = options
        route_enabled = bool(options["route"]["enabled"])
        overview_enabled = bool(options["overview"]["enabled"])
        skipped = {
            "status": "skipped",
            "reason": "disabled-by-administrator-config",
        }
        manifest["routeCapture"] = {
            "enabled": route_enabled,
            "status": "pending" if route_enabled else "skipped",
            **({} if route_enabled else {"reason": skipped["reason"]}),
        }
        manifest["jobOverview"] = {
            "enabled": overview_enabled,
            "status": "pending" if overview_enabled else "skipped",
            **({} if overview_enabled else {"reason": skipped["reason"]}),
        }
        batch_id = manifest["batchId"]
        if not batch_id:
            raise PcbCaptureConfigurationError("strict PCB capture requires segment.name")
        target: dict[str, Any] | None = None
        if route_enabled:
            path_report_path = Path(options["channelPathReport"])
            if not path_report_path.is_file():
                raise FileNotFoundError(
                    f"Channel Path report not found: {path_report_path}"
                )
            source["channelPathReport"] = str(path_report_path)
            target = build_strict_batch_capture_target(
                _json_object(path_report_path), batch_id=batch_id
            )
            manifest["captureSet"] = {
                "unit": "snp-batch",
                "count": 1,
                "batchId": batch_id,
                "channels": target["channels"],
                "pathCount": target["pathCount"],
                "selectedNets": target["nets"],
                "selectedComponents": target["selectedComponents"],
                "endpointRefdes": target["endpointRefdes"],
                "startComponents": target["startComponents"],
                "endComponents": target["endComponents"],
                "referenceNetIncluded": False,
            }
            manifest["componentPresentation"] = _strict_component_presentation_set(
                batch_id=batch_id,
                start_components=target["startComponents"],
                end_components=target["endComponents"],
            )

        if not route_enabled and not overview_enabled:
            manifest["status"] = "skipped"
        elif plan_only:
            if route_enabled:
                assert target is not None
                entries = _strict_planned_entries(target, options)
                for entry in entries:
                    evidence_path = _write_evidence(
                        evidence_dir,
                        entry=entry,
                        source=source,
                        options=options,
                        render=None,
                        image_relative_path=None,
                        view_state_relative_path=None,
                        error=None,
                    )
                    entry["evidence"] = evidence_path.relative_to(
                        package_dir
                    ).as_posix()
                manifest["captures"] = entries
                manifest["routeCapture"]["status"] = "planned"
            if overview_enabled:
                manifest["jobOverview"]["status"] = "planned"
            manifest["status"] = "planned"
        else:
            geometry = inspect_channel_geometry(
                Path(context["reference"]["aedb"]),
                [target] if target is not None else [],
                aedt_version=options["aedtVersion"],
            )
            geometry_path = evidence_dir / "geometry_inventory.json"
            _write_json(geometry_path, geometry)
            manifest["geometryEvidence"] = geometry_path.relative_to(package_dir).as_posix()
            all_signal_layers = [str(item) for item in geometry.get("signalLayers") or []]
            if route_enabled:
                assert target is not None
                entries = build_strict_batch_capture_entries(target, geometry, options)
                source_siw = Path(context["reference"]["siw"])
                for entry in entries:
                    final_image_path = images_dir / entry["fileName"]
                    view_state_path = view_states_dir / f"{entry['captureId']}.siw"
                    entry["image"] = final_image_path.relative_to(package_dir).as_posix()
                    entry["viewStateProject"] = view_state_path.relative_to(
                        package_dir
                    ).as_posix()
                    color_preparation = prepare_strict_siw_capture_view_state(
                        source_siw,
                        view_state_path,
                        target_layer=_entry_target_layers(entry),
                        selected_nets=entry["netsOnLayer"],
                        highlight=options["highlight"],
                        view=options["view"],
                        aedt_version=str(options["aedtVersion"]),
                    )
                    rewrite_evidence = color_preparation["rewrite"]
                    capability_preflight = color_preparation["capabilityPreflight"]
                    entry["netColorStrategy"] = color_preparation["strategy"]
                    entry["allNetNames"] = color_preparation["allNetNames"]
                    render = _render_siwave_capture(
                        project_path=view_state_path,
                        image_path=final_image_path,
                        entry=entry,
                        all_signal_layers=all_signal_layers,
                        aedt_version=options["aedtVersion"],
                        image_options=options["image"],
                        highlight_options=options["highlight"],
                        require_runtime_filtering=False,
                        strict_customer=True,
                    )
                    render["colorCapabilityPreflight"] = capability_preflight
                    render["viewStateRewrite"] = rewrite_evidence
                    render["sourceProjectArtifact"] = view_state_path.relative_to(
                        package_dir
                    ).as_posix()
                    render["finalImageAnalysis"] = analyze_strict_capture_png(
                        final_image_path
                    )
                    entry["status"] = "ok"
                    evidence_path = _write_evidence(
                        evidence_dir,
                        entry=entry,
                        source=source,
                        options=options,
                        render=render,
                        image_relative_path=entry["image"],
                        view_state_relative_path=entry["viewStateProject"],
                        error=None,
                    )
                    entry["evidence"] = evidence_path.relative_to(
                        package_dir
                    ).as_posix()
                manifest["captures"] = entries
                manifest["artifacts"] = validate_strict_capture_artifacts(
                    package_dir, entries, started_at_ns=started_at_ns
                )
                manifest["componentPresentation"]["status"] = "verified"
                manifest["routeCapture"]["status"] = "ok"
            if overview_enabled:
                # Job 단위 전체 보드 overview: 첫 배치가 생성, 이후 배치는 재사용.
                overview_result = ensure_job_board_overview(
                    context,
                    options=options,
                    signal_layers=all_signal_layers,
                    run_dir=run_dir,
                )
                manifest["jobOverview"] = {
                    "enabled": True,
                    **overview_result,
                }
            manifest["status"] = "ok"
    except Exception as exc:
        manifest["status"] = "unresolved"
        manifest["unresolved"].append(
            {
                "stage": "strict-pcb-capture",
                "code": "strict-pcb-capture-failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    return _finalize_capture_package(
        staging_dir=package_dir,
        final_dir=final_package_dir,
        manifest=manifest,
        plan_only=plan_only,
    )


def run_pcb_capture(
    context: dict[str, Any],
    *,
    plan_only: bool = False,
) -> Path:
    if is_customer_strict_capture(context):
        return _run_strict_pcb_capture(context, plan_only=plan_only)
    run_dir = Path(context["workspace"]["runDir"])
    final_package_dir = run_dir / ("pcb_capture_plan" if plan_only else "pcb_capture")
    staging_prefix = "pcbp" if plan_only else "pcbc"
    # Keep the staging leaf intentionally short.  The package also contains
    # channel-derived evidence filenames, and the previous descriptive leaf plus
    # a full UUID could push otherwise valid Windows work roots over MAX_PATH.
    package_dir = run_dir / f".{staging_prefix}.{uuid.uuid4().hex[:8]}.tmp"
    _verified_package_child(run_dir, final_package_dir)
    _verified_package_child(run_dir, package_dir)
    images_dir = package_dir / "images"
    evidence_dir = package_dir / "evidence"
    view_states_dir = package_dir / "view_states"
    for path in (package_dir, images_dir, evidence_dir, view_states_dir):
        path.mkdir(parents=True, exist_ok=True)

    package_index_path = package_dir / "pcb_capture_package.json"
    created_at = datetime.now().isoformat(timespec="seconds")
    run_context_path = run_dir / "run_context.json"
    source = {
        "configPath": context.get("configPath"),
        "runContext": str(run_context_path) if run_context_path.is_file() else None,
        "referenceSiw": context["reference"]["siw"],
        "referenceEdb": context["reference"]["aedb"],
    }
    manifest: dict[str, Any] = {
        "schema": CAPTURE_MANIFEST_SCHEMA,
        "createdAt": created_at,
        "status": "unresolved",
        "planOnly": plan_only,
        "source": source,
        "contract": None,
        "decisionPending": DECISION_PENDING_DEFAULTS,
        "geometryEvidence": None,
        "overviewCaptures": [],
        "captures": [],
        "unresolved": [],
        "packageIndex": package_index_path.name,
    }

    try:
        options = normalize_capture_options(context)
        manifest["contract"] = options
        path_report_path = Path(options["channelPathReport"])
        if not path_report_path.exists():
            raise FileNotFoundError(f"channel path report not found: {path_report_path}")
        source["channelPathReport"] = str(path_report_path)
        path_report = _json_object(path_report_path)
        targets, unresolved = build_channel_capture_targets(
            path_report,
            selected_channels=options["channels"],
        )
        manifest["unresolved"].extend(unresolved)
        if not targets:
            raise RuntimeError("no resolved channel paths are available for PCB capture")

        if plan_only:
            overview_entries = _planned_overview_entries(options)
            entries = [_planned_entry(target) for target in targets]
            _ensure_unique_capture_filenames(overview_entries + entries)
            for entry in overview_entries + entries:
                evidence_path = _write_evidence(
                    evidence_dir,
                    entry=entry,
                    source=source,
                    options=options,
                    render=None,
                    image_relative_path=None,
                    view_state_relative_path=None,
                )
                entry["evidence"] = evidence_path.relative_to(package_dir).as_posix()
            manifest["overviewCaptures"] = overview_entries
            manifest["captures"] = entries
            manifest["status"] = _package_status(
                overview_entries + entries,
                plan_only=True,
                unresolved=manifest["unresolved"],
            )
        else:
            edb_path = Path(context["reference"]["aedb"])
            try:
                geometry = inspect_channel_geometry(
                    edb_path,
                    targets,
                    aedt_version=str(options["aedtVersion"]),
                )
            except Exception as exc:
                error = {
                    "stage": "geometry",
                    "code": "pyedb-geometry-inspection-failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "requiresAnsysRuntime": True,
                    "failureClassification": "unclassified-runtime-error",
                }
                manifest["unresolved"].append(error)
                overview_entries = _planned_overview_entries(options)
                entries = [_planned_entry(target) for target in targets]
                for entry in overview_entries + entries:
                    entry["status"] = "unresolved-geometry"
                    evidence_path = _write_evidence(
                        evidence_dir,
                        entry=entry,
                        source=source,
                        options=options,
                        render=None,
                        image_relative_path=None,
                        view_state_relative_path=None,
                        error=error,
                    )
                    entry["evidence"] = evidence_path.relative_to(package_dir).as_posix()
                manifest["overviewCaptures"] = overview_entries
                manifest["captures"] = entries
                manifest["status"] = "unresolved"
                return _finalize_capture_package(
                    staging_dir=package_dir,
                    final_dir=final_package_dir,
                    manifest=manifest,
                    plan_only=plan_only,
                )

            geometry_path = evidence_dir / "geometry_inventory.json"
            _write_json(geometry_path, geometry)
            manifest["geometryEvidence"] = geometry_path.relative_to(package_dir).as_posix()
            entries, geometry_unresolved = expand_targets_with_geometry(
                targets,
                geometry,
                options,
            )
            overview_entries, overview_unresolved = build_overview_capture_entries(
                geometry,
                options,
            )
            manifest["unresolved"].extend(geometry_unresolved)
            manifest["unresolved"].extend(overview_unresolved)
            source_siw = Path(context["reference"]["siw"])
            all_signal_layers = [str(item) for item in geometry.get("signalLayers") or []]
            _ensure_unique_capture_filenames(overview_entries + entries)

            for entry in overview_entries + entries:
                image_path = images_dir / entry["fileName"]
                view_state_path = view_states_dir / f"{entry['captureId']}.siw"
                is_overview = entry.get("kind") == "board-overview"
                rewrite_evidence: dict[str, Any] | None = None
                render: dict[str, Any] | None = None
                error: dict[str, Any] | None = None
                capture_project: Path | None = None
                view_state_rewritten = False
                try:
                    rewrite_evidence = rewrite_siw_view_state(
                        source_siw,
                        view_state_path,
                        target_layer=entry["layer"],
                        selected_nets=entry["netsOnLayer"],
                        highlight=options["highlight"],
                        show_dimension_markers=bool(
                            options["view"].get("showDimensionMarkers")
                        ),
                        show_grid=bool(options["view"].get("showGrid")),
                        show_pin_names=bool(options["view"].get("showPinNames")),
                        show_all_nets=is_overview,
                        preserve_other_net_colors=is_overview,
                        fill_target_layers=not is_overview,
                    )
                    capture_project = view_state_path
                    view_state_rewritten = True
                except Exception as exc:
                    error = {
                        "stage": "view-state",
                        "code": "siwave-view-state-rewrite-failed",
                        "message": f"{type(exc).__name__}: {exc}",
                        "fallback": (
                            "byte-for-byte package copy plus documented SIWave "
                            "selection/layer APIs"
                        ),
                    }
                    try:
                        if not source_siw.is_file():
                            raise FileNotFoundError(
                                f"reference SIW is not a file: {source_siw}"
                            )
                        shutil.copyfile(source_siw, view_state_path)
                        capture_project = view_state_path
                        rewrite_evidence = {
                            "sourcePath": str(source_siw),
                            "viewStatePath": str(view_state_path),
                            "targetLayer": entry["layer"],
                            "targetNets": entry["netsOnLayer"],
                            "mechanism": (
                                "byte-for-byte source copy; runtime filtering required; "
                                "source project remains unopened"
                            ),
                            "rewriteError": error,
                        }
                    except Exception as copy_exc:
                        error = {
                            "viewState": error,
                            "copy": {
                                "stage": "view-state-copy",
                                "code": "siwave-source-copy-failed",
                                "message": f"{type(copy_exc).__name__}: {copy_exc}",
                            },
                        }

                try:
                    if capture_project is None:
                        raise RuntimeError(
                            "no safe package-local SIW capture project is available"
                        )
                    render = _render_siwave_capture(
                        project_path=capture_project,
                        image_path=image_path,
                        entry=entry,
                        all_signal_layers=all_signal_layers,
                        aedt_version=str(options["aedtVersion"]),
                        image_options=options["image"],
                        highlight_options=options["highlight"],
                        require_runtime_filtering=not view_state_rewritten,
                    )
                    if not view_state_rewritten:
                        render.setdefault("fallbacks", []).append(
                            {
                                "code": "view-state-rewrite-unavailable",
                                "message": (
                                    "captured from a package-local byte copy using runtime "
                                    "layer visibility and net selection"
                                ),
                            }
                        )
                        render["status"] = "generated-with-fallback"
                    entry["status"] = str(render["status"])
                except Exception as exc:
                    entry["status"] = "unresolved-render"
                    render_error = {
                        "stage": "render",
                        "code": "siwave-capture-failed",
                        "message": f"{type(exc).__name__}: {exc}",
                        "requiresAnsysRuntime": True,
                        "requiresGraphicalDesktop": True,
                        "failureClassification": "unclassified-runtime-error",
                        "manualFallback": (
                            "Open the package-local view-state SIW in SIWave, show only the "
                            "listed target layer, then "
                            + (
                                "show all nets and Fit All"
                                if is_overview
                                else "select the listed P/N route nets and Fit Selection"
                            )
                            + ", then export the modeling workspace as PNG."
                        ),
                    }
                    error = render_error if error is None else {
                        "viewState": error,
                        "render": render_error,
                    }
                    manifest["unresolved"].append(
                        {
                            "captureId": entry["captureId"],
                            **render_error,
                        }
                    )
                    if image_path.exists():
                        image_path.unlink()

                image_relative = (
                    image_path.relative_to(package_dir).as_posix()
                    if image_path.exists() and image_path.stat().st_size > 0
                    else None
                )
                view_state_relative = (
                    view_state_path.relative_to(package_dir).as_posix()
                    if view_state_path.exists()
                    else None
                )
                if rewrite_evidence is not None:
                    if render is None:
                        render = {}
                    rewrite_evidence["viewStatePath"] = view_state_relative
                    render["viewStateRewrite"] = rewrite_evidence
                if render is not None:
                    render["sourceProjectArtifact"] = view_state_relative
                    render["sidecarArtifacts"] = sorted(
                        path.relative_to(package_dir).as_posix()
                        for path in view_states_dir.glob(f"{view_state_path.stem}.*")
                        if path != view_state_path and path.is_file()
                    )
                evidence_path = _write_evidence(
                    evidence_dir,
                    entry=entry,
                    source=source,
                    options=options,
                    render=render,
                    image_relative_path=image_relative,
                    view_state_relative_path=view_state_relative,
                    error=error,
                )
                entry["image"] = image_relative
                entry["viewStateProject"] = view_state_relative
                entry["evidence"] = evidence_path.relative_to(package_dir).as_posix()

            manifest["overviewCaptures"] = overview_entries
            manifest["captures"] = entries
            manifest["status"] = _package_status(
                overview_entries + entries,
                plan_only=False,
                unresolved=manifest["unresolved"],
            )
    except Exception as exc:
        manifest["status"] = "unresolved"
        manifest["unresolved"].append(
            {
                "stage": "configuration-or-planning",
                "code": "pcb-capture-planning-failed",
                "message": f"{type(exc).__name__}: {exc}",
                "licenseRequired": False,
            }
        )

    return _finalize_capture_package(
        staging_dir=package_dir,
        final_dir=final_package_dir,
        manifest=manifest,
        plan_only=plan_only,
    )


def _write_package_index(path: Path, manifest: dict[str, Any]) -> None:
    overview_captures = []
    for entry in manifest.get("overviewCaptures") or []:
        overview_captures.append(
            {
                "captureId": entry.get("captureId"),
                "kind": entry.get("kind"),
                "view": entry.get("view"),
                "layer": entry.get("layer"),
                "layerSelection": entry.get("layerSelection"),
                "status": entry.get("status"),
                "image": entry.get("image"),
                "evidence": entry.get("evidence"),
            }
        )
    captures = []
    for entry in manifest.get("captures") or []:
        captures.append(
            {
                "captureId": entry.get("captureId"),
                "channel": entry.get("channel"),
                "layer": entry.get("layer"),
                "pathCompleteness": entry.get("pathCompleteness"),
                "status": entry.get("status"),
                "image": entry.get("image"),
                "evidence": entry.get("evidence"),
            }
        )
    payload = {
        "schema": CAPTURE_PACKAGE_SCHEMA,
        "createdAt": manifest.get("createdAt"),
        "status": manifest.get("status"),
        "routeCapture": manifest.get("routeCapture"),
        "jobOverview": manifest.get("jobOverview"),
        "manifest": "pcb_capture_manifest.json",
        "geometryEvidence": manifest.get("geometryEvidence"),
        "overviewCaptures": overview_captures,
        "captures": captures,
        "unresolvedCount": len(manifest.get("unresolved") or []),
        "publication": manifest.get("publication"),
        "packageRoot": ".",
    }
    _write_json(path, payload)


def build_standalone_capture_context(
    config_path: Path,
    *,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = _json_object(config_path)
    segment = config.get("segment") or config.get("interface") or {}
    segment_name = segment.get("name") or segment.get("interface") or "default"
    strategy = str(
        segment.get("strategy")
        or config.get("strategy")
        or config.get("ports", {}).get("mode")
        or "default"
    )
    run_name = (
        f"{_safe_name(str(segment_name))}__"
        f"{_safe_name(strategy.replace('-', '_'))}"
    )
    resolved_work_dir = (work_dir or ROOT_DIR / "work").resolve()
    run_dir = resolved_work_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    reference_siw = Path(config["layout"]["referenceSiw"]).resolve()
    reference_edb = Path(config["layout"]["referenceEdb"]).resolve()
    if not reference_siw.exists():
        raise FileNotFoundError(f"referenceSiw not found: {reference_siw}")
    if not reference_edb.exists():
        raise FileNotFoundError(f"referenceEdb not found: {reference_edb}")
    pcb_capture = dict(config.get("pcbCapture") or {})
    if not str(pcb_capture.get("aedtVersion") or "").strip():
        configured_version = (
            config.get("aedtVersion")
            or (config.get("inputProvenance") or {}).get("aedtVersion")
            or (config.get("preprocessing") or {}).get("version")
        )
        if configured_version:
            pcb_capture["aedtVersion"] = str(configured_version)
    return {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "configPath": str(config_path),
        "reference": {
            "siw": str(reference_siw),
            "aedb": str(reference_edb),
        },
        "segment": segment,
        "nets": config.get("nets", {}),
        "channelPath": config.get("channelPath", {}),
        "seriesModels": config.get("seriesModels", {}),
        "pcbCapture": pcb_capture,
        "workspace": {
            "root": str(ROOT_DIR),
            "runName": run_name,
            "runDir": str(run_dir.resolve()),
        },
    }


def pcb_capture_worker_main(argv: Sequence[str] | None = None) -> int:
    """Execute one live capture from an immutable serialized run context."""

    parser = argparse.ArgumentParser(
        description="Internal fresh-process worker for graphical SIWave PCB capture."
    )
    parser.add_argument("--worker-context", required=True, type=Path)
    parser.add_argument("--worker-result", required=True, type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args(argv)

    context_path = args.worker_context.resolve()
    result_path = args.worker_result.resolve()
    worker_pid = os.getpid()
    actual_parent_pid = os.getppid()
    receipt: dict[str, Any] = {
        "schema": PCB_CAPTURE_WORKER_RESULT_SCHEMA,
        "status": "error",
        "workerPid": worker_pid,
        "parentPid": args.parent_pid,
        "requestedParentPid": args.parent_pid,
        "actualLauncherParentPid": actual_parent_pid,
        "contextPath": str(context_path),
        "contextSha256": None,
        "manifestPath": None,
        "manifestStatus": None,
    }
    exit_code = 1
    try:
        if (
            args.parent_pid <= 0
            or args.parent_pid == worker_pid
            or actual_parent_pid <= 0
            or actual_parent_pid == worker_pid
        ):
            raise RuntimeError(
                "PCB capture worker requires distinct positive worker and parent PIDs"
            )
        context_bytes = context_path.read_bytes()
        receipt["contextSha256"] = hashlib.sha256(context_bytes).hexdigest()
        context = json.loads(context_bytes.decode("utf-8"))
        if not isinstance(context, dict):
            raise PcbCaptureConfigurationError(
                f"run context root must be an object: {context_path}"
            )

        manifest_path = run_pcb_capture(context, plan_only=False).resolve()
        manifest = _json_object(manifest_path)
        manifest_status = str(manifest.get("status") or "")
        receipt.update(
            {
                "status": "completed",
                "manifestPath": str(manifest_path),
                "manifestStatus": manifest_status,
            }
        )
        exit_code = (
            0
            if manifest_status in {"ok", "ok-with-fallback", "skipped"}
            else 2
        )
    except Exception as exc:
        receipt.update(
            {
                "errorType": type(exc).__name__,
                "error": str(exc).strip() or type(exc).__name__,
            }
        )
        traceback.print_exc()
    finally:
        _write_json(result_path, receipt)
    return exit_code


if __name__ == "__main__":
    worker_exit_code = pcb_capture_worker_main()
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(worker_exit_code)
