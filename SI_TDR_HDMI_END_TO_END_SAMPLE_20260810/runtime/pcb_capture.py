from __future__ import annotations

import json
import math
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parent
DCIR_VENV_SITE_PACKAGES = (
    ROOT_DIR.parent / "DCIR" / "SIwave_DCIR-1p4p1" / ".venv" / "Lib" / "site-packages"
)
CAPTURE_MANIFEST_SCHEMA = "si-tdr-pcb-capture-manifest/1"
CAPTURE_EVIDENCE_SCHEMA = "si-tdr-pcb-capture-evidence/1"
CAPTURE_PACKAGE_SCHEMA = "si-tdr-pcb-capture-package/1"
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


def ensure_capture_dependencies() -> None:
    if DCIR_VENV_SITE_PACKAGES.exists():
        site_packages = str(DCIR_VENV_SITE_PACKAGES.resolve())
        if site_packages not in sys.path:
            sys.path.insert(0, site_packages)


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


def _unquote_siw_token(value: str) -> str:
    return str(value).strip("\"'`")


def normalize_capture_options(context: dict[str, Any]) -> dict[str, Any]:
    raw = context.get("pcbCapture") or {}
    if not isinstance(raw, dict):
        raise PcbCaptureConfigurationError("pcbCapture must be a JSON object")

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
    target_layer: str,
    selected_nets: list[str],
    highlight: dict[str, Any],
    show_dimension_markers: bool = False,
    show_grid: bool = False,
    show_pin_names: bool = False,
    show_all_nets: bool = False,
    preserve_other_net_colors: bool = False,
) -> dict[str, Any]:
    text = source_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    selected = set(selected_nets)
    layer_section = False
    net_section = False
    layers_found: set[str] = set()
    nets_found: set[str] = set()
    layer_records_changed = 0
    net_records_changed = 0
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
            values[7] = "0"
            values[11:16] = ["1"] * 5 if layer_name == target_layer else ["0"] * 5
            output_lines.append(" ".join(values) + newline)
            layer_records_changed += 1
            continue

        if net_section and len(values) > 5:
            net_name = _unquote_siw_token(values[1])
            nets_found.add(net_name)
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

    if target_layer not in layers_found:
        raise RuntimeError(f"target layer not found in SIWave view state: {target_layer}")
    missing_nets = sorted(selected - nets_found)
    if selected and len(missing_nets) == len(selected):
        raise RuntimeError("none of the target route nets were found in the SIWave view state")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(output_lines), encoding="utf-8")
    return {
        "sourcePath": str(source_path),
        "viewStatePath": str(output_path),
        "targetLayer": target_layer,
        "targetNets": selected_nets,
        "missingTargetNets": missing_nets,
        "layerRecordsChanged": layer_records_changed,
        "netRecordsChanged": net_records_changed,
        "showDimensionMarkers": show_dimension_markers,
        "showGrid": show_grid,
        "showPinNames": show_pin_names,
        "showAllNets": show_all_nets,
        "preserveOtherNetColors": preserve_other_net_colors,
        "viewDirectivesFound": view_directives_found,
        "mechanism": "SIWave text view-state rewrite; source project remains unchanged",
    }


def detect_siwave_capabilities(project: object) -> dict[str, bool]:
    method_names = [
        "ScrUnselectAll",
        "ScrSelectNet",
        "ScrShowSelectedNetsOnly",
        "ScrFitSelection",
        "ScrFitAll",
        "ScrSetLayerVisibility",
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
) -> dict[str, Any]:
    ensure_capture_dependencies()
    from pyedb.siwave import Siwave  # noqa: PLC0415

    app = None
    project = None
    requested_view = str(entry.get("viewMode") or "fit-selection")
    if requested_view not in {"fit-selection", "fit-all"}:
        raise RuntimeError(f"unsupported SIWave capture view: {requested_view}")
    render: dict[str, Any] = {
        "backend": "pyedb.siwave.Siwave + SIWave scripting",
        "aedtVersion": aedt_version,
        "executionMode": "graphical-windows-com",
        "headlessSupported": False,
        "sourceProjectFileName": project_path.name,
        "requestedView": requested_view,
        "requestedLayer": entry["layer"],
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
        if not capabilities["ScrSaveToPngFile"]:
            raise RuntimeError("required SIWave capability missing: ScrSaveToPngFile")

        layer_visibility_results: dict[str, bool] = {}
        if require_runtime_filtering and entry["layer"] not in all_signal_layers:
            raise RuntimeError(
                "runtime layer filtering cannot verify the target against the "
                "PyEDB signal-layer inventory"
            )
        if require_runtime_filtering and capabilities["ScrSetLayerVisibility"]:
            for layer_name in all_signal_layers:
                visible = layer_name == entry["layer"]
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
        if require_runtime_filtering:
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
                    project.ScrFitSelection()
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
                                project.ScrShowSelectedNetsOnly(0)
                            except Exception as exc:
                                fallbacks.append(
                                    {
                                        "code": "show-all-nets-failed",
                                        "message": f"{type(exc).__name__}: {exc}",
                                    }
                                )
                        render["actualHighlight"] = {
                            "mode": "selected-net-highlight-with-context",
                            "color": "SIWave selection overlay",
                        }
                else:
                    can_fit_selection = False
            except Exception as exc:
                render["fitSelectionError"] = f"{type(exc).__name__}: {exc}"
                if require_runtime_filtering:
                    raise
                can_fit_selection = False

        if requested_view == "fit-selection" and not can_fit_selection:
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
        project.ScrSaveToPngFile(str(image_path))
        if not image_path.exists() or image_path.stat().st_size <= 0:
            raise RuntimeError("ScrSaveToPngFile returned without writing a non-empty PNG")
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
        "schema": CAPTURE_EVIDENCE_SCHEMA,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "status": entry["status"],
        "captureId": entry["captureId"],
        "source": source,
        "target": {
            "kind": entry.get("kind", "channel-net-zoom"),
            "view": entry.get("view"),
            "channel": entry.get("channel"),
            "polarities": entry.get("polarities") or [],
            "pathCompleteness": entry.get("pathCompleteness"),
            "nets": entry.get("nets") or [],
            "netsOnLayer": entry.get("netsOnLayer"),
            "startComponents": entry.get("startComponents") or [],
            "endpointComponents": entry.get("endpointComponents") or [],
            "layer": entry.get("layer"),
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
    successful_statuses = {"planned", "ok", "ok-with-fallback"}
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


def run_pcb_capture(
    context: dict[str, Any],
    *,
    plan_only: bool = False,
) -> Path:
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
