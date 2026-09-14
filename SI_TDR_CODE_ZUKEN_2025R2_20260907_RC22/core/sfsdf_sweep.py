"""Strict SIWave SFSDF frequency parser and documented COM command adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .syz_options import normalize_syz_settings
except ImportError:  # Support compact customer core imports.
    from syz_options import normalize_syz_settings  # type: ignore[no-redef]


SFSDF_PARSE_SCHEMA = "si-tdr-sfsdf-frequency-definition/1"
SFSDF_APPLICATION_SCHEMA = "si-tdr-sfsdf-command-application/1"
SYZ_OPTIONS_APPLICATION_SCHEMA = "si-tdr-administrator-syz-options-application/1"
PARSER_COMMAND_APPLY_MODE = "approved-parser-command"
PARSER_COMMAND_CAPABILITY_IDENTITY = (
    "siwave-sfsdf-parser-documented-com-sweep-adapter"
)
MAX_GRID_POINTS = 1_000_000
FREQUENCY_GRID_SIGNIFICANT_DIGITS = 12
TLB_VOID_SUCCESS_APIS = frozenset(
    {
        "ScrSetSyzInterpSweep",
        "ScrSIwaveSyzComputeExactDcPoint",
        "ScrSIwaveSyzEnforceCausality",
        "ScrSIwaveSyzEnforcePassivity",
    }
)

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SINGLE = re.compile(rf"^(?P<value>{_NUMBER})Hz$")
_LIN = re.compile(
    rf"^LIN (?P<minimum>{_NUMBER})Hz (?P<maximum>{_NUMBER})Hz "
    rf"(?P<step>{_NUMBER})Hz$"
)
_COUNT = re.compile(
    rf"^(?P<kind>LINC|DEC) (?P<minimum>{_NUMBER})Hz "
    rf"(?P<maximum>{_NUMBER})Hz (?P<count>[+-]?\d+)$"
)


class SfsdfSweepError(ValueError):
    """Raised when an SFSDF frequency definition cannot be proven exact."""


@dataclass(frozen=True)
class ParsedSfsdf:
    path: Path
    source_sha256: str
    rows: tuple[dict[str, Any], ...]
    frequencies_hz: tuple[float, ...]
    evidence: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(token: str, *, line_number: int, field: str) -> Decimal:
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} {field} is not a finite decimal: {token!r}"
        ) from exc
    if not value.is_finite() or value < 0:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} {field} must be finite and non-negative"
        )
    return value


def _canonical_frequency(value: Decimal) -> str:
    if value == 0:
        return "0Hz"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + "Hz"


def _grid_contract(frequencies_hz: Sequence[float]) -> dict[str, Any]:
    if not frequencies_hz:
        raise SfsdfSweepError("SFSDF frequency grid is empty")
    if any(
        not math.isfinite(value) or value < 0
        for value in frequencies_hz
    ):
        raise SfsdfSweepError("SFSDF frequency grid contains invalid values")
    if any(
        current <= previous
        for previous, current in zip(frequencies_hz, frequencies_hz[1:])
    ):
        raise SfsdfSweepError(
            "SFSDF rows must produce one strictly increasing, non-overlapping grid"
        )
    digest = frequency_grid_sha256(frequencies_hz)
    return {
        "frequencyCount": len(frequencies_hz),
        "firstFrequencyHz": frequencies_hz[0],
        "lastFrequencyHz": frequencies_hz[-1],
        "frequencyGridSha256": digest,
    }


def frequency_grid_sha256(frequencies_hz: Sequence[float]) -> str:
    """Hash a full grid after solver-portable significant-digit normalization."""

    format_spec = f".{FREQUENCY_GRID_SIGNIFICANT_DIGITS}g"
    return hashlib.sha256(
        "\n".join(format(value, format_spec) for value in frequencies_hz).encode(
            "ascii"
        )
    ).hexdigest()


def _linear_count_grid(minimum: Decimal, maximum: Decimal, count: int) -> list[float]:
    with localcontext() as context:
        context.prec = 50
        span = maximum - minimum
        points = [
            float(minimum + (span * Decimal(index) / Decimal(count - 1)))
            for index in range(count)
        ]
    points[0] = float(minimum)
    points[-1] = float(maximum)
    return points


def _decade_count_grid(
    minimum: Decimal,
    maximum: Decimal,
    points_per_decade: int,
    *,
    line_number: int,
) -> list[float]:
    start = float(minimum)
    stop = float(maximum)
    decade_span = math.log10(stop) - math.log10(start)
    interval_count_float = decade_span * points_per_decade
    interval_count = round(interval_count_float)
    if not math.isclose(
        interval_count_float,
        interval_count,
        rel_tol=0.0,
        abs_tol=1e-10,
    ):
        raise SfsdfSweepError(
            f"SFSDF line {line_number} DEC range does not end on a "
            "points-per-decade interval"
        )
    if interval_count < 1 or interval_count + 1 > MAX_GRID_POINTS:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} DEC expands beyond {MAX_GRID_POINTS} points"
        )
    start_log = math.log(start)
    stop_log = math.log(stop)
    points = [
        math.exp(start_log + ((stop_log - start_log) * index / interval_count))
        for index in range(interval_count + 1)
    ]
    points[0] = start
    points[-1] = stop
    return points


def _row_grid(
    *,
    kind: str,
    minimum: Decimal,
    maximum: Decimal,
    last_token: str | None,
    line_number: int,
) -> tuple[list[float], dict[str, Any]]:
    if kind == "single":
        value = float(minimum)
        return [value], {
            "frequencyHz": value,
            "frequencyArg": _canonical_frequency(minimum),
        }
    if maximum <= minimum:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} maximum must be greater than minimum"
        )
    if last_token is None:
        raise SfsdfSweepError(f"SFSDF line {line_number} is missing its final value")

    if kind == "LIN":
        step = _decimal(last_token, line_number=line_number, field="step")
        if step <= 0:
            raise SfsdfSweepError(f"SFSDF line {line_number} step must be positive")
        with localcontext() as context:
            context.prec = 50
            quotient = (maximum - minimum) / step
        integral = quotient.to_integral_value()
        if quotient != integral:
            raise SfsdfSweepError(
                f"SFSDF line {line_number} LIN stop is not exactly reachable by step"
            )
        count = int(integral) + 1
        if count > MAX_GRID_POINTS:
            raise SfsdfSweepError(
                f"SFSDF line {line_number} expands beyond {MAX_GRID_POINTS} points"
            )
        with localcontext() as context:
            context.prec = 50
            points = [float(minimum + (step * index)) for index in range(count)]
        points[-1] = float(maximum)
        return points, {
            "minimumHz": float(minimum),
            "maximumHz": float(maximum),
            "stepHz": float(step),
            "minimumArg": _canonical_frequency(minimum),
            "maximumArg": _canonical_frequency(maximum),
            "stepArg": _canonical_frequency(step),
        }

    if re.fullmatch(r"[+-]?\d+", last_token) is None:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} {kind} count must be an integer"
        )
    count = int(last_token)
    minimum_count = 1 if kind == "DEC" else 2
    if count < minimum_count or count > MAX_GRID_POINTS:
        raise SfsdfSweepError(
            f"SFSDF line {line_number} {kind} count must be between "
            f"{minimum_count} and "
            f"{MAX_GRID_POINTS}"
        )
    if kind == "DEC":
        if minimum <= 0:
            raise SfsdfSweepError(
                f"SFSDF line {line_number} DEC minimum must be positive"
            )
        points = _decade_count_grid(
            minimum,
            maximum,
            count,
            line_number=line_number,
        )
    else:
        points = _linear_count_grid(minimum, maximum, count)
    return points, {
        "minimumHz": float(minimum),
        "maximumHz": float(maximum),
        "count": count,
        "countSemantic": (
            "points-per-decade" if kind == "DEC" else "total-point-count"
        ),
        "minimumArg": _canonical_frequency(minimum),
        "maximumArg": _canonical_frequency(maximum),
        "isLog": kind == "DEC",
    }


def parse_sfsdf_frequency_definition(path: Path) -> ParsedSfsdf:
    """Parse the statically confirmed SIWave Save/Load frequency grammar.

    Empty or whitespace-only lines are ignored and counted. Comments, unknown
    tokens, and trailing tokens are not part of the grammar and fail closed.
    """

    source = path.resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise SfsdfSweepError(f"SFSDF source is missing or empty: {source}")
    try:
        text = source.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise SfsdfSweepError(f"SFSDF must be UTF-8/ASCII text: {source}") from exc
    if any(ord(character) > 0x7F for character in text):
        raise SfsdfSweepError("SFSDF grammar permits ASCII tokens only")

    parsed_rows: list[dict[str, Any]] = []
    frequencies: list[float] = []
    blank_lines = 0
    for line_number, source_line in enumerate(text.splitlines(), start=1):
        stripped = source_line.strip()
        if not stripped:
            blank_lines += 1
            continue
        single = _SINGLE.fullmatch(stripped)
        if single is not None:
            minimum = _decimal(
                single.group("value"), line_number=line_number, field="frequency"
            )
            kind = "single"
            maximum = minimum
            last_token = None
        else:
            linear = _LIN.fullmatch(stripped)
            counted = _COUNT.fullmatch(stripped)
            if linear is None and counted is None:
                raise SfsdfSweepError(
                    f"SFSDF line {line_number} does not match single/LIN/LINC/DEC grammar: "
                    f"{source_line!r}"
                )
            ranged = linear or counted
            assert ranged is not None
            kind = "LIN" if linear is not None else ranged.group("kind")
            minimum = _decimal(
                ranged.group("minimum"), line_number=line_number, field="minimum"
            )
            maximum = _decimal(
                ranged.group("maximum"), line_number=line_number, field="maximum"
            )
            last_token = (
                ranged.group("step") if linear is not None else ranged.group("count")
            )
        row_grid, normalized = _row_grid(
            kind=kind,
            minimum=minimum,
            maximum=maximum,
            last_token=last_token,
            line_number=line_number,
        )
        if len(frequencies) + len(row_grid) > MAX_GRID_POINTS:
            raise SfsdfSweepError(
                "SFSDF cumulative frequency grid expands beyond "
                f"{MAX_GRID_POINTS} points at line {line_number}"
            )
        frequencies.extend(row_grid)
        parsed_rows.append(
            {
                "lineNumber": line_number,
                "sourceLine": source_line,
                "kind": kind,
                "generatedPointCount": len(row_grid),
                **normalized,
            }
        )
    if not parsed_rows:
        raise SfsdfSweepError("SFSDF contains no frequency rows")
    grid = _grid_contract(frequencies)
    source_hash = _sha256(source)
    evidence = {
        "schema": SFSDF_PARSE_SCHEMA,
        "status": "parsed",
        "sourcePath": str(source),
        "sourceSfsdfSha256": source_hash,
        "sourceSizeBytes": source.stat().st_size,
        "encoding": "utf-8-sig-ascii-token-subset",
        "blankLinePolicy": "ignored-and-counted",
        "blankLineCount": blank_lines,
        "rowCount": len(parsed_rows),
        "rows": parsed_rows,
        "grid": grid,
    }
    return ParsedSfsdf(
        path=source,
        source_sha256=source_hash,
        rows=tuple(parsed_rows),
        frequencies_hz=tuple(frequencies),
        evidence=evidence,
    )


def _success_evidence(result: Any, *, api: str) -> dict[str, Any]:
    if result is None:
        if api not in TLB_VOID_SUCCESS_APIS:
            raise SfsdfSweepError(
                f"{api} did not return positive success evidence: {result!r}"
            )
        return {
            "pythonType": "NoneType",
            "repr": "None",
            "successContract": "siwave-tlb-vt-void-no-exception",
        }
    if result is False or result == 0:
        raise SfsdfSweepError(
            f"{api} did not return positive success evidence: {result!r}"
        )
    return {
        "pythonType": type(result).__name__,
        "repr": repr(result),
        "successContract": "positive-return-value",
    }


def _invoke(
    project: Any,
    *,
    api: str,
    args: Sequence[Any],
    phase: str,
    sequence: int,
) -> dict[str, Any]:
    method = getattr(project, api, None)
    if not callable(method):
        raise SfsdfSweepError(f"SIWave project does not expose {api}")
    try:
        result = method(*args)
    except Exception as exc:
        raise SfsdfSweepError(f"{api} raised an exception: {exc}") from exc
    return {
        "sequence": sequence,
        "phase": phase,
        "api": api,
        "args": list(args),
        "returnEvidence": _success_evidence(result, api=api),
    }


def _row_operation(row: Mapping[str, Any]) -> tuple[str, list[Any]]:
    kind = row["kind"]
    if kind == "single":
        frequency = row["frequencyHz"]
        return "ScrAppendSweep", ["syz", frequency, frequency, 1, False]
    if kind == "LIN":
        return "ScrAppendSteppedSweep", [
            "syz",
            row["minimumHz"],
            row["maximumHz"],
            row["stepHz"],
        ]
    return "ScrAppendSweep", [
        "syz",
        row["minimumHz"],
        row["maximumHz"],
        row["count"],
        bool(row["isLog"]),
    ]


def apply_sfsdf_parser_commands(
    project: Any,
    *,
    parsed: ParsedSfsdf,
    syz_settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply parsed rows and administrator settings through documented COM APIs."""

    settings = normalize_syz_settings(
        syz_settings,
        where="selected SYZ Profile settings",
    )
    operations: list[dict[str, Any]] = []

    def call(api: str, args: Sequence[Any], phase: str) -> None:
        operations.append(
            _invoke(
                project,
                api=api,
                args=args,
                phase=phase,
                sequence=len(operations) + 1,
            )
        )

    call("ScrClearAllSweeps", ["syz"], "frequency-grid")
    for row in parsed.rows:
        api, args = _row_operation(row)
        call(api, args, "frequency-grid")

    manager_application = apply_administrator_syz_options(
        project,
        syz_settings=settings,
        starting_sequence=len(operations) + 1,
    )
    operations.extend(manager_application["operations"])

    identity_payload = {
        "sourceSfsdfSha256": parsed.source_sha256,
        "expectedSolutionGrid": parsed.evidence["grid"],
        "syzSettings": settings,
        "operations": operations,
    }
    evidence_identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema": SFSDF_APPLICATION_SCHEMA,
        "status": "applied",
        "applyMode": PARSER_COMMAND_APPLY_MODE,
        "capabilityIdentity": PARSER_COMMAND_CAPABILITY_IDENTITY,
        "evidenceIdentity": evidence_identity,
        "sourceSfsdfSha256": parsed.source_sha256,
        "parseEvidence": parsed.evidence,
        "expectedSolutionGrid": parsed.evidence["grid"],
        "administratorSyzOptions": settings,
        "operations": operations,
        "verificationBoundary": {
            "sourceGrammar": "siwave-2024.2-save-load-static-format",
            "commandReceiptsVerified": True,
            "nativeLoaderReadBackClaimed": False,
            "solveGridEquivalenceRequired": True,
        },
    }


def apply_administrator_syz_options(
    project: Any,
    *,
    syz_settings: Mapping[str, Any],
    starting_sequence: int = 1,
) -> dict[str, Any]:
    """Apply non-SFSDF SYZ options through their documented COM APIs."""

    if starting_sequence < 1:
        raise SfsdfSweepError("starting_sequence must be positive")
    settings = normalize_syz_settings(
        syz_settings,
        where="selected SYZ Profile settings",
    )
    calls = _administrator_option_calls(settings)
    operations = [
        _invoke(
            project,
            api=api,
            args=args,
            phase="administrator-syz-options",
            sequence=starting_sequence + offset,
        )
        for offset, (api, args) in enumerate(calls)
    ]
    identity_payload = {
        "administratorSyzOptions": settings,
        "operations": operations,
    }
    evidence_identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema": SYZ_OPTIONS_APPLICATION_SCHEMA,
        "status": "applied",
        "evidenceIdentity": evidence_identity,
        "administratorSyzOptions": settings,
        "operations": operations,
    }


def _administrator_option_calls(
    settings: Mapping[str, Any],
) -> list[tuple[str, list[Any]]]:
    calls: list[tuple[str, list[Any]]] = []
    interpolating = settings["sweepMode"] == "interpolating"
    calls.append(("ScrSetSyzInterpSweep", [interpolating]))
    if interpolating:
        interpolation = settings["interpolation"]
        calls.append(
            (
                "ScrSetSyzInterpSweepParams",
                [interpolation["convergence"], interpolation["maxInterpPts"]],
            )
        )
    calls.extend(
        [
            (
                "ScrSIwaveSyzComputeExactDcPoint",
                [settings["computeExactDcPoint"]],
            ),
            ("ScrSIwaveSyzEnforceCausality", [settings["enforceCausality"]]),
            ("ScrSIwaveSyzEnforcePassivity", [settings["enforcePassivity"]]),
        ]
    )
    return calls


def validate_administrator_syz_option_application(
    value: Any,
    *,
    syz_settings: Mapping[str, Any],
    starting_sequence: int = 1,
) -> dict[str, Any]:
    """Validate exact administrator-owned SYZ option COM receipts."""

    if not isinstance(value, Mapping):
        raise SfsdfSweepError("administrator SYZ option evidence must be an object")
    if (
        value.get("schema") != SYZ_OPTIONS_APPLICATION_SCHEMA
        or value.get("status") != "applied"
    ):
        raise SfsdfSweepError("administrator SYZ option evidence schema/status is invalid")
    settings = normalize_syz_settings(
        syz_settings,
        where="selected SYZ Profile settings",
    )
    if value.get("administratorSyzOptions") != settings:
        raise SfsdfSweepError("administrator SYZ option settings differ")
    operations = value.get("operations")
    calls = _administrator_option_calls(settings)
    if not isinstance(operations, list) or len(operations) != len(calls):
        raise SfsdfSweepError("administrator SYZ option operation count differs")
    for offset, (operation, expected) in enumerate(zip(operations, calls)):
        sequence = starting_sequence + offset
        api, args = expected
        if (
            not isinstance(operation, Mapping)
            or operation.get("sequence") != sequence
            or operation.get("phase") != "administrator-syz-options"
            or operation.get("api") != api
            or operation.get("args") != args
            or not isinstance(operation.get("returnEvidence"), Mapping)
            or not operation.get("returnEvidence")
        ):
            raise SfsdfSweepError(
                f"administrator SYZ option operation {sequence} differs"
            )
    identity_payload = {
        "administratorSyzOptions": settings,
        "operations": operations,
    }
    expected_identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    if value.get("evidenceIdentity") != expected_identity:
        raise SfsdfSweepError("administrator SYZ option evidence identity differs")
    return dict(value)


def validate_sfsdf_command_application(
    value: Any,
    *,
    parsed: ParsedSfsdf,
    syz_settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate parser-command evidence against source, settings, and exact calls."""

    if not isinstance(value, Mapping):
        raise SfsdfSweepError("SFSDF command application evidence must be an object")
    if value.get("schema") != SFSDF_APPLICATION_SCHEMA or value.get("status") != "applied":
        raise SfsdfSweepError("SFSDF command application schema/status is invalid")
    if (
        value.get("applyMode") != PARSER_COMMAND_APPLY_MODE
        or value.get("capabilityIdentity") != PARSER_COMMAND_CAPABILITY_IDENTITY
    ):
        raise SfsdfSweepError("SFSDF command application mode/capability is invalid")
    if value.get("sourceSfsdfSha256") != parsed.source_sha256:
        raise SfsdfSweepError("SFSDF command application source hash differs")
    evidence_identity = str(value.get("evidenceIdentity") or "")
    if re.fullmatch(r"[0-9a-f]{64}", evidence_identity) is None:
        raise SfsdfSweepError("SFSDF command application evidenceIdentity is invalid")
    if value.get("parseEvidence") != parsed.evidence:
        raise SfsdfSweepError("SFSDF command application parse evidence differs")
    if value.get("expectedSolutionGrid") != parsed.evidence["grid"]:
        raise SfsdfSweepError("SFSDF command application expected grid differs")
    settings = normalize_syz_settings(
        syz_settings,
        where="selected SYZ Profile settings",
    )
    if value.get("administratorSyzOptions") != settings:
        raise SfsdfSweepError("SFSDF command application administrator settings differ")
    boundary = value.get("verificationBoundary")
    if not isinstance(boundary, Mapping) or dict(boundary) != {
        "sourceGrammar": "siwave-2024.2-save-load-static-format",
        "commandReceiptsVerified": True,
        "nativeLoaderReadBackClaimed": False,
        "solveGridEquivalenceRequired": True,
    }:
        raise SfsdfSweepError("SFSDF command application verification boundary differs")
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        raise SfsdfSweepError("SFSDF command application operations are missing")
    expected_calls: list[tuple[str, list[Any], str]] = [
        ("ScrClearAllSweeps", ["syz"], "frequency-grid")
    ]
    expected_calls.extend(
        (*_row_operation(row), "frequency-grid") for row in parsed.rows
    )
    expected_calls.extend(
        (api, args, "administrator-syz-options")
        for api, args in _administrator_option_calls(settings)
    )
    if len(operations) != len(expected_calls):
        raise SfsdfSweepError("SFSDF command application operation count differs")
    for index, (operation, expected) in enumerate(
        zip(operations, expected_calls), start=1
    ):
        api, args, phase = expected
        if not isinstance(operation, Mapping):
            raise SfsdfSweepError(f"SFSDF operation {index} must be an object")
        if (
            operation.get("sequence") != index
            or operation.get("api") != api
            or operation.get("args") != args
            or operation.get("phase") != phase
            or not isinstance(operation.get("returnEvidence"), Mapping)
            or not operation.get("returnEvidence")
        ):
            raise SfsdfSweepError(f"SFSDF operation {index} evidence differs")
    identity_payload = {
        "sourceSfsdfSha256": parsed.source_sha256,
        "expectedSolutionGrid": parsed.evidence["grid"],
        "syzSettings": settings,
        "operations": operations,
    }
    expected_identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    if evidence_identity != expected_identity:
        raise SfsdfSweepError("SFSDF command application identity differs")
    return dict(value)
