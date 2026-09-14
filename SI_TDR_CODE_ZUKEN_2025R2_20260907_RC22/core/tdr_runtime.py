from __future__ import annotations

import csv
import hashlib
import importlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .channel.analysis_options import ANALYSIS_OPTION_FOLDER_PARTS
except ImportError:  # Support compact customer core imports.
    from channel.analysis_options import (  # type: ignore[no-redef]
        ANALYSIS_OPTION_FOLDER_PARTS,
    )

TDR_PROFILE_FIELDS = (
    "riseTimePs",
    "pulseRepetition",
    "pulseWidth",
    "timeDelay",
)
REMOVED_STRICT_TDR_FIELDS = frozenset(
    {
        "stepPs",
        "stopPs",
        "useTsConvolution",
        "useToConvolution",
        "differentialBridgeOhm",
    }
)

# These are implementation policy, not customer/Profile inputs.  They remain
# named here so a customer JSON field can never silently override them.
INTERNAL_TRANSIENT_STEP_PS = 7.5
INTERNAL_TRANSIENT_STOP_PS = 30000.0
INTERNAL_USE_TS_CONVOLUTION = False
DEFAULT_Y_AXIS_MARGIN_OHM = 30.0
REPORT_FONT_NAME = "Arial"
# Endpoint RefDes note 크기(8/10 기준 유지).
REPORT_FONT_SIZE_PT = 32
# 1400x800 AEDT 2025 R2 export에서 28/32pt는 밑줄이 plot 상단과 겹친다.
# 24pt는 기본 12pt보다 충분히 크면서 HDMI1/DP_RX 모두 상단 여백을 보존한다.
REPORT_TITLE_FONT_SIZE_PT = 24
REPORT_SUBTITLE_FONT_SIZE_PT = 12
REPORT_COLOR_RGB = (0, 0, 0)
STRICT_TDR_CIRCUIT_TOPOLOGY = "manual-snp-multi-diff"
STRICT_TDR_MODE = "differential"
STRICT_TDR_DESIGN_NAME = "TDR"
STRICT_TDR_RECORD_SCHEMA = "si-tdr-strict-native-tdr/1"
NATIVE_REPORT_IMAGE_ANALYSIS_SCHEMA = "si-tdr-native-report-image-analysis/1"
WAVEFORM_CSV_ANALYSIS_SCHEMA = "si-tdr-waveform-csv-analysis/1"
NATIVE_REPORT_OPERATION_SCHEMA = "si-tdr-native-report-operations/1"
NATIVE_REPORT_VISUAL_CONFIRMATION_SCHEMA = (
    "si-tdr-native-report-visual-confirmation/1"
)
NATIVE_REPORT_WIDTH_PX = 1400
NATIVE_REPORT_HEIGHT_PX = 800
ALLOWED_NATIVE_REPORT_JPEG_MODES = frozenset({"RGB", "L", "CMYK"})
WINDOWS_AEDT_LEGACY_PATH_LIMIT = 260
WINDOWS_AEDT_PATH_WARNING_THRESHOLD = 240
_PILLOW_IMAGE_MODULE: Any | None = None


class StrictTdrRuntimeError(RuntimeError):
    """Raised when the customer Circuit/TDR contract cannot be proven."""


NATIVE_REPORT_IMAGE_PREFIX = "Tdr_"

def pillow_image_module() -> Any:
    """Load Pillow without accepting the repository's incompatible DCIR venv copy.

    SI-TDR's requirements own Pillow.  Ansys imports may prepend the preserved
    customer DCIR virtualenv to ``sys.path``; that Python-version-specific copy
    must not shadow the active interpreter's Pillow binary extension.
    """

    global _PILLOW_IMAGE_MODULE
    if _PILLOW_IMAGE_MODULE is not None:
        return _PILLOW_IMAGE_MODULE
    excluded = (
        Path(__file__).resolve().parents[1]
        / "DCIR"
        / "SIwave_DCIR-1p4p1"
        / ".venv"
        / "Lib"
        / "site-packages"
    ).resolve()
    original_path = list(sys.path)
    try:
        sys.path[:] = [
            entry
            for entry in original_path
            if not entry or Path(entry).resolve() != excluded
        ]
        for module_name in [
            name for name in sys.modules if name == "PIL" or name.startswith("PIL.")
        ]:
            module = sys.modules.get(module_name)
            origin = Path(str(getattr(module, "__file__", "") or ".")).resolve()
            if origin.is_relative_to(excluded):
                del sys.modules[module_name]
        importlib.invalidate_caches()
        _PILLOW_IMAGE_MODULE = importlib.import_module("PIL.Image")
    except Exception as exc:  # pragma: no cover - deployment dependency error
        raise StrictTdrRuntimeError(
            "Pillow from the active SI-TDR runtime is required to validate JPEG"
        ) from exc
    finally:
        sys.path[:] = original_path
    return _PILLOW_IMAGE_MODULE


@dataclass(frozen=True)
class StrictTdrGroup:
    name: str
    channels: tuple[str, ...]
    profile_id: str
    selection_source: str
    option_path: Path
    option_sha256: str
    settings: dict[str, Any]
    reference_impedance_ohm: float
    target_min_ohm: float
    target_max_ohm: float
    spec_source: str
    y_min_ohm: float
    y_max_ohm: float
    x_min_ps: float
    x_max_ps: float

    @property
    def marker_x_ps(self) -> float:
        return (self.x_min_ps + self.x_max_ps) / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "channels": list(self.channels),
            "tdrProfile": {
                "profileId": self.profile_id,
                "selectionSource": self.selection_source,
                "resolvedPath": str(self.option_path),
                "sha256": self.option_sha256,
                "settings": dict(self.settings),
            },
            "specImpedance": {
                "referenceImpedanceOhm": self.reference_impedance_ohm,
                "targetRangeOhm": {
                    "lower": self.target_min_ohm,
                    "upper": self.target_max_ohm,
                    "source": "Spec",
                    "evidenceSource": self.spec_source,
                },
            },
            "farEndTermination": {
                "valueOhm": self.reference_impedance_ohm,
                "source": "Spec.referenceImpedanceOhm",
            },
            "view": {
                "xAxisPs": {"min": self.x_min_ps, "max": self.x_max_ps},
                "yAxisOhm": {"min": self.y_min_ohm, "max": self.y_max_ohm},
            },
            "marker": {
                "policy": "displayed-x-range-midpoint",
                "xPs": self.marker_x_ps,
                "count": 1,
            },
        }


@dataclass(frozen=True)
class StrictTdrContract:
    batch_id: str
    job_root: Path
    touchstone_path: Path
    expected_port_order: tuple[str, ...]
    groups: tuple[StrictTdrGroup, ...]
    y_axis_margin_ohm: float
    # 9.4.7.1: 생성 단계가 route 유도로 설치한 해석 stop과 그 출처.
    transient_stop_ps: float = INTERNAL_TRANSIENT_STOP_PS
    transient_stop_source: str = "internal-fixed-transient-policy"

    def group(self, name: str) -> StrictTdrGroup:
        for group in self.groups:
            if group.name == name:
                return group
        raise StrictTdrRuntimeError(f"unknown strict TDR report group: {name!r}")

    def group_for_channel(self, channel_name: str) -> StrictTdrGroup:
        matches = [group for group in self.groups if channel_name in group.channels]
        if len(matches) != 1:
            raise StrictTdrRuntimeError(
                f"channel {channel_name!r} must belong to exactly one TDR report group; "
                f"matches={[group.name for group in matches]}"
            )
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "si-tdr-strict-circuit-contract/1",
            "batchId": self.batch_id,
            "jobRoot": str(self.job_root),
            "touchstonePath": str(self.touchstone_path),
            "expectedPortOrder": list(self.expected_port_order),
            "yAxisMarginOhm": self.y_axis_margin_ohm,
            "groups": [group.to_dict() for group in self.groups],
            "internalTransientPolicy": {
                "policyId": "aedt-circuit-transient-v1",
                "stepPs": INTERNAL_TRANSIENT_STEP_PS,
                "stopPs": self.transient_stop_ps,
                "stopSource": self.transient_stop_source,
                "useTsConvolution": INTERNAL_USE_TS_CONVOLUTION,
                "customerConfigurable": False,
            },
        }


def is_customer_strict(context: Mapping[str, Any]) -> bool:
    return bool(context.get("customerComponentHandling"))


def validate_strict_topology_policy(context: Mapping[str, Any]) -> None:
    """Fail before opening AEDT when the internal strict Circuit policy drifted."""

    if not is_customer_strict(context):
        raise StrictTdrRuntimeError("strict TDR policy requires customer strict mode")
    tdr = _object(context.get("tdr"), where="tdr")
    _walk_removed(tdr, path="tdr")
    if tdr.get("circuitTopology") != STRICT_TDR_CIRCUIT_TOPOLOGY:
        raise StrictTdrRuntimeError(
            "strict TDR requires tdr.circuitTopology="
            f"{STRICT_TDR_CIRCUIT_TOPOLOGY!r}"
        )
    if tdr.get("mode") != STRICT_TDR_MODE:
        raise StrictTdrRuntimeError(
            f"strict TDR requires tdr.mode={STRICT_TDR_MODE!r}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        int(getattr(stat, "st_file_attributes", 0)) & 0x400
    )


def artifact_evidence(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise StrictTdrRuntimeError(f"required artifact is missing or empty: {resolved}")
    return {
        "path": str(resolved),
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _strict_string_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise StrictTdrRuntimeError(f"{where} must be a non-empty list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        folded = text.casefold()
        if not text or folded in seen:
            raise StrictTdrRuntimeError(
                f"{where} contains an empty or duplicate value"
            )
        seen.add(folded)
        result.append(text)
    return result


def analyze_native_report_jpeg(path: Path) -> dict[str, Any]:
    """Decode and characterize the exact AEDT native report JPG.

    Pillow is deliberately used at the producer/publisher boundary.  Merely
    checking a ``.jpg`` suffix or a non-zero size is not native-report proof.
    """

    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise StrictTdrRuntimeError(
            f"AEDT native report JPG is missing or empty: {resolved}"
        )
    try:
        with pillow_image_module().open(resolved) as image:
            image.load()
            image_format = str(image.format or "").upper()
            mode = str(image.mode or "")
            width, height = image.size
    except Exception as exc:
        raise StrictTdrRuntimeError(
            f"AEDT native report is not a decodable JPEG: {resolved}: {exc}"
        ) from exc
    if image_format != "JPEG":
        raise StrictTdrRuntimeError(
            f"AEDT native report format must be JPEG, got {image_format!r}: {resolved}"
        )
    if (width, height) != (NATIVE_REPORT_WIDTH_PX, NATIVE_REPORT_HEIGHT_PX):
        raise StrictTdrRuntimeError(
            "AEDT native report dimensions differ from the requested 1400x800: "
            f"{width}x{height}: {resolved}"
        )
    if mode not in ALLOWED_NATIVE_REPORT_JPEG_MODES:
        raise StrictTdrRuntimeError(
            f"AEDT native report JPEG mode is unsupported: {mode!r}: {resolved}"
        )
    return {
        "schema": NATIVE_REPORT_IMAGE_ANALYSIS_SCHEMA,
        "status": "verified",
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "sizeBytes": resolved.stat().st_size,
        "format": image_format,
        "mode": mode,
        "widthPx": width,
        "heightPx": height,
        "requestedResolutionPx": [
            NATIVE_REPORT_WIDTH_PX,
            NATIVE_REPORT_HEIGHT_PX,
        ],
    }


def native_report_jpeg_evidence(path: Path) -> dict[str, Any]:
    evidence = artifact_evidence(path)
    evidence["imageAnalysis"] = analyze_native_report_jpeg(path)
    return evidence


def analyze_waveform_csv(
    path: Path,
    *,
    expected_trace_names: Sequence[str],
) -> dict[str, Any]:
    """Parse the public waveform CSV without trusting its producer record."""

    resolved = path.resolve()
    trace_names = _strict_string_list(
        expected_trace_names,
        where="TDR waveform traceNames",
    )
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise StrictTdrRuntimeError(f"TDR waveform CSV is missing or empty: {resolved}")
    expected_header = [
        "Time [ps]",
        *[f"{trace_name} [ohm]" for trace_name in trace_names],
    ]
    row_count = 0
    first_time: float | None = None
    last_time: float | None = None
    try:
        with resolved.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, None)
            if header != expected_header:
                raise StrictTdrRuntimeError(
                    "TDR waveform CSV header/trace order differs: "
                    f"expected={expected_header}, actual={header}"
                )
            for line_number, row in enumerate(reader, start=2):
                if len(row) != len(expected_header):
                    raise StrictTdrRuntimeError(
                        f"TDR waveform CSV row {line_number} has {len(row)} columns; "
                        f"expected {len(expected_header)}"
                    )
                values: list[float] = []
                for column_number, cell in enumerate(row, start=1):
                    try:
                        value = float(cell)
                    except (TypeError, ValueError) as exc:
                        raise StrictTdrRuntimeError(
                            f"TDR waveform CSV row {line_number} column "
                            f"{column_number} is not numeric"
                        ) from exc
                    if not math.isfinite(value):
                        raise StrictTdrRuntimeError(
                            f"TDR waveform CSV row {line_number} column "
                            f"{column_number} is not finite"
                        )
                    values.append(value)
                current_time = values[0]
                if last_time is not None and current_time <= last_time:
                    raise StrictTdrRuntimeError(
                        "TDR waveform Time [ps] must be strictly increasing: "
                        f"row={line_number}, previous={last_time}, current={current_time}"
                    )
                if first_time is None:
                    first_time = current_time
                last_time = current_time
                row_count += 1
    except UnicodeDecodeError as exc:
        raise StrictTdrRuntimeError(
            f"TDR waveform CSV is not valid UTF-8: {resolved}"
        ) from exc
    if row_count <= 0 or first_time is None or last_time is None:
        raise StrictTdrRuntimeError("TDR waveform CSV contains no data rows")
    return {
        "schema": WAVEFORM_CSV_ANALYSIS_SCHEMA,
        "status": "verified",
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "sizeBytes": resolved.stat().st_size,
        "header": expected_header,
        "traceNames": trace_names,
        "rowCount": row_count,
        "timeGrid": {
            "firstPs": first_time,
            "lastPs": last_time,
            "strictlyIncreasing": True,
        },
    }


def _object(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StrictTdrRuntimeError(f"{where} must be an object")
    return value


def _identifier(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrictTdrRuntimeError(f"{where} must be a non-empty string")
    return value.strip()


def _artifact_basename(value: Any, *, where: str) -> str:
    name = _identifier(value, where=where)
    path = Path(name)
    if (
        path.name != name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise StrictTdrRuntimeError(f"{where} must be an artifact-safe basename")
    return name


def _finite(value: Any, *, where: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise StrictTdrRuntimeError(f"{where} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StrictTdrRuntimeError(f"{where} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "finite positive" if positive else "finite"
        raise StrictTdrRuntimeError(f"{where} must be a {qualifier} number")
    return number


def _walk_removed(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if path == "tdr" and key == "timeRangeResolution":
                # Generated provenance may describe the fixed internal
                # transient policy with legacy quantity names.  It is not a
                # customer/Profile input; those inputs are validated before
                # this record is attached to the run Config.
                continue
            if path == "tdr" and key == "transient":
                # 9.4.7.1: 생성 단계가 설치한 route 유도 stop.  모양은
                # _strict_generated_transient_stop이 별도로 fail-closed
                # 검증한다.  고객/Profile의 stopPs 선언은 계속 거부된다.
                _strict_generated_transient_stop(value)
                continue
            if key in REMOVED_STRICT_TDR_FIELDS:
                raise StrictTdrRuntimeError(
                    f"removed customer TDR field is not supported: {child_path}"
                )
            _walk_removed(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_removed(child, path=f"{path}[{index}]")


def _resolve_job_file(
    value: Any,
    *,
    job_root: Path,
    folder_parts: tuple[str, ...],
    extension: str,
    where: str,
) -> Path:
    text = _identifier(value, where=where)
    raw = Path(text)
    try:
        resolved = raw.resolve(strict=True)
        root = job_root.resolve(strict=True)
        folder = root.joinpath(*folder_parts).resolve(strict=True)
    except OSError as exc:
        raise StrictTdrRuntimeError(f"{where} cannot be resolved: {raw}") from exc
    if resolved.suffix.casefold() != extension:
        raise StrictTdrRuntimeError(f"{where} must use the {extension} extension")
    folder_label = "/".join(folder_parts)
    if root not in resolved.parents or resolved.parent != folder:
        raise StrictTdrRuntimeError(
            f"{where} must resolve directly inside Job-local {folder_label}/: {resolved}"
        )
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise StrictTdrRuntimeError(f"{where} is missing or empty: {resolved}")
    return resolved


def _load_exact_profile(path: Path, *, profile_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictTdrRuntimeError(
            f"TDR Profile {profile_id!r} is not valid JSON: {path}"
        ) from exc
    settings = _object(payload, where=f"TDR Profile {profile_id!r}")
    _walk_removed(settings, path=f"TDR Profile {profile_id!r}")
    if set(settings) != set(TDR_PROFILE_FIELDS):
        raise StrictTdrRuntimeError(
            f"TDR Profile {profile_id!r} must contain exactly "
            f"{list(TDR_PROFILE_FIELDS)}; actual={sorted(settings)}"
        )
    normalized = dict(settings)
    normalized["riseTimePs"] = _finite(
        normalized["riseTimePs"],
        where=f"TDR Profile {profile_id!r}.riseTimePs",
        positive=True,
    )
    for field in TDR_PROFILE_FIELDS[1:]:
        normalized[field] = _identifier(
            normalized[field], where=f"TDR Profile {profile_id!r}.{field}"
        )
    return normalized


def _view_bounds(group: Mapping[str, Any]) -> tuple[float, float]:
    raw_axis = ((_object(group.get("view") or {}, where="reportGroup.view")).get("xAxisPs") or {})
    axis = _object(raw_axis, where="reportGroup.view.xAxisPs")
    x_min = _finite(axis.get("min", 0.0), where="reportGroup.view.xAxisPs.min")
    x_max = _finite(
        axis.get("max", INTERNAL_TRANSIENT_STOP_PS),
        where="reportGroup.view.xAxisPs.max",
    )
    if x_min >= x_max:
        raise StrictTdrRuntimeError(
            f"report displayed X range must increase; got {x_min:g}..{x_max:g} ps"
        )
    return x_min, x_max


def prepare_strict_tdr_contract(
    context: Mapping[str, Any], touchstone_path: Path
) -> StrictTdrContract:
    if not is_customer_strict(context):
        raise StrictTdrRuntimeError("strict TDR contract requires customer strict mode")
    validate_strict_topology_policy(context)
    tdr = _object(context.get("tdr"), where="tdr")
    batch_id = _artifact_basename(
        (_object(context.get("segment"), where="segment")).get("name"),
        where="segment.name",
    )
    customer = _object(
        context.get("customerComponentHandling"), where="customerComponentHandling"
    )
    job_root = Path(_identifier(customer.get("jobRoot"), where="customerComponentHandling.jobRoot")).resolve()
    if not job_root.is_dir():
        raise StrictTdrRuntimeError(f"Job root does not exist: {job_root}")

    expected_order = tuple(
        _identifier(item, where="ports.portOrder[]")
        for item in (_object(context.get("ports"), where="ports").get("portOrder") or [])
    )
    if not expected_order or len(set(expected_order)) != len(expected_order):
        raise StrictTdrRuntimeError("ports.portOrder must be non-empty and unique")

    touchstone = touchstone_path.resolve()
    solve_manifest_path = Path(
        _identifier(
            (_object(context.get("workspace"), where="workspace")).get("runDir"),
            where="workspace.runDir",
        )
    ) / "channel_solve.json"
    try:
        solve_manifest = json.loads(solve_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictTdrRuntimeError(
            f"FB-07 solve manifest is missing or invalid: {solve_manifest_path}"
        ) from exc
    exported = [Path(str(value)).resolve() for value in solve_manifest.get("exportedTouchstoneFiles") or []]
    if solve_manifest.get("status") != "ok" or exported != [touchstone]:
        raise StrictTdrRuntimeError(
            "strict TDR requires the one exact current-batch Touchstone recorded by FB-07"
        )
    artifact_evidence(touchstone)
    requested = Path(str(solve_manifest.get("requestedTouchstone") or "")).resolve()
    if requested != touchstone:
        raise StrictTdrRuntimeError(
            f"FB-07 requested/exported Touchstone mismatch: {requested} != {touchstone}"
        )
    expected_suffix = f".s{len(expected_order)}p"
    if touchstone.suffix.casefold() != expected_suffix:
        raise StrictTdrRuntimeError(
            f"Touchstone extension {touchstone.suffix} does not match {len(expected_order)} ports"
        )
    if touchstone.stem != batch_id:
        raise StrictTdrRuntimeError(
            f"Touchstone basename must be the exact batch ID {batch_id!r}: {touchstone.name}"
        )
    solve_touchstone_evidence = _object(
        (_object(solve_manifest.get("artifacts"), where="channel_solve.artifacts")).get(
            "touchstone"
        ),
        where="channel_solve.artifacts.touchstone",
    )
    current_hash = sha256_file(touchstone)
    if solve_touchstone_evidence.get("sha256") != current_hash:
        raise StrictTdrRuntimeError(
            "FB-07 Touchstone changed after solve/export validation"
        )

    selection = _object(
        context.get("analysisOptionSelection"), where="analysisOptionSelection"
    )
    if selection.get("batchId") != batch_id:
        raise StrictTdrRuntimeError(
            "analysisOptionSelection.batchId does not match segment.name"
        )
    selections = _object(selection.get("tdr"), where="analysisOptionSelection.tdr")
    channels = {
        _identifier(raw.get("name"), where="tdr.channels[].name"): raw
        for raw in (tdr.get("channels") or [])
        if isinstance(raw, Mapping)
    }
    raw_groups = [raw for raw in (tdr.get("reportGroups") or []) if isinstance(raw, Mapping)]
    group_names = [_artifact_basename(raw.get("name"), where="tdr.reportGroups[].name") for raw in raw_groups]
    if not group_names or len({name.casefold() for name in group_names}) != len(group_names):
        raise StrictTdrRuntimeError("tdr.reportGroups names must be non-empty and unique")
    if set(selections) != set(group_names):
        raise StrictTdrRuntimeError(
            "analysisOptionSelection.tdr must select exactly every report group; "
            f"groups={sorted(group_names)}, selections={sorted(selections)}"
        )

    raw_margin = (_object(context.get("tdrReport") or {}, where="tdrReport")).get(
        "yAxisMarginOhm", DEFAULT_Y_AXIS_MARGIN_OHM
    )
    margin = _finite(raw_margin, where="tdrReport.yAxisMarginOhm", positive=True)
    groups: list[StrictTdrGroup] = []
    channel_coverage: list[str] = []
    for raw_group in raw_groups:
        name = _artifact_basename(raw_group.get("name"), where="tdr.reportGroups[].name")
        group_channels = tuple(
            _identifier(item, where=f"tdr.reportGroups.{name}.channels[]")
            for item in (raw_group.get("channels") or [])
        )
        if not group_channels or len(set(group_channels)) != len(group_channels):
            raise StrictTdrRuntimeError(
                f"TDR report group {name!r} channels must be non-empty and unique"
            )
        unknown = sorted(set(group_channels) - set(channels))
        if unknown:
            raise StrictTdrRuntimeError(
                f"TDR report group {name!r} references unknown channels: {unknown}"
            )
        channel_coverage.extend(group_channels)

        selected = _object(selections[name], where=f"analysisOptionSelection.tdr.{name}")
        if selected.get("itemId") != name:
            raise StrictTdrRuntimeError(f"TDR selection itemId mismatch for {name!r}")
        profile_id = _identifier(selected.get("profileId"), where=f"TDR selection {name}.profileId")
        selection_source = _identifier(
            selected.get("selectionSource"), where=f"TDR selection {name}.selectionSource"
        )
        if selection_source not in {"Spec.TDR_Option", "administratorDefault"}:
            raise StrictTdrRuntimeError(
                f"TDR selection {name!r} has unsupported selectionSource={selection_source!r}"
            )
        file_record = _object(selected.get("file"), where=f"TDR selection {name}.file")
        option_path = _resolve_job_file(
            file_record.get("resolvedPath"),
            job_root=job_root,
            folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["tdr"],
            extension=".json",
            where=f"TDR selection {name}.file.resolvedPath",
        )
        if file_record.get("fileName") != option_path.name:
            raise StrictTdrRuntimeError(f"TDR selection {name!r} fileName/path mismatch")
        option_hash = sha256_file(option_path)
        if file_record.get("sha256") != option_hash:
            raise StrictTdrRuntimeError(
                f"TDR selection {name!r} Profile changed after resolution"
            )
        settings = _load_exact_profile(option_path, profile_id=profile_id)
        selected_settings = _object(
            selected.get("settings"), where=f"TDR selection {name}.settings"
        )
        _walk_removed(selected_settings, path=f"analysisOptionSelection.tdr.{name}.settings")
        if dict(selected_settings) != settings:
            raise StrictTdrRuntimeError(
                f"TDR selection {name!r} settings do not match the selected Profile file"
            )

        references: list[float] = []
        ranges: list[tuple[float, float]] = []
        spec_sources: list[str] = []
        for channel_name in group_channels:
            channel = channels[channel_name]
            _walk_removed(channel, path=f"tdr.channels.{channel_name}")
            for field in TDR_PROFILE_FIELDS:
                if channel.get(field) != settings[field]:
                    raise StrictTdrRuntimeError(
                        f"channel {channel_name!r} did not receive {name!r} Profile field {field}"
                    )
            references.append(
                _finite(
                    channel.get("referenceImpedanceOhm"),
                    where=f"tdr.channels.{channel_name}.referenceImpedanceOhm",
                    positive=True,
                )
            )
            target = _object(
                channel.get("targetRangeOhm"),
                where=f"tdr.channels.{channel_name}.targetRangeOhm",
            )
            target_source = str(target.get("source") or "").strip()
            if target_source.casefold() not in {"spec", "customer_detailed_csv"}:
                raise StrictTdrRuntimeError(
                    f"channel {channel_name!r} targetRangeOhm must come from Spec"
                )
            spec_sources.append(target_source)
            lower = _finite(
                target.get("lower"),
                where=f"tdr.channels.{channel_name}.targetRangeOhm.lower",
                positive=True,
            )
            upper = _finite(
                target.get("upper"),
                where=f"tdr.channels.{channel_name}.targetRangeOhm.upper",
                positive=True,
            )
            if not lower <= references[-1] <= upper:
                raise StrictTdrRuntimeError(
                    f"channel {channel_name!r} requires Min <= Target <= Max"
                )
            ranges.append((lower, upper))
        if (
            len(set(references)) != 1
            or len(set(ranges)) != 1
            or len({source.casefold() for source in spec_sources}) != 1
        ):
            raise StrictTdrRuntimeError(
                f"TDR report group {name!r} has conflicting Spec Target/Min/Max values: "
                f"targets={references}, ranges={ranges}"
            )
        x_min, x_max = _view_bounds(raw_group)
        reference = references[0]
        groups.append(
            StrictTdrGroup(
                name=name,
                channels=group_channels,
                profile_id=profile_id,
                selection_source=selection_source,
                option_path=option_path,
                option_sha256=option_hash,
                settings=settings,
                reference_impedance_ohm=reference,
                target_min_ohm=ranges[0][0],
                target_max_ohm=ranges[0][1],
                spec_source=spec_sources[0],
                y_min_ohm=reference - margin,
                y_max_ohm=reference + margin,
                x_min_ps=x_min,
                x_max_ps=x_max,
            )
        )
    if sorted(channel_coverage) != sorted(channels):
        raise StrictTdrRuntimeError(
            "every strict TDR channel must belong to exactly one report group"
        )
    if len(channel_coverage) != len(set(channel_coverage)):
        raise StrictTdrRuntimeError("a strict TDR channel cannot belong to multiple report groups")
    transient_stop_ps, transient_stop_source = _strict_generated_transient_stop(tdr)
    return StrictTdrContract(
        batch_id=batch_id,
        job_root=job_root,
        touchstone_path=touchstone,
        expected_port_order=expected_order,
        groups=tuple(groups),
        y_axis_margin_ohm=margin,
        transient_stop_ps=transient_stop_ps,
        transient_stop_source=transient_stop_source,
    )


def ensure_fresh_targets(paths: Sequence[Path]) -> None:
    stale = [str(path.resolve()) for path in paths if path.exists()]
    if stale:
        raise StrictTdrRuntimeError(
            "strict TDR will not reuse stale output artifacts: " + ", ".join(stale)
        )


def validate_circuit_port_order(
    actual_order: Sequence[str], expected_order: Sequence[str]
) -> dict[str, Any]:
    actual = [str(item) for item in actual_order]
    expected = [str(item) for item in expected_order]
    if actual != expected:
        raise StrictTdrRuntimeError(
            "Circuit Touchstone component port order differs from Port Role Metadata: "
            f"expected={expected}, actual={actual}"
        )
    return {
        "status": "ok",
        "validationPoint": "immediately-before-circuit-wiring",
        "portCount": len(expected),
        "portOrder": expected,
    }


def _strict_generated_transient_stop(tdr: Mapping[str, Any]) -> tuple[float, str]:
    """Validate the generation-installed route-derived analysis stop.

    9.4.7.1: strict Run Config의 tdr.transient는 생성 단계가 설치한
    {stopPs, stopSource}만 허용한다.  없거나 모양이 다르면 fail-closed.
    """

    transient = tdr.get("transient")
    if not isinstance(transient, Mapping):
        raise StrictTdrRuntimeError(
            "strict TDR requires the generated route-derived tdr.transient.stopPs"
        )
    if set(transient) != {"stopPs", "stopSource"}:
        raise StrictTdrRuntimeError(
            "strict tdr.transient accepts exactly the generated stopPs/stopSource"
        )
    stop_ps = _finite(
        transient.get("stopPs"), where="tdr.transient.stopPs", positive=True
    )
    if stop_ps <= INTERNAL_TRANSIENT_STEP_PS:
        raise StrictTdrRuntimeError(
            "strict tdr.transient.stopPs must exceed the internal transient step"
        )
    # 최종 uniform 격자점이 정확히 stop에 놓여야 AEDT uniform CSV exporter가
    # 마지막 적응 구간에서 비유한값을 내지 않는다 (2026-08-20 R21 관측).
    step_count = stop_ps / INTERNAL_TRANSIENT_STEP_PS
    if abs(step_count - round(step_count)) > 1e-9:
        raise StrictTdrRuntimeError(
            "strict tdr.transient.stopPs must be an exact multiple of the "
            f"internal {INTERNAL_TRANSIENT_STEP_PS:g}ps transient step"
        )
    source = str(transient.get("stopSource") or "").strip()
    if not source:
        raise StrictTdrRuntimeError(
            "strict tdr.transient.stopSource provenance is required"
        )
    return stop_ps, source


def internal_transient_data(stop_ps: float = INTERNAL_TRANSIENT_STOP_PS) -> list[str]:
    """Uniform internal 7.5 ps step over the explicit 0..stopPs analysis range."""

    stop = _finite(stop_ps, where="strict transient stopPs", positive=True)
    return [f"{INTERNAL_TRANSIENT_STEP_PS:g}ps", f"{stop:g}ps"]


def validate_nexxim_output_path_budget(
    project_path: Path,
    *,
    design_name: str,
    port_count: int,
) -> dict[str, Any]:
    """Project Nexxim artifacts and fail only at the observed legacy limit.

    This function performs string/path calculations only.  It does not create
    directories, inspect the registry, or change Windows long-path settings.
    """

    if (
        isinstance(port_count, bool)
        or not isinstance(port_count, int)
        or port_count <= 0
    ):
        raise StrictTdrRuntimeError(
            "Circuit Touchstone port count must be a positive integer"
        )

    results = project_path.resolve().with_suffix(".aedtresults")
    design = _artifact_basename(design_name, where="Circuit design name")
    design_results = results / design
    temp = design_results / "temp"
    candidates = {
        "netlist": temp / "DV9999_S9999_V9999.cir",
        "legacyPreflightProbe": temp / "DV9999_S9999_V9999.cir_temp.nxm",
        "nexximSdf": temp / "DV9999_S9999_V9999.cir_temp.nxm" / "top.sdf",
        "stateSpaceModel": temp / f"sss_{'f' * 32}_{port_count}.sss",
        "solverLog": design_results / "DV9999_S9999_V9999.cir.log",
    }
    artifacts = {
        name: {
            "path": str(path),
            "length": len(str(path)),
            "exceedsLegacyLimit": (
                len(str(path)) >= WINDOWS_AEDT_LEGACY_PATH_LIMIT
            ),
        }
        for name, path in candidates.items()
    }
    longest_name, longest = max(
        artifacts.items(),
        key=lambda item: item[1]["length"],
    )
    longest_length = int(longest["length"])
    if longest_length >= WINDOWS_AEDT_LEGACY_PATH_LIMIT:
        raise StrictTdrRuntimeError(
            "AEDT/Nexxim projected output path would reach the Windows legacy "
            f"{WINDOWS_AEDT_LEGACY_PATH_LIMIT}-character limit "
            f"({longest_length} characters, artifact={longest_name}). Shorten the "
            "Job-local Circuit project or design name without moving work outside "
            f"the Job directory: {longest['path']}"
        )
    legacy_probe = artifacts["legacyPreflightProbe"]
    return {
        "status": (
            "warning"
            if longest_length >= WINDOWS_AEDT_PATH_WARNING_THRESHOLD
            else "ok"
        ),
        # Preserve the original fields for readers of existing records.
        "projectedTempPath": legacy_probe["path"],
        "projectedLength": legacy_probe["length"],
        "longestArtifact": longest_name,
        "longestPath": longest["path"],
        "longestLength": longest_length,
        "remainingCharacters": WINDOWS_AEDT_LEGACY_PATH_LIMIT - longest_length,
        "warningThreshold": WINDOWS_AEDT_PATH_WARNING_THRESHOLD,
        "limit": WINDOWS_AEDT_LEGACY_PATH_LIMIT,
        "artifacts": artifacts,
    }


def resolve_uniform_waveform_grid(
    *,
    trace_names: Sequence[str],
    samples_by_trace: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Resolve the common valid range for AEDT's native uniform CSV export."""

    names = _strict_string_list(trace_names, where="strict TDR waveform traceNames")
    trace_ranges: list[dict[str, Any]] = []
    for name in names:
        samples = samples_by_trace.get(name)
        if not samples:
            raise StrictTdrRuntimeError(f"TDR solution data is missing trace: {name}")
        times = [
            _finite(
                sample.get("time_ps"),
                where=f"samplesByTrace.{name}[{index}].time_ps",
            )
            for index, sample in enumerate(samples)
        ]
        if any(current <= previous for previous, current in zip(times, times[1:])):
            raise StrictTdrRuntimeError(
                f"TDR solution time grid is not strictly increasing for trace {name!r}"
            )
        trace_ranges.append(
            {
                "traceName": name,
                "sampleCount": len(times),
                "firstPs": times[0],
                "lastPs": times[-1],
            }
        )

    step_ps = INTERNAL_TRANSIENT_STEP_PS
    common_first = max(float(item["firstPs"]) for item in trace_ranges)
    common_last = min(float(item["lastPs"]) for item in trace_ranges)
    aligned_first = math.ceil((common_first / step_ps) - 1e-12) * step_ps
    aligned_last = math.floor((common_last / step_ps) + 1e-12) * step_ps
    if aligned_last < aligned_first:
        raise StrictTdrRuntimeError(
            "strict TDR traces have no common interval on the internal uniform grid"
        )
    interval_count = round((aligned_last - aligned_first) / step_ps)
    row_count = interval_count + 1
    if row_count <= 0:
        raise StrictTdrRuntimeError("strict TDR uniform waveform grid is empty")
    return {
        "policy": "aedt-native-common-valid-uniform-grid",
        "api": "ReportSetup.ExportUniformPointsToFile",
        "stepPs": step_ps,
        "firstPs": aligned_first,
        "lastPs": aligned_last,
        "rowCount": row_count,
        "traceRanges": trace_ranges,
    }


def native_waveform_csv_evidence(
    path: Path,
    *,
    trace_names: Sequence[str],
    uniform_grid: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate an AEDT-native uniform report export and record its provenance."""

    analysis = analyze_waveform_csv(path, expected_trace_names=trace_names)
    expected_row_count = int(uniform_grid.get("rowCount") or 0)
    expected_first = _finite(uniform_grid.get("firstPs"), where="uniformGrid.firstPs")
    expected_last = _finite(uniform_grid.get("lastPs"), where="uniformGrid.lastPs")
    if analysis["rowCount"] != expected_row_count:
        raise StrictTdrRuntimeError(
            "AEDT native waveform CSV row count differs from the uniform export grid"
        )
    actual_range = analysis["timeGrid"]
    if not math.isclose(actual_range["firstPs"], expected_first, rel_tol=0, abs_tol=1e-9):
        raise StrictTdrRuntimeError(
            "AEDT native waveform CSV first time differs from the uniform export grid"
        )
    if not math.isclose(actual_range["lastPs"], expected_last, rel_tol=0, abs_tol=1e-9):
        raise StrictTdrRuntimeError(
            "AEDT native waveform CSV last time differs from the uniform export grid"
        )
    evidence = artifact_evidence(path)
    evidence.update(
        {
            "rowCount": analysis["rowCount"],
            "traceNames": analysis["traceNames"],
            "csvAnalysis": analysis,
            "source": {
                "kind": "aedt-native-report-uniform-export",
                "api": "ReportSetup.ExportUniformPointsToFile",
                "uniformGrid": dict(uniform_grid),
                "pythonInterpolation": False,
            },
        }
    )
    return evidence


def apply_strict_native_report(
    app: Any,
    report: Any,
    group: StrictTdrGroup,
    *,
    endpoint_notes: Mapping[str, Any],
    native_target_range: Mapping[str, Any],
) -> dict[str, Any]:
    if not report or str(getattr(report, "plot_name", "")) != group.name:
        raise StrictTdrRuntimeError(
            f"AEDT native report title must be the Group name only: {group.name!r}"
        )
    if endpoint_notes.get("status") != "rendered":
        raise StrictTdrRuntimeError(
            f"Near/Far endpoint annotations were not rendered for {group.name!r}"
        )
    endpoint_labels = endpoint_notes.get("labels") or []
    if (
        not isinstance(endpoint_labels, list)
        or int(endpoint_notes.get("labelCount") or 0) != len(endpoint_labels)
        or len(endpoint_labels) < 2
        or len(endpoint_labels) % 2
    ):
        raise StrictTdrRuntimeError(
            f"Near/Far endpoint annotations are incomplete for {group.name!r}"
        )
    if any(not isinstance(label, Mapping) for label in endpoint_labels):
        raise StrictTdrRuntimeError(
            f"Near/Far endpoint annotation record is invalid for {group.name!r}"
        )
    for label in endpoint_labels:
        if (
            not str(label.get("refdes") or "").strip()
            or not str(label.get("noteName") or "").strip()
            or str(label.get("side") or "") not in {"start", "end"}
            or float(label.get("fontSizePt") or 0) != REPORT_FONT_SIZE_PT
            or not bool(label.get("bold"))
            or str(label.get("font") or "").casefold() != REPORT_FONT_NAME.casefold()
            or list(label.get("colorRgb") or []) != list(REPORT_COLOR_RGB)
        ):
            raise StrictTdrRuntimeError(
                f"Near/Far endpoint annotation style mismatch for {group.name!r}"
            )
    if native_target_range.get("status") != "rendered":
        raise StrictTdrRuntimeError(
            f"Spec Target Range was not rendered for {group.name!r}"
        )
    rendered_bands = native_target_range.get("targetBandsOhm") or []
    if len(rendered_bands) != 1:
        raise StrictTdrRuntimeError(
            f"strict report {group.name!r} must render exactly one Spec Target Range"
        )
    rendered_band = rendered_bands[0]
    rendered_lines = rendered_band.get("limitLines") or []
    rendered_label = rendered_band.get("label") or {}
    if (
        _finite(rendered_band.get("lower"), where="native Target lower")
        != group.target_min_ohm
        or _finite(rendered_band.get("upper"), where="native Target upper")
        != group.target_max_ohm
        or int(native_target_range.get("limitLineCount") or 0) != 2
        or int(native_target_range.get("labelCount") or 0) != 1
        or not isinstance(rendered_lines, list)
        or len(rendered_lines) != 2
        or any(not isinstance(item, Mapping) for item in rendered_lines)
        or [str(item.get("bound") or "") for item in rendered_lines]
        != ["lower", "upper"]
        or any(
            not str(item.get("lineName") or "").strip()
            or item.get("styled") is not True
            or item.get("violationEmphasis") is not False
            or item.get("hatchPixels") != 0
            for item in rendered_lines
        )
        or not isinstance(rendered_label, Mapping)
        or not str(rendered_label.get("noteName") or "").strip()
        or rendered_label.get("styled") is not True
    ):
        raise StrictTdrRuntimeError(
            f"AEDT native Target Range evidence differs from Spec for {group.name!r}"
        )

    header_ok = report.edit_header(
        company_name="",
        show_design_name=False,
        font=REPORT_FONT_NAME,
        title_size=REPORT_TITLE_FONT_SIZE_PT,
        subtitle_size=REPORT_SUBTITLE_FONT_SIZE_PT,
        italic=False,
        bold=True,
        color=REPORT_COLOR_RGB,
    )
    if not header_ok:
        raise StrictTdrRuntimeError(f"AEDT report header style failed: {group.name}")
    y_ok = report.edit_y_axis_scaling(
        name="Y1",
        linear_scaling=True,
        min_scale=f"{group.y_min_ohm:g}ohm",
        max_scale=f"{group.y_max_ohm:g}ohm",
        units="ohm",
    )
    x_ok = report.edit_x_axis_scaling(
        linear_scaling=True,
        min_scale=f"{group.x_min_ps:g}ps",
        max_scale=f"{group.x_max_ps:g}ps",
        units="ps",
    )
    if not y_ok or not x_ok:
        raise StrictTdrRuntimeError(f"AEDT report axis scaling failed: {group.name}")

    marker_value = f"{group.marker_x_ps:g}ps"
    marker_name = report.add_cartesian_x_marker(marker_value)
    if not isinstance(marker_name, str) or not marker_name.strip():
        raise StrictTdrRuntimeError(
            f"AEDT did not return a native X Marker name for {group.name!r}"
        )
    update_return = app.post.oreportsetup.UpdateReports([group.name])
    if update_return is False or update_return == 0:
        raise StrictTdrRuntimeError(f"AEDT native report update failed: {group.name}")
    operations = {
        "schema": NATIVE_REPORT_OPERATION_SCHEMA,
        "status": "api-calls-verified-live-visual-pending",
        "operations": {
            "title": {
                "apiIdentity": "Standard.edit_header",
                "invocation": {
                    "companyName": "",
                    "showDesignName": False,
                    "font": REPORT_FONT_NAME,
                    "titleSizePt": REPORT_TITLE_FONT_SIZE_PT,
                    "subtitleSizePt": REPORT_SUBTITLE_FONT_SIZE_PT,
                    "italic": False,
                    "bold": True,
                    "colorRgb": list(REPORT_COLOR_RGB),
                },
                "result": {"succeeded": True, "returnValue": repr(header_ok)},
            },
            "yAxis": {
                "apiIdentity": "Standard.edit_y_axis_scaling",
                "invocation": {
                    "name": "Y1",
                    "linearScaling": True,
                    "minScale": f"{group.y_min_ohm:g}ohm",
                    "maxScale": f"{group.y_max_ohm:g}ohm",
                    "units": "ohm",
                },
                "result": {"succeeded": True, "returnValue": repr(y_ok)},
            },
            "xAxis": {
                "apiIdentity": "Standard.edit_x_axis_scaling",
                "invocation": {
                    "linearScaling": True,
                    "minScale": f"{group.x_min_ps:g}ps",
                    "maxScale": f"{group.x_max_ps:g}ps",
                    "units": "ps",
                },
                "result": {"succeeded": True, "returnValue": repr(x_ok)},
            },
            "xMarker": {
                "apiIdentity": "Standard.add_cartesian_x_marker",
                "invocation": {"value": marker_value},
                "result": {
                    "succeeded": True,
                    "returnedMarkerName": marker_name,
                },
                "readBack": {
                    "status": "not-claimed",
                    "reason": "PyAEDT marker collection read-back API is not documented",
                },
            },
            "updateReports": {
                "apiIdentity": "ReportSetup.UpdateReports",
                "invocation": {"reportNames": [group.name]},
                "result": {"succeeded": True, "returnValue": repr(update_return)},
            },
        },
        "liveVisualConfirmation": {
            "status": "pending",
            "requiredEvidence": (
                "AEDT 2025.2 saved-project/JPG visual confirmation of title, axes, "
                "center X Marker, endpoint RefDes and Target Range"
            ),
        },
    }
    return {
        "reportName": group.name,
        "title": {
            "text": group.name,
            "font": REPORT_FONT_NAME,
            "fontSizePt": REPORT_TITLE_FONT_SIZE_PT,
            "subtitleFontSizePt": REPORT_SUBTITLE_FONT_SIZE_PT,
            "bold": True,
            "colorRgb": list(REPORT_COLOR_RGB),
            "companyName": "",
            "showDesignName": False,
            "api": "Standard.edit_header",
        },
        "endpointNotes": dict(endpoint_notes),
        "nativeTargetRange": dict(native_target_range),
        "view": {
            "xAxisPs": {"min": group.x_min_ps, "max": group.x_max_ps},
            "yAxisOhm": {"min": group.y_min_ohm, "max": group.y_max_ohm},
            "yAxisSource": "Spec.referenceImpedanceOhm +/- administrator margin",
        },
        "xMarker": {
            "count": 1,
            "name": marker_name,
            "xPs": group.marker_x_ps,
            "valueArgument": marker_value,
            "policy": "(displayedXMin + displayedXMax) / 2",
            "api": "Standard.add_cartesian_x_marker",
            "apiReturnVerified": True,
            "collectionReadback": "not-claimed; AEDT 2025.2 live visual confirmation pending",
        },
        "reportUpdate": {
            "api": "ReportSetup.UpdateReports",
            "return": repr(update_return),
            "falseReturnRejected": True,
        },
        "operationEvidence": operations,
    }


def write_waveform_csv(
    path: Path,
    *,
    trace_names: Sequence[str],
    samples_by_trace: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    if path.exists():
        raise StrictTdrRuntimeError(f"stale waveform CSV exists: {path}")
    normalized_trace_names = _strict_string_list(
        trace_names,
        where="strict TDR waveform traceNames",
    )
    missing = [name for name in normalized_trace_names if not samples_by_trace.get(name)]
    if missing:
        raise StrictTdrRuntimeError(f"TDR solution data is missing traces: {missing}")
    lengths = {len(samples_by_trace[name]) for name in normalized_trace_names}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) <= 0:
        raise StrictTdrRuntimeError(
            "all strict TDR traces must have the same non-zero sample count"
        )
    row_count = next(iter(lengths))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["Time [ps]", *[f"{name} [ohm]" for name in normalized_trace_names]]
        )
        previous_time: float | None = None
        for index in range(row_count):
            times = [
                _finite(
                    samples_by_trace[name][index].get("time_ps"),
                    where=f"samplesByTrace.{name}[{index}].time_ps",
                )
                for name in normalized_trace_names
            ]
            if any(not math.isclose(value, times[0], rel_tol=0, abs_tol=1e-9) for value in times[1:]):
                raise StrictTdrRuntimeError(
                    f"TDR trace time grids differ at sample {index}: {times}"
                )
            if previous_time is not None and times[0] <= previous_time:
                raise StrictTdrRuntimeError(
                    "TDR waveform Time [ps] must be strictly increasing: "
                    f"sample={index}, previous={previous_time}, current={times[0]}"
                )
            previous_time = times[0]
            values = [
                _finite(
                    samples_by_trace[name][index].get("impedance_ohm"),
                    where=f"samplesByTrace.{name}[{index}].impedance_ohm",
                )
                for name in normalized_trace_names
            ]
            writer.writerow([f"{times[0]:.12g}", *[f"{value:.12g}" for value in values]])
    evidence = artifact_evidence(path)
    analysis = analyze_waveform_csv(
        path,
        expected_trace_names=normalized_trace_names,
    )
    evidence["rowCount"] = analysis["rowCount"]
    evidence["traceNames"] = analysis["traceNames"]
    evidence["csvAnalysis"] = analysis
    return evidence


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _contained_file_from_evidence(
    evidence: Any,
    *,
    base: Path,
    job_root: Path,
    where: str,
    expected: Path | None = None,
    started_at_ns: int | None = None,
) -> Path:
    if not isinstance(evidence, Mapping):
        raise StrictTdrRuntimeError(f"{where} evidence must be an object")
    value = evidence.get("path")
    if not isinstance(value, str) or not value.strip():
        raise StrictTdrRuntimeError(f"{where} evidence path is missing")
    raw = Path(value)
    resolved = (raw if raw.is_absolute() else base / raw).resolve()
    root = job_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StrictTdrRuntimeError(f"{where} is outside the Job root: {resolved}") from exc
    if expected is not None and resolved != expected.resolve():
        raise StrictTdrRuntimeError(
            f"{where} path differs: expected={expected.resolve()}, actual={resolved}"
        )
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise StrictTdrRuntimeError(f"{where} is missing or empty: {resolved}")
    if started_at_ns is not None and resolved.stat().st_mtime_ns < started_at_ns:
        raise StrictTdrRuntimeError(f"{where} is stale: {resolved}")
    if evidence.get("size") != resolved.stat().st_size:
        raise StrictTdrRuntimeError(f"{where} recorded size differs")
    if evidence.get("sha256") != sha256_file(resolved):
        raise StrictTdrRuntimeError(f"{where} recorded SHA-256 differs")
    return resolved


def _validate_native_operation_evidence(
    native: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
) -> str:
    name = str(group.get("name") or "")
    view = group.get("view") or {}
    x_axis = view.get("xAxisPs") or {}
    y_axis = view.get("yAxisOhm") or {}
    x_min = _finite(x_axis.get("min"), where=f"{name} x min")
    x_max = _finite(x_axis.get("max"), where=f"{name} x max")
    y_min = _finite(y_axis.get("min"), where=f"{name} y min")
    y_max = _finite(y_axis.get("max"), where=f"{name} y max")
    marker_x = (x_min + x_max) / 2.0
    title = native.get("title") or {}
    marker = native.get("xMarker") or {}
    native_view = native.get("view") or {}
    report_update = native.get("reportUpdate") or {}
    if (
        not isinstance(title, Mapping)
        or title.get("text") != name
        or title.get("font") != REPORT_FONT_NAME
        or title.get("fontSizePt") != REPORT_TITLE_FONT_SIZE_PT
        or title.get("subtitleFontSizePt") != REPORT_SUBTITLE_FONT_SIZE_PT
        or title.get("bold") is not True
        or title.get("colorRgb") != list(REPORT_COLOR_RGB)
        or title.get("companyName") != ""
        or title.get("showDesignName") is not False
        or title.get("api") != "Standard.edit_header"
    ):
        raise StrictTdrRuntimeError(f"{name} native report title/style evidence differs")
    if (
        native_view.get("xAxisPs") != {"min": x_min, "max": x_max}
        or native_view.get("yAxisOhm") != {"min": y_min, "max": y_max}
        or marker.get("count") != 1
        or not str(marker.get("name") or "").strip()
        or not math.isclose(
            _finite(marker.get("xPs"), where=f"{name} marker x"),
            marker_x,
            rel_tol=0,
            abs_tol=1e-9,
        )
        or marker.get("valueArgument") != f"{marker_x:g}ps"
        or marker.get("policy") != "(displayedXMin + displayedXMax) / 2"
        or marker.get("api") != "Standard.add_cartesian_x_marker"
        or marker.get("apiReturnVerified") is not True
        or not isinstance(report_update, Mapping)
        or report_update.get("api") != "ReportSetup.UpdateReports"
        or report_update.get("falseReturnRejected") is not True
    ):
        raise StrictTdrRuntimeError(
            f"{name} native report axes/Marker/update evidence differs"
        )
    operation_evidence = native.get("operationEvidence") or {}
    if (
        not isinstance(operation_evidence, Mapping)
        or operation_evidence.get("schema") != NATIVE_REPORT_OPERATION_SCHEMA
        or operation_evidence.get("status")
        != "api-calls-verified-live-visual-pending"
    ):
        raise StrictTdrRuntimeError(
            f"{name} native report operation evidence schema/status is invalid"
        )
    operations = operation_evidence.get("operations") or {}
    if not isinstance(operations, Mapping) or set(operations) != {
        "title",
        "yAxis",
        "xAxis",
        "xMarker",
        "updateReports",
    }:
        raise StrictTdrRuntimeError(f"{name} native report operation set differs")

    expected = {
        "title": (
            "Standard.edit_header",
            {
                "companyName": "",
                "showDesignName": False,
                "font": REPORT_FONT_NAME,
                "titleSizePt": REPORT_TITLE_FONT_SIZE_PT,
                "subtitleSizePt": REPORT_SUBTITLE_FONT_SIZE_PT,
                "italic": False,
                "bold": True,
                "colorRgb": list(REPORT_COLOR_RGB),
            },
        ),
        "yAxis": (
            "Standard.edit_y_axis_scaling",
            {
                "name": "Y1",
                "linearScaling": True,
                "minScale": f"{y_min:g}ohm",
                "maxScale": f"{y_max:g}ohm",
                "units": "ohm",
            },
        ),
        "xAxis": (
            "Standard.edit_x_axis_scaling",
            {
                "linearScaling": True,
                "minScale": f"{x_min:g}ps",
                "maxScale": f"{x_max:g}ps",
                "units": "ps",
            },
        ),
        "xMarker": (
            "Standard.add_cartesian_x_marker",
            {"value": f"{marker_x:g}ps"},
        ),
        "updateReports": (
            "ReportSetup.UpdateReports",
            {"reportNames": [name]},
        ),
    }
    for key, (api_identity, invocation) in expected.items():
        operation = operations.get(key)
        if (
            not isinstance(operation, Mapping)
            or operation.get("apiIdentity") != api_identity
            or operation.get("invocation") != invocation
        ):
            raise StrictTdrRuntimeError(
                f"{name} native report {key} API/invocation evidence differs"
            )
        result = operation.get("result") or {}
        if not isinstance(result, Mapping) or result.get("succeeded") is not True:
            raise StrictTdrRuntimeError(
                f"{name} native report {key} result is not successful"
            )
    marker_result = (operations["xMarker"].get("result") or {})
    if not str(marker_result.get("returnedMarkerName") or "").strip():
        raise StrictTdrRuntimeError(f"{name} native X Marker name is missing")
    marker_readback = operations["xMarker"].get("readBack") or {}
    if not isinstance(marker_readback, Mapping) or marker_readback.get("status") not in {
        "not-claimed",
        "verified",
    }:
        raise StrictTdrRuntimeError(f"{name} native X Marker read-back evidence is invalid")
    live_visual = operation_evidence.get("liveVisualConfirmation") or {}
    live_status = str(live_visual.get("status") or "")
    if live_status != "pending":
        raise StrictTdrRuntimeError(
            f"{name} production native report visual confirmation must remain pending"
        )
    return live_status


def _validate_endpoint_and_target_evidence(
    report: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    expected_directions: Sequence[Mapping[str, Any]],
) -> None:
    name = str(group.get("name") or "")
    native = report.get("strictNativeReport") or {}
    if not isinstance(native, Mapping):
        raise StrictTdrRuntimeError(f"{name} strictNativeReport is invalid")
    endpoint = report.get("endpointNotes") or {}
    target_range = report.get("nativeTargetRange") or {}
    if (
        not isinstance(endpoint, Mapping)
        or not isinstance(target_range, Mapping)
        or _canonical_json(endpoint) != _canonical_json(native.get("endpointNotes"))
        or _canonical_json(target_range)
        != _canonical_json(native.get("nativeTargetRange"))
    ):
        raise StrictTdrRuntimeError(
            f"{name} report/native endpoint or Target Range evidence differs"
        )

    grouped: dict[tuple[str, str], set[str]] = {}
    for direction in expected_directions:
        channel = str(direction.get("channel") or "").strip()
        start = str(direction.get("startRefdes") or "").strip()
        end = str(direction.get("endRefdes") or "").strip()
        if not channel or not start or not end:
            raise StrictTdrRuntimeError(
                f"{name} direction provenance lacks channel/startRefdes/endRefdes"
            )
        grouped.setdefault((start, end), set()).add(channel)
    expected_labels: list[dict[str, Any]] = []
    for row_index, pair in enumerate(sorted(grouped)):
        channels = sorted(grouped[pair])
        expected_labels.extend(
            [
                {
                    "side": "start",
                    "refdes": pair[0],
                    "channels": channels,
                    "rowIndex": row_index,
                },
                {
                    "side": "end",
                    "refdes": pair[1],
                    "channels": channels,
                    "rowIndex": row_index,
                },
            ]
        )
    labels = endpoint.get("labels") or []
    if (
        endpoint.get("status") != "rendered"
        or endpoint.get("labelCount") != len(expected_labels)
        or not isinstance(labels, list)
        or len(labels) != len(expected_labels)
    ):
        raise StrictTdrRuntimeError(f"{name} endpoint RefDes label set is incomplete")
    for actual, expected in zip(labels, expected_labels):
        if not isinstance(actual, Mapping):
            raise StrictTdrRuntimeError(f"{name} endpoint RefDes label is invalid")
        if any(actual.get(key) != value for key, value in expected.items()):
            raise StrictTdrRuntimeError(
                f"{name} endpoint RefDes/direction binding differs"
            )
        if (
            not str(actual.get("noteName") or "").strip()
            or actual.get("font") != REPORT_FONT_NAME
            or _finite(actual.get("fontSizePt"), where=f"{name} endpoint font")
            != REPORT_FONT_SIZE_PT
            or actual.get("bold") is not True
            or actual.get("colorRgb") != list(REPORT_COLOR_RGB)
        ):
            raise StrictTdrRuntimeError(f"{name} endpoint RefDes operation/style differs")

    spec = group.get("specImpedance") or {}
    target = spec.get("targetRangeOhm") or {}
    lower = _finite(target.get("lower"), where=f"{name} Target lower")
    upper = _finite(target.get("upper"), where=f"{name} Target upper")
    bands = target_range.get("targetBandsOhm") or []
    if (
        target_range.get("status") != "rendered"
        or target_range.get("limitLineCount") != 2
        or target_range.get("labelCount") != 1
        or not isinstance(bands, list)
        or len(bands) != 1
        or not isinstance(bands[0], Mapping)
    ):
        raise StrictTdrRuntimeError(f"{name} native Target Range set is incomplete")
    band = bands[0]
    lines = band.get("limitLines") or []
    label = band.get("label") or {}
    if (
        _finite(band.get("lower"), where=f"{name} native lower") != lower
        or _finite(band.get("upper"), where=f"{name} native upper") != upper
        or sorted(_strict_string_list(band.get("channels"), where=f"{name} Target channels"))
        != sorted(list(group.get("channels") or []))
        or sorted(_strict_string_list(band.get("traceNames"), where=f"{name} Target traces"))
        != sorted(list(report.get("traceNames") or []))
        or not isinstance(lines, list)
        or len(lines) != 2
        or any(not isinstance(item, Mapping) for item in lines)
        or [item.get("bound") for item in lines] != ["lower", "upper"]
        or [item.get("valueOhm") for item in lines] != [lower, upper]
        or any(
            not str(item.get("lineName") or "").strip()
            or item.get("styled") is not True
            or item.get("violationEmphasis") is not False
            or item.get("hatchPixels") != 0
            for item in lines
        )
        or not isinstance(label, Mapping)
        or not str(label.get("noteName") or "").strip()
        or label.get("styled") is not True
    ):
        raise StrictTdrRuntimeError(
            f"{name} native Target Range operation/Spec binding differs"
        )


def validate_strict_tdr_artifact_package(
    record: Mapping[str, Any],
    *,
    run_dir: Path,
    job_root: Path,
    expected_batch_id: str,
    started_at_ns: int | None = None,
) -> dict[str, Any]:
    """Fail closed on the strict native TDR record and its public artifacts."""

    run_dir = run_dir.resolve()
    job_root = job_root.resolve()
    try:
        run_dir.relative_to(job_root)
    except ValueError as exc:
        raise StrictTdrRuntimeError("strict TDR run directory is outside the Job root") from exc
    if (
        record.get("schema") != STRICT_TDR_RECORD_SCHEMA
        or record.get("status") != "ok"
        or record.get("buildMode") != "customer-strict-native-aedt-tdr"
    ):
        raise StrictTdrRuntimeError("strict native TDR record schema/status/mode is invalid")
    contract = record.get("strictContract") or {}
    if (
        not isinstance(contract, Mapping)
        or contract.get("schema") != "si-tdr-strict-circuit-contract/1"
        or contract.get("batchId") != expected_batch_id
    ):
        raise StrictTdrRuntimeError("strict Circuit contract/batch ID is invalid")
    groups = contract.get("groups") or []
    reports = record.get("reports") or []
    artifacts = record.get("artifacts") or {}
    report_artifacts = artifacts.get("nativeReportJpg") or []
    if (
        not isinstance(groups, list)
        or not groups
        or not isinstance(reports, list)
        or not isinstance(report_artifacts, list)
        or len(groups) != len(reports)
        or len(reports) != len(report_artifacts)
    ):
        raise StrictTdrRuntimeError("strict TDR Group/report/JPG sets differ")

    record_trace_names = _strict_string_list(
        record.get("traceNames"), where="strict TDR record.traceNames"
    )
    sample_count = record.get("sampleCount")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        raise StrictTdrRuntimeError("strict TDR record.sampleCount must be positive")
    probes = (record.get("manualTopology") or {}).get("tdrProbes") or []
    if not isinstance(probes, list) or any(not isinstance(item, Mapping) for item in probes):
        raise StrictTdrRuntimeError("strict TDR probe evidence is invalid")
    by_channel: dict[str, Mapping[str, Any]] = {}
    for probe in probes:
        channel = str(probe.get("channel") or "").strip()
        trace = str(probe.get("traceName") or "").strip()
        if not channel or not trace or channel in by_channel:
            raise StrictTdrRuntimeError("strict TDR probe channel/trace evidence differs")
        by_channel[channel] = probe
    if [str(item.get("traceName")) for item in probes] != record_trace_names:
        raise StrictTdrRuntimeError("strict TDR record/probe trace order differs")

    report_paths: list[Path] = []
    live_visual_statuses: list[str] = []
    covered_channels: list[str] = []
    covered_traces: list[str] = []
    circuit_dir = run_dir / "circuit"
    margin = _finite(contract.get("yAxisMarginOhm"), where="strict TDR Y margin")
    expected_impedance_by_channel: dict[str, float] = {}
    for group, report, evidence in zip(groups, reports, report_artifacts):
        if not isinstance(group, Mapping) or not isinstance(report, Mapping):
            raise StrictTdrRuntimeError("strict TDR Group/report entry is invalid")
        name = str(group.get("name") or "").strip()
        channels = _strict_string_list(group.get("channels"), where=f"{name} channels")
        profile = group.get("tdrProfile") or {}
        if (
            not isinstance(profile, Mapping)
            or not isinstance(profile.get("settings"), Mapping)
            or set(profile["settings"]) != set(TDR_PROFILE_FIELDS)
        ):
            raise StrictTdrRuntimeError(f"{name} TDR Profile fields differ")
        spec = group.get("specImpedance") or {}
        target = spec.get("targetRangeOhm") or {}
        reference = _finite(
            spec.get("referenceImpedanceOhm"), where=f"{name} Spec Target"
        )
        lower = _finite(target.get("lower"), where=f"{name} Spec Min")
        upper = _finite(target.get("upper"), where=f"{name} Spec Max")
        termination = group.get("farEndTermination") or {}
        view = group.get("view") or {}
        y_axis = view.get("yAxisOhm") or {}
        x_axis = view.get("xAxisPs") or {}
        if (
            lower > reference
            or upper < reference
            or target.get("source") != "Spec"
            or termination.get("source") != "Spec.referenceImpedanceOhm"
            or _finite(termination.get("valueOhm"), where=f"{name} termination")
            != reference
            or _finite(y_axis.get("min"), where=f"{name} Y min")
            != reference - margin
            or _finite(y_axis.get("max"), where=f"{name} Y max")
            != reference + margin
            or _finite(x_axis.get("max"), where=f"{name} X max")
            <= _finite(x_axis.get("min"), where=f"{name} X min")
        ):
            raise StrictTdrRuntimeError(
                f"{name} Spec Target/termination/axis contract differs"
            )
        for channel in channels:
            expected_impedance_by_channel[channel] = reference
        expected_traces: list[str] = []
        expected_directions: list[Mapping[str, Any]] = []
        for channel in channels:
            probe = by_channel.get(channel)
            if probe is None:
                raise StrictTdrRuntimeError(f"{name} channel has no TDR probe: {channel}")
            expected_traces.append(str(probe.get("traceName")))
            direction = probe.get("directionProvenance")
            if not isinstance(direction, Mapping):
                raise StrictTdrRuntimeError(
                    f"{name}/{channel} direction provenance is missing"
                )
            expected_directions.append(direction)
        if (
            report.get("name") != name
            or report.get("channels") != channels
            or report.get("traceNames") != expected_traces
            or _canonical_json(report.get("tdrProfile"))
            != _canonical_json(group.get("tdrProfile"))
            or _canonical_json(report.get("directionProvenance") or [])
            != _canonical_json(expected_directions)
        ):
            raise StrictTdrRuntimeError(
                f"{name} report/Group trace, channel, Profile or direction binding differs"
            )
        native = report.get("strictNativeReport") or {}
        if not isinstance(native, Mapping) or native.get("reportName") != name:
            raise StrictTdrRuntimeError(f"{name} strict native report identity differs")
        _validate_endpoint_and_target_evidence(
            report,
            group=group,
            expected_directions=expected_directions,
        )
        live_visual_statuses.append(
            _validate_native_operation_evidence(native, group=group)
        )
        expected_image = circuit_dir / f"{NATIVE_REPORT_IMAGE_PREFIX}{name}.jpg"
        image_path = _contained_file_from_evidence(
            evidence,
            base=run_dir,
            job_root=job_root,
            where=f"{name} native report JPG",
            expected=expected_image,
            started_at_ns=started_at_ns,
        )
        image_value = Path(str(report.get("imagePath") or ""))
        resolved_image_value = (
            image_value if image_value.is_absolute() else run_dir / image_value
        ).resolve()
        if resolved_image_value != image_path:
            raise StrictTdrRuntimeError(f"{name} report.imagePath differs from its artifact")
        actual_analysis = analyze_native_report_jpeg(image_path)
        if (
            _canonical_json(evidence.get("imageAnalysis"))
            != _canonical_json(actual_analysis)
            or _canonical_json(report.get("imageAnalysis"))
            != _canonical_json(actual_analysis)
        ):
            raise StrictTdrRuntimeError(f"{name} native report JPG analysis evidence differs")
        report_paths.append(image_path)
        covered_channels.extend(channels)
        covered_traces.extend(expected_traces)
    if (
        len(covered_channels) != len(by_channel)
        or set(covered_channels) != set(by_channel)
        or len(covered_traces) != len(record_trace_names)
        or set(covered_traces) != set(record_trace_names)
    ):
        raise StrictTdrRuntimeError("strict TDR reports do not cover probe channels/traces exactly once")
    for probe in probes:
        channel = str(probe.get("channel") or "")
        if (
            _finite(
                probe.get("referenceImpedanceOhm"),
                where=f"{channel} probe impedance",
            )
            != expected_impedance_by_channel.get(channel)
            or probe.get("referenceImpedanceOhmSource")
            != "Spec.referenceImpedanceOhm"
        ):
            raise StrictTdrRuntimeError(
                f"{channel} TDR probe Z0 is not the exact Spec Target"
            )
    far_end = (record.get("manualTopology") or {}).get("farEnd") or []
    if (
        not isinstance(far_end, list)
        or len(far_end) != len(probes)
        or any(not isinstance(item, Mapping) for item in far_end)
    ):
        raise StrictTdrRuntimeError("strict TDR far-end component evidence is invalid")
    far_channels: set[str] = set()
    for item in far_end:
        channel = str(item.get("channel") or "").strip()
        if (
            not channel
            or channel in far_channels
            or _finite(
                item.get("terminationValueOhm"),
                where=f"{channel} termination",
            )
            != expected_impedance_by_channel.get(channel)
            or item.get("terminationSource") != "Spec.referenceImpedanceOhm"
        ):
            raise StrictTdrRuntimeError(
                f"{channel} far-end termination is not the exact Spec Target"
            )
        far_channels.add(channel)
    if far_channels != set(expected_impedance_by_channel):
        raise StrictTdrRuntimeError("strict TDR far-end channel coverage differs")

    waveform_path = _contained_file_from_evidence(
        artifacts.get("waveformCsv"),
        base=run_dir,
        job_root=job_root,
        where="strict Tdr_waveform.csv",
        expected=circuit_dir / "Tdr_waveform.csv",
        started_at_ns=started_at_ns,
    )
    waveform_analysis = analyze_waveform_csv(
        waveform_path,
        expected_trace_names=record_trace_names,
    )
    waveform_evidence = artifacts.get("waveformCsv") or {}
    if (
        waveform_evidence.get("rowCount") != sample_count
        or waveform_evidence.get("traceNames") != record_trace_names
        or _canonical_json(waveform_evidence.get("csvAnalysis"))
        != _canonical_json(waveform_analysis)
    ):
        raise StrictTdrRuntimeError("strict TDR waveform CSV evidence/record differs")
    return {
        "reports": report_paths,
        "waveformCsv": waveform_path,
        "imageAnalyses": [
            dict((item or {}).get("imageAnalysis") or {})
            for item in report_artifacts
        ],
        "waveformAnalysis": waveform_analysis,
        "liveVisualConfirmation": (
            "verified"
            if live_visual_statuses and set(live_visual_statuses) == {"verified"}
            else "pending"
        ),
    }


def results_directory_evidence(path: Path) -> dict[str, Any]:
    if _is_link_or_reparse(path):
        raise StrictTdrRuntimeError(
            f"AEDT results directory cannot be a symlink/reparse point: {path}"
        )
    resolved = path.resolve()
    if not resolved.is_dir():
        raise StrictTdrRuntimeError(f"AEDT results directory is missing: {resolved}")
    members = list(resolved.rglob("*"))
    for item in members:
        if _is_link_or_reparse(item):
            raise StrictTdrRuntimeError(
                f"AEDT results directory contains a symlink/reparse point: {item}"
            )
    directories = sorted(
        item.relative_to(resolved).as_posix()
        for item in members
        if item.is_dir()
    )
    files = sorted(
        (item for item in members if item.is_file()),
        key=lambda item: item.relative_to(resolved).as_posix(),
    )
    if not files:
        raise StrictTdrRuntimeError(f"AEDT results directory is empty: {resolved}")
    manifest: list[dict[str, Any]] = []
    for item in files:
        if item.is_symlink():
            raise StrictTdrRuntimeError(
                f"AEDT results directory contains a symlink: {item}"
            )
        # AEDT/EDB는 자기 트리에 0바이트 부산물을 남길 수 있다.
        # Ansys 생성 트리 안에서는 빈 파일을 거부하지 않고 그대로 기록한다.
        manifest.append(
            {
                "relativePath": item.relative_to(resolved).as_posix(),
                "size": item.stat().st_size,
                "sha256": sha256_file(item),
            }
        )
    return {
        "path": str(resolved),
        "fileCount": len(manifest),
        "totalBytes": sum(int(item["size"]) for item in manifest),
        "treeSha256": hashlib.sha256(
            _canonical_json(
                {"directories": directories, "fileManifest": manifest}
            ).encode("utf-8")
        ).hexdigest(),
        "directories": directories,
        "fileManifest": manifest,
    }
