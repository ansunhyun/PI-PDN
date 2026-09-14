from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Mapping, Sequence
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from .console_progress import ConsoleLogSession, ConsoleProgress, format_elapsed
except ImportError:  # Support direct execution from the SI_TDR folder.
    from console_progress import (  # type: ignore[no-redef]
        ConsoleLogSession,
        ConsoleProgress,
        format_elapsed,
    )

try:
    from .result_export import (
        BatchExecutionInput,
        export_eden_web_results,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from result_export import (  # type: ignore[no-redef]
        BatchExecutionInput,
        export_eden_web_results,
    )

try:
    from .fullbatch_publish import (
        DirectorySnapshotRecorder,
        FullBatchPublishError,
        ensure_publish_target_available,
        publish_fullbatch_results,
        prepare_public_reference_archive,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from fullbatch_publish import (  # type: ignore[no-redef]
        DirectorySnapshotRecorder,
        FullBatchPublishError,
        ensure_publish_target_available,
        publish_fullbatch_results,
        prepare_public_reference_archive,
    )

try:
    from .channel.time_range import (
        TimeRangeResolutionError,
        format_tdr_time_ps,
        resolve_run_config_time_range,
        write_time_range_resolution,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from channel.time_range import (  # type: ignore[no-redef]
        TimeRangeResolutionError,
        format_tdr_time_ps,
        resolve_run_config_time_range,
        write_time_range_resolution,
    )

try:
    from .channel.target_band import (
        resolve_reference_impedance,
        validate_tdr_impedance_config,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from channel.target_band import (  # type: ignore[no-redef]
        resolve_reference_impedance,
        validate_tdr_impedance_config,
    )

try:
    from .preprocess import (
        INPUT_PROVENANCE_KIND,
        ReferencePreprocessError,
        ReferencePreprocessResult,
        build_external_reference_preprocess_plan,
        load_reference_preprocess_manifest,
        run_external_reference_preprocessor,
        run_reference_preprocessor,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from preprocess import (  # type: ignore[no-redef]
        INPUT_PROVENANCE_KIND,
        ReferencePreprocessError,
        ReferencePreprocessResult,
        build_external_reference_preprocess_plan,
        load_reference_preprocess_manifest,
        run_external_reference_preprocessor,
        run_reference_preprocessor,
    )

try:
    from .job_inputs import (
        is_external_heaven_request,
        resolve_job_inputs,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from job_inputs import (  # type: ignore[no-redef]
        is_external_heaven_request,
        resolve_job_inputs,
    )

try:
    from .admin_config import validate_administrator_config
except ImportError:  # Support direct execution from the SI_TDR folder.
    from admin_config import validate_administrator_config  # type: ignore[no-redef]

try:
    from .syz_runtime import apply_strict_syz_options, solve_strict_syz
except ImportError:  # Support direct execution from the SI_TDR folder.
    from syz_runtime import (  # type: ignore[no-redef]
        apply_strict_syz_options,
        solve_strict_syz,
    )

try:
    from . import tdr_runtime as strict_tdr_runtime
except ImportError:  # Support direct execution from the SI_TDR folder.
    import tdr_runtime as strict_tdr_runtime  # type: ignore[no-redef]

try:
    from .channel.analysis_options import resolve_analysis_option_catalog
except ImportError:  # Support direct execution from the SI_TDR folder.
    from channel.analysis_options import (  # type: ignore[no-redef]
        resolve_analysis_option_catalog,
    )

try:
    from .channel.port_contract import (
        ENDPOINT_LAYER_POLICY,
        PORT_CONTRACT_SCHEMA,
        REFERENCE_TERMINAL_POLICY,
        ReferenceNetResolutionError,
        build_endpoint_terminal_contract,
        resolve_reference_net_candidate,
        validate_planned_port_contract,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from channel.port_contract import (  # type: ignore[no-redef]
        ENDPOINT_LAYER_POLICY,
        PORT_CONTRACT_SCHEMA,
        REFERENCE_TERMINAL_POLICY,
        ReferenceNetResolutionError,
        build_endpoint_terminal_contract,
        resolve_reference_net_candidate,
        validate_planned_port_contract,
    )

try:
    from .customer_pre_solve import (
        CUSTOMER_PRE_SOLVE_RECORD,
        component_source_evidence,
        create_customer_pre_solve_validation,
        is_strict_customer_context,
        port_source_evidence,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from customer_pre_solve import (  # type: ignore[no-redef]
        CUSTOMER_PRE_SOLVE_RECORD,
        component_source_evidence,
        create_customer_pre_solve_validation,
        is_strict_customer_context,
        port_source_evidence,
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


def administrator_config_path() -> Path:
    """Locate the maintained Config in the repository or compact customer package."""

    candidates = (
        ROOT_DIR / "config.json",
        CONFIG_DIR / "si-tdr.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "administrator Config not found; checked: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def preflight_request_json(request_path: Path) -> dict[str, Any]:
    """Validate the positional request file before any Ansys API is imported."""

    ensure_exists(request_path, "external request JSON")
    try:
        with request_path.open("r", encoding="utf-8-sig") as fp:
            payload = json.load(fp)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"external request JSON is not readable JSON: {request_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("external request JSON root must be an object")
    return payload


def _resolve_aedt_version(
    config: dict[str, Any],
    *,
    preprocessing_result: ReferencePreprocessResult | None = None,
) -> str:
    preprocessing = config.get("preprocessing") or {}
    provenance = config.get("inputProvenance") or {}
    result_version = (
        preprocessing_result.aedt_version
        if preprocessing_result is not None
        else None
    )
    top_level_version = config.get("aedtVersion")
    provenance_version = (
        provenance.get("aedtVersion") if isinstance(provenance, dict) else None
    )
    declared = [
        str(value).strip()
        for value in (result_version, top_level_version, provenance_version)
        if value is not None
    ]
    if len(set(declared)) > 1:
        raise ValueError(
            "aedtVersion differs between preprocessing, Run Config, and provenance: "
            + ", ".join(declared)
        )
    candidates = (
        result_version,
        top_level_version,
        provenance_version,
        DEFAULT_AEDT_VERSION,
    )
    value = next((candidate for candidate in candidates if candidate is not None), None)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("aedtVersion must be a non-empty scalar value")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("aedtVersion must be a non-empty scalar value")
    if (
        result_version is None
        and top_level_version is None
        and provenance_version is None
        and isinstance(preprocessing, dict)
        and "version" in preprocessing
    ):
        raise ValueError(
            "preprocessing.version is not an AEDT version fallback; use top-level aedtVersion"
        )
    return normalized


def _context_aedt_version(context: dict[str, Any]) -> str:
    value = context.get("aedtVersion") or DEFAULT_AEDT_VERSION
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("run context aedtVersion must be a non-empty scalar value")
    return normalized


def _open_edb(edb_class: Any, context: dict[str, Any], edb_path: Path) -> Any:
    """Single version boundary for every runtime PyEDB open."""

    return edb_class(
        edbpath=str(edb_path),
        edbversion=_context_aedt_version(context),
    )


def _open_circuit(
    circuit_class: Any,
    context: dict[str, Any],
    *,
    project_path: Path,
    non_graphical: bool = True,
    validate_session: bool = False,
    expected_grpc_api: bool | None = None,
    max_attempts: int = 1,
    retry_delay_seconds: float = 0.0,
) -> Any:
    """Single version boundary for every runtime PyAEDT Circuit session."""

    if max_attempts < 1:
        raise ValueError("Circuit launch max_attempts must be positive")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        app = None
        try:
            app = circuit_class(
                project=str(project_path),
                version=_context_aedt_version(context),
                non_graphical=non_graphical,
                new_desktop=True,
                close_on_exit=True,
            )
            if validate_session:
                missing = [
                    name
                    for name in ("_oproject", "_odesign", "desktop_class")
                    if getattr(app, name, None) is None
                ]
                if missing:
                    raise RuntimeError(
                        "AEDT Circuit returned an incomplete session; missing "
                        + ", ".join(missing)
                    )
                actual_grpc_api = getattr(app.desktop_class, "is_grpc_api", None)
                if (
                    expected_grpc_api is not None
                    and actual_grpc_api is not expected_grpc_api
                ):
                    raise RuntimeError(
                        "AEDT Circuit opened with an unexpected API mode: "
                        f"expected is_grpc_api={expected_grpc_api}, "
                        f"actual={actual_grpc_api}"
                    )
            return app
        except Exception as exc:
            last_error = exc
            if app is not None:
                try:
                    app.release_desktop(close_projects=True, close_desktop=True)
                except Exception:
                    pass
            if attempt < max_attempts and retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)

    raise RuntimeError(
        "AEDT Circuit session initialization failed after "
        f"{max_attempts} attempt(s): {last_error}"
    ) from last_error


def _force_circuit_com_api(aedt_settings: Any) -> None:
    """Match the DCIR Windows AEDT launcher by selecting COM/PythonNET."""

    aedt_settings.use_grpc_api = False


def _open_siwave_solver(
    solver_class: Any,
    context: dict[str, Any],
    *,
    aedb_path: Path,
) -> Any:
    """Single version boundary for the SIWave command wrapper."""

    return solver_class(
        aedb_path=str(aedb_path),
        aedt_version=_context_aedt_version(context),
    )


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def ensure_embedded_site_packages() -> None:
    import sys

    if DCIR_VENV_SITE_PACKAGES.exists():
        site_packages = str(DCIR_VENV_SITE_PACKAGES.resolve())
        if site_packages not in sys.path:
            # Compatibility environment is a fallback.  A customer venv with
            # correctly installed PyEDB/Pillow must keep precedence.
            sys.path.append(site_packages)


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

    # 9.4.7.1: strict Config는 생성 단계가 route 유도 stop을
    # tdr.transient.stopPs 명시값으로 설치한다.  명시값이 없으면 resolver가
    # missing_analysis_stop으로 fail-closed된다(내부 30ns 주입 제거).
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
        "customerComponentHandling": config.get("customerComponentHandling", {}),
        "analysisOptionSelection": config.get("analysisOptionSelection"),
        "tdrReport": config.get("tdrReport", {}),
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
    if "seriesTreatment" in config:
        context["seriesTreatment"] = config["seriesTreatment"]
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
        "customer_component_manifest.json",
        "ports_apply.json",
        "pre_solve_port_contract.json",
        CUSTOMER_PRE_SOLVE_RECORD,
        "syz_setup.json",
        "channel_solve.json",
        "tdr_transient.json",
        f"{context['segment'].get('name', 'SEGMENT')}.siw",
        f"{context['segment'].get('name', 'SEGMENT')}.siwz",
        f"{context['segment'].get('name', 'SEGMENT')}.exec",
    ]:
        path = run_dir / file_name
        if path.exists():
            path.unlink()

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
        pedb = _open_edb(Edb, context, target_edb)
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
        pedb = _open_edb(Edb, context, target_edb)
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
    """Apply strict customer components or legacy models before port creation.

    Customer Run Configs use ``customerComponentHandling`` (Series short and
    administrator Config-backed Array modeling). Internal compatibility Configs keep ``seriesModels``
    plus ``seriesTreatment``. A skipped record is written when neither exists.
    """
    import sys

    run_dir = Path(context["workspace"]["runDir"])
    series_config = context.get("seriesModels") or {}
    customer_config = context.get("customerComponentHandling") or {}
    record_path = run_dir / (
        "customer_component_manifest.json"
        if customer_config
        else "series_models_apply.json"
    )

    def write_record(payload: dict[str, Any]) -> Path:
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        return record_path

    if series_config and customer_config:
        raise ValueError(
            "customerComponentHandling and legacy seriesModels cannot be enabled together"
        )
    if customer_config:
        if "seriesTreatment" in context:
            raise ValueError(
                "customerComponentHandling does not accept legacy seriesTreatment"
            )
        allowed_customer_fields = {
            "schema",
            "policySource",
            "series",
            "array",
            "arrayPolicy",
            "forbiddenCustomerInputs",
            "bom",
            "bomSnapshot",
            "selector",
            "resolvedColumns",
            "arrayCatalog",
            "arrayRuleResolution",
            "channelPathReport",
            "jobRoot",
        }
        unknown_customer_fields = sorted(
            set(customer_config) - allowed_customer_fields
        )
        if unknown_customer_fields:
            raise ValueError(
                "customerComponentHandling has unsupported fields: "
                f"{unknown_customer_fields}"
            )
        if (
            customer_config.get("series") != "short"
            or customer_config.get("array")
            != "bom-configured-source-column-authoritative"
        ):
            raise ValueError(
                "customerComponentHandling must use fixed series=short and "
                "array=bom-configured-source-column-authoritative"
            )
        report_value = customer_config.get("channelPathReport") or (
            context.get("channelPath") or {}
        ).get("report")
        report_path = _resolve_config_relative_path(
            str(report_value or ""), "customerComponentHandling.channelPathReport"
        )
        array_catalog = customer_config.get("arrayCatalog")
        if not isinstance(array_catalog, list):
            raise ValueError("customerComponentHandling.arrayCatalog must be a list")
        try:
            from .channel.array_resistor_bom import (
                ArrayResistorBomError,
                validate_array_bom_snapshot,
            )
        except ImportError:  # Support direct execution from the SI_TDR folder.
            from channel.array_resistor_bom import (  # type: ignore[no-redef]
                ArrayResistorBomError,
                validate_array_bom_snapshot,
            )
        try:
            validate_array_bom_snapshot(
                customer_config.get("bomSnapshot"),
                job_root=Path(str(customer_config.get("jobRoot") or "")),
            )
        except ArrayResistorBomError as exc:
            write_record(
                {
                    "schema": customer_config.get("schema"),
                    "status": "error",
                    "mode": "customer-strict",
                    "batchId": str((context.get("segment") or {}).get("name") or ""),
                    "targetEdb": str(target_edb),
                    "channelPathReport": str(report_path),
                    "bomSnapshot": customer_config.get("bomSnapshot"),
                    "error": str(exc),
                    "readBackStatus": "not-started-bom-snapshot-invalid",
                }
            )
            raise RuntimeError(
                "authoritative Array BOM/PartList snapshot could not be verified"
            ) from exc
        with report_path.open("r", encoding="utf-8") as fp:
            report_payload = json.load(fp)

        try:
            from .channel.customer_components import (
                CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
                CustomerComponentReadbackError,
                apply_customer_path_components,
                read_back_customer_component_application,
            )
        except ImportError:  # Support direct execution from the SI_TDR folder.
            from channel.customer_components import (  # type: ignore[no-redef]
                CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
                CustomerComponentReadbackError,
                apply_customer_path_components,
                read_back_customer_component_application,
            )

        ensure_embedded_site_packages()
        from pyedb import Edb

        source_evidence = component_source_evidence(context)
        pedb = None
        result: dict[str, Any]
        try:
            pedb = _open_edb(Edb, context, target_edb)
            try:
                result = apply_customer_path_components(
                    pedb,
                    report_payload,
                    array_catalog=array_catalog,
                    batch_id=str((context.get("segment") or {}).get("name") or ""),
                )
            except (CustomerComponentReadbackError, ValueError) as exc:
                write_record(
                    {
                        "schema": CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
                        "status": "error",
                        "mode": "customer-strict",
                        "batchId": str((context.get("segment") or {}).get("name") or ""),
                        "targetEdb": str(target_edb),
                        "channelPathReport": str(report_path),
                        "bomSnapshot": customer_config.get("bomSnapshot"),
                        "sourceEvidence": source_evidence,
                        "error": str(exc),
                        "readBackStatus": "unsupported-or-unverified",
                    }
                )
                raise RuntimeError(
                    "customer Array/Series model application could not be verified"
                ) from exc
            pedb.save()
        finally:
            if pedb is not None:
                try:
                    pedb.close()
                except Exception:
                    pass
        pedb = None
        try:
            pedb = _open_edb(Edb, context, target_edb)
            read_back = read_back_customer_component_application(pedb, result)
        except CustomerComponentReadbackError as exc:
            write_record(
                {
                    "schema": result.get("schema"),
                    "status": "error",
                    "mode": "customer-strict",
                    "batchId": str((context.get("segment") or {}).get("name") or ""),
                    "targetEdb": str(target_edb),
                    "channelPathReport": str(report_path),
                    "bomSnapshot": customer_config.get("bomSnapshot"),
                    "sourceEvidence": source_evidence,
                    "error": str(exc),
                    "readBackStatus": "unsupported-or-unverified",
                }
            )
            raise RuntimeError(
                "saved-EDB customer component read-back could not be verified"
            ) from exc
        finally:
            if pedb is not None:
                try:
                    pedb.close()
                except Exception:
                    pass
        write_record(
            {
                "status": "ok",
                "mode": "customer-strict",
                "batchId": str((context.get("segment") or {}).get("name") or ""),
                "targetEdb": str(target_edb),
                "channelPathReport": str(report_path),
                "sourceEvidence": source_evidence,
                "bomSnapshot": customer_config.get("bomSnapshot"),
                "selector": customer_config.get("selector"),
                "resolvedColumns": customer_config.get("resolvedColumns"),
                "applyEvidence": {
                    "modelSetOperationCount": (
                        len(result.get("modifiedComponents") or [])
                        + len(result.get("modeledArrayComponents") or [])
                    ),
                    "edbReadBack": "verified-after-save-reopen",
                    "readBackCapabilityIdentity": read_back["capabilityIdentity"],
                },
                "readBack": read_back,
                **result,
            }
        )
        return record_path

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
        from .channel.series_models import apply_series_models, load_part_library
    except ImportError:  # Support direct execution from the SI_TDR folder.
        from channel.series_models import apply_series_models, load_part_library

    ensure_embedded_site_packages()
    from pyedb import Edb

    part_library = load_part_library(part_library_path)
    with report_path.open("r", encoding="utf-8") as fp:
        report_payload = json.load(fp)

    pedb = None
    try:
        pedb = _open_edb(Edb, context, target_edb)
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


def _validate_pre_solve_port_contract(context: dict[str, Any]) -> Path | None:
    """Persist intended port count/order/roles before any SYZ solver checkout."""

    ports_config = context.get("ports") or {}
    strict = bool(
        ports_config.get("portContractValidation") == "strict"
        or ports_config.get("roleMetadataVersion") is not None
    )
    if not strict:
        return None
    expected_order = [str(value) for value in ports_config.get("portOrder") or []]
    metadata_path = _resolve_metadata_path(context)
    with metadata_path.open("r", encoding="utf-8") as stream:
        metadata_payload = json.load(stream)
    record = validate_planned_port_contract(
        expected_order=expected_order,
        touchstone_port_count=int(
            ports_config.get("touchstonePortCount") or len(expected_order)
        ),
        metadata_payload=metadata_payload,
        tdr_channels=[
            channel
            for channel in (context.get("tdr") or {}).get("channels") or []
            if isinstance(channel, Mapping)
        ],
        require_role_metadata=True,
    )
    record.update(
        {
            "metadataPath": str(metadata_path),
            "portOrderPolicy": ports_config.get("portOrderPolicy"),
        }
    )
    if is_strict_customer_context(context):
        record.update(
            {
                "batchId": str((context.get("segment") or {}).get("name") or ""),
                "metadataEvidence": port_source_evidence(context)["metadata"],
            }
        )
    run_dir = Path(context["workspace"]["runDir"])
    record_path = run_dir / "pre_solve_port_contract.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    if record["issues"]:
        codes = [str(issue.get("code")) for issue in record["issues"]]
        raise RuntimeError(
            "Port Role/Touchstone order contract failed before SYZ solve: "
            + ", ".join(codes)
        )
    return record_path


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


def _pin_name(pin: object, raw_pin: object) -> str:
    value = getattr(pin, "component_pin", None)
    if value is not None and str(value).strip():
        return str(value).strip()
    getter = getattr(raw_pin, "GetName", None)
    if callable(getter):
        value = getter()
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("EDB component pin does not expose a pin name")


def _select_same_component_reference_pin(
    pedb,
    *,
    component_name: str,
    signal_pin: object,
    reference_net: str,
    layer_name: str,
) -> dict[str, Any]:
    """Select the closest real reference-net pin on the endpoint component/layer."""

    signal_raw = (
        getattr(signal_pin, "_edb_padstackinstance", None)
        or getattr(signal_pin, "_edb_object", None)
        or signal_pin
    )
    signal_position = pedb.components.get_pin_position(signal_raw)
    signal_xy = (float(signal_position[0]), float(signal_position[1]))
    candidates: list[dict[str, Any]] = []
    for pin in pedb.components.get_pin_from_component(component_name):
        raw_pin = (
            getattr(pin, "_edb_padstackinstance", None)
            or getattr(pin, "_edb_object", None)
            or pin
        )
        net_getter = getattr(raw_pin, "GetNet", None)
        raw_net = net_getter() if callable(net_getter) else None
        net_name_getter = getattr(raw_net, "GetName", None)
        candidate_net = (
            str(net_name_getter()).strip()
            if callable(net_name_getter)
            else str(getattr(pin, "net_name", "") or "").strip()
        )
        if candidate_net.casefold() != reference_net.casefold():
            continue
        try:
            ok, start_layer, stop_layer = raw_pin.GetLayerRange()
        except (AttributeError, TypeError, ValueError):
            continue
        if not ok or _edb_layer_name(start_layer) != layer_name:
            continue
        position = pedb.components.get_pin_position(raw_pin)
        position_xy = (float(position[0]), float(position[1]))
        distance = math.hypot(
            position_xy[0] - signal_xy[0], position_xy[1] - signal_xy[1]
        )
        candidates.append(
            {
                "pin": pin,
                "rawPin": raw_pin,
                "pinName": _pin_name(pin, raw_pin),
                "net": candidate_net,
                "layer": start_layer,
                "layerName": layer_name,
                "pinLayerRange": {
                    "start": _edb_layer_name(start_layer),
                    "stop": _edb_layer_name(stop_layer),
                },
                "position": {"x": str(position[0]), "y": str(position[1])},
                "distanceMeter": distance,
            }
        )
    if not candidates:
        raise ValueError(
            "same-component reference pin was not found on the endpoint layer: "
            f"component={component_name}, net={reference_net}, layer={layer_name}"
        )
    selected = min(
        candidates,
        key=lambda item: (
            float(item["distanceMeter"]),
            str(item["pinName"]).casefold(),
            str(item["pinName"]),
        ),
    )
    return {
        **selected,
        "policy": REFERENCE_TERMINAL_POLICY,
        "candidateCount": len(candidates),
    }


def _read_boolean_member(value: object, *names: str) -> bool:
    for name in names:
        member = getattr(value, name, None)
        if member is None:
            continue
        try:
            resolved = member() if callable(member) else member
        except Exception:
            continue
        if isinstance(resolved, bool):
            return resolved
    return False


def _edb_net_name(value: object, fallback: object | None = None) -> str:
    for name in ("name", "net_name", "GetName"):
        member = getattr(value, name, None)
        if member is None:
            continue
        try:
            resolved = member() if callable(member) else member
        except Exception:
            continue
        if resolved:
            return str(resolved)
    return str(fallback or "")


def _edb_reference_net_candidates(
    pedb: object,
    *,
    preferred_name: str | None = None,
) -> list[dict[str, Any]]:
    nets_api = getattr(pedb, "nets", None)
    candidates: dict[str, dict[str, Any]] = {}

    def add(
        name: str,
        net: object | None,
        *,
        is_power_ground: bool,
        evidence: str,
    ) -> None:
        normalized = name.strip()
        if not normalized:
            return
        key = normalized.casefold()
        item = candidates.setdefault(
            key,
            {
                "name": normalized,
                "exists": True,
                "isGround": False,
                "isPowerGround": False,
                "evidence": [],
            },
        )
        if net is not None:
            item["isGround"] = bool(
                item["isGround"]
                or _read_boolean_member(net, "is_ground", "isGround")
            )
            item["isPowerGround"] = bool(
                item["isPowerGround"]
                or _read_boolean_member(
                    net,
                    "is_power_ground",
                    "is_power_gnd",
                    "isPowerGround",
                )
            )
        item["isPowerGround"] = bool(item["isPowerGround"] or is_power_ground)
        if evidence not in item["evidence"]:
            item["evidence"].append(evidence)

    if nets_api is not None:
        for collection_name in ("nets", "instances"):
            collection = getattr(nets_api, collection_name, None)
            if isinstance(collection, Mapping):
                for key, net in collection.items():
                    add(
                        _edb_net_name(net, key),
                        net,
                        is_power_ground=False,
                        evidence=f"pedb.nets.{collection_name}",
                    )
        power_nets = getattr(nets_api, "power_nets", None)
        if isinstance(power_nets, Mapping):
            iterable = power_nets.items()
        elif isinstance(power_nets, (list, tuple, set)):
            iterable = ((value, value) for value in power_nets)
        else:
            iterable = ()
        for key, net in iterable:
            add(
                _edb_net_name(net, key),
                net,
                is_power_ground=True,
                evidence="pedb.nets.power_nets",
            )

        if preferred_name:
            get_net = getattr(nets_api, "get_net_by_name", None)
            if callable(get_net):
                try:
                    preferred = get_net(preferred_name)
                except Exception:
                    preferred = None
                if preferred:
                    add(
                        preferred_name,
                        preferred,
                        is_power_ground=False,
                        evidence="pedb.nets.get_net_by_name(configured hint)",
                    )
    return list(candidates.values())


def _resolve_customer_endpoint_port_contract(
    pedb: object,
    pin: object,
    *,
    component_name: str,
    pin_name: str,
    signal_net: str,
    preferred_reference_net: str | None,
) -> dict[str, Any]:
    layer_resolution = _resolve_padstack_positive_layer(pedb, pin)
    reference_selection = resolve_reference_net_candidate(
        _edb_reference_net_candidates(
            pedb,
            preferred_name=preferred_reference_net,
        ),
        preferred_name=preferred_reference_net,
        signal_net=signal_net,
    )
    return build_endpoint_terminal_contract(
        component=component_name,
        pin=pin_name,
        signal_net=signal_net,
        pin_layer=str(layer_resolution["name"]),
        pin_layer_evidence={
            "source": layer_resolution["source"],
            "pinLayerRange": layer_resolution["pinLayerRange"],
        },
        reference_selection=reference_selection,
    )


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
    customer_layer_contract = (
        context.get("ports", {}).get("terminalLayerPolicy")
        == ENDPOINT_LAYER_POLICY
    )
    configured_positive_layer = (
        None
        if customer_layer_contract
        else positive_params.get("layer")
        or context.get("ports", {}).get("positiveLayer")
    )
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

    pin_pos = pedb.components.get_pin_position(pin_instance)
    fallback_point = {"x": str(pin_pos[0]), "y": str(pin_pos[1])}
    terminal_contract: dict[str, Any] | None = None
    if customer_layer_contract:
        raw_reference = port_record.get("reference") or {}
        preferred_reference_net = str(
            raw_reference.get("net")
            or context.get("ports", {}).get("referenceNet")
            or context.get("nets", {}).get("reference")
            or ""
        ).strip() or None
        terminal_contract = _resolve_customer_endpoint_port_contract(
            pedb,
            pin,
            component_name=component_name,
            pin_name=pin_name,
            signal_net=net_name,
            preferred_reference_net=preferred_reference_net,
        )
        reference = raw_reference
        reference_params = reference.get("parameters") or {}
        reference_spec = {
            "name": str(reference.get("name") or f"{port_record['name']}_ref"),
            "net": terminal_contract["referenceNet"],
            "layer": positive_layer_name,
            "point": (reference_params.get("point") or {}) or fallback_point,
        }
    else:
        reference_spec = _metadata_reference_spec(
            context,
            port_record,
            fallback_point=fallback_point,
        )

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

    reference_pin_selection: dict[str, Any] | None = None
    if customer_layer_contract:
        reference_pin_selection = _select_same_component_reference_pin(
            pedb,
            component_name=component_name,
            signal_pin=pin,
            reference_net=reference_spec["net"],
            layer_name=positive_layer_name,
        )
        reference_terminal = api.cell.terminal.PadstackInstanceTerminal.Create(
            pedb.siwave._active_layout,
            reference_pin_selection["rawPin"].GetNet(),
            reference_spec["name"],
            reference_pin_selection["rawPin"],
            reference_pin_selection["layer"],
        )
    else:
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

    result = {
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
        "referencePoint": (
            reference_pin_selection["position"]
            if reference_pin_selection is not None
            else reference_spec["point"]
        ),
    }
    if terminal_contract is not None:
        result["terminalLayerContract"] = terminal_contract
        result["referenceNetSelection"] = terminal_contract[
            "referenceNetSelection"
        ]
        assert reference_pin_selection is not None
        result["referenceTerminalType"] = "PadstackInstanceTerminal"
        result["referenceComponent"] = component_name
        result["referencePin"] = reference_pin_selection["pinName"]
        result["referencePinSelection"] = {
            "policy": reference_pin_selection["policy"],
            "candidateCount": reference_pin_selection["candidateCount"],
            "component": component_name,
            "pin": reference_pin_selection["pinName"],
            "net": reference_pin_selection["net"],
            "layer": reference_pin_selection["layerName"],
            "pinLayerRange": reference_pin_selection["pinLayerRange"],
            "position": reference_pin_selection["position"],
            "distanceMeter": reference_pin_selection["distanceMeter"],
        }
    return result


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
    _validate_pre_solve_port_contract(context)
    strict_customer = is_strict_customer_context(context)
    strict_source_evidence = (
        port_source_evidence(context) if strict_customer else None
    )

    pedb = None
    created_ports: list[dict[str, Any]] = []
    try:
        pedb = _open_edb(Edb, context, target_edb)
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
                if strict_customer:
                    raise RuntimeError(
                        "strict customer Port recreation found an existing excitation "
                        f"after cleanup: {port_name}"
                    )
                created_ports.append({"name": port_name, "applyMode": "existing"})
                continue

            terminal_type = str((port_record.get("positive") or {}).get("terminalType") or "")
            try:
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
            except ReferenceNetResolutionError as exc:
                write_ports_apply_record(
                    {
                        "schema": PORT_CONTRACT_SCHEMA,
                        "status": "error",
                        "mode": "metadata-snp",
                        "batchId": str((context.get("segment") or {}).get("name") or ""),
                        "targetEdb": str(target_edb),
                        "metadataPath": str(metadata_path),
                        **(
                            {"sourceEvidence": strict_source_evidence}
                            if strict_source_evidence is not None
                            else {}
                        ),
                        "failedPort": port_name,
                        "createdPortsBeforeFailure": created_ports,
                        "referenceNetResolution": exc.evidence,
                    },
                    run_dir,
                )
                raise
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

    record_path = write_ports_apply_record(
        {
            "schema": PORT_CONTRACT_SCHEMA,
            "status": "ok",
            "mode": "metadata-snp",
            "batchId": str((context.get("segment") or {}).get("name") or ""),
            "targetEdb": str(target_edb),
            "sourceEdb": str(ref_edb),
            "metadataPath": str(metadata_path),
            **(
                {"sourceEvidence": strict_source_evidence}
                if strict_source_evidence is not None
                else {}
            ),
            "portCount": int(context.get("ports", {}).get("touchstonePortCount") or len(port_order)),
            "createdPorts": created_ports,
            "portOrder": [item["name"] for item in created_ports],
            "singleEndedImpedanceOhm": impedance,
        },
        run_dir,
    )
    if strict_customer:
        create_customer_pre_solve_validation(context, target_edb=target_edb)
    return record_path


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
        pedb = _open_edb(Edb, context, target_edb)
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
    if context.get("customerComponentHandling"):
        ensure_embedded_site_packages()
        run_dir = Path(context["workspace"]["runDir"])
        target_edb = run_dir / Path(context["reference"]["aedb"]).name
        return apply_strict_syz_options(context, target_edb=target_edb)

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
        pedb = _open_edb(Edb, context, target_edb)
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
    if context.get("customerComponentHandling"):
        return solve_strict_syz(context)
    _validate_pre_solve_port_contract(context)

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
        pedb = _open_edb(Edb, context, target_edb)
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

    solver = _open_siwave_solver(
        SiwaveSolve,
        context,
        aedb_path=target_edb,
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


def _optional_progress_stage(
    progress: ConsoleProgress | None,
    area: str,
    label: str,
    *,
    detail: str | None = None,
    depth: int = 2,
):
    if progress is None:
        return nullcontext()
    return progress.stage(
        area,
        label,
        detail=detail,
        depth=depth,
    )


def _complete_progress_stage(stage: Any, result: str) -> None:
    if stage is not None:
        stage.complete(result)


def _tdr_progress_event(
    progress: ConsoleProgress | None,
    area: str,
    state: str,
    message: str,
    *,
    depth: int = 3,
) -> None:
    if progress is not None:
        progress.event(area, state, message, depth=depth)


def _tdr_progress_item(
    progress: ConsoleProgress | None,
    area: str,
    index: int,
    total: int,
    state: str,
    message: str,
    *,
    depth: int = 3,
) -> None:
    if progress is not None:
        progress.item(
            area,
            index,
            total,
            state,
            message,
            depth=depth,
        )


def run_tdr(
    context: dict[str, Any],
    *,
    progress: ConsoleProgress | None = None,
) -> Path:
    strict_mode = strict_tdr_runtime.is_customer_strict(context)
    strict_record_path = Path(context["workspace"]["runDir"]) / "tdr_transient.json"
    try:
        if strict_mode:
            strict_tdr_runtime.validate_strict_topology_policy(context)
        touchstone_path = _resolved_touchstone_path(context)
        ensure_exists(touchstone_path, "touchstone")
        _validate_touchstone_port_contract(context, touchstone_path)

        if _is_manual_snp_multi_diff_tdr(context):
            return run_manual_snp_multi_diff_tdr(context, progress=progress)
        if _is_manual_s4p_diff_strategy(context):
            if strict_mode:
                raise strict_tdr_runtime.StrictTdrRuntimeError(
                    "customer strict TDR requires manual-snp-multi-diff topology"
                )
            return run_manual_s4p_diff_tdr(context)
        if strict_mode:
            raise strict_tdr_runtime.StrictTdrRuntimeError(
                "customer strict TDR requires manual-snp-multi-diff topology"
            )
    except Exception as exc:
        if strict_mode and not strict_record_path.exists():
            strict_record_path.parent.mkdir(parents=True, exist_ok=True)
            with strict_record_path.open("w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "status": "error",
                        "buildMode": "customer-strict-native-aedt-tdr",
                        "batchId": str((context.get("segment") or {}).get("name") or ""),
                        "errorType": type(exc).__name__,
                        "error": str(exc),
                    },
                    fp,
                    indent=2,
                    ensure_ascii=False,
                )
                fp.write("\n")
        raise

    ensure_embedded_site_packages()
    from ansys.aedt.core import Circuit

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")
    touchstone_path = _stage_circuit_touchstone(circuit_dir, touchstone_path)

    project_path = circuit_dir / f"{segment_name}_TDR.aedt"
    record_path = run_dir / "tdr_transient.json"
    schematic_image_path = circuit_dir / f"{segment_name}_schematic.jpg"

    port_names = _touchstone_port_names(touchstone_path)
    tx_probe_pins, tx_reference_pins, termination_pins = _tdr_probe_pin_config(context, port_names)
    differential_mode = context["tdr"].get("mode") == "differential"

    app = None
    try:
        app = _open_circuit(Circuit, context, project_path=project_path)

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
        touchstone_model_portable = _make_touchstone_models_project_relative(
            app,
            touchstone_path,
        )
        if not touchstone_model_portable:
            raise RuntimeError("Circuit Touchstone model could not use a project-relative path")

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
        archive_path = _archive_circuit_project(app, project_path)

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
            "archivePath": archive_path,
            "touchstoneModelPortable": touchstone_model_portable,
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

    from ansys.aedt.core import Circuit

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")
    touchstone_path = _stage_circuit_touchstone(circuit_dir, touchstone_path)

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
        app = _open_circuit(Circuit, context, project_path=project_path)

        design_name = f"{segment_name}_Transient_Manual"
        app.insert_design(design_name)
        app.modeler.schematic.schematic_units = "meter"

        sub = app.modeler.components.create_touchstone_component(
            str(touchstone_path),
            location=[0.0, 0.0],
            show_bitmap=False,
        )
        touchstone_model_portable = _set_touchstone_model_project_relative(
            sub,
            touchstone_path,
        )
        if not touchstone_model_portable:
            raise RuntimeError("Circuit Touchstone model could not use a project-relative path")
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
        # analyze 후 모델 정의를 다시 쓰면(설계 수정) solve 결과가 무효화되어
        # 저장 시 transient 데이터가 프로젝트에 남지 않는다. refresh는 solve 전에만 한다.
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
        archive_path = _archive_circuit_project(app, project_path)

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
            "archivePath": archive_path,
            "touchstoneModelPortable": touchstone_model_portable,
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


def _stage_circuit_touchstone(circuit_dir: Path, source_path: Path) -> Path:
    """Copy an sNp beside the Circuit project so the whole folder is portable."""
    source = source_path.resolve()
    touchstone_dir = circuit_dir / "touchstone"
    touchstone_dir.mkdir(parents=True, exist_ok=True)
    target = touchstone_dir / source.name
    if source != target.resolve():
        shutil.copy2(source, target)
    return target


def _set_touchstone_model_project_relative(component, touchstone_path: Path) -> bool:
    """Point a Circuit Touchstone model at its project-relative copy."""
    try:
        model_data = component.model_data
        if not model_data or "filename" not in model_data.props:
            return False
        model_data.props["filename"] = (
            f"$PROJECTDIR\\touchstone\\{touchstone_path.name}"
        )
        return bool(model_data.update())
    except Exception:
        return False


def _make_touchstone_models_project_relative(app, touchstone_path: Path) -> bool:
    updated = False
    try:
        components = app.modeler.components.components.values()
    except Exception:
        return False
    for component in components:
        try:
            component_path = str(component.component_path or "")
        except Exception:
            continue
        if Path(component_path.replace("\\", "/")).name != touchstone_path.name:
            continue
        updated = (
            _set_touchstone_model_project_relative(component, touchstone_path)
            or updated
        )
    return updated


def _archive_circuit_project(app, project_path: Path) -> str:
    """Create a portable AEDTZ containing the solved results and external sNp."""
    archive_path = project_path.with_suffix(".aedtz")
    try:
        if archive_path.exists():
            archive_path.unlink()
        archived = bool(
            app.archive_project(
                project_path=str(archive_path),
                include_external_files=True,
                include_results_file=True,
                notes="SI-TDR portable Circuit project with solved TDR results",
            )
        )
    except Exception as exc:
        raise RuntimeError(f"Circuit project archive failed: {archive_path}") from exc
    if not archived or not archive_path.exists() or archive_path.stat().st_size <= 0:
        raise RuntimeError(f"Circuit project archive is missing or empty: {archive_path}")
    return str(archive_path)


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
    angle: int | None = None,
) -> object:
    pin_x, pin_y = _pin_location(pin)
    if angle is None:
        # AEDT page-port symbols extend away from their connection point at
        # 180 degrees on the right and 0 degrees on the left.  Deriving the
        # orientation from the lead direction keeps the symbol and its label
        # off the wire instead of laying them back across it.
        angle = 180 if dx > 0.0 else 0
    page_port = app.modeler.schematic.create_page_port(page_name, [pin_x + dx, pin_y + dy], angle=angle)
    _connect_pins_with_wire(app, pin, page_port.pins[0])
    return page_port


def _create_snp_page_ports(app, sub_pins: dict[str, object], channels: list[dict[str, Any]]) -> list[dict[str, object]]:
    used_ports = []
    seen: set[str] = set()
    pin_x_values = [_pin_location(pin)[0] for pin in sub_pins.values()]
    snp_center_x = (min(pin_x_values) + max(pin_x_values)) / 2.0
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
            dx = -0.00635 if pin_x <= snp_center_x else 0.00635
            page_port = _create_named_page_port_at_pin(app, pin, port_name, dx=dx)
            used_ports.append({"port": port_name, "pagePort": page_port.name, "pin": pin.name})
    return used_ports


def _place_far_end_page_block(
    app,
    *,
    page_name: str,
    resistor_name: str,
    location: list[float],
    page_x: float,
) -> dict[str, object]:
    resistor = app.modeler.schematic.create_resistor(name=resistor_name, value="1g", location=location, angle=0)
    resistor_pins = sorted(resistor.pins, key=lambda pin: (float(pin.location[0]), float(pin.location[1])))
    signal_pin = resistor_pins[0]
    ground_pin = resistor_pins[-1]
    signal_x, _signal_y = _pin_location(signal_pin)
    page_dx = page_x - signal_x
    page_port = _create_named_page_port_at_pin(
        app,
        signal_pin,
        page_name,
        dx=page_dx,
    )
    return {
        "resistor": resistor.name,
        "signalPin": signal_pin,
        "groundPin": ground_pin,
        "pagePort": page_port.name,
        "pageName": page_name,
    }


def _multi_diff_layout(context: dict[str, Any]) -> dict[str, float]:
    configured = (context.get("tdr", {}).get("layout") or {}).get("manualSnpMultiDiff") or {}
    far_bridge_half_pin_span = 0.00508
    near_x = float(configured.get("nearX", -0.090))
    far_x = float(configured.get("farX", 0.125))
    layout = {
        "snpX": float(configured.get("snpX", (near_x + far_x) / 2.0)),
        "nearX": near_x,
        "farX": far_x,
        "yStart": float(configured.get("yStart", 0.085)),
        "yStep": float(configured.get("yStep", -0.022)),
        "groupGap": float(configured.get("groupGap", 0.0127)),
        "pageOffset": float(configured.get("pageOffset", 0.01016)),
        "pairDy": float(configured.get("pairDy", far_bridge_half_pin_span)),
        "tdrPageDy": float(configured.get("tdrPageDy", 0.0)),
        "farResistorDx": float(configured.get("farResistorDx", 0.018)),
        "headingGap": float(configured.get("headingGap", 0.02032)),
        "channelLabelDx": float(configured.get("channelLabelDx", 0.03048)),
    }
    if layout["yStep"] == 0.0:
        raise ValueError("tdr.layout.manualSnpMultiDiff.yStep must be non-zero")
    for key in (
        "groupGap",
        "pageOffset",
        "pairDy",
        "farResistorDx",
        "headingGap",
        "channelLabelDx",
    ):
        if layout[key] < 0.0:
            raise ValueError(f"tdr.layout.manualSnpMultiDiff.{key} must be non-negative")
    # This is a graphical clearance constraint, not an electrical setting.
    # Normalize legacy compact layouts upward so existing run contexts become
    # readable without requiring a configuration migration.
    layout["pairDy"] = max(layout["pairDy"], far_bridge_half_pin_span)
    return layout


def _multi_diff_channel_rows(
    context: dict[str, Any],
    channels: list[dict[str, Any]],
    layout: dict[str, float],
) -> list[dict[str, object]]:
    group_by_channel: dict[str, str] = {}
    for group in context.get("tdr", {}).get("reportGroups") or []:
        group_name = str(group.get("name") or "").strip()
        for channel_name in group.get("channels") or []:
            channel_key = str(channel_name)
            previous = group_by_channel.get(channel_key)
            if previous is not None and previous != group_name:
                raise ValueError(
                    f"TDR channel {channel_key} belongs to multiple report groups"
                )
            group_by_channel[channel_key] = group_name

    direction = -1.0 if layout["yStep"] < 0.0 else 1.0
    rows: list[dict[str, object]] = []
    previous_group: str | None = None
    y = layout["yStart"]
    for index, channel in enumerate(channels):
        channel_name = str(channel["name"])
        group_name = str(
            channel.get("reportGroup") or group_by_channel.get(channel_name) or ""
        ).strip()
        starts_group = bool(
            index > 0
            and group_name
            and previous_group
            and group_name != previous_group
        )
        if index > 0:
            y += layout["yStep"]
        if starts_group:
            y += direction * layout["groupGap"]
        rows.append(
            {
                "index": index,
                "channel": channel_name,
                "displayName": str(channel.get("displayName") or channel_name),
                "reportGroup": group_name or None,
                "startsGroup": starts_group,
                "y": y,
            }
        )
        previous_group = group_name or previous_group
    return rows


def _create_multi_diff_layout_labels(
    app,
    *,
    channel_rows: list[dict[str, object]],
    layout: dict[str, float],
) -> list[dict[str, object]]:
    if not channel_rows:
        return []
    heading_y = max(float(row["y"]) for row in channel_rows) + layout["headingGap"]
    label_specs = [
        ("TDR SOURCE / NEAR", layout["nearX"] - layout["channelLabelDx"], heading_y),
        ("sNp CHANNEL", layout["snpX"] - 0.01524, heading_y),
        ("FAR TERMINATION", layout["farX"] - layout["pageOffset"], heading_y),
    ]
    label_specs.extend(
        (
            str(row["displayName"]),
            layout["nearX"] - layout["channelLabelDx"],
            float(row["y"]) + layout["pairDy"],
        )
        for row in channel_rows
    )

    records: list[dict[str, object]] = []
    for text, x, y in label_specs:
        created = app.modeler.create_text(text, x_origin=x, y_origin=y, text_size=12)
        if not created:
            raise RuntimeError(f"AEDT could not create schematic layout label: {text}")
        records.append({"text": text, "x": x, "y": y})
    return records


def _place_diff_channel_tdr_block(
    app,
    *,
    channel: dict[str, Any],
    row_y: float,
    layout: dict[str, float],
    context: dict[str, Any],
    strict_group: Any | None = None,
) -> dict[str, object]:
    y = row_y
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
    if strict_group is not None:
        # The strict differential probe consumes the Spec differential target
        # directly.  It must not pass through the legacy single-ended doubling
        # rule because that would turn a 100-ohm target into 200 ohms.
        z0_ohm = float(strict_group.reference_impedance_ohm)
        rise_time_ps = float(strict_group.settings["riseTimePs"])
        pulse_repetition = str(strict_group.settings["pulseRepetition"])
        pulse_width = str(strict_group.settings["pulseWidth"])
        time_delay = str(strict_group.settings["timeDelay"])
    else:
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
        dx=layout["pageOffset"],
        dy=layout["tdrPageDy"],
    )
    near_neg_page = _create_named_page_port_at_pin(
        app,
        tdr_probe.pins[1],
        near_negative,
        dx=layout["pageOffset"],
        dy=-layout["tdrPageDy"],
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
        "targetImpedanceOhmRole": (
            "Spec" if strict_group is not None else "legacy-reference-alias"
        ),
        "referenceImpedanceOhmSource": (
            "Spec.referenceImpedanceOhm" if strict_group is not None else "legacy"
        ),
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
    row_y: float,
    layout: dict[str, float],
    context: dict[str, Any],
    diff_bridge_ohm: float | None = None,
    strict_group: Any | None = None,
) -> dict[str, object]:
    y = row_y
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
        page_x=layout["farX"] - layout["pageOffset"],
    )
    far_neg = _place_far_end_page_block(
        app,
        page_name=far_negative,
        resistor_name=f"R_{channel_name}_FAR_NEG",
        location=[resistor_x, y - pair_dy],
        page_x=layout["farX"] - layout["pageOffset"],
    )

    ground_pins = [far_pos["groundPin"], far_neg["groundPin"]]
    ground_locations = [_pin_location(pin) for pin in ground_pins]
    ground_trunk_x = max(x for x, _y in ground_locations) + 0.00508
    ground_anchor_y = min(y_pos for _x, y_pos in ground_locations) - 0.00254
    shared_ground = _place_shared_far_end_ground(
        app,
        ground_pins,
        trunk_x=ground_trunk_x,
        ground_y=ground_anchor_y,
        ground_component_dy=-0.00254,
    )

    if strict_group is not None and diff_bridge_ohm is not None:
        raise strict_tdr_runtime.StrictTdrRuntimeError(
            "strict far-end termination cannot use a legacy differentialBridgeOhm"
        )
    termination_ohm = (
        float(strict_group.reference_impedance_ohm)
        if strict_group is not None
        else diff_bridge_ohm
    )
    far_end_diff_bridge = None
    if termination_ohm is not None:
        far_end_diff_bridge = _place_far_end_diff_bridge(
            app,
            far_pos["signalPin"],
            far_neg["signalPin"],
            name=f"R_{channel_name}_FAR_DIFF",
            value_ohm=termination_ohm,
            location=[layout["farX"], y],
            route_x=layout["farX"],
        )

    record = {
        "channel": channel_name,
        "farEndResistors": [far_pos["resistor"], far_neg["resistor"]],
        "farEndDiffBridge": far_end_diff_bridge,
        "farEndGrounds": [shared_ground],
        "farEndPagePorts": [far_pos["pagePort"], far_neg["pagePort"]],
        "farPorts": [far_positive, far_negative],
        "directionProvenance": direction_contract,
    }
    if strict_group is not None:
        record["terminationValueOhm"] = termination_ohm
        record["terminationSource"] = "Spec.referenceImpedanceOhm"
    else:
        record["differentialBridgeOhm"] = diff_bridge_ohm
    return record


def run_manual_snp_multi_diff_tdr(
    context: dict[str, Any],
    *,
    progress: ConsoleProgress | None = None,
) -> Path:
    ensure_embedded_site_packages()
    from ansys.aedt.core import Circuit
    from ansys.aedt.core.generic.settings import settings as aedt_settings

    run_dir = Path(context["workspace"]["runDir"])
    circuit_dir = Path(context["workspace"]["circuitDir"])
    circuit_dir.mkdir(parents=True, exist_ok=True)
    segment_name = context["segment"].get("name", "SEGMENT")
    touchstone_path = _resolved_touchstone_path(context)
    ensure_exists(touchstone_path, "touchstone")
    strict_mode = strict_tdr_runtime.is_customer_strict(context)
    if strict_mode:
        _force_circuit_com_api(aedt_settings)
    strict_contract = (
        strict_tdr_runtime.prepare_strict_tdr_contract(context, touchstone_path)
        if strict_mode
        else None
    )
    touchstone_path = _stage_circuit_touchstone(circuit_dir, touchstone_path)

    if strict_mode:
        project_path = circuit_dir / f"{segment_name}.aedt"
        strict_targets = [
            project_path,
            project_path.with_suffix(".aedtz"),
            circuit_dir / "Tdr_waveform.csv",
            *[
                circuit_dir / f"{TDR_REPORT_IMAGE_PREFIX}{group.name}.jpg"
                for group in strict_contract.groups
            ],
        ]
        strict_tdr_runtime.ensure_fresh_targets(strict_targets)
        results_path = project_path.with_suffix(".aedtresults")
        if results_path.exists():
            raise strict_tdr_runtime.StrictTdrRuntimeError(
                f"strict TDR will not reuse stale AEDT results: {results_path}"
            )
    else:
        project_path = _cleanup_circuit_project(circuit_dir, segment_name)
    record_path = run_dir / "tdr_transient.json"
    design_name = (
        strict_tdr_runtime.STRICT_TDR_DESIGN_NAME
        if strict_mode
        else f"{segment_name}_Transient_Manual"
    )
    nexxim_path_budget = None

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
    active_manual_progress_area: str | None = None
    try:
        if strict_mode:
            with _optional_progress_stage(
                progress,
                "TDR-PATH",
                "Check the projected Nexxim output path budget",
                detail=(
                    f"project={project_path}, design={design_name}, "
                    f"warning>={strict_tdr_runtime.WINDOWS_AEDT_PATH_WARNING_THRESHOLD}, "
                    f"limit={strict_tdr_runtime.WINDOWS_AEDT_LEGACY_PATH_LIMIT}"
                ),
            ) as progress_stage:
                nexxim_path_budget = (
                    strict_tdr_runtime.validate_nexxim_output_path_budget(
                        project_path,
                        design_name=design_name,
                        port_count=len(strict_contract.expected_port_order),
                    )
                )
                artifacts = nexxim_path_budget["artifacts"]
                for index, (artifact_name, artifact) in enumerate(
                    artifacts.items(),
                    start=1,
                ):
                    _tdr_progress_item(
                        progress,
                        "TDR-PATH",
                        index,
                        len(artifacts),
                        (
                            "WARN"
                            if artifact["length"]
                            >= nexxim_path_budget["warningThreshold"]
                            else "DONE"
                        ),
                        f"{artifact_name}={artifact['length']} chars, "
                        f"path={artifact['path']}",
                    )
                if nexxim_path_budget["status"] == "warning":
                    _tdr_progress_event(
                        progress,
                        "TDR-PATH",
                        "WARN",
                        "Projected Nexxim path is close to the Windows legacy "
                        f"limit: {nexxim_path_budget['longestLength']}/"
                        f"{nexxim_path_budget['limit']} chars, "
                        f"remaining={nexxim_path_budget['remainingCharacters']}, "
                        f"artifact={nexxim_path_budget['longestArtifact']}",
                    )
                _complete_progress_stage(
                    progress_stage,
                    f"status={nexxim_path_budget['status']}, "
                    f"longest={nexxim_path_budget['longestArtifact']}:"
                    f"{nexxim_path_budget['longestLength']}/"
                    f"{nexxim_path_budget['limit']}, "
                    f"remaining={nexxim_path_budget['remainingCharacters']}",
                )

        with _optional_progress_stage(
            progress,
            "TDR-SESSION",
            "Launch and validate the AEDT Circuit session",
            detail=(
                f"AEDT {_context_aedt_version(context)}, "
                f"api={'COM/PythonNET' if strict_mode else 'configured-default'}"
            ),
        ) as progress_stage:
            app = _open_circuit(
                Circuit,
                context,
                project_path=project_path,
                # PyAEDT documents native Cartesian X Marker creation as graphical-only.
                non_graphical=not strict_mode,
                validate_session=strict_mode,
                expected_grpc_api=False if strict_mode else None,
                max_attempts=3 if strict_mode else 1,
                retry_delay_seconds=10.0 if strict_mode else 0.0,
            )
            actual_grpc_api = getattr(app.desktop_class, "is_grpc_api", None)
            _complete_progress_stage(
                progress_stage,
                f"project={app.project_name}, isGrpcApi={actual_grpc_api}",
            )

        with _optional_progress_stage(
            progress,
            "TDR-PROJECT",
            "Create the Circuit project and transient design",
            detail=f"project={project_path}, design={design_name}",
        ) as progress_stage:
            app.insert_design(design_name)
            app.modeler.schematic.schematic_units = "meter"
            _complete_progress_stage(
                progress_stage,
                f"project={app.project_name}, design={app.design_name}",
            )

        layout = _multi_diff_layout(context)
        channel_rows = _multi_diff_channel_rows(context, channels, layout)
        snp_y = (
            (float(channel_rows[0]["y"]) + float(channel_rows[-1]["y"])) / 2.0
            if channel_rows
            else 0.0
        )
        with _optional_progress_stage(
            progress,
            "TDR-SNP",
            "Import Touchstone and validate the Circuit port order",
            detail=(
                f"file={touchstone_path.name}, expectedPorts={len(port_names)}, "
                f"channels={len(channels)}"
            ),
        ) as progress_stage:
            sub = app.modeler.components.create_touchstone_component(
                str(touchstone_path),
                location=[layout["snpX"], snp_y],
                show_bitmap=False,
            )
            touchstone_model_portable = _set_touchstone_model_project_relative(
                sub,
                touchstone_path,
            )
            if not touchstone_model_portable:
                raise RuntimeError(
                    "Circuit Touchstone model could not use a project-relative path"
                )
            touchstone_model_refreshed = _refresh_touchstone_model(sub)
            component_port_order = [str(pin.name) for pin in sub.pins]
            circuit_port_contract = (
                strict_tdr_runtime.validate_circuit_port_order(
                    component_port_order,
                    strict_contract.expected_port_order,
                )
                if strict_mode
                else None
            )
            sub_pins = {pin.name: pin for pin in sub.pins}
            _complete_progress_stage(
                progress_stage,
                f"component={sub.name}, ports={len(component_port_order)}, "
                f"order={'validated' if strict_mode else 'loaded'}",
            )

        tdr_probes: list[dict[str, object]] = []
        far_end_records: list[dict[str, object]] = []
        trace_names: list[str] = []
        trace_report_contexts: dict[str, dict[str, Any]] = {}

        with _optional_progress_stage(
            progress,
            "TDR-TOPOLOGY",
            "Build probes, terminations, page ports, and layout labels",
            detail=f"channels={len(channels)}, Touchstone ports={len(sub_pins)}",
        ) as progress_stage:
            snp_page_ports = _create_snp_page_ports(app, sub_pins, channels)
            schematic_labels = _create_multi_diff_layout_labels(
                app,
                channel_rows=channel_rows,
                layout=layout,
            )

            for channel_index, (channel, row) in enumerate(
                zip(channels, channel_rows),
                start=1,
            ):
                if strict_mode:
                    channel_group = strict_contract.group_for_channel(
                        str(channel["name"])
                    )
                    diff_bridge_ohm = None
                else:
                    channel_group = None
                    diff_bridge_ohm = _far_end_differential_bridge_ohm(
                        context,
                        channel,
                    )
                tdr_record = _place_diff_channel_tdr_block(
                    app,
                    channel=channel,
                    row_y=float(row["y"]),
                    layout=layout,
                    context=context,
                    strict_group=channel_group,
                )
                far_record = _place_diff_channel_far_block(
                    app,
                    channel=channel,
                    row_y=float(row["y"]),
                    layout=layout,
                    context=context,
                    diff_bridge_ohm=diff_bridge_ohm,
                    strict_group=channel_group,
                )
                if strict_mode:
                    far_record["tdrReportGroup"] = channel_group.name
                trace_name = str(tdr_record["traceName"])
                trace_names.append(trace_name)
                if strict_mode:
                    tdr_record["tdrReportGroup"] = channel_group.name
                    # 데이터 조회는 solve 전체 범위여야 한다. group x_min/x_max는
                    # 화면 표시(view) 범위라서 여기에 쓰면 파형 데이터가 view로
                    # 잘리고 uniform CSV 경계에 비유한값이 생긴다 (R20 실행 관측).
                    trace_report_contexts[trace_name] = _tdr_report_context(
                        context,
                        time_start_ps=0.0,
                        time_stop_ps=strict_contract.transient_stop_ps,
                    )
                tdr_probes.append(tdr_record)
                far_end_records.append(far_record)
                _tdr_progress_item(
                    progress,
                    "TDR-TOPOLOGY",
                    channel_index,
                    len(channels),
                    "DONE",
                    f"channel={channel['name']}, trace={trace_name}, "
                    f"near={channel['nearPositive']}/{channel['nearNegative']}, "
                    f"far={channel['farPositive']}/{channel['farNegative']}",
                )
            _complete_progress_stage(
                progress_stage,
                f"probes={len(tdr_probes)}, terminations={len(far_end_records)}, "
                f"pagePorts={len(snp_page_ports)}",
            )

        with _optional_progress_stage(
            progress,
            "TDR-SETUP",
            "Configure the Nexxim transient setup",
            detail="Apply the transient grid and Touchstone convolution policy.",
        ) as progress_stage:
            setup = app.create_setup(
                name="Transient_TDR",
                setup_type=app.SETUPS.NexximTransient,
            )
            transient_data = (
                strict_tdr_runtime.internal_transient_data(
                    strict_contract.transient_stop_ps
                )
                if strict_mode
                else _tdr_transient_data(context)
            )
            setup.props["TransientData"] = transient_data
            use_ts_convolution = (
                strict_tdr_runtime.INTERNAL_USE_TS_CONVOLUTION
                if strict_mode
                else _tdr_use_ts_convolution(context)
            )
            if use_ts_convolution:
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

            saved = app.save_project(
                file_name=str(project_path.resolve()),
                overwrite=True,
            )
            if not saved:
                raise RuntimeError(
                    f"PyAEDT could not save Circuit project to {project_path}"
                )
            _complete_progress_stage(
                progress_stage,
                f"setup=Transient_TDR, transient={transient_data}, "
                f"tsConvolution={use_ts_convolution}",
            )

        with _optional_progress_stage(
            progress,
            "TDR-ANALYZE",
            "Run the Nexxim transient analysis",
            detail="Wait for AEDT Analyze(Transient_TDR) to return.",
        ) as progress_stage:
            analysis_ok = app.analyze_setup("Transient_TDR", blocking=True)
            if not analysis_ok:
                raise RuntimeError(
                    "PyAEDT did not report a successful Transient_TDR solve"
                )
            _complete_progress_stage(
                progress_stage,
                "apiReturn=True; numerical result verification follows",
            )

        with _optional_progress_stage(
            progress,
            "TDR-RESULTS-DIR",
            "Inspect the immediate AEDT results directory",
            detail="This is diagnostic evidence; trace queries remain authoritative.",
        ) as progress_stage:
            if progress_stage is not None:
                immediate_results = project_path.with_suffix(".aedtresults")
                result_files = (
                    [path for path in immediate_results.rglob("*") if path.is_file()]
                    if immediate_results.is_dir()
                    else []
                )
                result_bytes = sum(path.stat().st_size for path in result_files)
                if result_files:
                    progress_stage.complete(
                        f"path={immediate_results}, files={len(result_files)}, "
                        f"bytes={result_bytes}"
                    )
                else:
                    _tdr_progress_event(
                        progress,
                        "TDR-RESULTS-DIR",
                        "WARN",
                        f"No result files were visible immediately after Analyze: "
                        f"{immediate_results}",
                    )
                    progress_stage.complete(
                        f"path={immediate_results}, files=0, "
                        "status=not-visible; trace verification follows"
                    )

        report_records = []
        report_groups = _tdr_report_groups(context, tdr_probes)
        report_started = time.monotonic()
        active_manual_progress_area = "TDR-REPORT"
        _tdr_progress_event(
            progress,
            "TDR-REPORT",
            "START",
            f"Create AEDT native reports and JPG files - groups={len(report_groups)}",
            depth=2,
        )
        for report_index, report_group in enumerate(report_groups, start=1):
            report_view = (
                report_group.get("view")
                if isinstance(report_group.get("view"), dict)
                else None
            )
            native_report_evidence = None
            if strict_mode:
                strict_group = strict_contract.group(str(report_group["name"]))
                report = app.post.create_report(
                    expressions=[str(item) for item in report_group["traceNames"]],
                    setup_sweep_name="Transient_TDR",
                    domain="Time",
                    primary_sweep_variable="Time",
                    plot_name=strict_group.name,
                    context=_tdr_report_context(
                        context,
                        time_start_ps=strict_group.x_min_ps,
                        time_stop_ps=strict_group.x_max_ps,
                    ),
                )
                if not report:
                    raise strict_tdr_runtime.StrictTdrRuntimeError(
                        f"PyAEDT did not create native report {strict_group.name!r}"
                    )
                _set_tdr_report_trace_display_names(
                    report,
                    trace_names=[str(item) for item in report_group["traceNames"]],
                    display_names=[str(item) for item in report_group["displayNames"]],
                )
                native_target_range = _add_tdr_native_target_ranges(
                    report,
                    context,
                    trace_names=[str(item) for item in report_group["traceNames"]],
                    channels=[str(item) for item in report_group["channels"]],
                    view={
                        "xAxisPs": {
                            "min": strict_group.x_min_ps,
                            "max": strict_group.x_max_ps,
                        }
                    },
                )
                endpoint_notes = _add_tdr_native_endpoint_notes(
                    report,
                    context,
                    trace_names=[str(item) for item in report_group["traceNames"]],
                    channels=[str(item) for item in report_group["channels"]],
                )
                native_report_evidence = strict_tdr_runtime.apply_strict_native_report(
                    app,
                    report,
                    strict_group,
                    endpoint_notes=endpoint_notes,
                    native_target_range=native_target_range,
                )
                report_name = strict_group.name
                x_min, x_max = strict_group.x_min_ps, strict_group.x_max_ps
                y_min, y_max = strict_group.y_min_ohm, strict_group.y_max_ohm
            else:
                report_name, endpoint_notes, native_target_range = _create_tdr_report_multi(
                    app,
                    context,
                    trace_names=[str(item) for item in report_group["traceNames"]],
                    channels=[str(item) for item in report_group["channels"]],
                    display_names=[
                        str(item) for item in report_group["displayNames"]
                    ],
                    setup_name="Transient_TDR",
                    plot_name=str(report_group["name"]),
                    view=report_view,
                )
                x_min, x_max = _tdr_time_view_range_ps(
                    context,
                    view=report_view,
                )
                y_min, y_max = _tdr_impedance_view_range(
                    context,
                    view=report_view,
                )
            report_image_path = _export_tdr_report_image(app, circuit_dir, report_name)
            if strict_mode and report_image_path is None:
                raise strict_tdr_runtime.StrictTdrRuntimeError(
                    f"AEDT native report JPG export failed: {report_name}"
                )
            report_image_analysis = (
                strict_tdr_runtime.analyze_native_report_jpeg(
                    Path(str(report_image_path))
                )
                if strict_mode
                else None
            )
            report_records.append(
                {
                    "name": report_name,
                    "imagePath": report_image_path,
                    "imageAnalysis": report_image_analysis,
                    "traceNames": report_group["traceNames"],
                    "channels": report_group["channels"],
                    "displayNames": report_group["displayNames"],
                    "endpointNotes": endpoint_notes,
                    "nativeTargetRange": native_target_range,
                    "strictNativeReport": native_report_evidence,
                    "tdrProfile": (
                        strict_contract.group(str(report_group["name"])).to_dict().get(
                            "tdrProfile"
                        )
                        if strict_mode
                        else None
                    ),
                    "directionProvenance": report_group.get(
                        "directionProvenance"
                    ) or [],
                    "appliedView": {
                        "xAxisPs": {"min": x_min, "max": x_max},
                        "yAxisOhm": {"min": y_min, "max": y_max},
                    },
                }
            )
            _tdr_progress_item(
                progress,
                "TDR-REPORT",
                report_index,
                len(report_groups),
                "DONE",
                f"report={report_name}, traces={len(report_group['traceNames'])}, "
                f"image={report_image_path}",
            )
        if strict_mode and [item["name"] for item in report_records] != [
            group.name for group in strict_contract.groups
        ]:
            raise strict_tdr_runtime.StrictTdrRuntimeError(
                "AEDT native report set/order does not match strict TDR items: "
                f"expected={[group.name for group in strict_contract.groups]}, "
                f"actual={[item['name'] for item in report_records]}"
            )
        primary_report = report_records[0] if report_records else {}
        # analyze 후 모델 정의를 다시 쓰면(설계 수정) solve 결과가 무효화되어
        # 저장 시 transient 데이터가 프로젝트에 남지 않는다. refresh는 solve 전에만 한다.
        saved_after_analyze = app.save_project(file_name=str(project_path.resolve()), overwrite=True)
        if not saved_after_analyze:
            raise RuntimeError(f"PyAEDT could not save analyzed Circuit project to {project_path}")
        _tdr_progress_event(
            progress,
            "TDR-REPORT",
            "DONE",
            f"reports={len(report_records)}, projectSaved=True "
            f"({format_elapsed(time.monotonic() - report_started)})",
            depth=2,
        )
        active_manual_progress_area = None

        samples_by_trace: dict[str, list[dict[str, float | int]]] = {}
        sample_count = 0
        trace_units: dict[str, str] = {}
        data_started = time.monotonic()
        active_manual_progress_area = "TDR-DATA"
        _tdr_progress_event(
            progress,
            "TDR-DATA",
            "START",
            f"Load solution data for {len(trace_names)} traces",
            depth=2,
        )
        for trace_index, trace_name in enumerate(trace_names, start=1):
            solution_data = app.post.get_solution_data(
                expressions=trace_name,
                setup_sweep_name="Transient_TDR",
                domain="Time",
                context=trace_report_contexts.get(trace_name),
            )
            if not solution_data:
                _tdr_progress_item(
                    progress,
                    "TDR-DATA",
                    trace_index,
                    len(trace_names),
                    "FAIL",
                    f"trace={trace_name}, reason=no solution data, "
                    f"context={trace_report_contexts.get(trace_name)}",
                )
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
            trace_first = (
                samples_by_trace[trace_name][0]["time_ps"]
                if samples_by_trace[trace_name]
                else None
            )
            trace_last = (
                samples_by_trace[trace_name][-1]["time_ps"]
                if samples_by_trace[trace_name]
                else None
            )
            _tdr_progress_item(
                progress,
                "TDR-DATA",
                trace_index,
                len(trace_names),
                "DONE" if count else "FAIL",
                f"trace={trace_name}, samples={count}, "
                f"rangePs={trace_first}..{trace_last}",
            )

        missing_trace_names = [
            trace_name
            for trace_name in trace_names
            if not samples_by_trace.get(trace_name)
        ]
        _tdr_progress_event(
            progress,
            "TDR-DATA",
            "DONE" if not missing_trace_names else "FAIL",
            f"loaded={len(trace_names) - len(missing_trace_names)}/{len(trace_names)}, "
            f"missing={missing_trace_names} "
            f"({format_elapsed(time.monotonic() - data_started)})",
            depth=2,
        )
        active_manual_progress_area = None

        all_time_values_ps = [
            float(sample["time_ps"])
            for trace_samples in samples_by_trace.values()
            for sample in trace_samples
        ]
        archive_path = None
        strict_artifacts = None
        strict_contract_record = None
        project_name = app.project_name
        design_name = app.design_name
        if strict_mode:
            waveform_csv = circuit_dir / "Tdr_waveform.csv"
            with _optional_progress_stage(
                progress,
                "TDR-VERIFY",
                "Validate the common native waveform grid",
                detail=(
                    f"expectedTraces={len(trace_names)}, "
                    f"loadedTraces={len(samples_by_trace)}"
                ),
            ) as progress_stage:
                uniform_grid = strict_tdr_runtime.resolve_uniform_waveform_grid(
                    trace_names=trace_names,
                    samples_by_trace=samples_by_trace,
                )
                _complete_progress_stage(
                    progress_stage,
                    f"rangePs={uniform_grid['firstPs']}..{uniform_grid['lastPs']}, "
                    f"stepPs={uniform_grid['stepPs']}, rows={uniform_grid['rowCount']}",
                )
            export_started = time.monotonic()
            active_manual_progress_area = "TDR-EXPORT"
            _tdr_progress_event(
                progress,
                "TDR-EXPORT",
                "START",
                "Export native CSV, save/archive the project, and verify artifacts",
                depth=2,
            )
            waveform_report_name = "Tdr_waveform"
            waveform_report = app.post.create_report(
                expressions=trace_names,
                setup_sweep_name="Transient_TDR",
                domain="Time",
                primary_sweep_variable="Time",
                plot_name=waveform_report_name,
                context=_tdr_report_context(
                    context,
                    time_start_ps=float(uniform_grid["firstPs"]),
                    time_stop_ps=float(uniform_grid["lastPs"]),
                ),
            )
            if not waveform_report:
                raise strict_tdr_runtime.StrictTdrRuntimeError(
                    "PyAEDT did not create the temporary native waveform export report"
                )
            try:
                exported_waveform = Path(
                    app.post.export_report_to_csv(
                        str(circuit_dir),
                        waveform_report_name,
                        uniform=True,
                        start=f"{float(uniform_grid['firstPs']):g}ps",
                        end=f"{float(uniform_grid['lastPs']):g}ps",
                        step=f"{float(uniform_grid['stepPs']):g}ps",
                    )
                ).resolve()
                if exported_waveform != waveform_csv.resolve():
                    raise strict_tdr_runtime.StrictTdrRuntimeError(
                        "AEDT native waveform export returned an unexpected path: "
                        f"{exported_waveform}"
                    )
            finally:
                if not app.post.delete_report(waveform_report_name):
                    raise strict_tdr_runtime.StrictTdrRuntimeError(
                        "PyAEDT did not remove the temporary waveform export report"
                    )
            waveform_evidence = strict_tdr_runtime.native_waveform_csv_evidence(
                waveform_csv,
                trace_names=trace_names,
                uniform_grid=uniform_grid,
            )
            _tdr_progress_item(
                progress,
                "TDR-EXPORT",
                1,
                4,
                "DONE",
                f"waveformCsv={waveform_csv}, rows={waveform_evidence['rowCount']}",
            )
            saved_after_waveform_export = app.save_project(
                file_name=str(project_path.resolve()), overwrite=True
            )
            if not saved_after_waveform_export:
                raise RuntimeError(
                    f"PyAEDT could not save Circuit project after waveform export: {project_path}"
                )
            archive_path = _archive_circuit_project(app, project_path)
            project_evidence = strict_tdr_runtime.artifact_evidence(project_path)
            archive_evidence = strict_tdr_runtime.artifact_evidence(Path(archive_path))
            _tdr_progress_item(
                progress,
                "TDR-EXPORT",
                2,
                4,
                "DONE",
                f"project={project_path}, archive={archive_path}",
            )
            report_evidence = [
                strict_tdr_runtime.native_report_jpeg_evidence(
                    Path(str(item["imagePath"]))
                )
                for item in report_records
            ]
            # AEDT removes transient cache members from ``.aedtresults`` while
            # closing the project/Desktop.  Record the durable directory only
            # after that cleanup; otherwise the publisher correctly sees an
            # aggregate drift between the pre-close evidence and final files.
            app.release_desktop(close_projects=True, close_desktop=True)
            app = None
            results_evidence = strict_tdr_runtime.results_directory_evidence(
                project_path.with_suffix(".aedtresults")
            )
            _tdr_progress_item(
                progress,
                "TDR-EXPORT",
                3,
                4,
                "DONE",
                f"aedtResults={project_path.with_suffix('.aedtresults')}, "
                f"files={results_evidence.get('fileCount', 'unknown')}",
            )
            # 9.4.6: 공개 <Batch>.aedt는 자기 DB <Batch>.aedb와 한 세트다.
            circuit_aedb_evidence = strict_tdr_runtime.results_directory_evidence(
                project_path.with_suffix(".aedb")
            )
            _tdr_progress_item(
                progress,
                "TDR-EXPORT",
                4,
                4,
                "DONE",
                f"aedb={project_path.with_suffix('.aedb')}, "
                f"files={circuit_aedb_evidence.get('fileCount', 'unknown')}",
            )
            strict_artifacts = {
                "project": project_evidence,
                "archive": archive_evidence,
                "nativeReportJpg": report_evidence,
                "waveformCsv": waveform_evidence,
                "aedtResults": results_evidence,
                "aedb": circuit_aedb_evidence,
                "freshness": "all exact targets were absent before this strict run",
            }
            strict_contract_record = strict_contract.to_dict()
            sample_count = int(waveform_evidence["rowCount"])
            _tdr_progress_event(
                progress,
                "TDR-EXPORT",
                "DONE",
                f"csv={waveform_csv}, archive={archive_path} "
                f"({format_elapsed(time.monotonic() - export_started)})",
                depth=2,
            )
            active_manual_progress_area = None
        else:
            with _optional_progress_stage(
                progress,
                "TDR-EXPORT",
                "Archive the Circuit project",
                detail=f"project={project_path}",
            ) as progress_stage:
                archive_path = _archive_circuit_project(app, project_path)
                _complete_progress_stage(
                    progress_stage,
                    f"archive={archive_path}",
                )
        record = {
            "schema": (
                strict_tdr_runtime.STRICT_TDR_RECORD_SCHEMA
                if strict_mode
                else None
            ),
            "status": "ok",
            "buildMode": (
                "customer-strict-native-aedt-tdr"
                if strict_mode
                else "manual-snp-multi-diff"
            ),
            "touchstonePath": str(touchstone_path),
            "projectPath": str(project_path),
            "projectName": project_name,
            "designName": design_name,
            "setupName": "Transient_TDR",
            "traceNames": trace_names,
            "reportName": primary_report.get("name"),
            "reportImagePath": primary_report.get("imagePath"),
            "reports": report_records,
            "sampleCount": sample_count,
            "timeUnit": "ps",
            "traceUnits": trace_units,
            "analysisStopPs": (
                strict_contract.transient_stop_ps
                if strict_mode
                else float(
                    (context.get("tdr", {}).get("transient") or {}).get(
                        "stopPs",
                        30000,
                    )
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
            "archivePath": archive_path,
            "strictContract": strict_contract_record,
            "circuitPortContract": circuit_port_contract,
            "artifacts": strict_artifacts,
            "touchstoneModelPortable": touchstone_model_portable,
            "touchstoneModelRefreshed": touchstone_model_refreshed,
            "nexximPathBudget": nexxim_path_budget,
            "manualTopology": {
                "touchstoneComponent": sub.name,
                "snpPagePorts": snp_page_ports,
                "tdrProbes": tdr_probes,
                "farEnd": far_end_records,
                "layout": {
                    "style": "reviewable-row-columns-v2",
                    "parameters": layout,
                    "snpLocation": {"x": layout["snpX"], "y": snp_y},
                    "channelRows": channel_rows,
                    "labels": schematic_labels,
                },
            },
            "samplesByTrace": samples_by_trace,
        }
        if strict_mode:
            strict_tdr_runtime.validate_strict_tdr_artifact_package(
                record,
                run_dir=run_dir,
                job_root=strict_contract.job_root,
                expected_batch_id=str(segment_name),
            )
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(record, fp, indent=2)
        return record_path
    except Exception as exc:
        if active_manual_progress_area is not None:
            _tdr_progress_event(
                progress,
                active_manual_progress_area,
                "FAIL",
                str(exc).strip() or type(exc).__name__,
                depth=2,
            )
        error_record = {
            "status": "error",
            "buildMode": (
                "customer-strict-native-aedt-tdr"
                if strict_mode
                else "manual-snp-multi-diff"
            ),
            "batchId": str(segment_name),
            "touchstonePath": str(touchstone_path),
            "projectPath": str(project_path),
            "designName": design_name,
            "nexximPathBudget": nexxim_path_budget,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }
        record_path.parent.mkdir(parents=True, exist_ok=True)
        with record_path.open("w", encoding="utf-8") as fp:
            json.dump(error_record, fp, indent=2, ensure_ascii=False)
            fp.write("\n")
        raise
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


def _place_shared_far_end_ground(
    app,
    pins: list[object],
    *,
    trunk_x: float,
    ground_y: float,
    ground_component_dy: float = 0.00508,
) -> str:
    schematic = app.modeler.schematic
    for pin in pins:
        x, y = pin.location
        schematic.create_wire(points=[[x, y], [trunk_x, y], [trunk_x, ground_y]])

    gnd = schematic.create_gnd([trunk_x, ground_y + ground_component_dy])
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
    name: str,
    value_ohm: float,
    location: list[float],
    route_x: float | None = None,
) -> str:
    resistor = app.modeler.schematic.create_resistor(name=name, value=str(value_ohm), location=location, angle=90)
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


def _tdr_report_context(
    context: dict[str, Any],
    *,
    time_start_ps: float | None = None,
    time_stop_ps: float | None = None,
) -> dict[str, Any]:
    report_context: dict[str, Any] = {
        "differential_pairs": context["tdr"].get("mode") == "differential",
    }
    if time_start_ps is not None:
        report_context["time_start"] = f"{float(time_start_ps):g}ps"
    if time_stop_ps is not None:
        report_context["time_stop"] = f"{float(time_stop_ps):g}ps"
    return report_context


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
    display_names: list[str] | None = None,
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
    if display_names is not None:
        _set_tdr_report_trace_display_names(
            report,
            trace_names=trace_names,
            display_names=display_names,
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


def _set_tdr_report_trace_display_names(
    report,
    *,
    trace_names: list[str],
    display_names: list[str],
) -> list[dict[str, str]]:
    if len(trace_names) != len(display_names):
        raise RuntimeError(
            "AEDT report trace/display name mapping length mismatch"
        )

    normalized_names = [str(name).strip() for name in display_names]
    if any(not name for name in normalized_names):
        raise RuntimeError("AEDT report display names must not be empty")
    casefolded_names = [name.casefold() for name in normalized_names]
    if len(set(casefolded_names)) != len(casefolded_names):
        raise RuntimeError("AEDT report display names must be unique within a chart")

    traces = list(report.traces)
    traces_by_name = {str(trace.name): trace for trace in traces}
    missing = [name for name in trace_names if name not in traces_by_name]
    if missing:
        raise RuntimeError(
            "AEDT report trace rename could not find expressions: "
            + ", ".join(missing)
        )

    mappings: list[dict[str, str]] = []
    for trace_name, display_name in zip(trace_names, normalized_names):
        trace = traces_by_name[trace_name]
        trace.name = display_name
        mappings.append(
            {
                "traceName": trace_name,
                "displayName": display_name,
            }
        )

    renamed = {str(trace.name) for trace in report.traces}
    unresolved = [name for name in normalized_names if name not in renamed]
    if unresolved:
        raise RuntimeError(
            "AEDT report trace display names were not persisted: "
            + ", ".join(unresolved)
        )
    return mappings


def _tdr_report_groups(context: dict[str, Any], tdr_probes: list[dict[str, object]]) -> list[dict[str, object]]:
    configured_groups = context.get("tdr", {}).get("reportGroups") or []
    if not configured_groups:
        return [
            {
                "name": f"{context['segment'].get('name', 'SEGMENT')}_TDR_Report",
                "traceNames": [str(probe["traceName"]) for probe in tdr_probes],
                "channels": [str(probe["channel"]) for probe in tdr_probes],
                "displayNames": [
                    str(probe.get("displayName") or probe["channel"])
                    for probe in tdr_probes
                ],
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
                "displayNames": [
                    str(probe.get("displayName") or probe["channel"])
                    for probe in matched_probes
                ],
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


TDR_REPORT_IMAGE_PREFIX = "Tdr_"

def _export_tdr_report_image(app, circuit_dir: Path, report_name: str | None) -> str | None:
    if not report_name:
        return None
    try:
        ok = bool(app.post.export_report_to_jpg(str(circuit_dir), report_name, width=1400, height=800, image_format="jpg"))
    except Exception:
        return None
    exported_path = circuit_dir / f"{report_name}.jpg"
    if not ok or not exported_path.exists() or exported_path.stat().st_size <= 0:
        return None
    # AEDT 프로젝트 안의 Report 이름은 Group 그대로 두고, 내보낸 파일명만
    # 용도가 드러나도록 Tdr_ 접두사를 붙인다.
    report_image_path = circuit_dir / f"{TDR_REPORT_IMAGE_PREFIX}{report_name}.jpg"
    if report_image_path.exists():
        report_image_path.unlink()
    exported_path.replace(report_image_path)
    if not report_image_path.exists() or report_image_path.stat().st_size <= 0:
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


def _load_tdr_endpoint_annotation_module():
    if __package__:
        from . import tdr_endpoint_annotations
    else:
        import tdr_endpoint_annotations
    return tdr_endpoint_annotations


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SI-TDR FullBatch workflow")
    parser.add_argument(
        "config",
        help=(
            "Path to the external Heaven request JSON (the filename is not fixed); "
            "legacy Run Config and generation-request JSON are accepted during migration"
        ),
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="Generate a Run Config from the customer CSV request before executing the selected flow",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate the positional JSON file without starting Ansys or writing outputs",
    )
    parser.add_argument(
        "--prepare-reference-only",
        action="store_true",
        help=(
            "Build the customer reference SIW/AEDB from the external Heaven request "
            "and stop before Channel Path/SYZ/TDR"
        ),
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
    return parser.parse_args(argv)


def _requested_operation_labels(args: argparse.Namespace) -> list[str]:
    operations: list[str] = []
    if getattr(args, "customer_fullbatch", False):
        operations.append("Reference preprocessing and strict Config generation")
    elif getattr(args, "generate_config", False):
        operations.append("Reference preprocessing and Config generation")
    elif getattr(args, "prepare_reference_only", False):
        operations.append("Reference preprocessing")
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
    if getattr(args, "run_tdr", False) or getattr(args, "tdr_only", False):
        operations.append("Export AEDT native TDR results")
    if getattr(args, "capture_pcb_routes", False):
        operations.append(
            "Apply PCB capture policy"
            if getattr(args, "customer_fullbatch", False)
            else "Capture PCB images"
        )
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


PCB_CAPTURE_WORKER_RESULT_SCHEMA = "si-tdr-pcb-capture-worker-result/1"


def _run_pcb_capture_in_fresh_process(
    context_path: Path,
    context: Mapping[str, Any],
    *,
    command_runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Run graphical SIWave capture outside the long-lived FullBatch process."""

    resolved_context_path = Path(context_path).resolve()
    if not resolved_context_path.is_file():
        raise RuntimeError(
            f"PCB capture worker context does not exist: {resolved_context_path}"
        )
    context_sha256 = hashlib.sha256(resolved_context_path.read_bytes()).hexdigest()
    run_dir = Path(str(context["workspace"]["runDir"])).resolve()
    worker_path = Path(__file__).resolve().with_name("pcb_capture.py")
    if not worker_path.is_file():
        raise RuntimeError(f"PCB capture worker is missing: {worker_path}")

    receipt_path = run_dir / f".pcb_capture_worker_{uuid.uuid4().hex}.json"
    command = [
        sys.executable,
        str(worker_path),
        "--worker-context",
        str(resolved_context_path),
        "--worker-result",
        str(receipt_path),
        "--parent-pid",
        str(os.getpid()),
    ]
    try:
        completed = command_runner(
            command,
            cwd=str(worker_path.parent),
            check=False,
        )
        return_code = int(completed.returncode)
        if not receipt_path.is_file():
            raise RuntimeError(
                "PCB capture worker exited without a result receipt "
                f"(exit code {return_code})"
            )
        receipt = _record_payload(receipt_path)
        if receipt.get("schema") != PCB_CAPTURE_WORKER_RESULT_SCHEMA:
            raise RuntimeError("PCB capture worker result schema is invalid")
        receipt_context_path = Path(
            str(receipt.get("contextPath") or "")
        ).resolve()
        if receipt_context_path != resolved_context_path:
            raise RuntimeError("PCB capture worker used a different run context")
        worker_pid = receipt.get("workerPid")
        if not isinstance(worker_pid, int) or worker_pid == os.getpid():
            raise RuntimeError("PCB capture worker did not prove fresh-process isolation")
        if receipt.get("parentPid") != os.getpid():
            raise RuntimeError("PCB capture worker receipt has the wrong parent PID")
        if receipt.get("requestedParentPid") != os.getpid():
            raise RuntimeError("PCB capture worker received the wrong parent PID")
        actual_launcher_parent_pid = receipt.get("actualLauncherParentPid")
        if (
            not isinstance(actual_launcher_parent_pid, int)
            or actual_launcher_parent_pid <= 0
            or actual_launcher_parent_pid == worker_pid
        ):
            raise RuntimeError(
                "PCB capture worker reported an invalid launcher parent PID"
            )
        if receipt.get("status") != "completed":
            error_type = str(receipt.get("errorType") or "worker-error")
            error = str(receipt.get("error") or "unknown PCB capture worker failure")
            raise RuntimeError(f"PCB capture worker failed: {error_type}: {error}")
        if receipt.get("contextSha256") != context_sha256:
            raise RuntimeError("PCB capture worker run-context hash is invalid")
        current_context_sha256 = hashlib.sha256(
            resolved_context_path.read_bytes()
        ).hexdigest()
        if current_context_sha256 != context_sha256:
            raise RuntimeError("PCB capture worker changed the run context")

        manifest_path = Path(str(receipt.get("manifestPath") or "")).resolve()
        try:
            manifest_path.relative_to(run_dir)
        except ValueError as exc:
            raise RuntimeError(
                "PCB capture worker returned a manifest outside the batch run directory"
            ) from exc
        manifest_parent = manifest_path.parent.name
        if (
            manifest_path.name != "pcb_capture_manifest.json"
            or (
                manifest_parent != "pcb_capture"
                and not manifest_parent.startswith("pcb_capture_failed_")
            )
            or not manifest_path.is_file()
        ):
            raise RuntimeError(
                f"PCB capture worker manifest is missing or invalid: {manifest_path}"
            )
        manifest = _record_payload(manifest_path)
        manifest_status = str(manifest.get("status") or "")
        if receipt.get("manifestStatus") != manifest_status:
            raise RuntimeError(
                "PCB capture worker manifest status does not match its receipt"
            )
        expected_return_code = (
            0 if manifest_status in {"ok", "ok-with-fallback", "skipped"} else 2
        )
        if return_code != expected_return_code:
            raise RuntimeError(
                "PCB capture worker exit code does not match the manifest status: "
                f"exit={return_code}, status={manifest_status or 'missing'}"
            )
        print(
            "PCB capture fresh-process worker completed: "
            f"pid={worker_pid}, exit={return_code}, manifest={manifest_path}",
            flush=True,
        )
        return manifest_path
    finally:
        try:
            receipt_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_pcb_capture_with_process_policy(
    context_path: Path,
    context: dict[str, Any],
    *,
    live_capture: bool,
    route_enabled: bool,
    overview_enabled: bool,
    in_process_runner: Callable[..., Path],
) -> Path:
    if live_capture and (route_enabled or overview_enabled):
        return _run_pcb_capture_in_fresh_process(context_path, context)
    return in_process_runner(context, plan_only=not live_capture)


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
    strict_artifacts = transient_record.get("artifacts") or {}
    for label, key in (
        ("AEDT archive", "archive"),
        ("TDR waveform CSV", "waveformCsv"),
    ):
        record = strict_artifacts.get(key) or {}
        if record.get("path"):
            artifacts.append((label, Path(record["path"])))
    for record in strict_artifacts.get("nativeReportJpg") or []:
        if record.get("path"):
            artifacts.append(("AEDT TDR image", Path(record["path"])))
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
    snapshot_callback: Callable[[str, str], None] | None = None,
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
        if snapshot_callback is not None:
            snapshot_callback(
                "after-siwave",
                str(context["segment"].get("name") or context["workspace"]["runName"]),
            )
    if args.run_tdr or args.tdr_only:
        with progress.stage(
            "TDR-SOLVE",
            "Solve AEDT Circuit TDR",
            detail="Connect the sNp model in Circuit and solve the transient TDR response.",
        ) as stage:
            transient_record_path = run_tdr(context, progress=progress)
            record = _record_payload(transient_record_path)
            trace_names = record.get("traceNames") or []
            stage.complete(
                f"status={record.get('status', 'unknown')}, "
                f"traces={len(trace_names)}, samples={record.get('sampleCount', 'unknown')}"
            )
    if args.plan_pcb_capture or args.capture_pcb_routes:
        if __package__:
            from .pcb_capture import run_pcb_capture
        else:
            from pcb_capture import run_pcb_capture

        capture_options = context.get("pcbCapture") or {}
        route_option = capture_options.get("route") or {}
        overview_option = capture_options.get("overview") or {}
        route_enabled = route_option.get("enabled", True) is True
        overview_enabled = overview_option.get("enabled", True) is True
        pcb_label = (
            "Apply PCB capture policy"
            if args.capture_pcb_routes
            else "Plan PCB capture"
        )
        with progress.stage(
            "PCB-CAPTURE",
            pcb_label,
            detail=(
                "Process administrator capture settings: "
                f"route={'enabled' if route_enabled else 'disabled'}, "
                f"overview={'enabled' if overview_enabled else 'disabled'}."
            ),
        ) as stage:
            pcb_capture_manifest_path = _run_pcb_capture_with_process_policy(
                context_path,
                context,
                live_capture=bool(args.capture_pcb_routes),
                route_enabled=route_enabled,
                overview_enabled=overview_enabled,
                in_process_runner=run_pcb_capture,
            )
            record = _record_payload(pcb_capture_manifest_path)
            pcb_capture_status = record.get("status")
            route_capture = record.get("routeCapture") or {}
            job_overview = record.get("jobOverview") or {}
            if route_capture.get("status") == "skipped":
                progress.info(
                    "PCB-ROUTE",
                    "Skipped: disabled by administrator Config.",
                )
            if job_overview.get("status") == "skipped":
                progress.info(
                    "PCB-OVERVIEW",
                    "Skipped: disabled by administrator Config.",
                )
            expected_status = (
                pcb_capture_status in {"ok", "ok-with-fallback", "skipped"}
                if args.capture_pcb_routes
                else pcb_capture_status in {"planned", "skipped"}
            )
            summary = (
                f"status={pcb_capture_status}, "
                f"route={route_capture.get('status', 'unknown')}, "
                f"overview={job_overview.get('status', 'unknown')}, "
                f"unresolved={len(record.get('unresolved') or [])}"
            )
            if expected_status:
                stage.complete(summary)
            else:
                stage.fail(summary)
        if snapshot_callback is not None:
            snapshot_callback(
                "after-circuit-capture",
                str(context["segment"].get("name") or context["workspace"]["runName"]),
            )

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
    pcb_record = _record_payload(pcb_capture_manifest_path)
    progress.info("ARTIFACT", "Generated output files")
    for label, path in _result_artifacts(
        context,
        solve_record=solve_record,
        transient_record=transient_record,
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
    if pcb_capture_manifest_path is not None:
        progress.detail("PCB capture record", pcb_capture_manifest_path)
        progress.detail("PCB capture status", pcb_capture_status)
    if args.capture_pcb_routes and pcb_capture_status not in {
        "ok",
        "ok-with-fallback",
        "skipped",
    }:
        return 2
    if (
        args.plan_pcb_capture
        and not args.capture_pcb_routes
        and pcb_capture_status not in {"planned", "skipped"}
    ):
        return 2
    return 0


@dataclass(frozen=True)
class RequestExecutionOutcome:
    status: int
    batches: tuple[BatchExecutionInput, ...]
    result_dirs: tuple[Path, ...]
    generation_manifest: Path | None = None
    error: str | None = None


def _default_external_fullbatch_requested(
    args: argparse.Namespace,
    *,
    external_request: bool,
) -> bool:
    if not external_request:
        return False
    explicit_flow_options = (
        "generate_config",
        "preflight_only",
        "prepare_reference_only",
        "apply_ports",
        "setup_syz",
        "solve_touchstone",
        "run_tdr",
        "tdr_only",
        "plan_pcb_capture",
        "capture_pcb_routes",
    )
    return not any(bool(getattr(args, name, False)) for name in explicit_flow_options)


def _customer_fullbatch_args(args: argparse.Namespace) -> argparse.Namespace:
    values = dict(vars(args))
    values.update(
        {
            "generate_config": False,
            "apply_ports": True,
            "setup_syz": True,
            "solve_touchstone": True,
            "run_tdr": True,
            "tdr_only": False,
            "plan_pcb_capture": False,
            "capture_pcb_routes": True,
            "customer_fullbatch": True,
        }
    )
    return argparse.Namespace(**values)


def _execute_external_customer_fullbatch(
    args: argparse.Namespace,
    config_path: Path,
    progress: ConsoleProgress,
    *,
    job_root: Path,
    administrator_path: Path,
    external_reference_plan: Any,
    snapshots: DirectorySnapshotRecorder,
) -> RequestExecutionOutcome:
    if __package__:
        from .config_generation import (
            ConfigGenerationError,
            prepare_external_customer_batches,
        )
    else:
        from config_generation import (  # type: ignore[no-redef]
            ConfigGenerationError,
            prepare_external_customer_batches,
        )

    try:
        with progress.stage(
            "REFERENCE",
            "Preprocess external customer reference",
            detail="Build the common customer reference SIW/AEDB pair.",
        ) as stage:
            reference_result = run_external_reference_preprocessor(
                external_reference_plan
            )
            stage.complete(
                f"status=ready, branch={reference_result.mode}, "
                f"aedt={reference_result.aedt_version}"
            )
        snapshots.capture(
            "after-reference-preprocess",
            creator="external-reference-preprocessor",
        )

        with progress.stage(
            "CONFIG",
            "Generate strict customer batch Configs",
            detail="Resolve the customer Spec into one strict Run Config per sNp batch.",
        ) as stage:
            preparation = prepare_external_customer_batches(
                config_path,
                administrator_config_path=administrator_path,
                reference_result=reference_result,
                work_root=job_root / "work" / "fullbatch",
                progress=progress,
            )
            generation = preparation.outcome
            if generation.status != 0:
                stage.fail(f"status={generation.status}")
            else:
                stage.complete(f"batches={len(generation.run_configs)}")
        snapshots.capture(
            "after-config-generation",
            creator="strict-customer-config-generation",
        )
    except (ReferencePreprocessError, ConfigGenerationError) as exc:
        progress.error("FULLBATCH", f"Customer preparation stopped: {exc}")
        return RequestExecutionOutcome(
            status=2,
            batches=(),
            result_dirs=(),
            error=str(exc),
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        progress.error("FULLBATCH", "Unexpected preparation failure; see the full log.")
        return RequestExecutionOutcome(
            status=1,
            batches=(),
            result_dirs=(),
            error=str(exc).strip() or type(exc).__name__,
        )

    if generation.status != 0 or not generation.run_configs:
        return RequestExecutionOutcome(
            status=generation.status or 2,
            batches=(),
            result_dirs=(),
            generation_manifest=generation.batch_manifest or generation.manifest,
            error="strict customer batch Config generation did not complete",
        )

    batch_records: list[BatchExecutionInput] = []
    result_dirs: list[Path] = []

    def snapshot_callback(stage: str, batch_name: str) -> None:
        snapshots.capture(stage, creator=f"batch:{batch_name}")

    for index, generated_config in enumerate(generation.run_configs, start=1):
        config = generated_config.resolve()
        run_dir = config.parent.parent.resolve()
        progress.batch(index, len(generation.run_configs), _prepared_config_display_name(config))
        try:
            status = _run_prepared_config(
                args,
                config,
                generated_run_dir=run_dir,
                progress=progress,
                snapshot_callback=snapshot_callback,
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            error = str(exc).strip() or type(exc).__name__
            batch_records.append(
                BatchExecutionInput(
                    config_path=config,
                    run_dir=run_dir,
                    status_code=1,
                    error=error,
                )
            )
            return RequestExecutionOutcome(
                status=1,
                batches=tuple(batch_records),
                result_dirs=tuple(result_dirs),
                generation_manifest=generation.batch_manifest or generation.manifest,
                error=error,
            )
        batch_records.append(
            BatchExecutionInput(
                config_path=config,
                run_dir=run_dir,
                status_code=status,
                error=f"batch stopped with exit code {status}" if status else None,
            )
        )
        result_dirs.append(run_dir)
        if status != 0:
            return RequestExecutionOutcome(
                status=status,
                batches=tuple(batch_records),
                result_dirs=tuple(result_dirs),
                generation_manifest=generation.batch_manifest or generation.manifest,
                error=f"batch stopped with exit code {status}",
            )
    return RequestExecutionOutcome(
        status=0,
        batches=tuple(batch_records),
        result_dirs=tuple(result_dirs),
        generation_manifest=generation.batch_manifest or generation.manifest,
    )


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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = _resolve_config_input_path(args.config)
    try:
        request_payload = preflight_request_json(config_path)
        job_input_resolution = (
            resolve_job_inputs(config_path, payload=request_payload)
            if is_external_heaven_request(request_payload)
            else None
        )
        administrator_config = None
        administrator_path = None
        resolved_analysis_options = None
        external_reference_plan = None
        array_bom_preflight = None
        if job_input_resolution is not None:
            administrator_path = administrator_config_path()
            administrator_config = validate_administrator_config(
                load_config(administrator_path)
            )
            resolved_analysis_options = resolve_analysis_option_catalog(
                administrator_config,
                job_root=job_input_resolution.job_root,
            )
            external_reference_plan = build_external_reference_preprocess_plan(
                administrator_config,
                job_input_resolution,
                resolved_analysis_options,
                administrator_config_path=administrator_path,
                work_dir=job_input_resolution.job_root / "work" / "reference_preprocess",
            )
            if __package__:
                from .channel.customer_components import resolve_customer_array_catalog
            else:
                from channel.customer_components import resolve_customer_array_catalog
            array_bom_preflight = resolve_customer_array_catalog(
                administrator_config,
                bom_path=job_input_resolution.input("BOM").path,
                administrator_config_path=administrator_path,
                job_root=job_input_resolution.job_root,
            )
    except (OSError, ValueError, ReferencePreprocessError) as exc:
        print(f"SI-TDR preflight failed: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "preflight_only", False):
        print(f"SI-TDR preflight passed: {config_path}")
        if job_input_resolution is not None:
            for item in job_input_resolution.inputs:
                print(
                    f"  {item.field.json_path_text}: {item.path} "
                    f"(selected from {item.selected_from})"
                )
            assert administrator_config is not None
            assert administrator_path is not None
            assert resolved_analysis_options is not None
            assert external_reference_plan is not None
            assert array_bom_preflight is not None
            print(
                "  administrator Config: "
                f"{administrator_path} "
                f"(designInputType="
                f"{external_reference_plan.design_input_type}, "
                f"aedtVersion={administrator_config['aedtVersion']})"
            )
            print(
                "  analysis option defaults: "
                f"SYZ={resolved_analysis_options.default_syz_profile_id}, "
                f"TDR={resolved_analysis_options.default_tdr_profile_id} "
                "(resolved from Job Setup/SWS, Setup/SFSDF, Setup/TDR folders)"
            )
            print(
                "  reference preprocessing: "
                f"branch={external_reference_plan.branch}, "
                f"SYZ={external_reference_plan.syz_profile_id}, "
                f"SWS={external_reference_plan.sws.name}, "
                f"aedtVersion={external_reference_plan.aedt_version}"
            )
            print(
                "  Array resistor BOM source: "
                "configuredPriority="
                f"{array_bom_preflight['selector']['arrayResistanceSourceColumns']}, "
                "selectedColumn="
                f"{array_bom_preflight['resolvedColumns']['arrayResistanceSource']}, "
                f"sha256={array_bom_preflight['bom']['sha256']}, "
                f"AR rows={len(array_bom_preflight['arrayCatalog'])}"
            )
        return 0
    if getattr(args, "prepare_reference_only", False):
        if external_reference_plan is None:
            print(
                "SI-TDR reference preprocessing requires an external Heaven request",
                file=sys.stderr,
            )
            return 2
        try:
            result = run_external_reference_preprocessor(external_reference_plan)
        except ReferencePreprocessError as exc:
            print(f"SI-TDR reference preprocessing failed: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "status": "ready",
                    "designInputType": result.design_input_type,
                    "preprocessingBranch": result.mode,
                    "aedtVersion": result.aedt_version,
                    "referenceSiw": str(result.reference_siw),
                    "referenceAedb": str(result.reference_aedb),
                    "manifest": str(result.manifest_path),
                    "manifestId": result.manifest_id,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    log_dir = _resolve_log_dir(config_path, getattr(args, "log_dir", None))
    started_at = datetime.now().astimezone()

    default_external_fullbatch = _default_external_fullbatch_requested(
        args,
        external_request=job_input_resolution is not None,
    )
    if default_external_fullbatch:
        assert job_input_resolution is not None
        assert administrator_path is not None
        assert external_reference_plan is not None
        fullbatch_args = _customer_fullbatch_args(args)
        snapshots = DirectorySnapshotRecorder(job_input_resolution.job_root)
        try:
            snapshots.capture("before-execution", creator="main.py FullBatch orchestrator")
        except FullBatchPublishError as exc:
            print(f"SI-TDR FullBatch snapshot failed: {exc}", file=sys.stderr)
            return 1

        with ConsoleLogSession(log_dir) as log_session:
            assert log_session.progress_stream is not None
            progress = ConsoleProgress(stream=log_session.progress_stream)
            progress.start_run(
                config_path,
                _requested_operation_labels(fullbatch_args),
                full_log_path=log_session.full_log_path,
                progress_log_path=log_session.progress_log_path,
            )
            try:
                ensure_publish_target_available(job_input_resolution.job_root / "outputs")
            except FullBatchPublishError as exc:
                progress.error("PUBLISH", str(exc))
                progress.finish_run(2, result_dirs=())
                return 2

            outcome = _execute_external_customer_fullbatch(
                fullbatch_args,
                config_path,
                progress,
                job_root=job_input_resolution.job_root,
                administrator_path=administrator_path,
                external_reference_plan=external_reference_plan,
                snapshots=snapshots,
            )
            status = outcome.status
            result_dirs = list(outcome.result_dirs)
            if status == 0:
                try:
                    with progress.stage(
                        "PUBLISH",
                        "Validate and publish customer FullBatch results",
                        detail=(
                            "Build a separate staging tree, enforce the provisional "
                            "allowlist and JSON references, then atomically publish outputs."
                        ),
                    ) as stage:
                        prepare_public_reference_archive(
                            job_input_resolution.job_root,
                            str(administrator_config["aedtVersion"]),
                        )
                        published = publish_fullbatch_results(
                            request_path=config_path,
                            job_root=job_input_resolution.job_root,
                            runtime_root=ROOT_DIR,
                            batches=outcome.batches,
                            started_at=started_at,
                            completed_at=datetime.now().astimezone(),
                            generation_manifest=outcome.generation_manifest,
                        )
                        stage.complete(
                            f"contract={published.contract}, files={len(published.public_files)}"
                        )
                    progress.artifact("Customer outputs", published.output_dir)
                    progress.detail("Publish manifest", published.manifest_path)
                    result_dirs.append(published.output_dir)
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    progress.error("PUBLISH", f"FullBatch publish failed: {exc}")
                    try:
                        snapshots.capture(
                            "publish-failed",
                            creator="provisional FullBatch publisher",
                        )
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                    status = 1
            progress.finish_run(status, result_dirs=result_dirs)
            return status

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
