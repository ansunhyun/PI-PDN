from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from ..admin_config import normalize_pcb_capture_policy
except ImportError:  # Support compact customer core imports.
    from admin_config import normalize_pcb_capture_policy  # type: ignore[no-redef]

from .analysis_templates import apply_default_analysis_templates
from .time_range import (
    ROUTE_VIEW_DERIVED_STOP_SOURCE,
    apply_tdr_time_range_resolution,
    derive_route_view_transient_stop_ps,
    resolve_tdr_time_range,
)


SI_TDR_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLEAR_EXISTING_PORTS = True
DEFAULT_TOUCHSTONE_REFERENCE_IMPEDANCE_OHM = 50.0
STRICT_TDR_CIRCUIT_TOPOLOGY = "manual-snp-multi-diff"
STRICT_TDR_MODE = "differential"
STRICT_PCB_CAPTURE_CONTRACT = "customer-strict-snp-batch/2"
TDR_OPTION_SETTING_FIELDS = (
    "riseTimePs",
    "pulseRepetition",
    "pulseWidth",
    "timeDelay",
)
STRICT_REMOVED_TDR_FIELDS = frozenset(
    {"stepPs", "stopPs", "useTsConvolution", "useToConvolution", "differentialBridgeOhm"}
)
# 9.4.7.1: 생성 단계가 route 유도 stop을 tdr.transient.stopPs 명시값으로
# 설치하므로 strip은 stopPs를 더 이상 제거하지 않는다.  고객/관리자 입력의
# stopPs 선언은 여전히 STRICT_REMOVED_TDR_FIELDS로 거부한다.
STRICT_STRIPPED_TDR_FIELDS = frozenset(
    {"stepPs", "useTsConvolution", "useToConvolution", "differentialBridgeOhm"}
)
# 유도 preview 전용 placeholder.  stop은 관계 검증(stop >= viewMax)만 통과하면
# 되고 preview 결과는 폐기된다.
STRICT_STOP_DERIVATION_PREVIEW_PS = 1.0e9

# 9.4.7: strict 고객 경로는 채널 route 길이에서 report view를 유도한다.
# 값은 docs/si-tdr-route-length-time-range.md의 제안 sample과 동일하며
# 고객 확정 대기(validation-only) 상태를 그대로 표기한다.  해석 길이
# (solve/data stop)는 문서 계약상 report_view_only에서 view와 독립이며,
# strict runtime 내부 고정 stop(30 ns)을 명시적 analysis stop으로 주입한다.
STRICT_TDR_TIME_RANGE_POLICY: dict[str, Any] = {
    "mode": "route_length",
    "routeLengthSource": "resolved_channel_path",
    "differentialLengthPolicy": "max_positive_negative",
    "channelAggregationPolicy": "max_selected_channel",
    "propagation": {
        "velocity": {"value": 0.149896229, "unit": "mm/ps"},
    },
    "roundTripFactor": 2,
    "observationWindow": {
        "scope": "report_view_only",
        "reflectionRoundTripCount": 2,
        "riseTimeGuardMultiplier": 10,
        "riseTime": {"value": 50, "unit": "ps"},
        "quantization": {"mode": "ceil", "step": {"value": 0.5, "unit": "ns"}},
        "reportGroupAggregationPolicy": "max_channel_view",
        "commonGroupedReportPolicy": "per_report_group_view",
        "implementationStatus": "validation-only",
        "evaluationStatus": "not_evaluated",
        "customerApproval": "pending",
    },
    "manualOverrideAllowed": False,
}



def _install_strict_route_derived_transient_stop(
    tdr: dict[str, Any],
    path_report: dict[str, Any] | None,
    path_report_source: str | None,
) -> None:
    """9.4.7.1: route 유도 view에서 해석 stop을 계산해 명시값으로 설치한다.

    view 계산은 resolver의 관측 공식을 preview 호출로 재사용한다(중복 구현
    금지).  preview가 route_length로 resolve되지 않으면 stop을 설치하지 않고,
    최종 resolve가 같은 이유와 missing_analysis_stop으로 fail-closed된다.
    """

    preview = resolve_tdr_time_range(
        deepcopy(tdr),
        path_report,
        path_report_source=path_report_source,
        internal_analysis_stop_ps=STRICT_STOP_DERIVATION_PREVIEW_PS,
        internal_analysis_stop_source="strict-route-stop-derivation-preview",
    )
    if preview.get("status") != "resolved" or preview.get("mode") != "route_length":
        return
    view_max_ps = (preview.get("effective") or {}).get("viewMaxPs")
    stop_ps = derive_route_view_transient_stop_ps(view_max_ps)
    tdr["transient"] = {
        "stopPs": float(stop_ps),
        "stopSource": ROUTE_VIEW_DERIVED_STOP_SOURCE,
    }


def _strip_internal_resolution_fields(value: Any) -> None:
    """Keep removed customer keys out of a strict generated Run Config.

    The route-length resolver can use a stop time internally while deriving the
    displayed X range.  That internal evidence is written to its own runtime
    record; it must not reappear as a customer-adjustable Run Config field.
    """

    if isinstance(value, dict):
        for key in list(value):
            if key in STRICT_STRIPPED_TDR_FIELDS:
                value.pop(key)
            else:
                _strip_internal_resolution_fields(value[key])
    elif isinstance(value, list):
        for child in value:
            _strip_internal_resolution_fields(child)


def _reject_strict_removed_customer_tdr_fields(value: Any, *, path: str = "tdr") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in STRICT_REMOVED_TDR_FIELDS:
                raise ValueError(
                    f"strict customer Run Config rejects removed TDR field: {child_path}"
                )
            _reject_strict_removed_customer_tdr_fields(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_strict_removed_customer_tdr_fields(
                child, path=f"{path}[{index}]"
            )


def _require_strict_spec_impedance(channels: list[Any]) -> None:
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ValueError(f"strict customer tdr.channels[{index}] must be an object")
        name = str(channel.get("name") or index)
        if "referenceImpedanceOhm" not in channel:
            raise ValueError(
                f"strict customer channel {name!r} requires Spec referenceImpedanceOhm"
            )
        target_range = channel.get("targetRangeOhm")
        if not isinstance(target_range, dict) or not {
            "lower",
            "upper",
            "source",
        }.issubset(target_range):
            raise ValueError(
                f"strict customer channel {name!r} requires Spec Target/Min/Max"
            )
        if str(target_range.get("source") or "").casefold() not in {
            "spec",
            "customer_detailed_csv",
        }:
            raise ValueError(
                f"strict customer channel {name!r} Target/Min/Max must come from Spec"
            )


def _apply_strict_tdr_topology_policy(tdr: dict[str, Any]) -> None:
    """Install the non-customer Circuit topology policy without hiding conflicts."""

    expected = {
        "circuitTopology": STRICT_TDR_CIRCUIT_TOPOLOGY,
        "mode": STRICT_TDR_MODE,
    }
    for field, required_value in expected.items():
        configured = tdr.get(field)
        if configured is not None and configured != required_value:
            raise ValueError(
                f"strict customer Run Config requires tdr.{field}="
                f"{required_value!r}; got {configured!r}"
            )
        tdr[field] = required_value


def _strict_pcb_capture_policy(
    configured_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the fixed customer capture policy, separate from legacy PoC options."""

    return {
        **normalize_pcb_capture_policy(configured_policy),
        "contract": STRICT_PCB_CAPTURE_CONTRACT,
        "captureUnit": "snp-batch-union",
        "fileNames": {"route": "route.png"},
        "highlight": {
            "mode": "context",
            "selectedColor": "0xFF0000",
            "contextColor": "0x808080",
            "includeReferenceNet": False,
        },
        "view": {
            "mode": "fit-selection",
            "fallback": "none",
            "showDimensionMarkers": False,
            "showGrid": False,
            "showPinNames": False,
        },
        "image": {
            "format": "png",
            "widthPx": 1920,
            "heightPx": 1080,
            "resizeMode": "contain-white",
        },
        "componentPresentation": {
            "pathComponentVisibility": {
                "status": "deferred-follow-up",
                "releaseBlocking": False,
            },
        },
    }


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


def _apply_analysis_option_selection(
    tdr: dict[str, Any],
    selection: dict[str, Any],
) -> None:
    raw_tdr_selections = selection.get("tdr")
    if not isinstance(raw_tdr_selections, dict):
        raise ValueError("analysisOptionSelection.tdr must be an object")
    report_groups = {
        str(group.get("name") or "").casefold(): group
        for group in tdr.get("reportGroups") or []
        if isinstance(group, dict) and group.get("name")
    }
    channels = {
        str(channel.get("name") or ""): channel
        for channel in tdr.get("channels") or []
        if isinstance(channel, dict) and channel.get("name")
    }
    for item_id, raw_item_selection in raw_tdr_selections.items():
        item_selection = raw_item_selection
        if not isinstance(item_selection, dict):
            raise ValueError(
                f"analysisOptionSelection.tdr.{item_id} must be an object"
            )
        report_group = report_groups.get(str(item_id).casefold())
        if report_group is None:
            raise ValueError(
                f"analysisOptionSelection references unknown TDR item {item_id!r}; "
                f"available={sorted(group.get('name') for group in report_groups.values())}"
            )
        settings = item_selection.get("settings")
        if not isinstance(settings, dict):
            raise ValueError(
                f"analysisOptionSelection.tdr.{item_id}.settings must be an object"
            )
        missing = [field for field in TDR_OPTION_SETTING_FIELDS if field not in settings]
        if missing:
            raise ValueError(
                f"analysisOptionSelection.tdr.{item_id}.settings is missing {missing}"
            )
        for channel_name in report_group.get("channels") or []:
            channel = channels.get(str(channel_name))
            if channel is None:
                raise ValueError(
                    f"TDR item {item_id!r} references unknown channel {channel_name!r}"
                )
            for field in TDR_OPTION_SETTING_FIELDS:
                channel[field] = deepcopy(settings[field])


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
    analysis_option_selection: dict[str, Any] | None = None,
    customer_component_handling: dict[str, Any] | None = None,
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
    strict_customer_mode = customer_component_handling is not None
    if strict_customer_mode:
        if analysis_option_selection is None:
            raise ValueError(
                "strict customer Run Config requires analysis_option_selection"
            )
        if syz_frequency_sweep is not None:
            raise ValueError(
                "strict customer Run Config cannot use JSON syz.frequencySweep"
            )
        if "frequencySweep" in (config.get("syz") or {}):
            raise ValueError(
                "strict customer Run Config cannot inherit JSON syz.frequencySweep"
            )
        if part_library_path is not None or series_treatment is not None:
            raise ValueError(
                "customer_component_handling cannot be combined with legacy Part Library "
                "or seriesTreatment"
            )
        if "seriesModels" in config or "seriesTreatment" in config:
            raise ValueError(
                "strict customer Run Config cannot inherit seriesModels or seriesTreatment"
            )
        if analysis_settings.get("referenceLayer"):
            raise ValueError(
                "strict customer Run Config cannot use a fixed referenceLayer"
            )
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
    if strict_customer_mode:
        if "referenceLayer" in ports:
            raise ValueError(
                "strict customer Run Config cannot inherit ports.referenceLayer"
            )
        # 관리자 Config는 이 정책들을 선언하지 않는다. 생성 Run Config에서 고정한다.
        ports["terminalLayerPolicy"] = "endpoint-pin-same-layer"
        ports["portContractValidation"] = "strict"
        ports["referenceNetPolicy"] = "edb-ground-auto-fail-closed"
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
    if strict_customer_mode:
        _apply_strict_tdr_topology_policy(tdr)
    base_channel_defaults = {
        str(channel.get("name")): deepcopy(channel)
        for channel in tdr.get("channels") or []
        if isinstance(channel, dict) and channel.get("name")
    }
    tdr["channels"] = deepcopy(fragment["tdr"]["channels"])
    if strict_customer_mode:
        _require_strict_spec_impedance(tdr["channels"])
    for channel in tdr["channels"]:
        if not isinstance(channel, dict):
            continue
        defaults = base_channel_defaults.get(str(channel.get("name") or ""))
        if defaults is not None:
            _fill_missing_channel_settings(channel, defaults)
    if tdr.get("referenceImpedanceOhm") is None:
        first_channel_reference = next(
            (
                channel.get("referenceImpedanceOhm")
                for channel in tdr["channels"]
                if isinstance(channel, dict)
                and channel.get("referenceImpedanceOhm") is not None
            ),
            None,
        )
        if first_channel_reference is not None:
            # Runtime requires a global fallback even though the customer Spec
            # remains authoritative per channel.
            tdr["referenceImpedanceOhm"] = deepcopy(first_channel_reference)
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

    if analysis_option_selection is not None:
        _apply_analysis_option_selection(tdr, analysis_option_selection)
        config["analysisOptionSelection"] = deepcopy(analysis_option_selection)

    if strict_customer_mode:
        _reject_strict_removed_customer_tdr_fields(tdr)
        if tdr.get("timeRangePolicy") is not None:
            raise ValueError(
                "strict customer Run Config installs its own tdr.timeRangePolicy; "
                "the administrator Config must not declare one"
            )
        tdr["timeRangePolicy"] = deepcopy(STRICT_TDR_TIME_RANGE_POLICY)
        result_processing = tdr.setdefault("resultProcessing", {})
        result_processing["endpointAnnotations"] = {
            "enabled": True,
            "mode": "boundary_labels",
            "layout": {
                "placement": "plot_inside",
                "fontSizePt": 32,
                "wrapWidthChars": 24,
            },
            "source": "internal-strict-report-policy",
        }

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

    if strict_customer_mode:
        if channel_path_report_path is None:
            raise ValueError(
                "customer_component_handling requires channel_path_report_path"
            )
        assert customer_component_handling is not None
        customer_contract = deepcopy(customer_component_handling)
        if customer_contract.get("series") != "short":
            raise ValueError("strict customer Series policy must be short")
        if customer_contract.get("array") != "bom-configured-source-column-authoritative":
            raise ValueError(
                "strict customer Array policy must be "
                "bom-configured-source-column-authoritative"
            )
        if not isinstance(customer_contract.get("arrayCatalog"), list):
            raise ValueError(
                "strict customer component handling requires arrayCatalog"
            )
        customer_contract["channelPathReport"] = si_tdr_relative_or_absolute(
            channel_path_report_path
        )
        config["customerComponentHandling"] = customer_contract
        config.pop("seriesModels", None)
        config.pop("seriesTreatment", None)
        config["pcbCapture"] = _strict_pcb_capture_policy(config.get("pcbCapture"))

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
    path_report_source_value = (
        si_tdr_relative_or_absolute(channel_path_report_path)
        if channel_path_report_path is not None
        else None
    )
    if strict_customer_mode:
        _install_strict_route_derived_transient_stop(
            tdr, path_report, path_report_source_value
        )
    time_range_resolution = resolve_tdr_time_range(
        tdr,
        path_report,
        path_report_source=path_report_source_value,
    )
    if time_range_resolution["status"] == "resolved":
        apply_tdr_time_range_resolution(tdr, time_range_resolution)
        if strict_customer_mode:
            _strip_internal_resolution_fields(tdr)
    else:
        tdr["timeRangeResolution"] = time_range_resolution
        if strict_customer_mode:
            _strip_internal_resolution_fields(tdr)


    segment = config.setdefault("segment", {})
    segment["strategy"] = strategy
    segment["scope"] = scope

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(config, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return config
