from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    try:
        from .console_progress import ConsoleProgress
    except ImportError:  # Support direct execution from the SI_TDR folder.
        from console_progress import ConsoleProgress  # type: ignore[no-redef]

try:
    from .channel import (
        build_minimal_discovery_targets,
        build_minimal_input_pin_requests,
        build_minimal_path_net_evidence,
        DetailedInputValidationError,
        load_array_mapping_library,
        load_channel_targets_csv,
        normalize_detailed_csv,
        load_minimal_channel_inputs_csv,
        load_minimal_input_profile,
        load_minimal_target_overrides,
        lookup_pin_requests,
        normalize_minimal_inputs,
        write_channel_path_report,
        write_detailed_channel_targets_csv,
        write_detailed_normalization_report,
        write_minimal_normalization_report,
        write_normalized_channel_targets_csv,
        write_port_metadata_from_channel_paths,
        write_tdr_config_fragment,
        write_time_range_resolution,
    )
    from .channel.run_config import build_run_config
    from .channel.series_models import validate_treatment_config
except ImportError:  # Support direct execution of main.py from the SI_TDR folder.
    from channel import (  # type: ignore[no-redef]
        build_minimal_discovery_targets,
        build_minimal_input_pin_requests,
        build_minimal_path_net_evidence,
        DetailedInputValidationError,
        load_array_mapping_library,
        load_channel_targets_csv,
        normalize_detailed_csv,
        load_minimal_channel_inputs_csv,
        load_minimal_input_profile,
        load_minimal_target_overrides,
        lookup_pin_requests,
        normalize_minimal_inputs,
        write_channel_path_report,
        write_detailed_channel_targets_csv,
        write_detailed_normalization_report,
        write_minimal_normalization_report,
        write_normalized_channel_targets_csv,
        write_port_metadata_from_channel_paths,
        write_tdr_config_fragment,
        write_time_range_resolution,
    )
    from channel.run_config import build_run_config  # type: ignore[no-redef]
    from channel.series_models import validate_treatment_config  # type: ignore[no-redef]

try:
    from .preprocess import (
        ReferencePreprocessError,
        load_reference_preprocess_manifest,
        run_reference_preprocessor,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from preprocess import (  # type: ignore[no-redef]
        ReferencePreprocessError,
        load_reference_preprocess_manifest,
        run_reference_preprocessor,
    )


class ConfigGenerationError(ValueError):
    """Raised when the customer generation request is invalid."""


@dataclass(frozen=True)
class ConfigGenerationOutcome:
    status: int
    output_dir: Path
    run_config: Path
    manifest: Path
    unresolved: Path
    run_configs: tuple[Path, ...] = ()
    batch_manifest: Path | None = None


def _path_status_counts(path_result) -> dict[str, int]:
    counts = {"resolved": 0, "dropped": 0, "unresolved": 0}
    for path in path_result.paths:
        status = str(path.status)
        if status.startswith("resolved"):
            counts["resolved"] += 1
        elif status.startswith("dropped"):
            counts["dropped"] += 1
        else:
            counts["unresolved"] += 1
    return counts


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _channel_path_unresolved(path_result, target_keys_by_channel: dict[str, str]) -> list[dict]:
    unresolved: list[dict] = []
    for path in path_result.paths:
        if str(path.status).startswith("resolved"):
            continue
        unresolved.append(
            {
                "targetKey": target_keys_by_channel.get(path.channel),
                "channel": path.channel,
                "polarity": path.polarity,
                "stage": "channel_path",
                "code": path.status,
                "message": path.error,
                "start": {
                    "component": path.start_component,
                    "pin": path.start_pin,
                    "net": path.start_net,
                },
                "endpointCandidates": path.endpoint_candidates or [],
                "requiredOverride": [
                    "endpointRefdes",
                    "endpointPosPin",
                    "endpointNegPin",
                    "reason",
                ],
            }
        )
    return unresolved


def _generate(argv: list[str] | None = None) -> ConfigGenerationOutcome:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a main.py-ready SI-TDR config from target CSV and AEDB by running "
            "Channel Path traversal, TDR config fragment generation, and port metadata generation."
        )
    )
    parser.add_argument("csv", type=Path, help="Target channel CSV path")
    parser.add_argument("aedb", type=Path, help="Reference AEDB path")
    parser.add_argument("base_config", type=Path, help="Base main.py config JSON")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Directory for generated JSON artifacts")
    parser.add_argument("--name", default="csv_path_run", help="Artifact file name prefix")
    parser.add_argument(
        "--reference-siw",
        type=Path,
        help="Optional staged SIW paired with the AEDB; written into the generated Run Config.",
    )
    parser.add_argument(
        "--reference-preprocess-manifest",
        type=Path,
        help=(
            "Internal SI-TDR reference-preprocess manifest. Its reference paths "
            "must match the AEDB/SIW arguments."
        ),
    )
    parser.add_argument("--aedt-version", default="2024.2", help="AEDT version used to open AEDB")
    parser.add_argument(
        "--array-mapping",
        type=Path,
        help=(
            "Optional legacy Part Library JSON/config. New SI-TDR Configs may define "
            "BOM.compProp rules directly in base_config instead."
        ),
    )
    parser.add_argument(
        "--series-treatment",
        type=Path,
        help=(
            "Optional JSON containing seriesTreatment policy. When supplied, the selected "
            "--array-mapping/Part Library and generated Channel Path report are wired into "
            "the run config so models are installed before port creation."
        ),
    )
    input_mode = parser.add_mutually_exclusive_group()
    input_mode.add_argument(
        "--minimal-profile",
        type=Path,
        help="Treat csv as the 3-column minimal schema and normalize it with this rule/template profile.",
    )
    parser.add_argument(
        "--snp-file",
        help=(
            "Select one normalized sNp batch by its internal base name. Final customer "
            "CSVs derive this name from Function + Version + Designator + Group + "
            "Direction; the legacy explicit SNP_File column remains supported."
        ),
    )
    input_mode.add_argument(
        "--detailed-input",
        action="store_true",
        help=(
            "Treat csv as the customer detailed Reference schema, validate adjacent P/M rows, "
            "and normalize it to the internal ChannelTarget model."
        ),
    )
    parser.add_argument(
        "--minimal-overrides",
        type=Path,
        help="Optional target-key override JSON for unresolved minimal-input decisions.",
    )
    parser.add_argument("--max-depth", type=int, default=8, help="Maximum Channel Path traversal depth")
    parser.add_argument("--port-order-template", type=Path, help="Optional Golden-compatible portOrder template JSON")
    parser.add_argument(
        "--strict-port-order-template",
        action="store_true",
        help="Fail if template contains ports not produced by current channel paths.",
    )
    parser.add_argument(
        "--port-layer",
        help=(
            "Legacy positive-terminal layer override. Omit it to resolve each "
            "Padstack Pin layer automatically from EDB GetLayerRange()."
        ),
    )
    parser.add_argument(
        "--reference-layer",
        help=(
            "Optional Reference-net point-terminal layer override. Base Config "
            "ports.referenceLayer remains the normal source."
        ),
    )
    parser.add_argument("--port-impedance-ohm", type=float, default=50.0, help="Single-ended port impedance")
    parser.add_argument("--strategy", default="csv-path-generated-metadata-snp", help="Run strategy name")
    parser.add_argument("--scope", default="csv-path-generated-port-metadata", help="Run scope name")
    args = parser.parse_args(argv)
    if args.minimal_overrides and not args.minimal_profile:
        parser.error("--minimal-overrides requires --minimal-profile")
    if args.snp_file and not args.detailed_input:
        parser.error("--snp-file requires --detailed-input")
    if args.reference_siw and not args.reference_siw.is_file():
        parser.error(f"reference SIW not found: {args.reference_siw}")
    input_provenance = None
    if args.reference_preprocess_manifest:
        try:
            preprocess_result = load_reference_preprocess_manifest(
                args.reference_preprocess_manifest
            )
        except ReferencePreprocessError as exc:
            parser.error(str(exc))
        if args.aedb.resolve() != preprocess_result.reference_aedb:
            parser.error(
                "AEDB argument does not match reference preprocess manifest"
            )
        if args.reference_siw is None:
            parser.error(
                "reference preprocess manifest requires the paired --reference-siw"
            )
        if args.reference_siw.resolve() != preprocess_result.reference_siw:
            parser.error(
                "reference SIW argument does not match reference preprocess manifest"
            )
        input_provenance = preprocess_result.as_input_provenance()
    try:
        base_payload = json.loads(args.base_config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        parser.error(f"base config not found: {args.base_config}")
    except json.JSONDecodeError as exc:
        parser.error(f"invalid base config JSON {args.base_config}: {exc}")
    if not isinstance(base_payload, dict):
        parser.error(f"base config root must be an object: {args.base_config}")

    inline_bom = base_payload.get("BOM")
    has_inline_bom_rules = isinstance(inline_bom, dict) and inline_bom.get("compProp") is not None
    array_mapping_path = args.array_mapping
    if has_inline_bom_rules:
        if array_mapping_path is not None and array_mapping_path.resolve() != args.base_config.resolve():
            parser.error(
                "base config BOM.compProp and --array-mapping cannot both define Part rules"
            )
        array_mapping_path = args.base_config

    inline_treatment = base_payload.get("seriesTreatment")
    if inline_treatment is not None and not isinstance(inline_treatment, dict):
        parser.error(f"{args.base_config} seriesTreatment must be an object")
    if (
        inline_treatment is not None
        and args.series_treatment is not None
        and args.series_treatment.resolve() != args.base_config.resolve()
    ):
        parser.error(
            "base config seriesTreatment and --series-treatment cannot both define policy"
        )

    series_treatment = inline_treatment
    series_treatment_source = args.base_config if inline_treatment is not None else None
    if args.series_treatment:
        try:
            treatment_payload = json.loads(args.series_treatment.read_text(encoding="utf-8"))
        except FileNotFoundError:
            parser.error(f"series treatment config not found: {args.series_treatment}")
        except json.JSONDecodeError as exc:
            parser.error(f"invalid series treatment JSON {args.series_treatment}: {exc}")
        if not isinstance(treatment_payload, dict) or not isinstance(
            treatment_payload.get("seriesTreatment"), dict
        ):
            parser.error(
                f"{args.series_treatment} must contain a seriesTreatment object"
            )
        series_treatment = treatment_payload["seriesTreatment"]
        series_treatment_source = args.series_treatment
    if series_treatment is not None and array_mapping_path is None:
        parser.error("seriesTreatment requires BOM.compProp or --array-mapping/Part Library")
    try:
        load_array_mapping_library(array_mapping_path)
        if series_treatment is not None:
            validate_treatment_config(series_treatment)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.name
    path_report = output_dir / f"{prefix}_channel_paths.json"
    config_fragment = output_dir / f"{prefix}_tdr_config_fragment.json"
    port_metadata = output_dir / f"{prefix}_port_metadata.json"
    run_config = output_dir / f"{prefix}_run_config.json"
    manifest_path = output_dir / f"{prefix}_manifest.json"
    unresolved_path = output_dir / f"{prefix}_unresolved.json"
    time_range_path = output_dir / f"{prefix}_tdr_time_range.json"
    normalization_report_path = output_dir / (
        f"{prefix}_detailed_input_report.json"
        if args.detailed_input
        else f"{prefix}_minimal_input_report.json"
    )
    normalized_csv_path = output_dir / f"{prefix}_normalized_targets.csv"
    discovery_path_report = output_dir / f"{prefix}_minimal_discovery_paths.json"

    normalization_result = None
    normalization_kind: str | None = None
    selected_snp_file: str | None = None
    normalization_input_count = 0
    target_keys_by_channel: dict[str, str] = {}
    if args.minimal_profile:
        minimal_inputs = load_minimal_channel_inputs_csv(args.csv)
        normalization_kind = "minimal"
        normalization_input_count = len(minimal_inputs)
        profile = load_minimal_input_profile(args.minimal_profile)
        overrides = load_minimal_target_overrides(args.minimal_overrides)
        discovery_targets, discovery_target_keys = build_minimal_discovery_targets(minimal_inputs)
        discovery_result = write_channel_path_report(
            args.aedb,
            discovery_targets,
            discovery_path_report,
            aedt_version=args.aedt_version,
            array_mapping_path=array_mapping_path,
            max_depth=args.max_depth,
        )
        path_net_evidence = build_minimal_path_net_evidence(
            discovery_result, discovery_target_keys
        )
        pin_lookup = lookup_pin_requests(
            args.aedb,
            build_minimal_input_pin_requests(minimal_inputs),
            aedt_version=args.aedt_version,
        )
        normalization_result = normalize_minimal_inputs(
            minimal_inputs,
            pin_lookup,
            profile,
            source_csv=args.csv,
            overrides=overrides,
            path_net_evidence=path_net_evidence,
        )
        write_minimal_normalization_report(normalization_result, normalization_report_path)
        write_normalized_channel_targets_csv(normalization_result, normalized_csv_path)
        targets = normalization_result.targets
        target_keys_by_channel = {
            str(record["normalizedTarget"]["interface"])
            + "_"
            + str(record["normalizedTarget"]["channel"]): str(record["targetKey"])
            for record in normalization_result.records
            if record.get("normalizedTarget")
        }
        if normalization_result.unresolved:
            unresolved_payload = {
                "version": 2,
                "status": "blocked_normalization_unresolved",
                "normalization": normalization_result.unresolved,
                "channelPath": [],
                "portRoleMetadata": [],
                "portMetadata": [],
                "tdrTimeRange": [],
            }
            _write_json(unresolved_path, unresolved_payload)
            manifest = {
                "status": "blocked_normalization_unresolved",
                "inputFormat": normalization_kind,
                "csv": str(args.csv),
                "aedb": str(args.aedb),
                "baseConfig": str(args.base_config),
                "referenceSiw": str(args.reference_siw) if args.reference_siw else None,
                "referencePreprocessManifest": (
                    str(args.reference_preprocess_manifest)
                    if args.reference_preprocess_manifest
                    else None
                ),
                "referencePreprocessManifestId": (
                    input_provenance.get("manifestId")
                    if input_provenance
                    else None
                ),
                "minimalProfile": str(args.minimal_profile),
                "minimalOverrides": str(args.minimal_overrides) if args.minimal_overrides else None,
                "minimalInputReport": str(normalization_report_path),
                "minimalDiscoveryPathReport": str(discovery_path_report),
                "normalizedTargetsCsv": str(normalized_csv_path),
                "combinedUnresolved": str(unresolved_path),
                "summary": {
                    "targetsInput": len(minimal_inputs),
                    "targetsNormalized": len(targets),
                    "normalizationUnresolved": len(normalization_result.unresolved),
                },
            }
            _write_json(manifest_path, manifest)
            for key, value in manifest["summary"].items():
                print(f"{key}={value}")
            print(f"unresolved={unresolved_path}")
            print(f"manifest={manifest_path}")
            return ConfigGenerationOutcome(
                status=2,
                output_dir=output_dir,
                run_config=run_config,
                manifest=manifest_path,
                unresolved=unresolved_path,
            )
        # Round-trip through the existing parser to enforce the internal model boundary.
        targets = load_channel_targets_csv(normalized_csv_path)
    elif args.detailed_input:
        normalization_kind = "detailed"
        normalization_result = normalize_detailed_csv(args.csv)
        normalization_input_count = len(normalization_result.records)
        write_detailed_normalization_report(
            normalization_result,
            normalization_report_path,
        )
        if args.snp_file:
            try:
                selected_batch = normalization_result.batch_for(args.snp_file)
            except DetailedInputValidationError as exc:
                parser.error(str(exc))
            selected_snp_file = selected_batch.snp_file
            targets = selected_batch.targets
            normalization_input_count = len(selected_batch.pairs)
        else:
            targets = normalization_result.targets
            if not normalization_result.unresolved and len(normalization_result.batches) > 1:
                parser.error(
                    "detailed CSV contains multiple sNp batches; select one with --snp-file"
                )
            if len(normalization_result.batches) == 1:
                selected_snp_file = normalization_result.batches[0].snp_file
        write_detailed_channel_targets_csv(
            normalization_result,
            normalized_csv_path,
            snp_file=selected_snp_file,
        )
        target_keys_by_channel = {target.name: target.name for target in targets}
        if normalization_result.unresolved:
            unresolved_payload = {
                "version": 1,
                "status": "blocked_normalization_unresolved",
                "normalization": normalization_result.unresolved,
                "channelPath": [],
                "portMetadata": [],
                "tdrTimeRange": [],
            }
            _write_json(unresolved_path, unresolved_payload)
            manifest = {
                "status": "blocked_normalization_unresolved",
                "inputFormat": normalization_kind,
                "snpFile": selected_snp_file,
                "csv": str(args.csv),
                "aedb": str(args.aedb),
                "baseConfig": str(args.base_config),
                "detailedInputReport": str(normalization_report_path),
                "normalizedTargetsCsv": str(normalized_csv_path),
                "combinedUnresolved": str(unresolved_path),
                "summary": {
                    "targetsInput": normalization_input_count,
                    "targetsNormalized": len(targets),
                    "normalizationUnresolved": len(normalization_result.unresolved),
                    "snpFiles": len(normalization_result.batches),
                },
            }
            _write_json(manifest_path, manifest)
            for key, value in manifest["summary"].items():
                print(f"{key}={value}")
            print(f"unresolved={unresolved_path}")
            print(f"manifest={manifest_path}")
            return ConfigGenerationOutcome(
                status=2,
                output_dir=output_dir,
                run_config=run_config,
                manifest=manifest_path,
                unresolved=unresolved_path,
            )
        # Round-trip through the existing parser to enforce the adapter boundary.
        targets = load_channel_targets_csv(normalized_csv_path)
    else:
        targets = load_channel_targets_csv(args.csv)

    path_result = write_channel_path_report(
        args.aedb,
        targets,
        path_report,
        aedt_version=args.aedt_version,
        array_mapping_path=array_mapping_path,
        max_depth=args.max_depth,
    )
    fragment = write_tdr_config_fragment(
        targets,
        path_report,
        config_fragment,
        port_order_template_path=args.port_order_template,
        strict_port_order_template=args.strict_port_order_template,
    )
    fragment_unresolved = list(fragment.get("unresolved") or [])
    if fragment_unresolved:
        path_unresolved = _channel_path_unresolved(
            path_result, target_keys_by_channel
        )
        unresolved_payload = {
            "version": 2,
            "status": "blocked_port_role_metadata_unresolved",
            "normalization": (
                normalization_result.unresolved if normalization_result else []
            ),
            "channelPath": path_unresolved,
            "portRoleMetadata": fragment_unresolved,
            "portMetadata": [],
            "tdrTimeRange": [],
        }
        _write_json(unresolved_path, unresolved_payload)
        manifest = {
            "status": unresolved_payload["status"],
            "csv": str(args.csv),
            "aedb": str(args.aedb),
            "baseConfig": str(args.base_config),
            "channelPathReport": str(path_report),
            "tdrConfigFragment": str(config_fragment),
            "combinedUnresolved": str(unresolved_path),
            "summary": {
                "targets": len(targets),
                "pathsTotal": len(path_result.paths),
                "pathsResolved": path_result.resolved_count,
                "portRoleMetadataUnresolved": len(fragment_unresolved),
            },
        }
        _write_json(manifest_path, manifest)
        return ConfigGenerationOutcome(
            status=1,
            output_dir=output_dir,
            run_config=run_config,
            manifest=manifest_path,
            unresolved=unresolved_path,
        )
    metadata = write_port_metadata_from_channel_paths(
        path_report,
        config_fragment,
        port_metadata,
        layer=args.port_layer,
        impedance_ohm=args.port_impedance_ohm,
    )
    syz_template_id = getattr(normalization_result, "syz_template_id", None)
    syz_frequency_sweep = getattr(normalization_result, "syz_frequency_sweep", None)
    config = build_run_config(
        base_config_path=args.base_config,
        config_fragment_path=config_fragment,
        port_metadata_path=port_metadata,
        output_path=run_config,
        strategy=args.strategy,
        scope=args.scope,
        syz_template_id=syz_template_id,
        syz_frequency_sweep=syz_frequency_sweep,
        part_library_path=array_mapping_path if series_treatment is not None else None,
        channel_path_report_path=path_report,
        series_treatment=series_treatment,
        reference_edb_path=args.aedb,
        reference_siw_path=args.reference_siw,
        aedt_version=args.aedt_version,
        port_impedance_ohm=args.port_impedance_ohm,
        analysis_settings={
            **_read_profile_analysis_settings(args.minimal_profile),
            **(
                {
                    "name": selected_snp_file,
                    "touchstoneBaseName": selected_snp_file,
                }
                if selected_snp_file
                else {}
            ),
            "referenceLayer": args.reference_layer,
        },
        input_provenance=input_provenance,
    )
    analysis_template_selection = config.get("analysisTemplateSelection")
    if not isinstance(analysis_template_selection, dict):
        analysis_template_selection = {}
    resolved_syz_template_id = (
        analysis_template_selection.get("syzTemplateId") or syz_template_id
    )
    resolved_tdr_template_id = analysis_template_selection.get("tdrTemplateId")
    time_range_resolution = config["tdr"]["timeRangeResolution"]
    write_time_range_resolution(time_range_path, time_range_resolution)
    status_counts = _path_status_counts(path_result)
    path_unresolved = _channel_path_unresolved(path_result, target_keys_by_channel)
    missing_ports = metadata["selection"]["missingPorts"]
    port_metadata_unresolved = (
        [
            {
                "stage": "port_metadata",
                "code": "missing_port_metadata",
                "message": "one or more requested ports could not be generated from resolved channel paths",
                "missingPorts": missing_ports,
            }
        ]
        if missing_ports
        else []
    )
    time_range_unresolved = (
        [
            {
                "stage": "tdr_time_range",
                "code": "time_range_unresolved",
                "message": "route-length TDR view/stop policy could not be resolved",
                "issues": time_range_resolution.get("issues") or [],
                "evidence": str(time_range_path),
            }
        ]
        if time_range_resolution.get("status") != "resolved"
        else []
    )
    has_unresolved = bool(
        path_unresolved or port_metadata_unresolved or time_range_unresolved
    )
    unresolved_payload = {
        "version": 2,
        "status": "blocked_preparation_unresolved" if has_unresolved else "ok",
        "normalization": normalization_result.unresolved if normalization_result else [],
        "channelPath": path_unresolved,
        "portRoleMetadata": [],
        "portMetadata": port_metadata_unresolved,
        "tdrTimeRange": time_range_unresolved,
    }
    _write_json(unresolved_path, unresolved_payload)

    manifest = {
        "status": unresolved_payload["status"],
        "inputFormat": normalization_kind or "channel-targets",
        "snpFile": selected_snp_file,
        "csv": str(args.csv),
        "aedb": str(args.aedb),
        "baseConfig": str(args.base_config),
        "referenceSiw": str(args.reference_siw) if args.reference_siw else None,
        "referencePreprocessManifest": (
            str(args.reference_preprocess_manifest)
            if args.reference_preprocess_manifest
            else None
        ),
        "referencePreprocessManifestId": (
            input_provenance.get("manifestId") if input_provenance else None
        ),
        "minimalProfile": str(args.minimal_profile) if args.minimal_profile else None,
        "minimalOverrides": str(args.minimal_overrides) if args.minimal_overrides else None,
        "minimalInputReport": (
            str(normalization_report_path) if normalization_kind == "minimal" else None
        ),
        "detailedInputReport": (
            str(normalization_report_path) if normalization_kind == "detailed" else None
        ),
        "minimalDiscoveryPathReport": (
            str(discovery_path_report) if normalization_kind == "minimal" else None
        ),
        "normalizedTargetsCsv": str(normalized_csv_path) if normalization_result else None,
        "combinedUnresolved": str(unresolved_path),
        "arrayMapping": str(array_mapping_path) if array_mapping_path else None,
        "seriesTreatmentConfig": (
            str(series_treatment_source) if series_treatment_source else None
        ),
        "channelPathReport": str(path_report),
        "tdrConfigFragment": str(config_fragment),
        "portMetadata": str(port_metadata),
        "tdrTimeRange": str(time_range_path),
        "runConfig": str(run_config),
        "syzTemplateId": resolved_syz_template_id,
        "tdrTemplateId": resolved_tdr_template_id,
        "summary": {
            "targets": len(targets),
            "normalizationUnresolved": len(normalization_result.unresolved) if normalization_result else 0,
            "snpFiles": (
                len(normalization_result.batches)
                if normalization_kind == "detailed"
                else None
            ),
            "pathsTotal": len(path_result.paths),
            "pathsResolved": path_result.resolved_count,
            "pathsDropped": status_counts["dropped"],
            "pathsUnresolved": status_counts["unresolved"],
            "channels": len(fragment["tdr"]["channels"]),
            "portCount": fragment["ports"]["touchstonePortCount"],
            "portMetadataSelected": metadata["selection"]["selectedPortCount"],
            "portMetadataMissing": len(missing_ports),
            "combinedUnresolved": (
                len(path_unresolved)
                + len(port_metadata_unresolved)
                + len(time_range_unresolved)
            ),
            "tdrTimeRangeStatus": time_range_resolution.get("status"),
            "tdrTimeRangeMode": time_range_resolution.get("mode"),
            "seriesModelsConfigured": series_treatment is not None,
            "strategy": config["segment"]["strategy"],
        },
    }
    _write_json(manifest_path, manifest)

    for key, value in manifest["summary"].items():
        print(f"{key}={value}")
    print(f"runConfig={run_config}")
    print(f"unresolved={unresolved_path}")
    print(f"manifest={manifest_path}")
    return ConfigGenerationOutcome(
        status=1 if has_unresolved else 0,
        output_dir=output_dir,
        run_config=run_config,
        manifest=manifest_path,
        unresolved=unresolved_path,
    )


def _read_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigGenerationError(f"generation request not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigGenerationError(f"invalid generation request JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigGenerationError(f"generation request root must be an object: {path}")
    if payload.get("schemaVersion") != 1:
        raise ConfigGenerationError(
            f"unsupported generation request schemaVersion={payload.get('schemaVersion')!r}; expected 1"
        )
    return payload


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigGenerationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigGenerationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigGenerationError(f"{label} root must be an object: {path}")
    return payload


def _read_profile_analysis_settings(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = _read_json_object(path, label="minimal input profile")
    raw_settings = payload.get("analysis") or {}
    if not isinstance(raw_settings, dict):
        raise ConfigGenerationError(
            f"minimal input profile analysis must be an object: {path}"
        )

    settings: dict[str, str] = {}
    for key in ("name", "interface", "referenceNet", "referenceLayer"):
        value = raw_settings.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ConfigGenerationError(
                f"minimal input profile analysis.{key} must be a non-empty string"
            )
        settings[key] = value.strip()
    return settings


def _request_string(payload: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ConfigGenerationError(f"generation request requires {key}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigGenerationError(f"generation request {key} must be a non-empty string")
    return value.strip()


def _request_identifier(
    payload: dict[str, Any],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = _request_string(payload, key) or default
    if value is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
        raise ConfigGenerationError(
            f"generation request {key} must use only letters, numbers, dot, underscore, and hyphen"
        )
    return value


def _resolve_input_path(
    runtime_root: Path,
    request_dir: Path,
    payload: dict[str, Any],
    key: str,
    *,
    required: bool = False,
) -> Path | None:
    raw_value = _request_string(payload, key, required=required)
    if raw_value is None:
        return None
    path = Path(raw_value)
    if path.is_absolute():
        resolved = path.resolve()
        if resolved.exists():
            return resolved
        raise ConfigGenerationError(
            f"generation request {key} not found: {resolved}"
        )

    request_relative = (request_dir / path).resolve()
    if request_relative.exists():
        return request_relative
    runtime_relative = (runtime_root / path).resolve()
    if runtime_relative.exists():
        return runtime_relative
    raise ConfigGenerationError(
        f"generation request {key} not found: {request_relative}"
    )


def _replace_argv_option(argv: list[str], flag: str, value: str) -> list[str]:
    updated = list(argv)
    try:
        index = updated.index(flag)
    except ValueError as exc:
        raise ConfigGenerationError(f"internal generation option missing: {flag}") from exc
    if index + 1 >= len(updated):
        raise ConfigGenerationError(f"internal generation option has no value: {flag}")
    updated[index + 1] = value
    return updated


def prepare_from_request(
    request_path: Path,
    *,
    runtime_root: Path,
    work_root: Path,
    progress: ConsoleProgress | None = None,
) -> ConfigGenerationOutcome:
    """Generate a run config from a customer-editable CSV request."""

    payload = _read_request(request_path)
    request_dir = request_path.resolve().parent
    name = _request_identifier(payload, "name")
    strategy = _request_identifier(
        payload,
        "strategy",
        default="csv-path-generated-metadata-snp",
    )
    scope = _request_identifier(
        payload,
        "scope",
        default="csv-path-generated-port-metadata",
    )

    output_subdir_value = (
        _request_string(payload, "outputSubdir")
        or f"{name}/generated_config"
    )
    output_subdir = Path(output_subdir_value)
    if output_subdir.is_absolute() or ".." in output_subdir.parts:
        raise ConfigGenerationError(
            "generation request outputSubdir must stay below the configured work directory"
        )
    output_dir = (work_root / output_subdir).resolve()
    work_root_resolved = work_root.resolve()
    if output_dir != work_root_resolved and work_root_resolved not in output_dir.parents:
        raise ConfigGenerationError(
            "generation request outputSubdir resolved outside the configured work directory"
        )

    csv_path = _resolve_input_path(
        runtime_root,
        request_dir,
        payload,
        "csv",
        required=True,
    )
    base_config_path = _resolve_input_path(
        runtime_root,
        request_dir,
        payload,
        "baseConfig",
        required=True,
    )
    aedb_path = _resolve_input_path(
        runtime_root,
        request_dir,
        payload,
        "aedb",
    )
    reference_siw_path = _resolve_input_path(
        runtime_root,
        request_dir,
        payload,
        "referenceSiw",
    )
    reference_preprocess_manifest_path = None
    if aedb_path is None:
        assert base_config_path is not None
        try:
            if progress is not None:
                with progress.stage(
                    "REFERENCE",
                    "Build design reference",
                    detail="Use Zuken/ANF input with STK/BOM/SWS to prepare the SIW/AEDB pair.",
                ) as stage:
                    preprocess_result = run_reference_preprocessor(
                        base_config_path,
                        work_dir=work_root / name / "reference_preprocess",
                        runtime_root=runtime_root,
                    )
                    stage.complete(
                        f"SIW={preprocess_result.reference_siw.name}, "
                        f"AEDB={preprocess_result.reference_aedb.name}"
                    )
            else:
                preprocess_result = run_reference_preprocessor(
                    base_config_path,
                    work_dir=work_root / name / "reference_preprocess",
                    runtime_root=runtime_root,
                )
        except ReferencePreprocessError as exc:
            raise ConfigGenerationError(
                "generation request has no aedb and SI-TDR reference "
                f"preprocessing failed: {exc}"
            ) from exc
        aedb_path = preprocess_result.reference_aedb
        reference_siw_path = preprocess_result.reference_siw
        reference_preprocess_manifest_path = preprocess_result.manifest_path
    assert csv_path is not None
    assert aedb_path is not None
    assert base_config_path is not None

    minimal_profile_path = _resolve_input_path(
        runtime_root,
        request_dir,
        payload,
        "minimalProfile",
    )
    requested_input_format = _request_string(payload, "inputFormat")
    input_format = (
        requested_input_format.casefold()
        if requested_input_format is not None
        else ("minimal" if minimal_profile_path is not None else "channel-targets")
    )
    if input_format not in {"channel-targets", "minimal", "detailed"}:
        raise ConfigGenerationError(
            "generation request inputFormat must be channel-targets, minimal, or detailed"
        )
    if input_format == "minimal" and minimal_profile_path is None:
        raise ConfigGenerationError(
            "generation request inputFormat=minimal requires minimalProfile"
        )
    if input_format != "minimal" and minimal_profile_path is not None:
        raise ConfigGenerationError(
            f"generation request inputFormat={input_format} cannot use minimalProfile"
        )
    if input_format != "minimal" and _request_string(payload, "minimalOverrides") is not None:
        raise ConfigGenerationError(
            f"generation request inputFormat={input_format} cannot use minimalOverrides"
        )
    profile_analysis = _read_profile_analysis_settings(minimal_profile_path)

    base_config = _read_json_object(base_config_path, label="base config")
    base_preprocessing = base_config.get("preprocessing") or {}
    if not isinstance(base_preprocessing, dict):
        raise ConfigGenerationError(
            f"base config preprocessing must be an object: {base_config_path}"
        )
    if "aedtVersion" in payload:
        aedt_version = payload["aedtVersion"]
    elif "version" in base_preprocessing:
        aedt_version = base_preprocessing["version"]
    elif "aedtVersion" in base_config:
        aedt_version = base_config["aedtVersion"]
    else:
        aedt_version = "2024.2"
    if (
        isinstance(aedt_version, bool)
        or not isinstance(aedt_version, (str, int, float))
        or not str(aedt_version).strip()
    ):
        raise ConfigGenerationError("AEDT version must be a non-empty scalar value")
    base_ports = base_config.get("ports") or {}
    if not isinstance(base_ports, dict):
        raise ConfigGenerationError(
            f"base config ports must be an object: {base_config_path}"
        )

    legacy_port_layer = _request_string(payload, "portLayer")

    reference_layer = _request_string(payload, "referenceLayer")
    if reference_layer is None:
        reference_layer = str(
            profile_analysis.get("referenceLayer")
            or base_ports.get("referenceLayer")
            or ""
        ).strip()
    if not reference_layer:
        raise ConfigGenerationError(
            "reference layer must be configured by generation request referenceLayer, "
            "profile analysis.referenceLayer, or base config ports.referenceLayer"
        )

    raw_port_impedance = payload.get("portImpedanceOhm")
    if raw_port_impedance is None:
        raw_port_impedance = base_ports.get("singleEndedImpedanceOhm", 50.0)
    if isinstance(raw_port_impedance, bool):
        raise ConfigGenerationError("port impedance must be a positive number")
    try:
        port_impedance_ohm = float(raw_port_impedance)
    except (TypeError, ValueError) as exc:
        raise ConfigGenerationError("port impedance must be a positive number") from exc
    if port_impedance_ohm <= 0:
        raise ConfigGenerationError("port impedance must be a positive number")

    argv = [
        str(csv_path),
        str(aedb_path),
        str(base_config_path),
        "--output-dir",
        str(output_dir),
        "--name",
        name,
        "--aedt-version",
        str(aedt_version),
        "--max-depth",
        str(payload.get("maxDepth", 8)),
        "--reference-layer",
        reference_layer,
        "--port-impedance-ohm",
        str(port_impedance_ohm),
        "--strategy",
        strategy,
        "--scope",
        scope,
    ]
    if legacy_port_layer is not None:
        argv.extend(["--port-layer", legacy_port_layer])
    if input_format == "detailed":
        argv.append("--detailed-input")

    if reference_siw_path is not None:
        argv.extend(["--reference-siw", str(reference_siw_path)])
    if reference_preprocess_manifest_path is not None:
        argv.extend(
            [
                "--reference-preprocess-manifest",
                str(reference_preprocess_manifest_path),
            ]
        )

    optional_paths = {
        "partLibrary": "--array-mapping",
        "seriesTreatmentConfig": "--series-treatment",
        "minimalProfile": "--minimal-profile",
        "minimalOverrides": "--minimal-overrides",
        "portOrderTemplate": "--port-order-template",
    }
    for key, flag in optional_paths.items():
        path = (
            minimal_profile_path
            if key == "minimalProfile"
            else _resolve_input_path(runtime_root, request_dir, payload, key)
        )
        if path is not None:
            argv.extend([flag, str(path)])

    if payload.get("strictPortOrderTemplate", False):
        argv.append("--strict-port-order-template")

    if input_format != "detailed":
        if progress is None:
            return _generate(argv)
        with progress.stage(
            "CONFIG-BUILD",
            "Generate Channel Paths and run Config",
            detail=f"input format={input_format}",
        ) as stage:
            outcome = _generate(argv)
            if outcome.status == 0:
                stage.complete("batches=1, unresolved=0")
            else:
                stage.fail(
                    f"status={outcome.status}, unresolved={outcome.unresolved}"
                )
            return outcome

    try:
        if progress is not None:
            with progress.stage(
                "CSV",
                "Validate detailed CSV and build analysis batches",
                detail=f"input={csv_path.name}",
            ) as stage:
                detailed_result = normalize_detailed_csv(csv_path)
                stage.complete(
                    f"channels={len(detailed_result.targets)}, "
                    f"batches={len(detailed_result.batches)}, "
                    f"unresolved={len(detailed_result.unresolved)}"
                )
        else:
            detailed_result = normalize_detailed_csv(csv_path)
    except DetailedInputValidationError as exc:
        raise ConfigGenerationError(str(exc)) from exc

    if detailed_result.unresolved:
        if progress is None:
            return _generate(argv)
        with progress.stage(
            "CONFIG-BUILD",
            "Write unresolved CSV report",
        ) as stage:
            outcome = _generate(argv)
            stage.fail(
                f"status={outcome.status}, unresolved={outcome.unresolved}"
            )
            return outcome
    if not detailed_result.batches:
        raise ConfigGenerationError("detailed CSV produced no sNp batches")

    if len(detailed_result.batches) == 1:
        batch = detailed_result.batches[0]
        if progress is not None:
            with progress.stage(
                "CONFIG-BATCH",
                "Generate analysis-batch run Config",
                detail=f"1/1 {batch.snp_file} ({len(batch.targets)} channels)",
            ) as stage:
                outcome = _generate([*argv, "--snp-file", batch.snp_file])
                if outcome.status == 0:
                    stage.complete("unresolved=0")
                else:
                    stage.fail(
                        f"status={outcome.status}, unresolved={outcome.unresolved}"
                    )
        else:
            outcome = _generate([*argv, "--snp-file", batch.snp_file])
        return ConfigGenerationOutcome(
            status=outcome.status,
            output_dir=outcome.output_dir,
            run_config=outcome.run_config,
            manifest=outcome.manifest,
            unresolved=outcome.unresolved,
            run_configs=(outcome.run_config,) if outcome.status == 0 else (),
        )

    batch_outcomes: list[tuple[str, ConfigGenerationOutcome]] = []
    for index, batch in enumerate(detailed_result.batches, start=1):
        batch_output_dir = output_dir.parent / batch.snp_file / output_dir.name
        batch_argv = _replace_argv_option(
            argv,
            "--output-dir",
            str(batch_output_dir),
        )
        batch_argv = _replace_argv_option(batch_argv, "--name", batch.snp_file)
        if progress is not None:
            with progress.stage(
                "CONFIG-BATCH",
                "Generate analysis-batch run Config",
                detail=(
                    f"{index}/{len(detailed_result.batches)} {batch.snp_file} "
                    f"({len(batch.targets)} channels)"
                ),
            ) as stage:
                outcome = _generate(
                    [*batch_argv, "--snp-file", batch.snp_file]
                )
                if outcome.status == 0:
                    stage.complete("unresolved=0")
                else:
                    stage.fail(
                        f"status={outcome.status}, unresolved={outcome.unresolved}"
                    )
        else:
            outcome = _generate([*batch_argv, "--snp-file", batch.snp_file])
        batch_outcomes.append((batch.snp_file, outcome))

    output_dir.mkdir(parents=True, exist_ok=True)
    batch_manifest_path = output_dir / f"{name}_batch_manifest.json"
    batch_unresolved_path = output_dir / f"{name}_batch_unresolved.json"
    batch_status = max(outcome.status for _, outcome in batch_outcomes)
    batch_records = [
        {
            "snpFile": snp_file,
            "status": outcome.status,
            "outputDir": str(outcome.output_dir),
            "runConfig": str(outcome.run_config),
            "manifest": str(outcome.manifest),
            "unresolved": str(outcome.unresolved),
        }
        for snp_file, outcome in batch_outcomes
    ]
    _write_json(
        batch_unresolved_path,
        {
            "version": 1,
            "status": "ok" if batch_status == 0 else "blocked_batch_generation",
            "batches": [
                record for record in batch_records if int(record["status"]) != 0
            ],
        },
    )
    _write_json(
        batch_manifest_path,
        {
            "version": 1,
            "status": "ok" if batch_status == 0 else "blocked_batch_generation",
            "inputFormat": "detailed",
            "csv": str(csv_path),
            "snpFileCount": len(batch_records),
            "runConfigs": [record["runConfig"] for record in batch_records],
            "combinedUnresolved": str(batch_unresolved_path),
            "batches": batch_records,
        },
    )
    run_configs = tuple(
        outcome.run_config
        for _, outcome in batch_outcomes
        if outcome.status == 0
    )
    first_outcome = batch_outcomes[0][1]
    return ConfigGenerationOutcome(
        status=batch_status,
        output_dir=output_dir,
        run_config=first_outcome.run_config,
        manifest=batch_manifest_path,
        unresolved=batch_unresolved_path,
        run_configs=run_configs,
        batch_manifest=batch_manifest_path,
    )
