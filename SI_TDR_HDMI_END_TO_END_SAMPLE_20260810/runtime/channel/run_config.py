from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .analysis_templates import apply_default_analysis_templates
from .time_range import apply_tdr_time_range_resolution, resolve_tdr_time_range


SI_TDR_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLEAR_EXISTING_PORTS = True
DEFAULT_TOUCHSTONE_REFERENCE_IMPEDANCE_OHM = 50.0


def read_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def si_tdr_relative_or_absolute(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(SI_TDR_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _fill_missing_channel_settings(
    channel: dict[str, Any],
    defaults: dict[str, Any],
) -> None:
    """Fill settings absent from a generated channel without overriding CSV data."""
    for key, value in defaults.items():
        if key == "name":
            continue
        if key not in channel:
            channel[key] = deepcopy(value)
            continue
        if isinstance(channel[key], dict) and isinstance(value, dict):
            _fill_missing_channel_settings(channel[key], value)


def build_run_config(
    *,
    base_config_path: Path,
    config_fragment_path: Path,
    port_metadata_path: Path,
    output_path: Path,
    strategy: str = "csv-path-generated-metadata-snp",
    scope: str = "csv-path-generated-port-metadata",
    syz_template_id: str | None = None,
    syz_frequency_sweep: dict[str, Any] | None = None,
    part_library_path: Path | None = None,
    channel_path_report_path: Path | None = None,
    series_treatment: dict[str, Any] | None = None,
    reference_edb_path: Path | None = None,
    reference_siw_path: Path | None = None,
    aedt_version: str | None = None,
    port_impedance_ohm: float = DEFAULT_TOUCHSTONE_REFERENCE_IMPEDANCE_OHM,
    analysis_settings: dict[str, Any] | None = None,
    input_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = read_json_object(base_config_path)
    fragment = read_json_object(config_fragment_path)
    apply_default_analysis_templates(config)

    if aedt_version is not None:
        normalized_aedt_version = str(aedt_version).strip()
        if not normalized_aedt_version:
            raise ValueError("aedt_version must be a non-empty string")
        config["aedtVersion"] = normalized_aedt_version

    analysis_settings = analysis_settings or {}
    if analysis_settings.get("name") or analysis_settings.get("interface"):
        segment = config.setdefault("segment", {})
        if analysis_settings.get("name"):
            segment["name"] = str(analysis_settings["name"])
        if analysis_settings.get("interface"):
            segment["interface"] = str(analysis_settings["interface"])
    if analysis_settings.get("referenceNet"):
        config.setdefault("nets", {})["reference"] = str(
            analysis_settings["referenceNet"]
        )
    if analysis_settings.get("referenceLayer"):
        config.setdefault("ports", {})["referenceLayer"] = str(
            analysis_settings["referenceLayer"]
        )

    if reference_edb_path is not None or reference_siw_path is not None:
        layout = config.setdefault("layout", {})
        if reference_edb_path is not None:
            layout["referenceEdb"] = str(reference_edb_path.resolve())
        if reference_siw_path is not None:
            layout["referenceSiw"] = str(reference_siw_path.resolve())

    if input_provenance is not None:
        config["inputProvenance"] = deepcopy(input_provenance)
    else:
        config.pop("inputProvenance", None)

    ports = config.setdefault("ports", {})
    ports["clearExistingPorts"] = bool(
        ports.get("clearExistingPorts", DEFAULT_CLEAR_EXISTING_PORTS)
    )
    ports["singleEndedImpedanceOhm"] = float(port_impedance_ohm)
    ports["mode"] = "metadata-snp"
    ports["metadataPath"] = si_tdr_relative_or_absolute(port_metadata_path)
    ports["touchstonePortCount"] = fragment["ports"]["touchstonePortCount"]
    ports["portOrder"] = fragment["ports"]["portOrder"]
    ports["portOrderPolicy"] = fragment["ports"].get("portOrderPolicy")
    if fragment["ports"].get("roleMetadataVersion") is not None:
        ports["roleMetadataVersion"] = int(
            fragment["ports"]["roleMetadataVersion"]
        )

    tdr = config.setdefault("tdr", {})
    base_channel_defaults = {
        str(channel.get("name")): deepcopy(channel)
        for channel in tdr.get("channels") or []
        if isinstance(channel, dict) and channel.get("name")
    }
    tdr["channels"] = deepcopy(fragment["tdr"]["channels"])
    for channel in tdr["channels"]:
        if not isinstance(channel, dict):
            continue
        defaults = base_channel_defaults.get(str(channel.get("name") or ""))
        if defaults is not None:
            _fill_missing_channel_settings(channel, defaults)
    if channel_path_report_path is not None:
        endpoint_source = si_tdr_relative_or_absolute(channel_path_report_path)
        for channel in tdr["channels"]:
            metadata = channel.get("measurementEndpoints")
            if isinstance(metadata, dict):
                metadata["sourceArtifact"] = endpoint_source
    base_report_views = {
        str(group.get("name")): deepcopy(group["view"])
        for group in tdr.get("reportGroups") or []
        if isinstance(group, dict)
        and group.get("name")
        and isinstance(group.get("view"), dict)
    }
    report_groups = deepcopy(fragment["tdr"]["reportGroups"])
    for group in report_groups:
        if not isinstance(group, dict) or isinstance(group.get("view"), dict):
            continue
        base_view = base_report_views.get(str(group.get("name") or ""))
        if base_view is not None:
            group["view"] = base_view
    tdr["reportGroups"] = report_groups

    if syz_frequency_sweep is not None:
        syz = config.setdefault("syz", {})
        syz["frequencySweep"] = syz_frequency_sweep
        if syz_template_id:
            syz["templateId"] = syz_template_id
            selection = config.get("analysisTemplateSelection")
            if isinstance(selection, dict):
                selection["syzTemplateId"] = syz_template_id
                sources = selection.setdefault("source", {})
                if isinstance(sources, dict):
                    sources["syz"] = "normalization_profile"
    if analysis_settings.get("touchstoneBaseName"):
        config.setdefault("syz", {})["touchstoneBaseName"] = str(
            analysis_settings["touchstoneBaseName"]
        )

    if channel_path_report_path is not None:
        config["channelPath"] = {
            "report": si_tdr_relative_or_absolute(channel_path_report_path),
        }

    if part_library_path is not None:
        if channel_path_report_path is None:
            raise ValueError("part_library_path requires channel_path_report_path")
        config["seriesModels"] = {
            "partLibrary": si_tdr_relative_or_absolute(part_library_path),
            "channelPathReport": si_tdr_relative_or_absolute(channel_path_report_path),
        }
        config["seriesTreatment"] = series_treatment or {}

    path_report = (
        read_json_object(channel_path_report_path)
        if channel_path_report_path is not None
        else None
    )
    time_range_resolution = resolve_tdr_time_range(
        tdr,
        path_report,
        path_report_source=(
            si_tdr_relative_or_absolute(channel_path_report_path)
            if channel_path_report_path is not None
            else None
        ),
    )
    if time_range_resolution["status"] == "resolved":
        apply_tdr_time_range_resolution(tdr, time_range_resolution)
    else:
        tdr["timeRangeResolution"] = time_range_resolution

    segment = config.setdefault("segment", {})
    segment["strategy"] = strategy
    segment["scope"] = scope

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(config, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return config
