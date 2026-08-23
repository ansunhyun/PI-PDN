from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from SI_TDR.console_progress import ConsoleLogSession, ConsoleProgress
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from console_progress import (  # type: ignore[no-redef]
        ConsoleLogSession,
        ConsoleProgress,
    )

try:
    from SI_TDR.result_export import (
        BatchExecutionInput,
        export_eden_web_results,
    )
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from result_export import (  # type: ignore[no-redef]
        BatchExecutionInput,
        export_eden_web_results,
    )

try:
    from SI_TDR.channel.time_range import (
        TimeRangeResolutionError,
        format_tdr_time_ps,
        resolve_run_config_time_range,
        write_time_range_resolution,
    )
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from channel.time_range import (  # type: ignore[no-redef]
        TimeRangeResolutionError,
        format_tdr_time_ps,
        resolve_run_config_time_range,
        write_time_range_resolution,
    )

try:
    from SI_TDR.channel.target_band import (
        add_tdr_impedance_chart_overlays,
        build_tdr_impedance_metadata,
        resolve_reference_impedance,
        validate_tdr_impedance_config,
        write_tdr_waveform_csv,
    )
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from channel.target_band import (  # type: ignore[no-redef]
        add_tdr_impedance_chart_overlays,
        build_tdr_impedance_metadata,
        resolve_reference_impedance,
        validate_tdr_impedance_config,
        write_tdr_waveform_csv,
    )

try:
    from SI_TDR.preprocess import (
        INPUT_PROVENANCE_KIND,
        ReferencePreprocessError,
        ReferencePreprocessResult,
        load_reference_preprocess_manifest,
        run_reference_preprocessor,
    )
except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
    from preprocess import (  # type: ignore[no-redef]
        INPUT_PROVENANCE_KIND,
        ReferencePreprocessError,
        ReferencePreprocessResult,
        load_reference_preprocess_manifest,
        run_reference_preprocessor,
    )


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
WORK_DIR = ROOT_DIR / "work"
OUTPUT_DIR = ROOT_DIR / "outputs"
DCIR_VENV_SITE_PACKAGES = (
    ROOT_DIR.parent / "DCIR" / "SIwave_DCIR-1p4p1" / ".venv" / "Lib" / "site-packages"
)
DEFAULT_AEDT_VERSION = "2024.2"

PORT_CREATION_ALIASES = {
    "net-to-reference": "ansys-net-to-reference",
    "ansys-net-to-reference": "ansys-net-to-reference",
    "create-circuit-port-on-net": "ansys-net-to-reference",
    "pin-to-reference-layer": "ansys-pin-to-reference-layer",
    "ansys-pin-to-reference-layer": "ansys-pin-to-reference-layer",
    "create-port-between-pin-and-layer": "ansys-pin-to-reference-layer",
    "pin-to-pin": "ansys-pin-to-pin",
    "ansys-pin-to-pin": "ansys-pin-to-pin",
    "create-circuit-port-on-pin": "ansys-pin-to-pin",
    "padstack-to-reference-layer": "low-level-padstack-to-reference-layer",
    "direct-padstack-to-reference-layer": "low-level-padstack-to-reference-layer",
    "low-level-padstack-to-reference-layer": "low-level-padstack-to-reference-layer",
}

SUPPORTED_PORT_CREATION_STRATEGIES = sorted(set(PORT_CREATION_ALIASES.values()))


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _resolve_aedt_version(
    config: dict[str, Any],
    *,
    preprocessing_result: ReferencePreprocessResult | None = None,
) -> str:
    preprocessing = config.get("preprocessing") or {}
    provenance = config.get("inputProvenance") or {}
    candidates = (
        preprocessing_result.aedt_version if preprocessing_result is not None else None,
        config.get("aedtVersion"),
        provenance.get("aedtVersion") if isinstance(provenance, dict) else None,
        preprocessing.get("version") if isinstance(preprocessing, dict) else None,
        DEFAULT_AEDT_VERSION,
    )
    value = next((candidate for candidate in candidates if candidate is not None), None)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("aedtVersion must be a non-empty scalar value")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("aedtVersion must be a non-empty scalar value")
    return normalized


def _context_aedt_version(context: dict[str, Any]) -> str:
    value = context.get("aedtVersion") or DEFAULT_AEDT_VERSION
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("run context aedtVersion must be a non-empty scalar value")
    return normalized


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def ensure_embedded_site_packages() -> None:
    import sys

    if DCIR_VENV_SITE_PACKAGES.exists():
        site_packages = str(DCIR_VENV_SITE_PACKAGES.resolve())
        if site_packages not in sys.path:
            sys.path.insert(0, site_packages)


def _generation_output_root(request_path: Path) -> Path:
    """Return the automatic output root used by a generation request."""

    return (request_path.resolve().parent / "outputs").resolve()


def _resolve_config_input_path(
    config_value: str | Path,
    *,
    current_dir: Path | None = None,
) -> Path:
    """Resolve a CLI input from the current folder, then the runtime folder."""

    path = Path(config_value)
    if path.is_absolute():
        return path.resolve()
    current_folder_path = ((current_dir or Path.cwd()) / path).resolve()
    if current_folder_path.exists():
        return current_folder_path
    return (ROOT_DIR / path).resolve()


def _validate_input_provenance(
    config: dict[str, Any],
    reference_siw: Path,
    reference_edb: Path,
) -> dict[str, Any] | None:
    provenance = config.get("inputProvenance")
    if provenance is None:
        return None
    if not isinstance(provenance, dict):
        raise ValueError("inputProvenance must be an object")
    if provenance.get("kind") != INPUT_PROVENANCE_KIND:
        raise ValueError(
            f"unsupported inputProvenance kind={provenance.get('kind')!r}"
        )
    manifest_value = provenance.get("manifest")
    if not isinstance(manifest_value, str) or not manifest_value.strip():
        raise ValueError("inputProvenance.manifest must be a non-empty string")
    try:
        preprocess_result = load_reference_preprocess_manifest(Path(manifest_value))
    except ReferencePreprocessError as exc:
        raise ValueError(f"input provenance validation failed: {exc}") from exc
    if provenance.get("manifestId") != preprocess_result.manifest_id:
        raise ValueError("inputProvenance manifestId does not match the handoff manifest")
    if reference_siw.resolve() != preprocess_result.reference_siw:
        raise ValueError(
            "layout.referenceSiw does not match inputProvenance referenceSiw"
        )
    if reference_edb.resolve() != preprocess_result.reference_aedb:
        raise ValueError(
            "layout.referenceEdb does not match inputProvenance referenceAedb"
        )
    return provenance


def build_run_context(
    config_path: Path,
    *,
    run_dir: Path | None = None,
    output_dir: Path | None = None,
    preprocessing_result: ReferencePreprocessResult | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    validate_tdr_impedance_config(config.get("tdr") or {})
    aedt_version = _resolve_aedt_version(
        config,
        preprocessing_result=preprocessing_result,
    )

    segment = config.get("segment") or config.get("interface") or {}
    segment_name = segment.get("name") or segment.get("interface") or "default"
    strategy = str(segment.get("strategy") or config.get("strategy") or config.get("ports", {}).get("mode") or "default")
    run_name = f"{segment_name}__{strategy.replace('-', '_')}"
    run_dir = (run_dir or (WORK_DIR / run_name)).resolve()
    output_dir = (output_dir or OUTPUT_DIR).resolve()
    touchstone_dir = run_dir / "touchstone"
    circuit_dir = run_dir / "circuit"
    report_dir = run_dir / "reports"

    if preprocessing_result is not None:
        reference_siw = preprocessing_result.reference_siw
        reference_edb = preprocessing_result.reference_aedb
        input_provenance = preprocessing_result.as_input_provenance()
    else:
        reference_siw = Path(config["layout"]["referenceSiw"])
        reference_edb = Path(config["layout"]["referenceEdb"])
        input_provenance = _validate_input_provenance(
            config,
            reference_siw,
            reference_edb,
        )

    ensure_exists(reference_siw, "referenceSiw")
    ensure_exists(reference_edb, "referenceEdb")

    for folder in (run_dir, touchstone_dir, circuit_dir, report_dir, output_dir):
        folder.mkdir(parents=True, exist_ok=True)

    time_range_resolution = resolve_run_config_time_range(
        config,
        config_path=config_path,
        project_root=ROOT_DIR,
    )
    time_range_record_path = write_time_range_resolution(
        run_dir / "tdr_time_range.json",
        time_range_resolution,
    )
    if time_range_resolution.get("status") != "resolved":
        raise TimeRangeResolutionError(
            f"TDR time range is unresolved; review {time_range_record_path}"
        )

    pcb_capture = dict(config.get("pcbCapture") or {})
    pcb_capture.setdefault("aedtVersion", aedt_version)

    context = {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "configPath": str(config_path.resolve()),
        "aedtVersion": aedt_version,
        "reference": {
            "siw": str(reference_siw.resolve()),
            "aedb": str(reference_edb.resolve()),
        },
        "inputProvenance": input_provenance,
        "segment": segment,
        "nets": config["nets"],
        "ports": config.get("ports", {}),
        "endpoints": config.get("endpoints", {}),
        "seriesComponents": config.get("seriesComponents", []) or config.get("segmentBoundary", {}).get("seriesComponents", []),
        "channelPath": config.get("channelPath", {}),
        "seriesModels": config.get("seriesModels", {}),
        "seriesTreatment": config.get("seriesTreatment", {}),
        "pcbCapture": pcb_capture,
        "parallelProtection": config.get("parallelProtection", []),
        "syz": config.get("syz", {}),
        "tdr": config["tdr"],
        "workspace": {
            "root": str(ROOT_DIR.resolve()),
            "runName": run_name,
            "runDir": str(run_dir.resolve()),
            "touchstoneDir": str(touchstone_dir.resolve()),
            "circuitDir": str(circuit_dir.resolve()),
            "reportDir": str(report_dir.resolve()),
            "outputDir": str(output_dir),
            "timeRangeRecordPath": str(time_range_record_path.resolve()),
        },
        "nextSteps": [
            "Export Touchstone from reference AEDB",
            "Build Circuit TDR setup",
            "Run simulation",
            "Generate waveform image and result JSON",
        ],
    }
    return context


def write_context(context: dict[str, Any]) -> Path:
    run_dir = Path(context["workspace"]["runDir"])
    context_path = run_dir / "run_context.json"
    with context_path.open("w", encoding="utf-8") as fp:
        json.dump(context, fp, indent=2)
    return context_path


def _record_created_port_count(record_path: Path) -> int:
    if not record_path.exists():
        return 0
    with record_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    return len(payload.get("createdPorts") or [])


def _reset_run_workspace(context: dict[str, Any]) -> None:
    import shutil

    run_dir = Path(context["workspace"]["runDir"])
    target_edb = run_dir / Path(context["reference"]["aedb"]).name
    for path in [
        target_edb,
        Path(context["workspace"]["touchstoneDir"]),
        Path(context["workspace"]["circuitDir"]),
        Path(context["workspace"]["reportDir"]),
    ]:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    for file_name in [
        "series_models_apply.json",
        "ports_apply.json",
        "syz_setup.json",
        "channel_solve.json",
        "tdr_transient.json",
        "tdr_image.json",
        "tdr_waveform.csv",
        "tdr_minmax_markers.json",
        "tdr_minmax_markers.csv",
        "tdr_endpoint_annotations.json",
        "tdr_endpoint_annotations.csv",
    ]:
        path = run_dir / file_name
        if path.exists():
            path.unlink()

    output_image = Path(context["workspace"]["outputDir"]) / f"{context['workspace'].get('runName', 'tdr')}_waveform.png"
    if output_image.exists():
        output_image.unlink()

    for folder in [
        Path(context["workspace"]["touchstoneDir"]),
        Path(context["workspace"]["circuitDir"]),
        Path(context["workspace"]["reportDir"]),
    ]:
        folder.mkdir(parents=True, exist_ok=True)


def _port_strategy(context: dict[str, Any]) -> str:
    return str(context["segment"].get("strategy") or context["ports"].get("mode") or "diff-s2p")


def _se4_port_creation_strategy(context: dict[str, Any]) -> tuple[str, str]:
    raw_strategy = str(context["ports"].get("portCreation") or "net-to-reference").strip()
    normalized = raw_strategy.casefold()
    strategy = PORT_CREATION_ALIASES.get(normalized)
    if not strategy:
        supported = ", ".join(SUPPORTED_PORT_CREATION_STRATEGIES)
        aliases = ", ".join(sorted(PORT_CREATION_ALIASES))
        raise ValueError(
            f"unsupported ports.portCreation={raw_strategy!r}; "
            f"supported strategies: {supported}; aliases: {aliases}"
        )
    return raw_strategy, strategy


def _created_terminal_name(created: object, fallback: str) -> str:
    for attr_name in ["GetName", "name"]:
        try:
            value = getattr(created, attr_name)
        except Exception:
            continue
        try:
            resolved = value() if callable(value) else value
        except Exception:
            continue
        if resolved:
            return str(resolved)
    return fallback if created else str(created)


def _touchstone_suffix(context: dict[str, Any]) -> str:
    port_count = context.get("ports", {}).get("touchstonePortCount") or context.get("ports", {}).get("portCount")
    if port_count:
        return f".s{int(port_count)}p"
    if context["ports"].get("mode") == "single-ended-4port":
        return ".s4p"
    return ".s2p"


def _touchstone_path(context: dict[str, Any]) -> Path:
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_base_name = str(
        context.get("syz", {}).get("touchstoneBaseName")
        or f"{segment_name}_SYZ_SETUP"
    )
    return Path(context["workspace"]["touchstoneDir"]) / (
        f"{touchstone_base_name}{_touchstone_suffix(context)}"
    )


def _resolved_touchstone_path(context: dict[str, Any]) -> Path:
    expected = _touchstone_path(context)
    if expected.exists():
        return expected

    record_path = Path(context["workspace"]["runDir"]) / "channel_solve.json"
    if record_path.exists():
        with record_path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        exported = [Path(item) for item in payload.get("exportedTouchstoneFiles") or []]
        if len(exported) == 1 and exported[0].exists():
            return exported[0]
    return expected


def _is_manual_s4p_diff_strategy(context: dict[str, Any]) -> bool:
    return _port_strategy(context) == "manual-s4p-diff"


def _is_manual_snp_multi_diff_tdr(context: dict[str, Any]) -> bool:
    return str(context.get("tdr", {}).get("circuitTopology") or "").casefold() == "manual-snp-multi-diff"


def _tdr_channel_value(
    context: dict[str, Any],
    channel: dict[str, Any] | None,
    key: str,
    default: Any = None,
) -> Any:
    if channel is not None and key in channel:
        return channel[key]
    return context.get("tdr", {}).get(key, default)


def _tdr_single_ended_impedance(context: dict[str, Any], channel: dict[str, Any] | None = None) -> float:
    global_reference = resolve_reference_impedance(context.get("tdr") or {})
    reference = resolve_reference_impedance(
        channel or {},
        fallback=global_reference,
        where=f"tdr.channels[{(channel or {}).get('name', 'global')}]",
    )
    if reference.value_ohm is None:
        raise ValueError("TDR reference impedance is not configured")
    mode = str(_tdr_channel_value(context, channel, "mode", context.get("tdr", {}).get("mode") or "")).casefold()
    if mode == "differential":
        return reference.value_ohm / 2.0
    return reference.value_ohm


def _tdr_rise_time_ps(context: dict[str, Any], channel: dict[str, Any] | None = None) -> float:
    return float(_tdr_channel_value(context, channel, "riseTimePs", 30))


def _tdr_pulse_repetition(context: dict[str, Any], channel: dict[str, Any] | None = None) -> str:
    return str(_tdr_channel_value(context, channel, "pulseRepetition", "3000000ms"))


def _tdr_pulse_width(context: dict[str, Any], channel: dict[str, Any] | None = None) -> str | None:
    value = _tdr_channel_value(context, channel, "pulseWidth")
    return str(value) if value is not None else None


def _tdr_time_delay(context: dict[str, Any], channel: dict[str, Any] | None = None) -> str | None:
    value = _tdr_channel_value(context, channel, "timeDelay")
    return str(value) if value is not None else None


def _tdr_transient_data(context: dict[str, Any]) -> list[str]:
    transient = context.get("tdr", {}).get("transient") or {}
    step_ps = float(transient.get("stepPs", 7.5))
    stop_ps = float(transient.get("stopPs", 30000))
    return [format_tdr_time_ps(step_ps), format_tdr_time_ps(stop_ps)]


def _tdr_use_ts_convolution(context: dict[str, Any]) -> bool:
    transient = context.get("tdr", {}).get("transient") or {}
    return bool(transient.get("useTsConvolution", False))


def _side_net(side: dict[str, Any], pair: str, fallback: str) -> str:
    nets = side.get("nets") or {}
    return str(nets.get(pair) or fallback)


def _side_reference_component(side: dict[str, Any], fallback: str) -> str:
    return str(side.get("referenceRefdes") or side.get("refdes") or fallback)


def _side_pin(side: dict[str, Any], pair: str) -> str | None:
    pins = side.get("pins") or {}
    value = pins.get(pair)
    return str(value) if value is not None else None


def _boundary_components(context: dict[str, Any]) -> dict[str, str]:
    components = context["ports"]["boundarySide"]["components"]
    return {
        "positive": str(components["positive"]),
        "negative": str(components["negative"]),
    }


def _se4_port_specs(context: dict[str, Any]) -> list[dict[str, Any]]:
    segment_name = context["segment"].get("name", "SEGMENT")
    endpoint_side = context["ports"].get("endpointSide") or context["ports"].get("jackSide")
    if endpoint_side is None:
        raise ValueError("single-ended-4port config requires ports.endpointSide or ports.jackSide")
    boundary_side = context["ports"]["boundarySide"]
    boundary_components = _boundary_components(context)
    reference_net = str(context["nets"]["reference"])
    endpoint_reference_component = _side_reference_component(endpoint_side, str(endpoint_side["refdes"]))
    boundary_reference_component = _side_reference_component(boundary_side, endpoint_reference_component)
    reference_layer = str(context["ports"].get("referenceLayer") or "Layer2")
    return [
        {
            "role": "endpoint_positive",
            "name": f"{segment_name}_P1_ENDPOINT_POS",
            "positiveComponent": str(endpoint_side["refdes"]),
            "positiveNet": _side_net(endpoint_side, "positive", str(context["nets"]["positive"])),
            "positivePin": _side_pin(endpoint_side, "positive"),
            "negativeComponent": endpoint_reference_component,
            "negativeNet": reference_net,
            "referenceLayer": reference_layer,
        },
        {
            "role": "endpoint_negative",
            "name": f"{segment_name}_P2_ENDPOINT_NEG",
            "positiveComponent": str(endpoint_side["refdes"]),
            "positiveNet": _side_net(endpoint_side, "negative", str(context["nets"]["negative"])),
            "positivePin": _side_pin(endpoint_side, "negative"),
            "negativeComponent": endpoint_reference_component,
            "negativeNet": reference_net,
            "referenceLayer": reference_layer,
        },
        {
            "role": "boundary_positive",
            "name": f"{segment_name}_P3_BOUNDARY_POS",
            "positiveComponent": boundary_components["positive"],
            "positiveNet": _side_net(boundary_side, "positive", str(context["nets"]["positive"])),
            "positivePin": _side_pin(boundary_side, "positive"),
            "negativeComponent": boundary_reference_component,
            "negativeNet": reference_net,
            "referenceLayer": reference_layer,
        },
        {
            "role": "boundary_negative",
            "name": f"{segment_name}_P4_BOUNDARY_NEG",
            "positiveComponent": boundary_components["negative"],
            "positiveNet": _side_net(boundary_side, "negative", str(context["nets"]["negative"])),
            "positivePin": _side_pin(boundary_side, "negative"),
            "negativeComponent": boundary_reference_component,
            "negativeNet": reference_net,
            "referenceLayer": reference_layer,
        },
    ]


def _diff_port_specs(context: dict[str, Any]) -> list[dict[str, Any]]:
    segment_name = context["segment"].get("name", "SEGMENT")
    source_side = context["ports"]["sourceSide"]
    boundary_positive = next(
        item["refdes"] for item in context["seriesComponents"] if item.get("role") == "positive-boundary"
    )
    boundary_negative = next(
        item["refdes"] for item in context["seriesComponents"] if item.get("role") == "negative-boundary"
    )
    return [
        {
            "role": "source",
            "name": f"{segment_name}_P1_SOURCE_DIFF",
            "positiveComponent": source_side["refdes"],
            "positiveNet": context["nets"]["positive"],
            "negativeComponent": source_side["refdes"],
            "negativeNet": context["nets"]["negative"],
        },
        {
            "role": "boundary",
            "name": f"{segment_name}_P2_BOUNDARY_DIFF",
            "positiveComponent": boundary_positive,
            "positiveNet": context["nets"]["positive"],
            "negativeComponent": boundary_negative,
            "negativeNet": context["nets"]["negative"],
        },
    ]


def apply_diff_s2p_ports(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    from pyedb import Edb

    if context["ports"].get("mode") != "differential":
        raise ValueError("apply_diff_s2p_ports requires ports.mode=differential")

    run_dir = Path(context["workspace"]["runDir"])
    record_path = run_dir / "ports_apply.json"

    target_edb = _prepare_run_edb(context)

    reference_net = context["nets"]["reference"]
    impedance = float(resolve_reference_impedance(context["tdr"]).value_ohm)
    port_specs = _diff_port_specs(context)

    pedb = None
    created_ports: list[dict[str, Any]] = []
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        existing_port_names = set(getattr(getattr(pedb, "excitations", {}), "keys", lambda: [])())
        for spec in port_specs:
            apply_mode = "existing" if spec["name"] in existing_port_names else "created"
            created = pedb.siwave.create_circuit_port_on_net(
                spec["positiveComponent"],
                spec["positiveNet"],
                spec["negativeComponent"],
                spec["negativeNet"],
                impedance,
                spec["name"],
            )
            created_ports.append(
                {
                    "name": str(created),
                    "role": spec["role"],
                    "positiveComponent": spec["positiveComponent"],
                    "positiveNet": spec["positiveNet"],
                    "negativeComponent": spec["negativeComponent"],
                    "negativeNet": spec["negativeNet"],
                    "referenceNet": reference_net,
                    "applyMode": apply_mode,
                }
            )
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    return write_ports_apply_record(
        {
            "status": "ok",
            "mode": "differential",
            "targetEdb": str(target_edb),
            "createdPorts": created_ports,
            "portOrder": [item["name"] for item in created_ports],
        },
        run_dir,
    )


def apply_se4_s4p_ports(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    from pyedb import Edb

    if context["ports"].get("mode") != "single-ended-4port":
        raise ValueError("apply_se4_s4p_ports requires ports.mode=single-ended-4port")

    run_dir = Path(context["workspace"]["runDir"])
    record_path = run_dir / "ports_apply.json"

    target_edb = _prepare_run_edb(context)

    impedance = _tdr_single_ended_impedance(context)
    port_specs = _se4_port_specs(context)

    pedb = None
    created_ports: list[dict[str, Any]] = []
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        existing_port_names = set(getattr(getattr(pedb, "excitations", {}), "keys", lambda: [])())
        requested_port_creation, port_creation_strategy = _se4_port_creation_strategy(context)
        for spec in port_specs:
            apply_mode = "existing" if spec["name"] in existing_port_names else "created"
            if port_creation_strategy == "ansys-pin-to-reference-layer":
                if not spec.get("positivePin"):
                    raise ValueError(f"positive pin is required for {requested_port_creation}: {spec['name']}")
                created = pedb.siwave.create_port_between_pin_and_layer(
                    component_name=spec["positiveComponent"],
                    pins_name=spec["positivePin"],
                    layer_name=spec["referenceLayer"],
                    reference_net=spec["negativeNet"],
                    impedance=impedance,
                )
            elif port_creation_strategy == "low-level-padstack-to-reference-layer":
                if not spec.get("positivePin"):
                    raise ValueError(f"positive pin is required for {requested_port_creation}: {spec['name']}")
                created = _create_padstack_to_reference_layer_port(
                    pedb,
                    component_name=spec["positiveComponent"],
                    pin_name=spec["positivePin"],
                    net_name=spec["positiveNet"],
                    reference_net=spec["negativeNet"],
                    reference_layer=spec["referenceLayer"],
                    impedance=impedance,
                    port_name=spec["name"],
                )
            elif port_creation_strategy == "ansys-pin-to-pin":
                if not spec.get("positivePin") or not spec.get("negativePin"):
                    raise ValueError(
                        "ansys-pin-to-pin requires positivePin and negativePin: "
                        f"{spec['name']}"
                    )
                positive_pin = _edb_component_pin(pedb, spec["positiveComponent"], spec["positivePin"], spec["positiveNet"])
                negative_pin = _edb_component_pin(pedb, spec["negativeComponent"], spec["negativePin"], spec["negativeNet"])
                created = pedb.siwave.create_circuit_port_on_pin(
                    positive_pin,
                    negative_pin,
                    impedance=impedance,
                    port_name=spec["name"],
                )
            elif port_creation_strategy == "ansys-net-to-reference":
                created = pedb.siwave.create_circuit_port_on_net(
                    spec["positiveComponent"],
                    spec["positiveNet"],
                    spec["negativeComponent"],
                    spec["negativeNet"],
                    impedance,
                    spec["name"],
                )
            else:
                raise AssertionError(f"unhandled port creation strategy: {port_creation_strategy}")
            created_name = _created_terminal_name(created, spec["name"])
            created_ports.append(
                {
                    "name": created_name,
                    "role": spec["role"],
                    "positiveComponent": spec["positiveComponent"],
                    "positiveNet": spec["positiveNet"],
                    "positivePin": spec.get("positivePin"),
                    "negativeComponent": spec["negativeComponent"],
                    "negativeNet": spec["negativeNet"],
                    "negativePin": spec.get("negativePin"),
                    "referenceLayer": spec.get("referenceLayer"),
                    "requestedPortCreation": requested_port_creation,
                    "portCreationStrategy": port_creation_strategy,
                    "applyMode": apply_mode,
                }
            )
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    return write_ports_apply_record(
        {
            "status": "ok",
            "mode": "single-ended-4port",
            "targetEdb": str(target_edb),
            "createdPorts": created_ports,
            "portOrder": [item["name"] for item in created_ports],
            "singleEndedImpedanceOhm": impedance,
            "portCreationStrategy": port_creation_strategy,
        },
        run_dir,
    )


def _edb_component_pin(pedb, component_name: str, pin_name: str, net_name: str | None = None):
    pins = pedb.components.get_pin_from_component(component_name, net_name) if net_name else pedb.components.get_pin_from_component(component_name)
    for pin in pins:
        candidate = getattr(pin, "component_pin", None)
        if candidate is None:
            try:
                candidate = pin.GetName()
            except Exception:
                candidate = None
        if str(candidate).strip() == str(pin_name).strip():
            return pin
    raise ValueError(f"pin not found: component={component_name}, pin={pin_name}, net={net_name}")


def _create_padstack_to_reference_layer_port(
    pedb,
    *,
    component_name: str,
    pin_name: str,
    net_name: str,
    reference_net: str,
    reference_layer: str,
    impedance: float,
    port_name: str,
):
    pin = _edb_component_pin(pedb, component_name, pin_name, net_name)
    pin_object = getattr(pin, "_edb_object", None) or pin
    ok, _start_layer, _stop_layer = pin_object.GetLayerRange()
    if not ok:
        raise ValueError(f"failed to get layer range: component={component_name}, pin={pin_name}, net={net_name}")

    api = pedb.siwave._edb
    pin_instance = getattr(pin, "_edb_padstackinstance", None) or pin_object
    positive_terminal = api.cell.terminal.PadstackInstanceTerminal.Create(
        pedb.siwave._active_layout,
        pin_instance.GetNet(),
        port_name,
        pin_instance,
        _start_layer,
    )
    positive_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    positive_terminal.SetImpedance(api.utility.value(impedance))
    positive_terminal.SetIsCircuitPort(False)

    reference_net_object = pedb.nets.get_net_by_name(reference_net)
    if not reference_net_object:
        raise ValueError(f"reference net not found: {reference_net}")
    reference_layer_object = pedb.stackup.signal_layers[reference_layer]._edb_layer
    pos = pedb.components.get_pin_position(pin_instance)
    position = api.geometry.point_data(
        api.utility.value(pos[0]),
        api.utility.value(pos[1]),
    )
    negative_terminal = api.cell.terminal.PointTerminal.Create(
        pedb.siwave._active_layout,
        reference_net_object.net_obj,
        f"{port_name}_ref",
        position,
        reference_layer_object,
    )
    negative_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    negative_terminal.SetImpedance(api.utility.value(impedance))
    negative_terminal.SetIsCircuitPort(False)
    if not positive_terminal.SetReferenceTerminal(negative_terminal):
        raise RuntimeError(f"failed to set reference terminal: {port_name}")
    return positive_terminal.GetName()


def _resolve_config_relative_path(raw_path: str, label: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    ensure_exists(path, label)
    return path


def _prepare_run_edb(context: dict[str, Any]) -> Path:
    """Reset the run workspace, copy the pristine reference AEDB, and install series models.

    Series electrical models (part library + seriesTreatment policy) are applied to the
    run copy only — the reference AEDB stays untouched so treatment can differ per
    analysis item and reruns always start clean.
    """
    import shutil

    ref_edb = Path(context["reference"]["aedb"])
    run_dir = Path(context["workspace"]["runDir"])
    target_edb = run_dir / ref_edb.name

    _reset_run_workspace(context)
    if not target_edb.exists():
        shutil.copytree(ref_edb, target_edb)
    apply_series_models_step(context, target_edb)
    return target_edb


def apply_series_models_step(context: dict[str, Any], target_edb: Path) -> Path:
    """Install series electrical models on the run AEDB before port creation.

    Configured by seriesModels (partLibrary + channelPathReport) and seriesTreatment
    in the run config; skipped with a record when seriesModels is absent.
    """
    import sys

    run_dir = Path(context["workspace"]["runDir"])
    record_path = run_dir / "series_models_apply.json"

    def write_record(payload: dict[str, Any]) -> Path:
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        return record_path

    series_config = context.get("seriesModels") or {}
    if not series_config:
        write_record({"status": "skipped", "reason": "seriesModels not configured"})
        return record_path

    part_library_path = _resolve_config_relative_path(
        str(series_config.get("partLibrary") or ""), "seriesModels.partLibrary"
    )
    report_path = _resolve_config_relative_path(
        str(series_config.get("channelPathReport") or ""), "seriesModels.channelPathReport"
    )

    repo_root = ROOT_DIR.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from SI_TDR.channel.series_models import apply_series_models, load_part_library
    except ModuleNotFoundError:  # Support direct execution from the SI_TDR folder.
        from channel.series_models import apply_series_models, load_part_library

    ensure_embedded_site_packages()
    from pyedb import Edb

    part_library = load_part_library(part_library_path)
    with report_path.open("r", encoding="utf-8") as fp:
        report_payload = json.load(fp)

    pedb = None
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        result = apply_series_models(
            pedb,
            report_payload=report_payload,
            part_library=part_library,
            treatment_config=context.get("seriesTreatment") or {},
        )
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    status = "unresolved-channels" if result["unresolved"] else "ok"
    record = {
        "status": status,
        "targetEdb": str(target_edb),
        "partLibrary": str(part_library_path),
        "channelPathReport": str(report_path),
        "treatmentConfig": context.get("seriesTreatment") or {},
        **result,
    }
    write_record(record)
    print(
        f"Series models: installed={len(result['installed'])} "
        f"inherited={len(result['inherited'])} unresolved={len(result['unresolved'])}"
    )
    if result["unresolved"]:
        affected = ", ".join(
            f"{item['channel']}/{item['polarity']}" for item in result["skippedChannels"]
        )
        raise RuntimeError(
            "unresolved series components on channel paths; SIWave analysis was not started. "
            f"See {record_path}. Affected channels: {affected}"
        )
    return record_path


def apply_ports(context: dict[str, Any]) -> Path:
    mode = context["ports"].get("mode")
    if mode == "existing-snp":
        return apply_existing_snp_ports(context)
    if mode == "metadata-snp":
        return apply_metadata_snp_ports(context)
    if mode == "differential":
        return apply_diff_s2p_ports(context)
    if mode == "single-ended-4port":
        return apply_se4_s4p_ports(context)
    raise ValueError(f"unsupported ports.mode: {mode}")


def apply_existing_snp_ports(context: dict[str, Any]) -> Path:
    """Use an AEDB that already contains the target SIWave ports.

    This is the Golden Sample baseline path. It verifies the SYZ/export
    pipeline before the ref.aedb port recreation logic is complete.
    """
    ref_edb = Path(context["reference"]["aedb"])
    run_dir = Path(context["workspace"]["runDir"])
    target_edb = _prepare_run_edb(context)

    repaired_ports: list[dict[str, Any]] = []
    if bool(context.get("ports", {}).get("repairMissingReferences")):
        repaired_ports = _repair_missing_existing_port_references(context, target_edb)

    expected_ports = context.get("ports", {}).get("portOrder") or []
    record = {
        "status": "ok",
        "mode": "existing-snp",
        "targetEdb": str(target_edb),
        "sourceEdb": str(ref_edb),
        "portCount": int(context.get("ports", {}).get("touchstonePortCount") or len(expected_ports)),
        "portOrder": expected_ports,
        "repairedReferences": repaired_ports,
        "message": "Existing AEDB ports copied as the Golden Sample baseline.",
    }
    return write_ports_apply_record(record, run_dir)


def _resolve_metadata_path(context: dict[str, Any]) -> Path:
    metadata_path = context.get("ports", {}).get("metadataPath")
    if not metadata_path:
        raise ValueError("ports.metadataPath is required for ports.mode=metadata-snp")
    path = Path(metadata_path)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    ensure_exists(path, "ports.metadataPath")
    return path


def _metadata_ports(context: dict[str, Any]) -> list[dict[str, Any]]:
    metadata_path = _resolve_metadata_path(context)
    with metadata_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    ports = payload.get("ports") or []
    if not ports:
        raise ValueError(f"metadata does not contain ports: {metadata_path}")
    return ports


def _metadata_port_order(context: dict[str, Any], ports: list[dict[str, Any]]) -> list[str]:
    configured = context.get("ports", {}).get("portOrder") or []
    if configured:
        return [str(item) for item in configured]
    return [str(item["name"]) for item in sorted(ports, key=lambda item: int(item.get("index") or 0))]


def _value_from_point(value: str | None) -> str:
    if value is None:
        raise ValueError("point coordinate is missing")
    return str(value)


def _metadata_point(api, point_record: dict[str, Any]):
    return api.geometry.point_data(
        api.utility.value(_value_from_point(point_record.get("x"))),
        api.utility.value(_value_from_point(point_record.get("y"))),
    )


def _metadata_layer(pedb, layer_name: str):
    try:
        return pedb.stackup.signal_layers[layer_name]._edb_layer
    except KeyError as exc:
        raise ValueError(f"layer not found: {layer_name}") from exc


def _edb_layer_name(layer: object) -> str:
    for attribute in ("GetName", "name"):
        value = getattr(layer, attribute, None)
        if value is None:
            continue
        resolved = value() if callable(value) else value
        if resolved:
            return str(resolved)
    raise ValueError("EDB layer does not expose a name")


def _resolve_padstack_positive_layer(
    pedb,
    pin: object,
    *,
    configured_layer_name: str | None = None,
) -> dict[str, Any]:
    """Resolve a pin terminal layer, retaining explicit metadata as a legacy override."""

    if configured_layer_name:
        return {
            "layer": _metadata_layer(pedb, configured_layer_name),
            "name": configured_layer_name,
            "source": "metadata_explicit_override",
            "pinLayerRange": None,
        }

    pin_object = getattr(pin, "_edb_object", None) or pin
    result = pin_object.GetLayerRange()
    try:
        ok, start_layer, stop_layer = result
    except (TypeError, ValueError) as exc:
        raise ValueError("pin GetLayerRange returned an invalid result") from exc
    if not ok:
        raise ValueError("failed to resolve pin layer range")
    start_name = _edb_layer_name(start_layer)
    stop_name = _edb_layer_name(stop_layer)
    return {
        "layer": start_layer,
        "name": start_name,
        "source": "pin_layer_range_start",
        "pinLayerRange": {
            "start": start_name,
            "stop": stop_name,
        },
    }


def _metadata_net(pedb, net_name: str):
    net_object = pedb.nets.get_net_by_name(net_name)
    if not net_object:
        raise ValueError(f"net not found: {net_name}")
    return net_object.net_obj


def _metadata_reference_spec(context: dict[str, Any], port_record: dict[str, Any], fallback_point: dict[str, str] | None = None) -> dict[str, Any]:
    configured_reference_net = str(
        context.get("ports", {}).get("referenceNet") or context.get("nets", {}).get("reference") or "GND"
    )
    configured_reference_layer = str(context.get("ports", {}).get("referenceLayer") or "Layer2")
    reference = port_record.get("reference") or {}
    reference_params = reference.get("parameters") or {}
    reference_point = (reference_params.get("point") or {}) if reference_params else {}
    return {
        "name": str(reference.get("name") or f"{port_record['name']}_ref"),
        "net": str(reference.get("net") or configured_reference_net),
        "layer": str(reference_params.get("layer") or configured_reference_layer),
        "point": reference_point or fallback_point,
    }


def _create_metadata_padstack_port(
    pedb,
    *,
    context: dict[str, Any],
    port_record: dict[str, Any],
    impedance: float,
) -> dict[str, Any]:
    positive = port_record["positive"]
    positive_params = positive.get("parameters") or {}
    padstack = positive_params.get("padstack") or {}
    component_name = str(padstack.get("component") or "")
    pin_name = str(padstack.get("pin") or "")
    net_name = str(positive.get("net") or padstack.get("net") or "")
    configured_positive_layer = positive_params.get("layer") or context.get("ports", {}).get("positiveLayer")
    if not component_name or not pin_name or not net_name:
        raise ValueError(f"invalid padstack metadata for port: {port_record.get('name')}")

    pin = _edb_component_pin(pedb, component_name, pin_name, net_name)
    pin_instance = getattr(pin, "_edb_padstackinstance", None) or getattr(pin, "_edb_object", None) or pin
    api = pedb.siwave._edb
    layer_resolution = _resolve_padstack_positive_layer(
        pedb,
        pin,
        configured_layer_name=(
            str(configured_positive_layer) if configured_positive_layer else None
        ),
    )
    positive_layer = layer_resolution["layer"]
    positive_layer_name = str(layer_resolution["name"])
    positive_terminal = api.cell.terminal.PadstackInstanceTerminal.Create(
        pedb.siwave._active_layout,
        pin_instance.GetNet(),
        str(port_record["name"]),
        pin_instance,
        positive_layer,
    )
    positive_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    positive_terminal.SetImpedance(api.utility.value(impedance))
    positive_terminal.SetIsCircuitPort(False)

    pin_pos = pedb.components.get_pin_position(pin_instance)
    fallback_point = {"x": str(pin_pos[0]), "y": str(pin_pos[1])}
    reference_spec = _metadata_reference_spec(context, port_record, fallback_point=fallback_point)
    reference_terminal = api.cell.terminal.PointTerminal.Create(
        pedb.siwave._active_layout,
        _metadata_net(pedb, reference_spec["net"]),
        reference_spec["name"],
        _metadata_point(api, reference_spec["point"]),
        _metadata_layer(pedb, reference_spec["layer"]),
    )
    reference_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    reference_terminal.SetImpedance(api.utility.value(impedance))
    reference_terminal.SetIsCircuitPort(False)
    if not positive_terminal.SetReferenceTerminal(reference_terminal):
        raise RuntimeError(f"failed to set reference terminal: {port_record['name']}")

    return {
        "name": positive_terminal.GetName(),
        "terminalType": "PadstackInstanceTerminal",
        "component": component_name,
        "pin": pin_name,
        "net": net_name,
        "layer": positive_layer_name,
        "layerSource": layer_resolution["source"],
        "pinLayerRange": layer_resolution["pinLayerRange"],
        "referenceNet": reference_spec["net"],
        "referenceLayer": reference_spec["layer"],
        "referencePoint": reference_spec["point"],
    }


def _create_metadata_point_port(
    pedb,
    *,
    context: dict[str, Any],
    port_record: dict[str, Any],
    impedance: float,
) -> dict[str, Any]:
    positive = port_record["positive"]
    positive_params = positive.get("parameters") or {}
    positive_point = (positive_params.get("point") or {}) if positive_params else {}
    positive_layer_name = str(positive_params.get("layer") or context.get("ports", {}).get("positiveLayer") or "Layer1")
    net_name = str(positive.get("net") or "")
    if not positive_point or not net_name:
        raise ValueError(f"invalid point metadata for port: {port_record.get('name')}")

    api = pedb.siwave._edb
    positive_terminal = api.cell.terminal.PointTerminal.Create(
        pedb.siwave._active_layout,
        _metadata_net(pedb, net_name),
        str(port_record["name"]),
        _metadata_point(api, positive_point),
        _metadata_layer(pedb, positive_layer_name),
    )
    positive_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    positive_terminal.SetImpedance(api.utility.value(impedance))
    positive_terminal.SetIsCircuitPort(False)

    reference_spec = _metadata_reference_spec(context, port_record, fallback_point=positive_point)
    reference_terminal = api.cell.terminal.PointTerminal.Create(
        pedb.siwave._active_layout,
        _metadata_net(pedb, reference_spec["net"]),
        reference_spec["name"],
        _metadata_point(api, reference_spec["point"]),
        _metadata_layer(pedb, reference_spec["layer"]),
    )
    reference_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
    reference_terminal.SetImpedance(api.utility.value(impedance))
    reference_terminal.SetIsCircuitPort(False)
    if not positive_terminal.SetReferenceTerminal(reference_terminal):
        raise RuntimeError(f"failed to set reference terminal: {port_record['name']}")

    return {
        "name": positive_terminal.GetName(),
        "terminalType": "PointTerminal",
        "net": net_name,
        "layer": positive_layer_name,
        "point": positive_point,
        "referenceNet": reference_spec["net"],
        "referenceLayer": reference_spec["layer"],
        "referencePoint": reference_spec["point"],
    }


def apply_metadata_snp_ports(context: dict[str, Any]) -> Path:
    """Recreate an N-port AEDB from exported Golden port metadata."""
    ensure_embedded_site_packages()
    from pyedb import Edb

    ref_edb = Path(context["reference"]["aedb"])
    run_dir = Path(context["workspace"]["runDir"])
    metadata_path = _resolve_metadata_path(context)
    port_records = _metadata_ports(context)
    port_order = _metadata_port_order(context, port_records)
    records_by_name = {str(item["name"]): item for item in port_records}
    impedance = float(context.get("ports", {}).get("singleEndedImpedanceOhm") or 50.0)

    target_edb = _prepare_run_edb(context)

    pedb = None
    created_ports: list[dict[str, Any]] = []
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        if bool(context.get("ports", {}).get("clearExistingPorts", True)):
            for excitation in list((getattr(pedb, "excitations", {}) or {}).values()):
                delete = getattr(excitation, "delete", None)
                if callable(delete):
                    delete()
        existing_port_names = set(getattr(getattr(pedb, "excitations", {}), "keys", lambda: [])())
        for port_name in port_order:
            if port_name not in records_by_name:
                raise ValueError(f"port is not present in metadata: {port_name}")
            port_record = records_by_name[port_name]
            if port_name in existing_port_names:
                created_ports.append({"name": port_name, "applyMode": "existing"})
                continue

            terminal_type = str((port_record.get("positive") or {}).get("terminalType") or "")
            if "PadstackInstanceTerminal" in terminal_type:
                created = _create_metadata_padstack_port(
                    pedb,
                    context=context,
                    port_record=port_record,
                    impedance=impedance,
                )
            elif "PointTerminal" in terminal_type:
                created = _create_metadata_point_port(
                    pedb,
                    context=context,
                    port_record=port_record,
                    impedance=impedance,
                )
            else:
                raise ValueError(f"unsupported terminal type for {port_name}: {terminal_type}")
            created["index"] = int(port_record.get("index") or len(created_ports) + 1)
            created["applyMode"] = "created"
            created_ports.append(created)
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    return write_ports_apply_record(
        {
            "status": "ok",
            "mode": "metadata-snp",
            "targetEdb": str(target_edb),
            "sourceEdb": str(ref_edb),
            "metadataPath": str(metadata_path),
            "portCount": int(context.get("ports", {}).get("touchstonePortCount") or len(port_order)),
            "createdPorts": created_ports,
            "portOrder": [item["name"] for item in created_ports],
            "singleEndedImpedanceOhm": impedance,
        },
        run_dir,
    )


def _repair_missing_existing_port_references(context: dict[str, Any], target_edb: Path) -> list[dict[str, Any]]:
    ensure_embedded_site_packages()
    from pyedb import Edb

    reference_net = str(context.get("ports", {}).get("referenceNet") or context.get("nets", {}).get("reference") or "GND")
    reference_layer = str(context.get("ports", {}).get("referenceLayer") or "Layer2")
    port_order = [str(item) for item in context.get("ports", {}).get("portOrder") or []]
    impedance = float(context.get("ports", {}).get("singleEndedImpedanceOhm") or 50.0)

    pedb = None
    repaired: list[dict[str, Any]] = []
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        api = pedb.siwave._edb
        reference_net_object = pedb.nets.get_net_by_name(reference_net)
        if not reference_net_object:
            raise ValueError(f"reference net not found: {reference_net}")
        reference_layer_object = pedb.stackup.signal_layers[reference_layer]._edb_layer
        excitations = getattr(pedb, "excitations", {}) or {}

        for port_name in port_order:
            term = excitations.get(port_name)
            if term is None:
                repaired.append({"name": port_name, "status": "missing"})
                continue
            raw = getattr(term, "_edb_object", None)
            if raw is None:
                repaired.append({"name": port_name, "status": "no-raw-terminal"})
                continue

            existing_ref = getattr(term, "ref_terminal", None) or getattr(term, "reference_terminal", None)
            if existing_ref is not None:
                continue

            terminal_type = str(getattr(term, "terminal_type", ""))
            if "PadstackInstanceTerminal" not in terminal_type:
                repaired.append({"name": port_name, "status": "skipped", "terminalType": terminal_type})
                continue

            params = raw.GetParameters()
            pin_instance = params[1]
            try:
                pos = pedb.components.get_pin_position(pin_instance)
            except Exception:
                # Fallback to the padstack center if the wrapper API cannot resolve it.
                center = pin_instance.GetPositionAndRotation()[0]
                pos = [str(center.X), str(center.Y)]
            position = api.geometry.point_data(
                api.utility.value(pos[0]),
                api.utility.value(pos[1]),
            )
            negative_terminal = api.cell.terminal.PointTerminal.Create(
                pedb.siwave._active_layout,
                reference_net_object.net_obj,
                f"{port_name}_ref",
                position,
                reference_layer_object,
            )
            negative_terminal.SetBoundaryType(api.cell.terminal.BoundaryType.PortBoundary)
            negative_terminal.SetImpedance(api.utility.value(impedance))
            negative_terminal.SetIsCircuitPort(False)
            if not raw.SetReferenceTerminal(negative_terminal):
                raise RuntimeError(f"failed to set reference terminal: {port_name}")
            repaired.append(
                {
                    "name": port_name,
                    "status": "reference-created",
                    "referenceNet": reference_net,
                    "referenceLayer": reference_layer,
                    "position": [str(pos[0]), str(pos[1])],
                }
            )
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass
    return repaired


def write_ports_apply_record(payload: dict[str, Any], run_dir: Path) -> Path:
    record_path = run_dir / "ports_apply.json"
    with record_path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=2)
    return record_path


def _tdr_frequency_sweep(interface: str) -> dict[str, Any]:
    normalized = interface.lower()
    if normalized.startswith("usb"):
        return {
            "start_freq_hz": 1,
            "stop_freq_hz": 20_000_000_000,
            "step_freq_hz": 100,
            "decade_count": 100,
            "sweeptype": 2,
            "distribution": "decade_count",
            "discrete_sweep": True,
        }
    if normalized.startswith("hdmi"):
        return {
            "start_freq_hz": 1,
            "stop_freq_hz": 20_000_000_000,
            "step_freq_hz": 100,
            "decade_count": 100,
            "sweeptype": 2,
            "distribution": "decade_count",
            "discrete_sweep": True,
        }
    return {
        "start_freq_hz": 1,
        "stop_freq_hz": 20_000_000_000,
        "step_freq_hz": 100,
        "decade_count": 100,
        "sweeptype": 2,
        "distribution": "decade_count",
        "discrete_sweep": True,
    }


def _context_frequency_sweep(context: dict[str, Any]) -> dict[str, Any]:
    configured = (context.get("syz") or {}).get("frequencySweep")
    if configured:
        return configured
    interface_name = context["segment"].get("interface", "")
    return _tdr_frequency_sweep(interface_name)


def _format_frequency_range(pedb, row: list[Any]) -> list[Any]:
    if len(row) != 4:
        raise ValueError(f"frequency range must have 4 items: {row!r}")
    mode, start, stop, count_or_step = row
    if isinstance(start, (int, float)):
        start = pedb.number_with_units(start, "Hz")
    if isinstance(stop, (int, float)):
        stop = pedb.number_with_units(stop, "Hz")
    if isinstance(count_or_step, (int, float)) and str(mode).casefold() == "linear scale":
        count_or_step = pedb.number_with_units(count_or_step, "Hz")
    return [str(mode), start, stop, count_or_step]


def _add_siwave_syz_setup(pedb, setup_name: str, sweep: dict[str, Any]):
    if "ranges" not in sweep:
        return pedb.siwave.add_siwave_syz_analysis(
            name=setup_name,
            sweeptype=int(sweep["sweeptype"]),
            start_freq=sweep["start_freq_hz"],
            stop_freq=sweep["stop_freq_hz"],
            decade_count=int(sweep["decade_count"]),
            step_freq=sweep["step_freq_hz"],
            discrete_sweep=bool(sweep["discrete_sweep"]),
        )

    setup = pedb.create_siwave_syz_setup(name=setup_name)
    if not setup:
        raise ValueError(f"SIwave SYZ setup already exists: {setup_name}")
    if "accuracy_level" in sweep:
        setup.si_slider_position = int(sweep["accuracy_level"])
    frequency_ranges = [_format_frequency_range(pedb, row) for row in sweep["ranges"]]
    added_sweep = setup.add_frequency_sweep(
        name=sweep.get("name"),
        frequency_sweep=frequency_ranges,
    )
    if bool(sweep.get("discrete_sweep", True)):
        added_sweep.freq_sweep_type = "kDiscreteSweep"
    return setup


def setup_syz(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    from pyedb import Edb

    run_dir = Path(context["workspace"]["runDir"])
    target_edb = run_dir / Path(context["reference"]["aedb"]).name
    ensure_exists(target_edb, "targetEdb")
    record_path = run_dir / "syz_setup.json"

    if record_path.exists():
        return record_path

    segment_name = context["segment"].get("name", "SEGMENT")
    setup_name = f"{segment_name}_SYZ_SETUP"
    sweep = _context_frequency_sweep(context)

    pedb = None
    apply_mode = "created"
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        try:
            try:
                setup = _add_siwave_syz_setup(pedb, setup_name, sweep)
            except TypeError:
                if "ranges" in sweep:
                    raise
                setup = pedb.siwave.add_siwave_syz_analysis(
                    distribution=str(sweep["distribution"]),
                    start_freq=sweep["start_freq_hz"],
                    stop_freq=sweep["stop_freq_hz"],
                    step_freq=sweep["step_freq_hz"],
                    discrete_sweep=bool(sweep["discrete_sweep"]),
                )
            resolved_name = str(getattr(setup, "name", "") or setup_name)
        except Exception as exc:
            if "exist" in str(exc).casefold():
                apply_mode = "existing"
                resolved_name = setup_name
            else:
                raise
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    record = {
        "status": "ok",
        "targetEdb": str(target_edb),
        "setup": {
            "name": resolved_name,
            "type": "siwave-syz",
            "applyMode": apply_mode,
            "frequencySweep": sweep,
        },
    }
    with record_path.open("w", encoding="utf-8") as fp:
        json.dump(record, fp, indent=2)
    return record_path


def solve_touchstone(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    from pyedb import Edb
    from pyedb.generic.process import SiwaveSolve

    run_dir = Path(context["workspace"]["runDir"])
    target_edb = run_dir / Path(context["reference"]["aedb"]).name
    ensure_exists(target_edb, "targetEdb")

    segment_name = context["segment"].get("name", "SEGMENT")
    setup_name = f"{segment_name}_SYZ_SETUP"
    touchstone_dir = Path(context["workspace"]["touchstoneDir"])
    touchstone_dir.mkdir(parents=True, exist_ok=True)
    touchstone_target = _touchstone_path(context)
    record_path = run_dir / "channel_solve.json"

    if touchstone_target.exists():
        record = {
            "status": "ok",
            "targetEdb": str(target_edb),
            "setupName": setup_name,
            "execFile": None,
            "requestedTouchstone": str(touchstone_target),
            "exportedTouchstoneFiles": [str(touchstone_target)],
            "message": "existing Touchstone reused; solve step skipped",
        }
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(record, fp, indent=2)
        return record_path

    pedb = None
    exec_file = None
    try:
        pedb = Edb(edbpath=str(target_edb), edbversion=_context_aedt_version(context))
        exec_file = pedb.siwave.create_exec_file(
            add_syz=True,
            export_touchstone=True,
            touchstone_file_path=str(touchstone_target),
        )
        pedb.save()
    finally:
        if pedb is not None:
            try:
                pedb.close()
            except Exception:
                pass

    solver = SiwaveSolve(
        aedb_path=str(target_edb),
        aedt_version=_context_aedt_version(context),
    )
    solver.solve()

    exported = _discover_touchstone_files(touchstone_dir)
    record = {
        "status": "ok" if exported else "warning",
        "targetEdb": str(target_edb),
        "setupName": setup_name,
        "execFile": str(exec_file) if exec_file else None,
        "requestedTouchstone": str(touchstone_target),
        "exportedTouchstoneFiles": [str(path) for path in exported],
    }
    with record_path.open("w", encoding="utf-8") as fp:
        json.dump(record, fp, indent=2)
    return record_path


def _discover_touchstone_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower().startswith(".s") and path.suffix.lower().endswith("p")
        ]
    )


def run_tdr(context: dict[str, Any]) -> Path:
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")
    _validate_touchstone_port_contract(context, touchstone_path)

    if _is_manual_snp_multi_diff_tdr(context):
        return run_manual_snp_multi_diff_tdr(context)
    if _is_manual_s4p_diff_strategy(context):
        return run_manual_s4p_diff_tdr(context)

    ensure_embedded_site_packages()
    from ansys.aedt.core import Circuit

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")

    project_path = circuit_dir / f"{segment_name}_TDR.aedt"
    record_path = run_dir / "tdr_transient.json"
    schematic_image_path = circuit_dir / f"{segment_name}_schematic.jpg"

    port_names = _touchstone_port_names(touchstone_path)
    tx_probe_pins, tx_reference_pins, termination_pins = _tdr_probe_pin_config(context, port_names)
    differential_mode = context["tdr"].get("mode") == "differential"

    app = None
    try:
        app = Circuit(
            project=str(project_path),
            version=_context_aedt_version(context),
            non_graphical=True,
            new_desktop=True,
            close_on_exit=True,
        )

        schematic_result = app.create_tdr_schematic_from_snp(
            input_file=str(touchstone_path),
            tx_schematic_pins=tx_probe_pins,
            tx_schematic_differential_pins=tx_reference_pins,
            termination_pins=termination_pins,
            differential=differential_mode,
            rise_time=_tdr_rise_time_ps(context),
            use_convolution=True,
            analyze=False,
            design_name=f"{segment_name}_Transient",
            impedance=_tdr_single_ended_impedance(context),
        )
        created, trace_names = _normalize_tdr_schematic_result(schematic_result)
        if not created:
            raise RuntimeError("PyAEDT did not create a TDR schematic")

        saved = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved:
            raise RuntimeError(f"PyAEDT could not save Circuit project to {project_path}")

        schematic_image_saved = False
        try:
            preview_ok = bool(app.export_design_preview_to_jpg(str(schematic_image_path)))
            schematic_image_saved = bool(
                preview_ok and schematic_image_path.exists() and schematic_image_path.stat().st_size > 0
            )
            if not schematic_image_saved and schematic_image_path.exists():
                try:
                    schematic_image_path.unlink()
                except Exception:
                    pass
        except Exception:
            schematic_image_saved = False

        setup_name = _resolve_tdr_setup_name(app)
        analysis_ok = app.analyze_setup(setup_name, blocking=True)
        if not analysis_ok:
            raise RuntimeError(f"PyAEDT did not report a successful {setup_name} solve")

        saved_after_analyze = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved_after_analyze:
            raise RuntimeError(f"PyAEDT could not save analyzed Circuit project to {project_path}")

        trace_name = trace_names[0] if trace_names else None
        if not trace_name:
            raise RuntimeError("PyAEDT did not return a TDR trace expression")

        report_name, endpoint_notes, native_target_range = _create_tdr_report(
            app,
            context,
            trace_name=trace_name,
            setup_name=setup_name,
        )
        report_image_path = _export_tdr_report_image(app, circuit_dir, report_name)
        app.save_project(file_name=str(project_path.resolve()), overwrite=True)

        solution_data = app.post.get_solution_data(
            expressions=trace_name,
            setup_sweep_name=setup_name,
            domain="Time",
        )
        if solution_data is None:
            raise RuntimeError("PyAEDT did not return TDR solution data")

        time_values_ps = _normalize_time_values(
            solution_data.primary_sweep_values,
            unit=(solution_data.units_sweeps or {}).get("Time"),
        )
        trace_values = _extract_solution_trace_values(solution_data, trace_name)
        sample_count = min(len(time_values_ps), len(trace_values))

        record = {
            "status": "ok",
            "touchstonePath": str(touchstone_path),
            "projectPath": str(project_path),
            "projectName": app.project_name,
            "designName": app.design_name,
            "setupName": setup_name,
            "traceNames": [str(item) for item in trace_names],
            "reportName": report_name,
            "reportImagePath": report_image_path,
            "endpointNotes": endpoint_notes,
            "nativeTargetRange": native_target_range,
            "sampleCount": sample_count,
            "timeUnit": "ps",
            "traceUnit": (solution_data.units_data or {}).get(trace_name, "ohm"),
            "projectSaved": True,
            "schematicImagePath": str(schematic_image_path) if schematic_image_saved and schematic_image_path.exists() else None,
            "samples": [
                {
                    "index": index,
                    "time_ps": round(float(time_values_ps[index]), 6),
                    "impedance_ohm": round(float(trace_values[index]), 6),
                }
                for index in range(sample_count)
            ],
        }
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(record, fp, indent=2)
        return record_path
    finally:
        if app is not None:
            try:
                app.release_desktop(close_projects=True, close_desktop=True)
            except Exception:
                pass


def run_manual_s4p_diff_tdr(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    import shutil

    from ansys.aedt.core import Circuit

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")

    project_path = circuit_dir / f"{segment_name}_TDR.aedt"
    record_path = run_dir / "tdr_transient.json"

    # Rebuild the manual Circuit project from a clean slate so stale designs or
    # page ports from previous attempts do not get merged into the new topology.
    for path in [
        project_path,
        circuit_dir / f"{segment_name}_TDR.aedb",
        circuit_dir / f"{segment_name}_TDR.aedtresults",
        circuit_dir / f"{segment_name}_TDR.pyaedt",
        circuit_dir / f"{segment_name}_TDR.aedt.lock",
    ]:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()

    port_names = _touchstone_port_names(touchstone_path)
    expected_port_names = [spec["name"] for spec in _se4_port_specs(context)]
    if len(port_names) < 4:
        port_names = expected_port_names

    app = None
    try:
        app = Circuit(
            project=str(project_path),
            version=_context_aedt_version(context),
            non_graphical=True,
            new_desktop=True,
            close_on_exit=True,
        )

        design_name = f"{segment_name}_Transient_Manual"
        app.insert_design(design_name)
        app.modeler.schematic.schematic_units = "meter"

        sub = app.modeler.components.create_touchstone_component(
            str(touchstone_path),
            location=[0.0, 0.0],
            show_bitmap=False,
        )
        touchstone_model_refreshed = _refresh_touchstone_model(sub)
        sub_pins = {pin.name: pin for pin in sub.pins}

        tdr_probe = app.modeler.schematic.create_component(
            component_library="Probes",
            component_name="TDR_Differential_Ended",
            location=[0.0, -0.0508],
            angle=270,
        )
        tdr_probe.parameters["Z0"] = 2 * _tdr_single_ended_impedance(context)
        tdr_probe.parameters["Pulse_repetition"] = _tdr_pulse_repetition(context)
        tdr_probe.parameters["Rise_time"] = f"{_tdr_rise_time_ps(context):g}ps"
        pulse_width = _tdr_pulse_width(context)
        if pulse_width is not None:
            tdr_probe.parameters["Pulse_width"] = pulse_width
        time_delay = _tdr_time_delay(context)
        if time_delay is not None:
            tdr_probe.parameters["Time_delay"] = time_delay

        positive_name = port_names[0]
        negative_name = port_names[1]
        boundary_positive_name = port_names[2]
        boundary_negative_name = port_names[3]

        _connect_with_named_page_ports(
            app,
            tdr_probe.pins[0],
            sub_pins[positive_name],
            positive_name,
            first_move=(0, 100),
            second_move=(-1000, 0),
        )
        _connect_with_named_page_ports(
            app,
            tdr_probe.pins[1],
            sub_pins[negative_name],
            negative_name,
            first_move=(0, -100),
            second_move=(-1000, 0),
        )

        far_pos = _place_far_end_termination(
            app,
            sub_pins[boundary_positive_name],
            name="R_FAR_POS",
            location=[0.0889, 0.0],
            route_x=0.07366,
        )
        far_neg = _place_far_end_termination(
            app,
            sub_pins[boundary_negative_name],
            name="R_FAR_NEG",
            location=[0.0889, -0.00254],
            route_x=0.07366,
        )
        far_end_ground = _place_shared_far_end_ground(
            app,
            [far_pos["groundPin"], far_neg["groundPin"]],
            trunk_x=0.10922,
            ground_y=0.0,
        )
        far_end_diff_bridge = None
        diff_bridge_ohm = _far_end_differential_bridge_ohm(context)
        if diff_bridge_ohm is not None:
            far_end_diff_bridge = _place_far_end_diff_bridge(
                app,
                far_pos["signalPin"],
                far_neg["signalPin"],
                value_ohm=diff_bridge_ohm,
                location=[0.08128, -0.00127],
                route_x=0.08128,
            )

        setup = app.create_setup(name="Transient_TDR", setup_type=app.SETUPS.NexximTransient)
        setup.props["TransientData"] = _tdr_transient_data(context)
        if _tdr_use_ts_convolution(context):
            app.oanalysis.AddAnalysisOptions(
                [
                    "NAME:DataBlock",
                    "DataBlockID:=",
                    8,
                    "Name:=",
                    "Nexxim Options",
                    [
                        "NAME:ModifiedOptions",
                        "ts_convolution:=",
                        True,
                    ],
                ]
            )
            setup.props["OptionName"] = "Nexxim Options"

        saved = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved:
            raise RuntimeError(f"PyAEDT could not save Circuit project to {project_path}")

        analysis_ok = app.analyze_setup("Transient_TDR", blocking=True)
        if not analysis_ok:
            raise RuntimeError("PyAEDT did not report a successful Transient_TDR solve")

        trace_name = f"O(A{tdr_probe.id}:zdiff)"
        report_name, endpoint_notes, native_target_range = _create_tdr_report(
            app,
            context,
            trace_name=trace_name,
            setup_name="Transient_TDR",
        )
        report_image_path = _export_tdr_report_image(app, circuit_dir, report_name)
        touchstone_model_refreshed = _refresh_touchstone_model(sub) or touchstone_model_refreshed
        saved_after_analyze = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved_after_analyze:
            raise RuntimeError(f"PyAEDT could not save analyzed Circuit project to {project_path}")

        solution_data = app.post.get_solution_data(
            expressions=trace_name,
            setup_sweep_name="Transient_TDR",
            domain="Time",
        )
        if solution_data is None:
            raise RuntimeError("PyAEDT did not return TDR solution data")

        time_values_ps = _normalize_time_values(
            solution_data.primary_sweep_values,
            unit=(solution_data.units_sweeps or {}).get("Time"),
        )
        trace_values = _extract_solution_trace_values(solution_data, trace_name)
        sample_count = min(len(time_values_ps), len(trace_values))

        record = {
            "status": "ok",
            "buildMode": "manual-s4p-diff",
            "touchstonePath": str(touchstone_path),
            "projectPath": str(project_path),
            "projectName": app.project_name,
            "designName": app.design_name,
            "setupName": "Transient_TDR",
            "traceNames": [trace_name],
            "reportName": report_name,
            "reportImagePath": report_image_path,
            "endpointNotes": endpoint_notes,
            "nativeTargetRange": native_target_range,
            "sampleCount": sample_count,
            "timeUnit": "ps",
            "traceUnit": (solution_data.units_data or {}).get(trace_name, "ohm"),
            "projectSaved": True,
            "touchstoneModelRefreshed": touchstone_model_refreshed,
            "manualTopology": {
                "touchstoneComponent": sub.name,
                "tdrProbeComponent": tdr_probe.name,
                "farEndResistors": [far_pos["resistor"], far_neg["resistor"]],
                "farEndDiffBridge": far_end_diff_bridge,
                "farEndGrounds": [far_end_ground],
                "nearEndPorts": [positive_name, negative_name],
                "farEndPorts": [boundary_positive_name, boundary_negative_name],
            },
            "schematicImagePath": None,
            "samples": [
                {
                    "index": index,
                    "time_ps": round(float(time_values_ps[index]), 6),
                    "impedance_ohm": round(float(trace_values[index]), 6),
                }
                for index in range(sample_count)
            ],
        }
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(record, fp, indent=2)
        return record_path
    finally:
        if app is not None:
            try:
                app.release_desktop(close_projects=True, close_desktop=True)
            except Exception:
                pass


def _cleanup_circuit_project(circuit_dir: Path, segment_name: str) -> Path:
    import shutil

    project_path = circuit_dir / f"{segment_name}_TDR.aedt"
    for path in [
        project_path,
        circuit_dir / f"{segment_name}_TDR.aedb",
        circuit_dir / f"{segment_name}_TDR.aedtresults",
        circuit_dir / f"{segment_name}_TDR.pyaedt",
        circuit_dir / f"{segment_name}_TDR.aedt.lock",
    ]:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    for image_path in circuit_dir.glob("*.jpg"):
        image_path.unlink()
    return project_path


def _tdr_channels(context: dict[str, Any]) -> list[dict[str, Any]]:
    channels = context.get("tdr", {}).get("channels") or []
    if not channels:
        raise ValueError("tdr.channels is required for manual-snp-multi-diff")
    required = ["name", "nearPositive", "nearNegative", "farPositive", "farNegative"]
    for channel in channels:
        missing = [key for key in required if not channel.get(key)]
        if missing:
            raise ValueError(f"invalid tdr channel {channel!r}; missing {missing}")
        _circuit_direction_contract(channel)
    return channels


def _circuit_direction_contract(channel: dict[str, Any]) -> dict[str, Any]:
    near_ports = [
        str(channel.get("nearPositive") or ""),
        str(channel.get("nearNegative") or ""),
    ]
    far_ports = [
        str(channel.get("farPositive") or ""),
        str(channel.get("farNegative") or ""),
    ]
    if not all([*near_ports, *far_ports]):
        raise ValueError(
            f"channel {channel.get('name')!r} has incomplete near/far port roles"
        )
    measurement_direction = channel.get("measurementDirection")
    near_endpoint = channel.get("nearEndpoint") or {}
    far_endpoint = channel.get("farEndpoint") or {}
    result_provenance = channel.get("resultProvenance") or {}
    if measurement_direction:
        if measurement_direction not in {
            "path_start_to_endpoint",
            "path_endpoint_to_start",
        }:
            raise ValueError(
                f"channel {channel.get('name')!r} has invalid measurementDirection"
            )
        if not near_endpoint.get("component") or not far_endpoint.get("component"):
            raise ValueError(
                f"channel {channel.get('name')!r} direction metadata requires near/far components"
            )
        if (
            result_provenance.get("startRefdes")
            != near_endpoint.get("component")
            or result_provenance.get("endRefdes")
            != far_endpoint.get("component")
        ):
            raise ValueError(
                f"channel {channel.get('name')!r} result provenance conflicts with near/far endpoints"
            )
    return {
        "channel": str(channel.get("name") or ""),
        "measurementDirection": measurement_direction or "legacy_unversioned",
        "sourceRole": "near",
        "sourcePorts": near_ports,
        "sourceEndpoint": near_endpoint or None,
        "terminationRole": "far",
        "terminationPorts": far_ports,
        "terminationEndpoint": far_endpoint or None,
        "reportGroup": channel.get("reportGroup"),
        "startRefdes": near_endpoint.get("component"),
        "endRefdes": far_endpoint.get("component"),
    }


def _place_far_end_page_termination(
    app,
    sub_pin,
    *,
    page_name: str,
    name: str,
    location: list[float],
    page_move: tuple[int, int] | None = None,
) -> dict[str, object]:
    resistor = app.modeler.schematic.create_resistor(name=name, value="1g", location=location, angle=0)
    resistor_pins = sorted(resistor.pins, key=lambda pin: (float(pin.location[0]), float(pin.location[1])))
    signal_pin = resistor_pins[0]
    ground_pin = resistor_pins[-1]
    _connect_with_named_page_ports(
        app,
        signal_pin,
        sub_pin,
        page_name,
        first_move=page_move,
        second_move=page_move,
    )
    return {
        "resistor": resistor.name,
        "signalPin": signal_pin,
        "groundPin": ground_pin,
        "pageName": page_name,
    }


def _pin_location(pin) -> tuple[float, float]:
    x, y = pin.location
    return float(x), float(y)


def _create_named_page_port_at_pin(
    app,
    pin,
    page_name: str,
    *,
    dx: float,
    dy: float = 0.0,
    angle: int = 0,
) -> object:
    pin_x, pin_y = _pin_location(pin)
    page_port = app.modeler.schematic.create_page_port(page_name, [pin_x + dx, pin_y + dy], angle=angle)
    _connect_pins_with_wire(app, pin, page_port.pins[0])
    return page_port


def _create_snp_page_ports(app, sub_pins: dict[str, object], channels: list[dict[str, Any]]) -> list[dict[str, object]]:
    used_ports = []
    seen: set[str] = set()
    for channel in channels:
        for key in ["nearPositive", "nearNegative", "farPositive", "farNegative"]:
            port_name = str(channel[key])
            if port_name in seen:
                continue
            seen.add(port_name)
            pin = sub_pins[port_name]
            pin_x, _pin_y = _pin_location(pin)
            # The sNp block remains central; page ports sit just outside the
            # pin so the schematic stays readable and logical nets do the long
            # distance connection.
            dx = -0.00635 if pin_x <= 0.0 else 0.00635
            angle = 180 if dx < 0 else 0
            page_port = _create_named_page_port_at_pin(app, pin, port_name, dx=dx, angle=angle)
            used_ports.append({"port": port_name, "pagePort": page_port.name, "pin": pin.name})
    return used_ports


def _place_local_ground_for_pin(app, pin, *, dx: float = 0.00635, dy: float = -0.004) -> str:
    pin_x, pin_y = _pin_location(pin)
    gnd = app.modeler.schematic.create_gnd([pin_x + dx, pin_y + dy])
    _connect_pins_with_wire(app, pin, gnd.pins[0])
    return gnd.name


def _place_far_end_page_block(
    app,
    *,
    page_name: str,
    resistor_name: str,
    location: list[float],
) -> dict[str, object]:
    resistor = app.modeler.schematic.create_resistor(name=resistor_name, value="1g", location=location, angle=0)
    resistor_pins = sorted(resistor.pins, key=lambda pin: (float(pin.location[0]), float(pin.location[1])))
    signal_pin = resistor_pins[0]
    ground_pin = resistor_pins[-1]
    page_port = _create_named_page_port_at_pin(app, signal_pin, page_name, dx=-0.00635, angle=180)
    ground = _place_local_ground_for_pin(app, ground_pin, dx=0.00635, dy=-0.004)
    return {
        "resistor": resistor.name,
        "signalPin": signal_pin,
        "groundPin": ground_pin,
        "ground": ground,
        "pagePort": page_port.name,
        "pageName": page_name,
    }


def _multi_diff_layout(context: dict[str, Any]) -> dict[str, float]:
    configured = (context.get("tdr", {}).get("layout") or {}).get("manualSnpMultiDiff") or {}
    return {
        "nearX": float(configured.get("nearX", -0.090)),
        "farX": float(configured.get("farX", 0.125)),
        "yStart": float(configured.get("yStart", 0.085)),
        "yStep": float(configured.get("yStep", -0.022)),
        "pageOffset": float(configured.get("pageOffset", 0.01016)),
        "pairDy": float(configured.get("pairDy", 0.0045)),
        "tdrPageDy": float(configured.get("tdrPageDy", 0.0)),
        "farResistorDx": float(configured.get("farResistorDx", 0.018)),
    }


def _place_diff_channel_tdr_block(
    app,
    *,
    channel: dict[str, Any],
    index: int,
    layout: dict[str, float],
    context: dict[str, Any],
) -> dict[str, object]:
    y = layout["yStart"] + layout["yStep"] * index
    channel_name = str(channel["name"])
    display_name = str(channel.get("displayName") or channel_name)
    near_positive = str(channel["nearPositive"])
    near_negative = str(channel["nearNegative"])

    tdr_probe = app.modeler.schematic.create_component(
        component_library="Probes",
        component_name="TDR_Differential_Ended",
        location=[layout["nearX"], y],
        angle=270,
    )
    z0_ohm = 2 * _tdr_single_ended_impedance(context, channel)
    rise_time_ps = _tdr_rise_time_ps(context, channel)
    pulse_repetition = _tdr_pulse_repetition(context, channel)
    pulse_width = _tdr_pulse_width(context, channel)
    time_delay = _tdr_time_delay(context, channel)
    direction_contract = _circuit_direction_contract(channel)
    tdr_probe.parameters["Z0"] = z0_ohm
    tdr_probe.parameters["Pulse_repetition"] = pulse_repetition
    tdr_probe.parameters["Rise_time"] = f"{rise_time_ps:g}ps"
    if pulse_width is not None:
        tdr_probe.parameters["Pulse_width"] = pulse_width
    if time_delay is not None:
        tdr_probe.parameters["Time_delay"] = time_delay

    near_pos_page = _create_named_page_port_at_pin(
        app,
        tdr_probe.pins[0],
        near_positive,
        dx=-layout["pageOffset"],
        dy=layout["tdrPageDy"],
        angle=180,
    )
    near_neg_page = _create_named_page_port_at_pin(
        app,
        tdr_probe.pins[1],
        near_negative,
        dx=-layout["pageOffset"],
        dy=-layout["tdrPageDy"],
        angle=180,
    )
    trace_name = f"O(A{tdr_probe.id}:zdiff)"
    return {
        "channel": channel_name,
        "displayName": display_name,
        "component": tdr_probe.name,
        "traceName": trace_name,
        "nearPorts": [near_positive, near_negative],
        "nearPagePorts": [near_pos_page.name, near_neg_page.name],
        "referenceImpedanceOhm": z0_ohm,
        "targetImpedanceOhm": z0_ohm,
        "targetImpedanceOhmRole": "legacy-reference-alias",
        "riseTimePs": rise_time_ps,
        "pulseRepetition": pulse_repetition,
        "pulseWidth": pulse_width,
        "timeDelay": time_delay,
        "directionProvenance": direction_contract,
    }


def _place_diff_channel_far_block(
    app,
    *,
    channel: dict[str, Any],
    index: int,
    layout: dict[str, float],
    context: dict[str, Any],
    diff_bridge_ohm: float | None,
) -> dict[str, object]:
    y = layout["yStart"] + layout["yStep"] * index
    channel_name = str(channel["name"])
    far_positive = str(channel["farPositive"])
    far_negative = str(channel["farNegative"])
    pair_dy = layout["pairDy"]
    resistor_x = layout["farX"] + layout["farResistorDx"]
    direction_contract = _circuit_direction_contract(channel)

    far_pos = _place_far_end_page_block(
        app,
        page_name=far_positive,
        resistor_name=f"R_{channel_name}_FAR_POS",
        location=[resistor_x, y + pair_dy],
    )
    far_neg = _place_far_end_page_block(
        app,
        page_name=far_negative,
        resistor_name=f"R_{channel_name}_FAR_NEG",
        location=[resistor_x, y - pair_dy],
    )

    far_end_diff_bridge = None
    if diff_bridge_ohm is not None:
        far_end_diff_bridge = _place_far_end_diff_bridge(
            app,
            far_pos["signalPin"],
            far_neg["signalPin"],
            value_ohm=diff_bridge_ohm,
            location=[layout["farX"], y],
            route_x=layout["farX"],
        )

    return {
        "channel": channel_name,
        "farEndResistors": [far_pos["resistor"], far_neg["resistor"]],
        "farEndDiffBridge": far_end_diff_bridge,
        "farEndGrounds": [far_pos["ground"], far_neg["ground"]],
        "farEndPagePorts": [far_pos["pagePort"], far_neg["pagePort"]],
        "farPorts": [far_positive, far_negative],
        "differentialBridgeOhm": diff_bridge_ohm,
        "directionProvenance": direction_contract,
    }


def run_manual_snp_multi_diff_tdr(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    from ansys.aedt.core import Circuit

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")

    project_path = _cleanup_circuit_project(circuit_dir, segment_name)
    record_path = run_dir / "tdr_transient.json"

    port_names = _touchstone_port_names(touchstone_path)
    channels = _tdr_channels(context)
    missing_ports = sorted(
        {
            port
            for channel in channels
            for port in [channel["nearPositive"], channel["nearNegative"], channel["farPositive"], channel["farNegative"]]
            if port not in port_names
        }
    )
    if missing_ports:
        raise ValueError(f"tdr channel ports are missing from touchstone: {missing_ports}")

    app = None
    try:
        app = Circuit(
            project=str(project_path),
            version=_context_aedt_version(context),
            non_graphical=True,
            new_desktop=True,
            close_on_exit=True,
        )

        design_name = f"{segment_name}_Transient_Manual"
        app.insert_design(design_name)
        app.modeler.schematic.schematic_units = "meter"

        sub = app.modeler.components.create_touchstone_component(
            str(touchstone_path),
            location=[0.0, 0.0],
            show_bitmap=False,
        )
        touchstone_model_refreshed = _refresh_touchstone_model(sub)
        sub_pins = {pin.name: pin for pin in sub.pins}

        snp_page_ports = _create_snp_page_ports(app, sub_pins, channels)
        tdr_probes: list[dict[str, object]] = []
        far_end_records: list[dict[str, object]] = []
        trace_names: list[str] = []

        layout = _multi_diff_layout(context)

        for index, channel in enumerate(channels):
            diff_bridge_ohm = _far_end_differential_bridge_ohm(context, channel)
            tdr_record = _place_diff_channel_tdr_block(
                app,
                channel=channel,
                index=index,
                layout=layout,
                context=context,
            )
            far_record = _place_diff_channel_far_block(
                app,
                channel=channel,
                index=index,
                layout=layout,
                context=context,
                diff_bridge_ohm=diff_bridge_ohm,
            )
            trace_name = str(tdr_record["traceName"])
            trace_names.append(trace_name)
            tdr_probes.append(tdr_record)
            far_end_records.append(far_record)

        setup = app.create_setup(name="Transient_TDR", setup_type=app.SETUPS.NexximTransient)
        setup.props["TransientData"] = _tdr_transient_data(context)
        if _tdr_use_ts_convolution(context):
            app.oanalysis.AddAnalysisOptions(
                [
                    "NAME:DataBlock",
                    "DataBlockID:=",
                    8,
                    "Name:=",
                    "Nexxim Options",
                    [
                        "NAME:ModifiedOptions",
                        "ts_convolution:=",
                        True,
                    ],
                ]
            )
            setup.props["OptionName"] = "Nexxim Options"

        saved = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved:
            raise RuntimeError(f"PyAEDT could not save Circuit project to {project_path}")

        analysis_ok = app.analyze_setup("Transient_TDR", blocking=True)
        if not analysis_ok:
            raise RuntimeError("PyAEDT did not report a successful Transient_TDR solve")

        report_records = []
        for report_group in _tdr_report_groups(context, tdr_probes):
            report_view = (
                report_group.get("view")
                if isinstance(report_group.get("view"), dict)
                else None
            )
            report_name, endpoint_notes, native_target_range = _create_tdr_report_multi(
                app,
                context,
                trace_names=[str(item) for item in report_group["traceNames"]],
                channels=[str(item) for item in report_group["channels"]],
                setup_name="Transient_TDR",
                plot_name=str(report_group["name"]),
                view=report_view,
            )
            report_image_path = _export_tdr_report_image(app, circuit_dir, report_name)
            x_min, x_max = _tdr_time_view_range_ps(
                context,
                view=report_view,
            )
            y_min, y_max = _tdr_impedance_view_range(
                context,
                view=report_view,
            )
            report_records.append(
                {
                    "name": report_name,
                    "imagePath": report_image_path,
                    "traceNames": report_group["traceNames"],
                    "channels": report_group["channels"],
                    "endpointNotes": endpoint_notes,
                    "nativeTargetRange": native_target_range,
                    "directionProvenance": report_group.get(
                        "directionProvenance"
                    ) or [],
                    "appliedView": {
                        "xAxisPs": {"min": x_min, "max": x_max},
                        "yAxisOhm": {"min": y_min, "max": y_max},
                    },
                }
            )
        primary_report = report_records[0] if report_records else {}
        touchstone_model_refreshed = _refresh_touchstone_model(sub) or touchstone_model_refreshed
        saved_after_analyze = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved_after_analyze:
            raise RuntimeError(f"PyAEDT could not save analyzed Circuit project to {project_path}")

        samples_by_trace: dict[str, list[dict[str, float | int]]] = {}
        sample_count = 0
        trace_units: dict[str, str] = {}
        for trace_name in trace_names:
            solution_data = app.post.get_solution_data(
                expressions=trace_name,
                setup_sweep_name="Transient_TDR",
                domain="Time",
            )
            if solution_data is None:
                continue
            trace_time_values_ps = _normalize_time_values(
                solution_data.primary_sweep_values,
                unit=(solution_data.units_sweeps or {}).get("Time"),
            )
            trace_values = _extract_solution_trace_values(solution_data, trace_name)
            count = min(len(trace_time_values_ps), len(trace_values))
            sample_count = max(sample_count, count)
            trace_units[trace_name] = (solution_data.units_data or {}).get(trace_name, "ohm")
            samples_by_trace[trace_name] = [
                {
                    "index": sample_index,
                    "time_ps": round(float(trace_time_values_ps[sample_index]), 6),
                    "impedance_ohm": round(float(trace_values[sample_index]), 6),
                }
                for sample_index in range(count)
            ]

        all_time_values_ps = [
            float(sample["time_ps"])
            for trace_samples in samples_by_trace.values()
            for sample in trace_samples
        ]
        record = {
            "status": "ok",
            "buildMode": "manual-snp-multi-diff",
            "touchstonePath": str(touchstone_path),
            "projectPath": str(project_path),
            "projectName": app.project_name,
            "designName": app.design_name,
            "setupName": "Transient_TDR",
            "traceNames": trace_names,
            "reportName": primary_report.get("name"),
            "reportImagePath": primary_report.get("imagePath"),
            "reports": report_records,
            "sampleCount": sample_count,
            "timeUnit": "ps",
            "traceUnits": trace_units,
            "analysisStopPs": float(
                (context.get("tdr", {}).get("transient") or {}).get(
                    "stopPs",
                    30000,
                )
            ),
            "dataTimeRangePs": (
                {
                    "min": min(all_time_values_ps),
                    "max": max(all_time_values_ps),
                }
                if all_time_values_ps
                else None
            ),
            "projectSaved": True,
            "touchstoneModelRefreshed": touchstone_model_refreshed,
            "manualTopology": {
                "touchstoneComponent": sub.name,
                "snpPagePorts": snp_page_ports,
                "tdrProbes": tdr_probes,
                "farEnd": far_end_records,
                "layout": layout,
            },
            "samplesByTrace": samples_by_trace,
        }
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(record, fp, indent=2)
        return record_path
    finally:
        if app is not None:
            try:
                app.release_desktop(close_projects=True, close_desktop=True)
            except Exception:
                pass


def _connect_with_named_page_ports(
    app,
    source_pin,
    target_pin,
    page_name: str,
    *,
    first_move: tuple[int, int] | None = None,
    second_move: tuple[int, int] | None = None,
) -> None:
    result = source_pin.connect_to_component(target_pin, page_name=page_name, use_wire=False)
    if isinstance(result, tuple) and len(result) == 3:
        ok, first, second = result
        if not ok:
            raise RuntimeError(f"failed to connect page ports for {page_name}")
        if first_move is not None:
            app.modeler.move(first, list(first_move), "mil")
        if second_move is not None:
            app.modeler.move(second, list(second_move), "mil")


def _connect_pins_with_wire(
    app,
    source_pin,
    target_pin,
    *,
    route_x: float | None = None,
) -> None:
    schematic = app.modeler.schematic
    source_x, source_y = source_pin.location
    target_x, target_y = target_pin.location
    if route_x is None:
        route_x = round((source_x + target_x) / 2.0, 6)

    points = [[source_x, source_y]]
    if source_x != route_x:
        points.append([route_x, source_y])
    if source_y != target_y:
        points.append([route_x, target_y])
    if points[-1] != [target_x, target_y]:
        points.append([target_x, target_y])
    schematic.create_wire(points=points)


def _place_far_end_termination(
    app,
    sub_pin,
    *,
    name: str,
    location: list[float],
    route_x: float | None = None,
) -> dict[str, object]:
    resistor = app.modeler.schematic.create_resistor(name=name, value="1g", location=location, angle=0)
    resistor_pins = sorted(resistor.pins, key=lambda pin: (float(pin.location[0]), float(pin.location[1])))
    signal_pin = resistor_pins[0]
    ground_pin = resistor_pins[-1]
    _connect_pins_with_wire(app, sub_pin, signal_pin, route_x=route_x)
    return {
        "resistor": resistor.name,
        "signalPin": signal_pin,
        "groundPin": ground_pin,
    }


def _place_shared_far_end_ground(app, pins: list[object], *, trunk_x: float, ground_y: float) -> str:
    schematic = app.modeler.schematic
    for pin in pins:
        x, y = pin.location
        schematic.create_wire(points=[[x, y], [trunk_x, y], [trunk_x, ground_y]])

    gnd = schematic.create_gnd([trunk_x, ground_y + 0.00508])
    gnd_pin = gnd.pins[0]
    schematic.create_wire(points=[[trunk_x, ground_y], [gnd_pin.location[0], gnd_pin.location[1]]])
    return gnd.name


def _far_end_differential_bridge_ohm(
    context: dict[str, Any],
    channel: dict[str, Any] | None = None,
) -> float | None:
    bridge = None
    channel_termination = channel.get("termination") if channel is not None else None
    if isinstance(channel_termination, dict) and "differentialBridgeOhm" in channel_termination:
        bridge = channel_termination.get("differentialBridgeOhm")
    else:
        termination = context.get("tdr", {}).get("termination") or {}
        bridge = termination.get("differentialBridgeOhm")
    return float(bridge) if bridge is not None else None


def _place_far_end_diff_bridge(
    app,
    positive_signal_pin,
    negative_signal_pin,
    *,
    value_ohm: float,
    location: list[float],
    route_x: float | None = None,
) -> str:
    resistor = app.modeler.schematic.create_resistor(name="R_FAR_DIFF", value=str(value_ohm), location=location, angle=90)
    resistor_pins = sorted(resistor.pins, key=lambda pin: (float(pin.location[1]), float(pin.location[0])), reverse=True)
    positive_pin = resistor_pins[0]
    negative_pin = resistor_pins[-1]
    _connect_pins_with_wire(app, positive_signal_pin, positive_pin, route_x=route_x)
    _connect_pins_with_wire(app, negative_signal_pin, negative_pin, route_x=route_x)
    return resistor.name


def _extract_solution_trace_values(solution_data, trace_name: str):
    get_expression_data = getattr(solution_data, "get_expression_data", None)
    if callable(get_expression_data):
        xy_values = get_expression_data(expression=trace_name)
        if not hasattr(xy_values, "__len__") or len(xy_values) != 2:
            raise RuntimeError("PyAEDT SolutionData returned an unexpected expression data shape")
        return xy_values[1]

    data_real = getattr(solution_data, "data_real", None)
    if callable(data_real):
        return data_real(trace_name)

    raise RuntimeError("PyAEDT SolutionData does not expose get_expression_data() or data_real()")


def _normalize_tdr_schematic_result(result: object) -> tuple[bool, list[str]]:
    if isinstance(result, tuple):
        if len(result) != 2:
            raise RuntimeError("PyAEDT returned an unexpected TDR schematic result tuple")
        created, trace_names = result
        return bool(created), [str(item) for item in trace_names or []]
    if isinstance(result, bool):
        return result, []
    raise RuntimeError("PyAEDT returned an unsupported TDR schematic result payload")


def _touchstone_port_names(path: Path) -> list[str]:
    port_names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped.startswith("! Port["):
            continue
        _, _, name = stripped.partition("=")
        normalized = name.strip()
        if normalized:
            port_names.append(normalized)
    if not port_names:
        suffix = path.suffix.lower()
        if suffix.startswith(".s") and suffix.endswith("p") and suffix[2:-1].isdigit():
            port_count = int(suffix[2:-1])
            return [str(index) for index in range(1, port_count + 1)]
    return port_names


def _validate_touchstone_port_contract(
    context: dict[str, Any],
    touchstone_path: Path,
) -> Path | None:
    expected_order = [
        str(item) for item in context.get("ports", {}).get("portOrder") or []
    ]
    if not expected_order:
        return None

    run_dir = Path(context["workspace"]["runDir"])
    record_path = run_dir / "touchstone_port_contract.json"
    actual_order = _touchstone_port_names(touchstone_path)
    metadata_path_value = context.get("ports", {}).get("metadataPath")
    metadata_path: Path | None = None
    metadata_order: list[str] | None = None
    metadata_indices: list[int] | None = None
    role_metadata_version: int | None = None
    role_metadata_order: list[str] | None = None
    configured_role_version = context.get("ports", {}).get(
        "roleMetadataVersion"
    )
    issues: list[dict[str, Any]] = []

    duplicate_expected = sorted(
        name for name, count in Counter(expected_order).items() if count > 1
    )
    if duplicate_expected:
        issues.append(
            {
                "code": "duplicate_expected_port_names",
                "ports": duplicate_expected,
            }
        )

    if configured_role_version is not None and not metadata_path_value:
        issues.append(
            {
                "code": "port_role_metadata_path_missing",
                "configured": configured_role_version,
            }
        )

    if metadata_path_value:
        metadata_path = Path(str(metadata_path_value))
        if not metadata_path.is_absolute():
            metadata_path = (ROOT_DIR / metadata_path).resolve()
        if not metadata_path.exists():
            issues.append(
                {
                    "code": "port_metadata_missing",
                    "path": str(metadata_path),
                }
            )
        else:
            with metadata_path.open("r", encoding="utf-8") as fp:
                metadata_payload = json.load(fp)
            metadata_ports = metadata_payload.get("ports") or []
            metadata_order = [str(item.get("name") or "") for item in metadata_ports]
            metadata_indices = [
                int(item.get("index") or 0) for item in metadata_ports
            ]
            if metadata_indices != list(range(1, len(metadata_indices) + 1)):
                issues.append(
                    {
                        "code": "port_metadata_indices_not_sequential",
                        "indices": metadata_indices,
                    }
                )
            if metadata_order != expected_order:
                issues.append(
                    {
                        "code": "port_metadata_order_mismatch",
                        "expected": expected_order,
                        "actual": metadata_order,
                    }
                )
            role_metadata = metadata_payload.get("portRoleMetadata")
            if role_metadata is None and configured_role_version is not None:
                issues.append(
                    {
                        "code": "port_role_metadata_missing",
                        "configured": configured_role_version,
                    }
                )
            elif role_metadata is not None:
                if not isinstance(role_metadata, dict):
                    issues.append(
                        {"code": "port_role_metadata_not_object"}
                    )
                else:
                    role_metadata_version = int(
                        role_metadata.get("schemaVersion") or 0
                    )
                    if role_metadata_version != 2 or (
                        configured_role_version is not None
                        and int(configured_role_version) != role_metadata_version
                    ):
                        issues.append(
                            {
                                "code": "port_role_metadata_version_mismatch",
                                "configured": configured_role_version,
                                "actual": role_metadata_version,
                            }
                        )
                    raw_role_order = role_metadata.get("portOrder") or []
                    role_metadata_order = [
                        str(item.get("name") or "")
                        for item in raw_role_order
                        if isinstance(item, dict)
                    ]
                    role_indices = [
                        int(item.get("index") or 0)
                        for item in raw_role_order
                        if isinstance(item, dict)
                    ]
                    if role_metadata_order != expected_order or role_indices != list(
                        range(1, len(expected_order) + 1)
                    ):
                        issues.append(
                            {
                                "code": "port_role_metadata_order_mismatch",
                                "expected": expected_order,
                                "actual": role_metadata_order,
                                "indices": role_indices,
                            }
                        )

                    context_channels = {
                        str(item.get("name") or ""): item
                        for item in context.get("tdr", {}).get("channels") or []
                        if isinstance(item, dict)
                    }
                    role_channels = role_metadata.get("channels") or []
                    role_names: list[str] = []
                    for role_channel in role_channels:
                        if not isinstance(role_channel, dict):
                            issues.append(
                                {"code": "port_role_metadata_channel_not_object"}
                            )
                            continue
                        channel_name = str(role_channel.get("name") or "")
                        context_channel = context_channels.get(channel_name)
                        if context_channel is None:
                            issues.append(
                                {
                                    "code": "port_role_metadata_channel_missing_from_tdr",
                                    "channel": channel_name,
                                }
                            )
                            continue
                        endpoint_ports: dict[str, list[str]] = {}
                        endpoint_components: dict[str, Any] = {}
                        endpoint_pins: dict[str, Any] = {}
                        for endpoint_name in ("near", "far"):
                            endpoint = role_channel.get(endpoint_name) or {}
                            ports = endpoint.get("ports") or {}
                            names = [
                                str((ports.get(polarity) or {}).get("name") or "")
                                for polarity in ("positive", "negative")
                            ]
                            indices = [
                                int((ports.get(polarity) or {}).get("index") or 0)
                                for polarity in ("positive", "negative")
                            ]
                            endpoint_ports[endpoint_name] = names
                            endpoint_components[endpoint_name] = endpoint.get(
                                "component"
                            )
                            endpoint_pins[endpoint_name] = endpoint.get("pins") or {}
                            role_names.extend(names)
                            if any(
                                not name
                                or name not in expected_order
                                or indices[index]
                                != expected_order.index(name) + 1
                                for index, name in enumerate(names)
                            ):
                                issues.append(
                                    {
                                        "code": "port_role_metadata_index_mismatch",
                                        "channel": channel_name,
                                        "endpoint": endpoint_name,
                                        "ports": names,
                                        "indices": indices,
                                    }
                                )
                        expected_near = [
                            str(context_channel.get("nearPositive") or ""),
                            str(context_channel.get("nearNegative") or ""),
                        ]
                        expected_far = [
                            str(context_channel.get("farPositive") or ""),
                            str(context_channel.get("farNegative") or ""),
                        ]
                        direction = role_channel.get("measurementDirection") or {}
                        if (
                            endpoint_ports.get("near") != expected_near
                            or endpoint_ports.get("far") != expected_far
                            or direction.get("value")
                            != context_channel.get("measurementDirection")
                            or endpoint_components.get("near")
                            != (context_channel.get("nearEndpoint") or {}).get(
                                "component"
                            )
                            or endpoint_components.get("far")
                            != (context_channel.get("farEndpoint") or {}).get(
                                "component"
                            )
                            or endpoint_pins.get("near")
                            != (context_channel.get("nearEndpoint") or {}).get("pins")
                            or endpoint_pins.get("far")
                            != (context_channel.get("farEndpoint") or {}).get("pins")
                        ):
                            issues.append(
                                {
                                    "code": "port_role_metadata_tdr_mismatch",
                                    "channel": channel_name,
                                }
                            )
                    if Counter(role_names) != Counter(expected_order):
                        issues.append(
                            {
                                "code": "port_role_metadata_coverage_mismatch",
                                "expected": expected_order,
                                "actual": role_names,
                            }
                        )

    if actual_order != expected_order:
        issues.append(
            {
                "code": "touchstone_header_order_mismatch",
                "expected": expected_order,
                "actual": actual_order,
            }
        )

    record = {
        "status": "error" if issues else "ok",
        "touchstone": str(touchstone_path),
        "portMetadata": str(metadata_path) if metadata_path else None,
        "portCount": len(expected_order),
        "policy": context.get("ports", {}).get("portOrderPolicy"),
        "expectedOrder": expected_order,
        "metadataOrder": metadata_order,
        "metadataIndices": metadata_indices,
        "portRoleMetadataVersion": role_metadata_version,
        "portRoleMetadataOrder": role_metadata_order,
        "touchstoneHeaderOrder": actual_order,
        "issues": issues,
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("w", encoding="utf-8") as fp:
        json.dump(record, fp, indent=2, ensure_ascii=False)
        fp.write("\n")

    if issues:
        issue_codes = [str(item["code"]) for item in issues]
        raise RuntimeError(
            "Touchstone Port contract validation failed before Circuit: "
            + ", ".join(issue_codes)
        )
    return record_path


def _tdr_probe_pin_config(context: dict[str, Any], port_names: list[str]) -> tuple[list[object], list[object] | None, list[object]]:
    if context["ports"].get("mode") == "single-ended-4port":
        expected = [spec["name"] for spec in _se4_port_specs(context)]
        pins = port_names if len(port_names) >= 4 else expected
        return [pins[0]], [pins[1]], pins[:4]

    if len(port_names) >= 2:
        return [port_names[0]], [port_names[1]], [port_names[0], port_names[1]]
    return ["1"], ["2"], ["1", "2"]


def _resolve_tdr_setup_name(app) -> str:
    setup_names = _setup_name_candidates(app)
    matched = [str(item) for item in setup_names if str(item).split(":", 1)[0].strip() == "Transient_TDR"]
    if len(matched) == 1:
        return matched[0]
    if len(setup_names) == 1:
        return str(setup_names[0])
    raise RuntimeError("PyAEDT did not expose a resolvable transient setup name")


def _setup_name_candidates(app) -> list[str]:
    for attr_name in [
        "existing_analysis_sweeps",
        "analysis_setup_list",
        "analysis_setup_names",
        "setup_names",
        "setups",
    ]:
        raw_value = getattr(app, attr_name, None)
        if not raw_value:
            continue
        values = list(raw_value if isinstance(raw_value, (list, tuple)) else [raw_value])
        normalized = []
        for item in values:
            if item is None:
                continue
            if hasattr(item, "name") and getattr(item, "name"):
                normalized.append(str(getattr(item, "name")))
            else:
                normalized.append(str(item))
        if normalized:
            return normalized
    return []


def _normalize_time_values(values, *, unit: str | None) -> list[float]:
    normalized_unit = (unit or "").strip().lower() or "ps"
    scale = {
        "s": 1.0e12,
        "sec": 1.0e12,
        "second": 1.0e12,
        "seconds": 1.0e12,
        "ms": 1.0e9,
        "msec": 1.0e9,
        "millisecond": 1.0e9,
        "milliseconds": 1.0e9,
        "us": 1.0e6,
        "usec": 1.0e6,
        "microsecond": 1.0e6,
        "microseconds": 1.0e6,
        "ns": 1.0e3,
        "nsec": 1.0e3,
        "nanosecond": 1.0e3,
        "nanoseconds": 1.0e3,
        "ps": 1.0,
    }.get(normalized_unit)
    if scale is None:
        raise RuntimeError(f"unsupported PyAEDT time unit: {unit}")
    return [float(value) * scale for value in values]


def _tdr_report_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "differential_pairs": context["tdr"].get("mode") == "differential",
    }


def _native_endpoint_notes_not_rendered(
    report_name: str | None,
    reason: str,
    *,
    status: str = "not_rendered",
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "reportName": report_name,
        "coordinateSystem": "aedt_report_layout",
        "visibleTextPolicy": "refdes_only",
        "startPlacement": "left_lower_inside_plot",
        "endPlacement": "right_lower_inside_plot",
        "dataCoordinateMarkerRendered": False,
        "actualArrivalTimePositioned": False,
        "labelCount": 0,
        "labels": [],
    }


def _add_tdr_native_endpoint_notes(
    report,
    context: dict[str, Any],
    *,
    trace_names: list[str],
    channels: list[str] | None = None,
) -> dict[str, Any]:
    endpoint_api = _load_tdr_endpoint_annotation_module()
    mapped_channels: list[str | None]
    if channels is not None and len(channels) == len(trace_names):
        mapped_channels = [str(channel) for channel in channels]
    elif channels is not None:
        return _native_endpoint_notes_not_rendered(
            getattr(report, "plot_name", None),
            "native report trace/channel mapping length mismatch",
        )
    else:
        configured_channels = [
            str(item.get("name") or "").strip()
            for item in context.get("tdr", {}).get("channels") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        mapped_channels = (
            [configured_channels[0]] * len(trace_names)
            if len(configured_channels) == 1
            else [None] * len(trace_names)
        )

    result = endpoint_api.analyze_endpoint_annotations(
        context.get("tdr", {}),
        dict(zip(trace_names, mapped_channels)),
        source={
            "artifactType": "aedt_native_report",
            "reportName": getattr(report, "plot_name", None),
            "traceNames": list(trace_names),
        },
    )
    native_result = endpoint_api.add_native_report_endpoint_notes(report, result)
    if native_result.get("status") == "render_failed":
        raise RuntimeError(
            "AEDT native endpoint Note rendering failed closed: "
            f"{native_result.get('reason')}"
        )
    return native_result


def _load_tdr_native_target_range_module():
    if __package__:
        from . import tdr_native_target_range
    else:
        import tdr_native_target_range

    return tdr_native_target_range


def _native_target_range_not_rendered(
    report_name: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": "si-tdr-aedt-native-target-range/v1",
        "status": "not_rendered",
        "reason": reason,
        "reportName": report_name,
        "targetBandsOhm": [],
        "limitLineCount": 0,
        "labelCount": 0,
    }


def _add_tdr_native_target_ranges(
    report,
    context: dict[str, Any],
    *,
    trace_names: list[str],
    channels: list[str] | None = None,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_range_api = _load_tdr_native_target_range_module()
    analysis = target_range_api.analyze_native_target_ranges(
        context.get("tdr", {}),
        trace_names,
        channels,
    )
    x_min, x_max = _tdr_time_view_range_ps(context, view=view)
    if x_min is None:
        x_min = 0.0
    if x_max is None:
        x_max = float(
            (context.get("tdr", {}).get("transient") or {}).get(
                "stopPs",
                30000,
            )
        )
    native_result = target_range_api.add_native_report_target_ranges(
        report,
        analysis,
        x_min_ps=x_min,
        x_max_ps=x_max,
    )
    if native_result.get("status") == "render_failed":
        raise RuntimeError(
            "AEDT native Target Range rendering failed closed: "
            f"{native_result.get('reason')}"
        )
    return native_result


def _create_tdr_report(
    app,
    context: dict[str, Any],
    *,
    trace_name: str,
    setup_name: str,
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    plot_name = f"{context['segment'].get('name', 'SEGMENT')}_TDR_Report"
    report = app.post.create_report(
        expressions=trace_name,
        setup_sweep_name=setup_name,
        domain="Time",
        primary_sweep_variable="Time",
        plot_name=plot_name,
        context=_tdr_report_context(context),
    )
    if not report:
        reason = "PyAEDT did not create the native TDR report"
        return (
            None,
            _native_endpoint_notes_not_rendered(None, reason),
            _native_target_range_not_rendered(None, reason),
        )
    _apply_tdr_report_view(report, context)
    native_target_range = _add_tdr_native_target_ranges(
        report,
        context,
        trace_names=[trace_name],
    )
    endpoint_notes = _add_tdr_native_endpoint_notes(
        report,
        context,
        trace_names=[trace_name],
    )
    _update_tdr_report(app, plot_name)
    return plot_name, endpoint_notes, native_target_range


def _create_tdr_report_multi(
    app,
    context: dict[str, Any],
    *,
    trace_names: list[str],
    channels: list[str] | None = None,
    setup_name: str,
    plot_name: str | None = None,
    view: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    plot_name = plot_name or f"{context['segment'].get('name', 'SEGMENT')}_TDR_Report"
    report = app.post.create_report(
        expressions=trace_names,
        setup_sweep_name=setup_name,
        domain="Time",
        primary_sweep_variable="Time",
        plot_name=plot_name,
        context=_tdr_report_context(context),
    )
    if not report:
        reason = "PyAEDT did not create the native TDR report"
        return (
            None,
            _native_endpoint_notes_not_rendered(None, reason),
            _native_target_range_not_rendered(None, reason),
        )
    _apply_tdr_report_view(report, context, view=view)
    native_target_range = _add_tdr_native_target_ranges(
        report,
        context,
        trace_names=trace_names,
        channels=channels,
        view=view,
    )
    endpoint_notes = _add_tdr_native_endpoint_notes(
        report,
        context,
        trace_names=trace_names,
        channels=channels,
    )
    _update_tdr_report(app, plot_name)
    return plot_name, endpoint_notes, native_target_range


def _tdr_report_groups(context: dict[str, Any], tdr_probes: list[dict[str, object]]) -> list[dict[str, object]]:
    configured_groups = context.get("tdr", {}).get("reportGroups") or []
    if not configured_groups:
        return [
            {
                "name": f"{context['segment'].get('name', 'SEGMENT')}_TDR_Report",
                "traceNames": [str(probe["traceName"]) for probe in tdr_probes],
                "channels": [str(probe["channel"]) for probe in tdr_probes],
                "directionProvenance": [
                    probe.get("directionProvenance") for probe in tdr_probes
                    if probe.get("directionProvenance")
                ],
            }
        ]

    groups: list[dict[str, object]] = []
    for group in configured_groups:
        group_name = str(group["name"])
        prefixes = [str(item) for item in group.get("channelPrefixes") or []]
        channel_names = {str(item) for item in group.get("channels") or []}
        matched_probes = [
            probe
            for probe in tdr_probes
            if str(probe["channel"]) in channel_names
            or any(str(probe["channel"]).startswith(prefix) for prefix in prefixes)
        ]
        if not matched_probes:
            continue
        groups.append(
            {
                "name": group_name,
                "traceNames": [str(probe["traceName"]) for probe in matched_probes],
                "channels": [str(probe["channel"]) for probe in matched_probes],
                "view": group.get("view"),
                "directionProvenance": [
                    probe.get("directionProvenance")
                    for probe in matched_probes
                    if probe.get("directionProvenance")
                ],
            }
        )
    return groups


def _update_tdr_report(app, report_name: str | None) -> bool:
    if not report_name:
        return False
    try:
        app.post.oreportsetup.UpdateReports([report_name])
        return True
    except Exception:
        return False


def _apply_tdr_report_view(report, context: dict[str, Any], *, view: dict[str, Any] | None = None) -> bool:
    y_min, y_max = _tdr_impedance_view_range(context, view=view)
    x_min, x_max = _tdr_time_view_range_ps(context, view=view)
    if y_min is None and y_max is None and x_min is None and x_max is None:
        return False

    def _scale_value(value: float | None) -> str | None:
        if value is None:
            return None
        return f"{value:g}ohm"

    edited = False
    try:
        edited = bool(
            report.edit_y_axis_scaling(
                name="Y1",
                linear_scaling=True,
                min_scale=_scale_value(y_min),
                max_scale=_scale_value(y_max),
                units="ohm",
            )
        )
    except Exception:
        pass

    def _time_value(value: float | None) -> str | None:
        if value is None:
            return None
        return format_tdr_time_ps(value)

    try:
        edited = bool(
            report.edit_x_axis_scaling(
                linear_scaling=True,
                min_scale=_time_value(x_min),
                max_scale=_time_value(x_max),
                units="ps",
            )
        ) or edited
    except Exception:
        pass

    return edited


def _export_tdr_report_image(app, circuit_dir: Path, report_name: str | None) -> str | None:
    if not report_name:
        return None
    try:
        ok = bool(app.post.export_report_to_jpg(str(circuit_dir), report_name, width=1400, height=800, image_format="jpg"))
    except Exception:
        return None
    report_image_path = circuit_dir / f"{report_name}.jpg"
    if not ok or not report_image_path.exists() or report_image_path.stat().st_size <= 0:
        return None
    return str(report_image_path)


def _refresh_touchstone_model(component) -> bool:
    try:
        model_data = component.model_data
    except Exception:
        return False
    if not model_data:
        return False
    try:
        return bool(model_data.update())
    except Exception:
        return False


def _tdr_impedance_view_range(
    context: dict[str, Any],
    *,
    view: dict[str, Any] | None = None,
) -> tuple[float | None, float | None]:
    global_view = context.get("tdr", {}).get("view") or {}
    y_axis = dict(global_view.get("yAxisOhm") or {})
    if view is not None:
        y_axis.update(view.get("yAxisOhm") or {})
    y_min = y_axis.get("min")
    y_max = y_axis.get("max")
    return (float(y_min) if y_min is not None else None, float(y_max) if y_max is not None else None)


def _tdr_time_view_range_ps(
    context: dict[str, Any],
    *,
    view: dict[str, Any] | None = None,
) -> tuple[float | None, float | None]:
    global_view = context.get("tdr", {}).get("view") or {}
    x_axis = dict(global_view.get("xAxisPs") or {})
    if view is not None:
        x_axis.update(view.get("xAxisPs") or {})
    x_min = x_axis.get("min")
    x_max = x_axis.get("max")
    return (float(x_min) if x_min is not None else None, float(x_max) if x_max is not None else None)


def _load_tdr_minmax_marker_module():
    if __package__:
        from . import tdr_minmax_marker
    else:
        import tdr_minmax_marker

    return tdr_minmax_marker


def _load_tdr_endpoint_annotation_module():
    if __package__:
        from . import tdr_endpoint_annotations
    else:
        import tdr_endpoint_annotations

    return tdr_endpoint_annotations


def _tdr_marker_analysis_samples(transient: dict[str, Any]) -> dict[str, object]:
    if "samplesByTrace" in transient:
        raw_samples_by_trace = transient.get("samplesByTrace")
        if not isinstance(raw_samples_by_trace, dict):
            raise RuntimeError("tdr_transient.json samplesByTrace must be an object")
        samples_by_trace = {
            str(trace_name): trace_samples
            for trace_name, trace_samples in raw_samples_by_trace.items()
        }
        for trace_name in transient.get("traceNames") or []:
            samples_by_trace.setdefault(str(trace_name), [])
        return samples_by_trace

    samples = transient.get("samples") or []
    trace_names = transient.get("traceNames") or []
    if not samples and not trace_names:
        return {}
    fallback_trace_name = str(trace_names[0] if trace_names else "TDR")
    return {fallback_trace_name: samples}


def _tdr_trace_display_names(transient: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for probe in (transient.get("manualTopology") or {}).get("tdrProbes") or []:
        trace = probe.get("traceName")
        display_name = probe.get("displayName") or probe.get("channel")
        if trace and display_name:
            labels[str(trace)] = str(display_name)
    return labels


def export_tdr_waveform_image(context: dict[str, Any]) -> Path:
    ensure_embedded_site_packages()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    marker_api = _load_tdr_minmax_marker_module()
    endpoint_api = _load_tdr_endpoint_annotation_module()

    run_dir = Path(context["workspace"]["runDir"])
    output_dir = Path(context["workspace"]["outputDir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    transient_path = run_dir / "tdr_transient.json"
    ensure_exists(transient_path, "tdr_transient")

    with transient_path.open("r", encoding="utf-8") as fp:
        transient = json.load(fp)

    samples = transient.get("samples") or []
    analysis_samples = _tdr_marker_analysis_samples(transient)
    if not analysis_samples:
        raise RuntimeError("tdr_transient.json does not contain samples")
    samples_by_trace = analysis_samples if "samplesByTrace" in transient else {}

    trace_labels = _tdr_trace_display_names(transient)
    impedance_metadata = build_tdr_impedance_metadata(context["tdr"], transient)
    trace_to_channel = {
        str(item["traceName"]): (
            str(item["channel"]) if item.get("channel") is not None else None
        )
        for item in impedance_metadata.get("channels") or []
        if item.get("traceName")
    }

    endpoint_json_path = run_dir / "tdr_endpoint_annotations.json"
    endpoint_csv_path = run_dir / "tdr_endpoint_annotations.csv"
    endpoint_result = endpoint_api.analyze_endpoint_annotations(
        context.get("tdr", {}),
        trace_to_channel,
        source={
            "kind": "tdr_transient_json_and_run_config",
            "transientPath": str(transient_path),
            "configPath": context.get("configPath"),
        },
    )
    endpoint_result["artifacts"] = {
        "jsonPath": str(endpoint_json_path),
        "csvPath": str(endpoint_csv_path),
    }

    fallback_trace_name = next(iter(analysis_samples))
    marker_config = (
        (context.get("tdr", {}).get("resultProcessing") or {}).get("minMaxMarkers")
    )
    marker_json_path = run_dir / "tdr_minmax_markers.json"
    marker_csv_path = run_dir / "tdr_minmax_markers.csv"
    marker_result = marker_api.analyze_tdr_minmax(
        analysis_samples,
        marker_config,
        trace_to_channel=trace_labels,
        marker_config_by_channel=marker_api.channel_marker_overrides(context.get("tdr", {})),
        unit_errors=marker_api.transient_unit_errors(
            transient,
            [
                str(trace_name)
                for trace_name, trace_samples in analysis_samples.items()
                if trace_samples
            ],
        ),
        source={
            "kind": "tdr_transient_json",
            "path": str(transient_path),
        },
    )
    marker_result["artifacts"] = {
        "jsonPath": str(marker_json_path),
        "csvPath": str(marker_csv_path),
    }
    marker_api.write_marker_results(
        marker_result,
        json_path=marker_json_path,
        csv_path=marker_csv_path,
    )

    fig, ax = plt.subplots(figsize=(10, 5), dpi=160)
    line_by_trace: dict[str, Any] = {}
    if samples_by_trace:
        all_x_values: list[float] = []
        all_y_values: list[float] = []
        for trace_name, trace_samples in samples_by_trace.items():
            if not trace_samples:
                continue
            x_values, y_values = marker_api.finite_trace_points(trace_samples)
            if not x_values:
                continue
            all_x_values.extend(x_values)
            all_y_values.extend(y_values)
            plotted = ax.plot(
                x_values,
                y_values,
                linewidth=1.4,
                label=trace_labels.get(str(trace_name), str(trace_name)),
            )
            if plotted:
                line_by_trace[str(trace_name)] = plotted[0]
    else:
        x_values, y_values = marker_api.finite_trace_points(samples)
        all_x_values = x_values
        all_y_values = y_values
        trace_name = ", ".join(transient.get("traceNames") or []) or "TDR"
        plotted = ax.plot(
            x_values,
            y_values,
            color="#0B4F6C",
            linewidth=2.0,
            label=trace_name,
        )
        if plotted:
            line_by_trace[fallback_trace_name] = plotted[0]
    impedance_overlays = add_tdr_impedance_chart_overlays(ax, impedance_metadata)
    marker_overlay = marker_api.add_marker_overlays(
        ax,
        marker_result,
        line_by_trace=line_by_trace,
    )
    endpoint_overlay = endpoint_api.add_endpoint_annotation_overlays(
        ax,
        endpoint_result,
    )
    endpoint_result["rendering"] = endpoint_overlay
    endpoint_api.write_endpoint_annotation_results(
        endpoint_result,
        json_path=endpoint_json_path,
        csv_path=endpoint_csv_path,
    )

    ax.set_title(f"{context['workspace'].get('runName', context['segment'].get('name', 'TDR'))} waveform")
    ax.set_xlabel("Time (ps)")
    ax.set_ylabel("Impedance (ohm)")
    y_min, y_max = _tdr_impedance_view_range(context)
    if y_min is not None or y_max is not None:
        auto_y_min, auto_y_max = ax.get_ylim()
        ax.set_ylim(
            y_min if y_min is not None else (min(all_y_values) if all_y_values else auto_y_min),
            y_max if y_max is not None else (max(all_y_values) if all_y_values else auto_y_max),
        )
    x_min, x_max = _tdr_time_view_range_ps(context)
    if x_min is not None or x_max is not None:
        auto_x_min, auto_x_max = ax.get_xlim()
        ax.set_xlim(
            x_min if x_min is not None else (min(all_x_values) if all_x_values else auto_x_min),
            x_max if x_max is not None else (max(all_x_values) if all_x_values else auto_x_max),
        )
    ax.grid(True, alpha=0.25)
    legend_layout = {
        "placement": "outside_right",
        "loc": "upper left",
        "bboxToAnchor": [1.01, 1.0],
        "figureExpansion": "bbox_inches_tight",
    }
    fig.tight_layout()
    ax.legend(
        loc=legend_layout["loc"],
        bbox_to_anchor=tuple(legend_layout["bboxToAnchor"]),
        borderaxespad=0.0,
    )

    image_path = output_dir / f"{context['workspace'].get('runName', context['segment'].get('name', 'tdr'))}_waveform.png"
    run_image_path = run_dir / f"{context['workspace'].get('runName', context['segment'].get('name', 'tdr'))}_waveform.png"
    fig.savefig(image_path, bbox_inches="tight")
    if run_image_path != image_path:
        fig.savefig(run_image_path, bbox_inches="tight")
    plt.close(fig)
    waveform_csv_path = write_tdr_waveform_csv(
        transient,
        impedance_metadata,
        run_dir / "tdr_waveform.csv",
    )

    record = {
        "status": "ok",
        "sourceTransient": str(transient_path),
        "imagePath": str(image_path),
        "runImagePath": str(run_image_path),
        "sampleCount": len(samples) if samples else max((len(items) for items in samples_by_trace.values()), default=0),
        "traceNames": transient.get("traceNames") or [],
        "waveformCsvPath": str(waveform_csv_path),
        "impedanceSettings": impedance_metadata,
        "chartOverlays": impedance_overlays,
        "legendLayout": legend_layout,
        "yAxisOhm": {
            "min": y_min,
            "max": y_max,
        },
        "xAxisPs": {
            "min": x_min,
            "max": x_max,
        },
        "minMaxMarkers": {
            "status": marker_result["status"],
            "jsonPath": str(marker_json_path),
            "csvPath": str(marker_csv_path),
            **marker_overlay,
        },
        "endpointAnnotations": {
            "status": endpoint_result["status"],
            "reason": endpoint_result["reason"],
            "jsonPath": str(endpoint_json_path),
            "csvPath": str(endpoint_csv_path),
            **endpoint_overlay,
        },
    }
    record_path = run_dir / "tdr_image.json"
    with record_path.open("w", encoding="utf-8") as fp:
        json.dump(record, fp, indent=2)
    return record_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare SI-TDR PoC workspace")
    parser.add_argument(
        "config",
        nargs="?",
        default=str(CONFIG_DIR / "poc-usb2-jack-a-diff-s2p.json"),
        help="Path to a Run Config or Config generation request JSON",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="Generate a Run Config from the customer CSV request before executing the selected flow",
    )
    parser.add_argument(
        "--apply-ports",
        action="store_true",
        help="Copy reference AEDB into the run directory and apply ports for the selected PoC",
    )
    parser.add_argument(
        "--setup-syz",
        action="store_true",
        help="Create a SIwave SYZ setup on the copied run AEDB",
    )
    parser.add_argument(
        "--solve-touchstone",
        action="store_true",
        help="Run SIwave SYZ solve and export Touchstone on the copied run AEDB",
    )
    parser.add_argument(
        "--run-tdr",
        action="store_true",
        help="Run a minimal PyAEDT Circuit TDR flow from the exported Touchstone",
    )
    parser.add_argument(
        "--tdr-only",
        action="store_true",
        help="Reuse the existing Touchstone in the run directory and rebuild only the Circuit/TDR result",
    )
    parser.add_argument(
        "--export-tdr-image",
        action="store_true",
        help="Render a png waveform from tdr_transient.json",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Directory for full and progress logs (default: <config folder>/logs)",
    )
    if (ROOT_DIR / "pcb_capture.py").exists():
        pcb_capture_group = parser.add_mutually_exclusive_group()
        pcb_capture_group.add_argument(
            "--plan-pcb-capture",
            action="store_true",
            help="Write the license-free PCB route/layer capture plan",
        )
        pcb_capture_group.add_argument(
            "--capture-pcb-routes",
            action="store_true",
            help="Capture resolved Channel Paths by occupied PCB signal layer with PyEDB/SIWave",
        )
    else:
        parser.set_defaults(
            plan_pcb_capture=False,
            capture_pcb_routes=False,
        )
    return parser.parse_args()


def _requested_operation_labels(args: argparse.Namespace) -> list[str]:
    operations: list[str] = []
    if getattr(args, "generate_config", False):
        operations.append("Reference preprocessing and Config generation")
    operations.append("Prepare run")
    if any(
        getattr(args, name, False)
        for name in ("apply_ports", "setup_syz", "solve_touchstone", "run_tdr")
    ):
        operations.append("Create ports")
    if any(
        getattr(args, name, False)
        for name in ("setup_syz", "solve_touchstone", "run_tdr")
    ):
        operations.append("Configure SIWave SYZ")
    if any(
        getattr(args, name, False)
        for name in ("solve_touchstone", "run_tdr")
    ):
        operations.append("Solve SIWave sNp")
    if getattr(args, "run_tdr", False) or getattr(args, "tdr_only", False):
        operations.append("Solve AEDT TDR")
    if any(
        getattr(args, name, False)
        for name in ("export_tdr_image", "run_tdr", "tdr_only")
    ):
        operations.append("Generate TDR results")
    if getattr(args, "capture_pcb_routes", False):
        operations.append("Capture PCB images")
    elif getattr(args, "plan_pcb_capture", False):
        operations.append("Plan PCB capture")
    return operations


def _prepared_config_display_name(config_path: Path) -> str:
    try:
        config = load_config(config_path)
        segment = config.get("segment") or config.get("interface") or {}
        name = segment.get("name") or segment.get("interface")
        if name:
            return str(name)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    suffix = "_run_config"
    return config_path.stem[: -len(suffix)] if config_path.stem.endswith(suffix) else config_path.stem


def _record_payload(record_path: Path | None) -> dict[str, Any]:
    if record_path is None:
        return {}
    try:
        payload = load_config(record_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_log_dir(config_path: Path, requested: Path | None) -> Path:
    if requested is None:
        return (config_path.resolve().parent / "logs").resolve()
    if requested.is_absolute():
        return requested.resolve()
    return (Path.cwd() / requested).resolve()


def _result_artifacts(
    context: dict[str, Any],
    *,
    solve_record: dict[str, Any],
    transient_record: dict[str, Any],
    image_record: dict[str, Any],
    pcb_record: dict[str, Any],
    pcb_manifest_path: Path | None,
) -> list[tuple[str, Path]]:
    artifacts: list[tuple[str, Path]] = [
        ("Reference SIW", Path(context["reference"]["siw"])),
        ("Reference AEDB", Path(context["reference"]["aedb"])),
    ]
    run_dir = Path(context["workspace"]["runDir"])
    for path in sorted(run_dir.glob("*.siw")):
        artifacts.append(("SIWave project", path))
    for path in sorted(run_dir.glob("*.siwaveresults/*/*.siw")):
        artifacts.append(("SIWave result", path))
    for value in solve_record.get("exportedTouchstoneFiles") or []:
        artifacts.append(("Touchstone", Path(value)))
    for label, key in (
        ("AEDT project", "projectPath"),
        ("AEDT TDR image", "reportImagePath"),
        ("AEDT schematic image", "schematicImagePath"),
    ):
        if transient_record.get(key):
            artifacts.append((label, Path(transient_record[key])))
    for label, key in (
        ("TDR waveform image", "runImagePath"),
        ("TDR waveform CSV", "waveformCsvPath"),
    ):
        if image_record.get(key):
            artifacts.append((label, Path(image_record[key])))
    for group_label, key in (
        ("TDR marker", "minMaxMarkers"),
        ("TDR endpoint", "endpointAnnotations"),
    ):
        group = image_record.get(key) or {}
        for suffix, path_key in (("JSON", "jsonPath"), ("CSV", "csvPath")):
            if group.get(path_key):
                artifacts.append((f"{group_label} {suffix}", Path(group[path_key])))
    if pcb_manifest_path is not None:
        package_dir = pcb_manifest_path.resolve().parent
        for entry in (pcb_record.get("overviewCaptures") or []) + (
            pcb_record.get("captures") or []
        ):
            image = entry.get("image")
            if image:
                artifacts.append(("PCB image", package_dir / image))

    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in artifacts:
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append((label, path))
    return unique


def _run_prepared_config(
    args: argparse.Namespace,
    config_path: Path,
    *,
    generated_run_dir: Path | None,
    progress: ConsoleProgress | None = None,
) -> int:
    progress = progress or ConsoleProgress()
    ensure_exists(config_path, "config")
    preprocessing_result = None
    selected_config = load_config(config_path)
    if (
        not args.generate_config
        and "preprocessingMode" in selected_config
        and "inputProvenance" not in selected_config
    ):
        try:
            with progress.stage(
                "REFERENCE",
                "Preprocess design reference",
                detail="Convert Zuken/ANF input into the SIWave SIW and AEDB reference pair.",
            ) as stage:
                preprocessing_result = run_reference_preprocessor(
                    config_path,
                    work_dir=WORK_DIR / "reference_preprocess",
                    runtime_root=ROOT_DIR,
                )
                preprocess_manifest = _record_payload(
                    preprocessing_result.manifest_path
                )
                stage.complete(
                    f"status=ready, "
                    f"attempt={preprocess_manifest.get('attempt', 'unknown')}"
                )
        except ReferencePreprocessError as exc:
            progress.error("REFERENCE", f"Reference preprocessing stopped: {exc}")
            return 2
        progress.detail("Reference manifest", preprocessing_result.manifest_path)
    with progress.stage(
        "PREPARE",
        "Prepare run context",
        detail="Validate Config, input provenance, TDR time range, and output directories.",
    ) as stage:
        context = build_run_context(
            config_path,
            run_dir=generated_run_dir,
            output_dir=generated_run_dir,
            preprocessing_result=preprocessing_result,
        )
        context_path = write_context(context)
        stage.complete(f"run={context['workspace']['runDir']}")
    ports_record_path = None
    syz_record_path = None
    solve_record_path = None
    transient_record_path = None
    image_record_path = None
    pcb_capture_manifest_path = None
    pcb_capture_status = None

    if args.apply_ports or args.setup_syz or args.solve_touchstone or args.run_tdr:
        with progress.stage(
            "PORTS",
            "Apply series models and create ports",
            detail="Apply models and SIWave ports to a clean run copy of the reference AEDB.",
        ) as stage:
            ports_record_path = apply_ports(context)
            record = _record_payload(ports_record_path)
            stage.complete(
                f"status={record.get('status', 'unknown')}, "
                f"ports={record.get('portCount', 'unknown')}"
            )
    if args.setup_syz or args.solve_touchstone or args.run_tdr:
        with progress.stage(
            "SYZ-SETUP",
            "Configure SIWave SYZ setup",
            detail="Configure the frequency sweep and SIWave SYZ setup.",
        ) as stage:
            syz_record_path = setup_syz(context)
            record = _record_payload(syz_record_path)
            setup = record.get("setup") or {}
            stage.complete(
                f"status={record.get('status', 'unknown')}, "
                f"setup={setup.get('name', 'unknown')}"
            )
    if args.solve_touchstone or args.run_tdr:
        with progress.stage(
            "SYZ-SOLVE",
            "Solve SIWave sNp",
            detail="This stage can run for several minutes without solver log output.",
        ) as stage:
            solve_record_path = solve_touchstone(context)
            record = _record_payload(solve_record_path)
            exported = record.get("exportedTouchstoneFiles") or []
            result_name = Path(exported[-1]).name if exported else "not-exported"
            stage.complete(
                f"status={record.get('status', 'unknown')}, output={result_name}"
            )
    if args.run_tdr or args.tdr_only:
        with progress.stage(
            "TDR-SOLVE",
            "Solve AEDT Circuit TDR",
            detail="Connect the sNp model in Circuit and solve the transient TDR response.",
        ) as stage:
            transient_record_path = run_tdr(context)
            record = _record_payload(transient_record_path)
            trace_names = record.get("traceNames") or []
            stage.complete(
                f"status={record.get('status', 'unknown')}, "
                f"traces={len(trace_names)}, samples={record.get('sampleCount', 'unknown')}"
            )
    if args.export_tdr_image or args.run_tdr or args.tdr_only:
        with progress.stage(
            "TDR-RESULT",
            "Generate TDR chart and CSV results",
            detail="Apply the view range, reference impedance, and Near/Far annotations.",
        ) as stage:
            image_record_path = export_tdr_waveform_image(context)
            record = _record_payload(image_record_path)
            image_path = record.get("runImagePath") or record.get("imagePath")
            stage.complete(
                f"status={record.get('status', 'unknown')}, "
                f"image={Path(image_path).name if image_path else 'unknown'}"
            )
    if args.plan_pcb_capture or args.capture_pcb_routes:
        if __package__:
            from .pcb_capture import run_pcb_capture
        else:
            from pcb_capture import run_pcb_capture

        pcb_label = "Capture PCB images" if args.capture_pcb_routes else "Plan PCB capture"
        with progress.stage(
            "PCB-CAPTURE",
            pcb_label,
            detail="Prepare PCB Top/Bottom and selected-channel route images.",
        ) as stage:
            pcb_capture_manifest_path = run_pcb_capture(
                context,
                plan_only=not args.capture_pcb_routes,
            )
            record = _record_payload(pcb_capture_manifest_path)
            pcb_capture_status = record.get("status")
            expected_status = (
                pcb_capture_status in {"ok", "ok-with-fallback"}
                if args.capture_pcb_routes
                else pcb_capture_status == "planned"
            )
            summary = (
                f"status={pcb_capture_status}, "
                f"overview={len(record.get('overviewCaptures') or [])}, "
                f"channels={len(record.get('captures') or [])}, "
                f"unresolved={len(record.get('unresolved') or [])}"
            )
            if expected_status:
                stage.complete(summary)
            else:
                stage.fail(summary)

    progress.info(
        "RESULT",
        f"Analysis batch completed: {context['workspace'].get('runName', context['segment'].get('name', 'unknown'))}",
    )
    progress.detail("Config", config_path)
    progress.detail("Reference SIW", context["reference"]["siw"])
    progress.detail("Reference AEDB", context["reference"]["aedb"])
    progress.detail("Result directory", context["workspace"]["runDir"])
    solve_record = _record_payload(solve_record_path)
    transient_record = _record_payload(transient_record_path)
    image_record = _record_payload(image_record_path)
    pcb_record = _record_payload(pcb_capture_manifest_path)
    progress.info("ARTIFACT", "Generated output files")
    for label, path in _result_artifacts(
        context,
        solve_record=solve_record,
        transient_record=transient_record,
        image_record=image_record,
        pcb_record=pcb_record,
        pcb_manifest_path=pcb_capture_manifest_path,
    ):
        progress.artifact(label, path)
    progress.detail("Context", context_path)
    if ports_record_path is not None:
        progress.detail("Port record", ports_record_path)
    if syz_record_path is not None:
        progress.detail("SYZ setup record", syz_record_path)
    if solve_record_path is not None:
        progress.detail("sNp solve record", solve_record_path)
    if transient_record_path is not None:
        progress.detail("TDR solve record", transient_record_path)
    if image_record_path is not None:
        progress.detail("TDR result record", image_record_path)
    if pcb_capture_manifest_path is not None:
        progress.detail("PCB capture record", pcb_capture_manifest_path)
        progress.detail("PCB capture status", pcb_capture_status)
    if args.capture_pcb_routes and pcb_capture_status not in {"ok", "ok-with-fallback"}:
        return 2
    if args.plan_pcb_capture and not args.capture_pcb_routes and pcb_capture_status != "planned":
        return 2
    return 0


@dataclass(frozen=True)
class RequestExecutionOutcome:
    status: int
    batches: tuple[BatchExecutionInput, ...]
    result_dirs: tuple[Path, ...]
    generation_manifest: Path | None = None
    error: str | None = None


def _execute_request(
    args: argparse.Namespace,
    config_path: Path,
    progress: ConsoleProgress,
) -> RequestExecutionOutcome:
    prepared_configs: list[tuple[Path, Path | None]] = [(config_path, None)]
    generation_manifest: Path | None = None
    batch_records: list[BatchExecutionInput] = []
    result_dirs: list[Path] = []
    if args.generate_config:
        if __package__:
            from .config_generation import ConfigGenerationError, prepare_from_request
        else:
            from config_generation import ConfigGenerationError, prepare_from_request

        generation_output_root = _generation_output_root(config_path)
        try:
            with progress.stage(
                "CONFIG",
                "Preprocess design and generate run Configs",
                detail="Validate the detailed CSV and generate one run Config per analysis batch.",
            ) as stage:
                generation = prepare_from_request(
                    config_path,
                    runtime_root=ROOT_DIR,
                    work_root=generation_output_root,
                    progress=progress,
                )
                if generation.status == 0:
                    configs = generation.run_configs or (generation.run_config,)
                    stage.complete(f"batches={len(configs)}, unresolved=0")
                else:
                    stage.fail(
                        f"status={generation.status}, unresolved={generation.unresolved}"
                    )
        except ConfigGenerationError as exc:
            progress.error("CONFIG", f"Config generation stopped: {exc}")
            return RequestExecutionOutcome(
                status=2,
                batches=(),
                result_dirs=(),
                error=str(exc),
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            progress.error("CONFIG", "Unexpected failure; see the full log.")
            return RequestExecutionOutcome(
                status=1,
                batches=(),
                result_dirs=(),
                error=str(exc).strip() or type(exc).__name__,
            )
        generation_manifest = generation.batch_manifest or generation.manifest
        if generation.status != 0:
            progress.error("CONFIG", f"Unresolved items: {generation.unresolved}")
            return RequestExecutionOutcome(
                status=generation.status,
                batches=(),
                result_dirs=(),
                generation_manifest=generation_manifest,
                error=f"unresolved items: {generation.unresolved}",
            )

        generated_configs = generation.run_configs or (generation.run_config,)
        prepared_configs = []
        for generated_config in generated_configs:
            resolved_config = generated_config.resolve()
            generated_run_dir = (
                resolved_config.parent.parent
                if len(generated_configs) > 1
                else generation.output_dir.resolve().parent
            )
            prepared_configs.append((resolved_config, generated_run_dir))
            progress.detail("Generated Config", resolved_config)
        if generation.batch_manifest is not None:
            progress.detail("Batch manifest", generation.batch_manifest)

    try:
        for index, (prepared_config, generated_run_dir) in enumerate(
            prepared_configs,
            start=1,
        ):
            progress.batch(
                index,
                len(prepared_configs),
                _prepared_config_display_name(prepared_config),
            )
            try:
                status = _run_prepared_config(
                    args,
                    prepared_config,
                    generated_run_dir=generated_run_dir,
                    progress=progress,
                )
            except Exception as exc:
                batch_records.append(
                    BatchExecutionInput(
                        config_path=prepared_config,
                        run_dir=generated_run_dir,
                        status_code=1,
                        error=str(exc).strip() or type(exc).__name__,
                    )
                )
                raise
            batch_records.append(
                BatchExecutionInput(
                    config_path=prepared_config,
                    run_dir=generated_run_dir,
                    status_code=status,
                    error=(f"batch stopped with exit code {status}" if status else None),
                )
            )
            if generated_run_dir is not None and generated_run_dir not in result_dirs:
                result_dirs.append(generated_run_dir)
            if status != 0:
                return RequestExecutionOutcome(
                    status=status,
                    batches=tuple(batch_records),
                    result_dirs=tuple(result_dirs),
                    generation_manifest=generation_manifest,
                    error=f"batch stopped with exit code {status}",
                )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        progress.error("RUN", "Unexpected failure; see the full log.")
        return RequestExecutionOutcome(
            status=1,
            batches=tuple(batch_records),
            result_dirs=tuple(result_dirs),
            generation_manifest=generation_manifest,
            error=str(exc).strip() or type(exc).__name__,
        )
    return RequestExecutionOutcome(
        status=0,
        batches=tuple(batch_records),
        result_dirs=tuple(result_dirs),
        generation_manifest=generation_manifest,
    )


def main() -> int:
    args = parse_args()
    config_path = _resolve_config_input_path(args.config)
    ensure_exists(config_path, "config input")
    log_dir = _resolve_log_dir(config_path, getattr(args, "log_dir", None))
    started_at = datetime.now().astimezone()

    with ConsoleLogSession(log_dir) as log_session:
        assert log_session.progress_stream is not None
        progress = ConsoleProgress(stream=log_session.progress_stream)
        progress.start_run(
            config_path,
            _requested_operation_labels(args),
            full_log_path=log_session.full_log_path,
            progress_log_path=log_session.progress_log_path,
        )
        outcome = _execute_request(args, config_path, progress)
        status = outcome.status
        result_dirs = list(outcome.result_dirs)
        try:
            with progress.stage(
                "EDEN-RESULT",
                "Export EDEN Web JSON",
                detail=(
                    "Write DCIR-compatible Web filenames with SI-TDR Batch, channel, "
                    "sNp, TDR, PCB, and log references."
                ),
            ) as stage:
                exported = export_eden_web_results(
                    request_path=config_path,
                    output_dir=_generation_output_root(config_path),
                    runtime_root=ROOT_DIR,
                    batches=outcome.batches,
                    exit_code=status,
                    started_at=started_at,
                    completed_at=datetime.now().astimezone(),
                    requested_operations={
                        "generateConfig": bool(getattr(args, "generate_config", False)),
                        "applyPorts": bool(getattr(args, "apply_ports", False)),
                        "setupSyz": bool(getattr(args, "setup_syz", False)),
                        "solveTouchstone": bool(getattr(args, "solve_touchstone", False)),
                        "runTdr": bool(getattr(args, "run_tdr", False)),
                        "tdrOnly": bool(getattr(args, "tdr_only", False)),
                        "exportTdrImage": bool(getattr(args, "export_tdr_image", False)),
                        "planPcbCapture": bool(getattr(args, "plan_pcb_capture", False)),
                        "capturePcbRoutes": bool(getattr(args, "capture_pcb_routes", False)),
                    },
                    generation_manifest=outcome.generation_manifest,
                    full_log_path=log_session.full_log_path,
                    progress_log_path=log_session.progress_log_path,
                    error=outcome.error,
                )
                stage.complete(
                    f"status={'ok' if status == 0 else 'failed'}, files=5"
                )
            progress.artifact("EDEN result", exported.result)
            progress.detail("EDEN result detail", exported.result_detail)
            for result_dir in exported.result_dirs:
                if result_dir not in result_dirs:
                    result_dirs.append(result_dir)
            if exported.output_dir not in result_dirs:
                result_dirs.append(exported.output_dir)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            progress.error("EDEN-RESULT", f"Web JSON export failed: {exc}")
            if status == 0:
                status = 1
        progress.finish_run(status, result_dirs=result_dirs)
        return status


if __name__ == "__main__":
    raise SystemExit(main())
