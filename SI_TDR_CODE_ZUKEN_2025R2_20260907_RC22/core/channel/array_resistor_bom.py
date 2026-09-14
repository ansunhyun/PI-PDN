"""Authoritative Array-resistor facts resolved from the Job BOM/Part List.

The maintained customer contract intentionally has no Array part library and
no administrator-authored electrical values.  A component is an Array only
when its reference designator starts with ``AR`` (case-insensitive).  Its
resistance comes from an explicit OHM token in one file-wide source column.
The administrator Config lists candidate column names in priority order; the
first normalized header match is selected for every Array row. Pin pairs are
derived later from the actual EDB pin count: four and eight pins are the only
supported package shapes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..preprocess.bom import parse_bom_rows
from ..preprocess.contracts import ReferencePreprocessError


ARRAY_BOM_POLICY = "bom-configured-source-column-authoritative-reapply"
ARRAY_BOM_RESOLUTION_SCHEMA = "si-tdr-array-resistor-bom-resolution/3"
ARRAY_BOM_SNAPSHOT_SCHEMA = "si-tdr-array-resistor-bom-snapshot/1"
ARRAY_REF_DES_PREFIX = "AR"
EDB_RESISTANCE_VALUE_STRING_MATCH_POLICY = (
    "exact-except-terminal-Ohm-or-ohm-suffix-case"
)
ARRAY_REF_DES_SOURCE_COLUMNS = (
    "Designator",
    "Designators",
    "RefDes",
    "Reference Designator",
    "Ref. Def",
    "Reference",
)
ARRAY_PIN_MAPS: dict[int, list[list[str]]] = {
    4: [["1", "4"], ["2", "3"]],
    8: [["1", "8"], ["2", "7"], ["3", "6"], ["4", "5"]],
}

# Require an explicit unit.  Parenthesized/model-code values such as ``(5R1)``
# and ``(10K)`` therefore cannot become electrical facts by accident.
_OHM_TOKEN = re.compile(
    r"(?<![A-Z0-9.])(?:(?P<sign>[+\-−])\s*)?"
    r"(?P<number>(?:\d+(?:\.\d+)?|\.\d+))\s*"
    r"(?P<scale>[KMG]?)\s*(?:OHMS?|Ω)(?![A-Z])",
    re.IGNORECASE,
)


class ArrayResistorBomError(ValueError):
    """The BOM cannot prove the strict Array-resistor contract."""


def normalize_resistance_edb_value_string(value: Any) -> str:
    """Normalize only the terminal ``Ohm``/``ohm`` suffix spelling.

    Numeric text and engineering-prefix case remain byte-for-byte significant;
    for example ``10kOhm`` and ``10Kohm`` are intentionally different.
    """

    if not isinstance(value, str) or value != value.strip():
        raise ArrayResistorBomError(
            "EDB resistance Value.ToString must be a trimmed string"
        )
    if value.endswith("Ohm"):
        return value
    if value.endswith("ohm"):
        return f"{value[:-3]}Ohm"
    raise ArrayResistorBomError(
        "EDB resistance Value.ToString must end with Ohm or ohm"
    )


def resistance_edb_value_string_matches(expected: Any, observed: Any) -> bool:
    """Return whether an observed EDB string matches the strict suffix policy."""

    try:
        return normalize_resistance_edb_value_string(
            expected
        ) == normalize_resistance_edb_value_string(observed)
    except ArrayResistorBomError:
        return False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_resistance_edb_input(resistance_ohm: Any) -> str:
    """Format a positive resistance using PyEDB 0.50.1 engineering-unit case.

    Values below 1 kOhm remain in ``Ohm``. Larger values use the largest exact
    engineering prefix from ``kOhm``, ``MOhm``, and ``GOhm``. The formatter is
    based on the normalized numeric value rather than the BOM token spelling,
    so equivalent tokens always produce the same EDB input string.
    """

    if isinstance(resistance_ohm, bool):
        raise ArrayResistorBomError("Array resistance must be a positive number")
    try:
        value = Decimal(str(resistance_ohm))
    except (InvalidOperation, ValueError) as exc:
        raise ArrayResistorBomError(
            "Array resistance must be a positive finite number"
        ) from exc
    if not value.is_finite() or value <= 0:
        raise ArrayResistorBomError(
            "Array resistance must be a positive finite number"
        )
    for factor, unit in (
        (Decimal("1e9"), "GOhm"),
        (Decimal("1e6"), "MOhm"),
        (Decimal("1e3"), "kOhm"),
        (Decimal(1), "Ohm"),
    ):
        if value >= factor:
            scaled = value / factor
            number = format(scaled.normalize(), "f")
            if "." in number:
                number = number.rstrip("0").rstrip(".")
            return f"{number}{unit}"
    number = format(value.normalize(), "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number}Ohm"


def resistance_semantic_binding_sha256(
    *,
    source_tokens: list[dict[str, Any]],
    resistance_ohm: float,
    edb_resistance_input: str,
) -> str:
    """Bind raw BOM token evidence to normalized and EDB-facing values."""

    if edb_resistance_input != canonical_resistance_edb_input(resistance_ohm):
        raise ArrayResistorBomError(
            "Array EDB resistance input is not canonical for resistanceOhm"
        )
    if not source_tokens:
        raise ArrayResistorBomError(
            "resolved Array resistance requires BOM source token evidence"
        )
    expected_fields = {
        "rowIndex",
        "sourceValueSha256",
        "token",
        "tokenSha256",
        "start",
        "end",
    }
    for index, source in enumerate(source_tokens):
        token = str(source.get("token") or "")
        token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if (
            set(source) != expected_fields
            or isinstance(source.get("rowIndex"), bool)
            or not isinstance(source.get("rowIndex"), int)
            or source["rowIndex"] < 0
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(source.get("sourceValueSha256") or "")
            )
            or not token
            or source.get("tokenSha256") != token_sha256
            or isinstance(source.get("start"), bool)
            or isinstance(source.get("end"), bool)
            or not isinstance(source.get("start"), int)
            or not isinstance(source.get("end"), int)
            or source["start"] < 0
            or source["end"] <= source["start"]
        ):
            raise ArrayResistorBomError(
                f"Array resistance source token evidence is invalid at index {index}"
            )
    identity = {
        "sourceTokens": deepcopy(source_tokens),
        "resistanceOhm": resistance_ohm,
        "edbResistanceInput": edb_resistance_input,
    }
    return _sha256_json(identity)


def _normalize_column(value: Any) -> str:
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def _split_refdes(value: Any) -> list[str]:
    return [
        token.strip().strip("'\"")
        for token in str(value or "").replace(";", ",").split(",")
        if token.strip().strip("'\"")
    ]


def is_array_refdes(value: Any) -> bool:
    return str(value or "").strip().casefold().startswith(
        ARRAY_REF_DES_PREFIX.casefold()
    )


def automatic_array_pin_map(pin_names: Any, *, refdes: str) -> list[list[str]]:
    """Return the fixed map for an exact four/eight-pin EDB component."""

    normalized = [str(value).strip() for value in pin_names]
    if any(not value for value in normalized):
        raise ArrayResistorBomError(f"Array {refdes} contains an empty EDB pin name")
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise ArrayResistorBomError(f"Array {refdes} contains duplicate EDB pin names")
    pin_count = len(normalized)
    pin_map = ARRAY_PIN_MAPS.get(pin_count)
    if pin_map is None:
        raise ArrayResistorBomError(
            f"Array {refdes} must have exactly 4 or 8 EDB pins; found {pin_count}"
        )
    expected = {str(index) for index in range(1, pin_count + 1)}
    actual = set(normalized)
    if actual != expected:
        raise ArrayResistorBomError(
            f"Array {refdes} EDB pins must be exactly 1..{pin_count}; "
            f"found {sorted(actual, key=str.casefold)}"
        )
    return deepcopy(pin_map)


def parse_site_specification_resistance(value: Any) -> dict[str, Any]:
    """Parse one unambiguous finite positive resistance from explicit OHM tokens."""

    text = str(value or "").strip()
    candidates: list[dict[str, Any]] = []
    exact_values: set[Decimal] = set()
    for match in _OHM_TOKEN.finditer(text):
        number = Decimal(match.group("number"))
        multiplier = {
            "": Decimal(1),
            "K": Decimal("1e3"),
            "M": Decimal("1e6"),
            "G": Decimal("1e9"),
        }[match.group("scale").upper()]
        resistance_decimal = number * multiplier
        if match.group("sign") in {"-", "−"}:
            resistance_decimal = -resistance_decimal
        resistance = float(resistance_decimal)
        if not math.isfinite(resistance) or resistance <= 0.0:
            continue
        exact_values.add(resistance_decimal)
        token = match.group(0)
        candidates.append(
            {
                "token": token,
                "tokenSha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "start": match.start(),
                "end": match.end(),
                "resistanceOhm": resistance,
                "edbResistanceInput": canonical_resistance_edb_input(
                    resistance_decimal
                ),
            }
        )
    if not candidates:
        return {
            "status": "missing-resistance",
            "resistanceOhm": None,
            "candidates": [],
        }
    if len(exact_values) != 1:
        return {
            "status": "ambiguous-resistance",
            "resistanceOhm": None,
            "candidates": candidates,
        }
    return {
        "status": "resolved",
        "resistanceOhm": float(next(iter(exact_values))),
        "candidates": candidates,
    }


def _column_index(rows: list[dict[str, str]], *, source: Path) -> dict[str, str]:
    columns: dict[str, str] = {}
    for row in rows:
        for column in row:
            normalized = _normalize_column(column)
            existing = columns.get(normalized)
            if existing is not None and existing != column:
                raise ArrayResistorBomError(
                    f"{source}: BOM columns normalize ambiguously: "
                    f"{existing!r}, {column!r}"
                )
            columns[normalized] = column
    return columns


def _resolve_source_column(
    columns: Mapping[str, str], candidates: tuple[str, ...], *, where: str
) -> str:
    matches = sorted(
        {
            actual
            for candidate in candidates
            for normalized, actual in columns.items()
            if normalized == _normalize_column(candidate)
        },
        key=str.casefold,
    )
    if len(matches) != 1:
        raise ArrayResistorBomError(
            f"{where} must resolve exactly one BOM/PartList column: "
            f"candidates={list(candidates)}, matches={matches}"
        )
    return matches[0]


def _configured_array_resistance_source_columns(
    administrator_config: Mapping[str, Any],
) -> tuple[str, ...]:
    bom = administrator_config.get("BOM")
    if not isinstance(bom, Mapping):
        raise ArrayResistorBomError(
            "BOM.arrayResistanceSourceColumns must be a non-empty string array"
        )
    if bom.get("schemaVersion") != 2 or bom.get("colKey") != ["Designator"]:
        raise ArrayResistorBomError(
            "BOM must use schemaVersion=2 and colKey=['Designator'] before "
            "arrayResistanceSourceColumns can be resolved"
        )
    if set(bom) != {
        "schemaVersion", "colKey", "arrayResistanceSourceColumns"
    }:
        raise ArrayResistorBomError(
            "BOM must contain only schemaVersion, colKey, and "
            "arrayResistanceSourceColumns"
        )
    raw_columns = bom.get("arrayResistanceSourceColumns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ArrayResistorBomError(
            "BOM.arrayResistanceSourceColumns must be a non-empty string array"
        )
    columns: list[str] = []
    normalized_seen: dict[str, str] = {}
    for index, raw_column in enumerate(raw_columns):
        if not isinstance(raw_column, str) or not raw_column.strip():
            raise ArrayResistorBomError(
                "BOM.arrayResistanceSourceColumns"
                f"[{index}] must be a non-empty string"
            )
        column = raw_column.strip()
        if raw_column != column:
            raise ArrayResistorBomError(
                "BOM.arrayResistanceSourceColumns"
                f"[{index}] must not have leading or trailing whitespace"
            )
        normalized = _normalize_column(column)
        if not normalized:
            raise ArrayResistorBomError(
                "BOM.arrayResistanceSourceColumns"
                f"[{index}] must contain letters or numbers"
            )
        existing = normalized_seen.get(normalized)
        if existing is not None:
            raise ArrayResistorBomError(
                "BOM.arrayResistanceSourceColumns contains duplicate normalized "
                f"columns: {existing!r}, {column!r}"
            )
        normalized_seen[normalized] = column
        columns.append(column)
    return tuple(columns)


def _resolve_prioritized_source_column(
    columns: Mapping[str, str], candidates: tuple[str, ...], *, where: str
) -> str:
    for candidate in candidates:
        actual = columns.get(_normalize_column(candidate))
        if actual is not None:
            return actual
    raise ArrayResistorBomError(
        f"{where} did not match any BOM/PartList column: "
        f"configuredPriority={list(candidates)}, actualColumns={list(columns.values())}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bom_snapshot(path: Path, *, job_root: Path) -> dict[str, Any]:
    root = job_root.resolve(strict=True)
    source = path.resolve(strict=True)
    try:
        relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArrayResistorBomError(
            f"BOM/PartList must remain inside the Job root: {source}"
        ) from exc
    stat = source.stat()
    return {
        "schema": ARRAY_BOM_SNAPSHOT_SCHEMA,
        "path": str(source),
        "jobRelativePath": relative,
        "sizeBytes": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "mtimeUtc": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(timespec="microseconds"),
        "sha256": _sha256(source),
    }


def validate_array_bom_snapshot(value: Any, *, job_root: Path) -> Path:
    if not isinstance(value, Mapping) or value.get("schema") != ARRAY_BOM_SNAPSHOT_SCHEMA:
        raise ArrayResistorBomError("Array BOM/PartList snapshot schema differs")
    relative = str(value.get("jobRelativePath") or "").strip()
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ArrayResistorBomError("Array BOM/PartList snapshot path is invalid")
    root = job_root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if path != Path(str(value.get("path") or "")).resolve():
        raise ArrayResistorBomError("Array BOM/PartList snapshot path differs")
    current = bom_snapshot(path, job_root=root)
    if json.dumps(value, sort_keys=True, separators=(",", ":")) != json.dumps(
        current, sort_keys=True, separators=(",", ":")
    ):
        raise ArrayResistorBomError(
            "Array BOM/PartList snapshot size/mtime/hash drift"
        )
    return path


def resolve_array_resistor_bom(
    administrator_config: Mapping[str, Any],
    *,
    job_root: Path,
    bom_path: Path,
) -> dict[str, Any]:
    """Resolve all BOM ``AR*`` rows without failing unrelated off-path rows."""

    source_columns = _configured_array_resistance_source_columns(
        administrator_config
    )
    try:
        rows = parse_bom_rows(
            bom_path,
            configured_designator_columns=ARRAY_REF_DES_SOURCE_COLUMNS,
        )
    except ReferencePreprocessError as exc:
        raise ArrayResistorBomError(str(exc)) from exc
    columns = _column_index(rows, source=bom_path)
    refdes_column = _resolve_source_column(
        columns,
        ARRAY_REF_DES_SOURCE_COLUMNS,
        where="Array RefDes source",
    )
    resistance_source_column = _resolve_prioritized_source_column(
        columns,
        source_columns,
        where="Array resistance source",
    )

    rows_by_refdes: dict[str, list[dict[str, str]]] = {}
    original_refdes: dict[str, str] = {}
    for row in rows:
        for refdes in _split_refdes(row.get(refdes_column)):
            if not is_array_refdes(refdes):
                continue
            key = refdes.casefold()
            original_refdes.setdefault(key, refdes)
            rows_by_refdes.setdefault(key, []).append(row)

    catalog: list[dict[str, Any]] = []
    resolution: list[dict[str, Any]] = []
    for key in sorted(rows_by_refdes, key=lambda item: original_refdes[item].casefold()):
        refdes = original_refdes[key]
        source_rows = rows_by_refdes[key]
        row_evidence: list[dict[str, Any]] = []
        statuses: list[str] = []
        values: list[float] = []
        exact_edb_inputs: list[str] = []
        for row_index, row in enumerate(source_rows):
            source_value = str(row.get(resistance_source_column) or "").strip()
            parsed = parse_site_specification_resistance(source_value)
            statuses.append(str(parsed["status"]))
            if parsed["resistanceOhm"] is not None:
                values.append(float(parsed["resistanceOhm"]))
                exact_edb_inputs.append(
                    str(parsed["candidates"][0]["edbResistanceInput"])
                )
            row_evidence.append(
                {
                    "rowIndex": row_index,
                    "sourceValue": source_value,
                    "sourceValueSha256": hashlib.sha256(
                        source_value.encode("utf-8")
                    ).hexdigest(),
                    "parseStatus": parsed["status"],
                    "candidates": deepcopy(parsed["candidates"]),
                }
            )
        unique_edb_inputs = sorted(set(exact_edb_inputs))
        if any(status != "resolved" for status in statuses):
            status = (
                "ambiguous-resistance"
                if "ambiguous-resistance" in statuses
                or len(unique_edb_inputs) > 1
                else "missing-resistance"
            )
            resistance: float | None = None
        elif len(unique_edb_inputs) != 1:
            status = "ambiguous-resistance"
            resistance = None
        else:
            status = "resolved"
            resistance = values[0]
        source_tokens = [
            {
                "rowIndex": row["rowIndex"],
                "sourceValueSha256": row["sourceValueSha256"],
                "token": candidate["token"],
                "tokenSha256": candidate["tokenSha256"],
                "start": candidate["start"],
                "end": candidate["end"],
            }
            for row in row_evidence
            for candidate in row["candidates"]
        ]
        edb_resistance_input = (
            canonical_resistance_edb_input(resistance)
            if resistance is not None
            else None
        )
        semantic_sha256 = (
            resistance_semantic_binding_sha256(
                source_tokens=source_tokens,
                resistance_ohm=resistance,
                edb_resistance_input=edb_resistance_input,
            )
            if resistance is not None and edb_resistance_input is not None
            else None
        )
        bom_evidence = {
            "source": str(bom_path.resolve()),
            "refDes": refdes,
            "refDesColumn": refdes_column,
            "sourceColumn": resistance_source_column,
            "sourceRowCount": len(source_rows),
            "selectionBasis": "all-rows-for-exact-refdes-must-agree",
            "rows": row_evidence,
        }
        record: dict[str, Any] = {
            "group": f"BOM:{refdes}",
            "status": status,
            "matchedDesignators": [refdes],
            "bomEvidence": bom_evidence,
            "bomSelection": {
                "policy": ARRAY_BOM_POLICY,
                "refDes": refdes,
                "sourceColumn": resistance_source_column,
                "selectionReason": (
                    "first configured normalized header match, fixed for all AR rows; "
                    "RefDes starts with AR and all exact rows agree"
                ),
                "sourceTokens": source_tokens,
                "resistanceOhm": resistance,
                "edbResistanceInput": edb_resistance_input,
                "resistanceSemanticSha256": semantic_sha256,
            },
        }
        if resistance is not None:
            record["model"] = {
                "type": "resistor",
                "r_ohm": resistance,
                "r_edb_input": edb_resistance_input,
            }
        catalog.append(record)
        resolution.append(
            {
                "refDes": refdes,
                "resolutionStatus": status,
                "resistanceOhm": resistance,
                "edbResistanceInput": edb_resistance_input,
                "resistanceSemanticSha256": semantic_sha256,
                "bomEvidence": deepcopy(bom_evidence),
            }
        )

    return {
        "schema": ARRAY_BOM_RESOLUTION_SCHEMA,
        "policy": ARRAY_BOM_POLICY,
        "selector": {
            "refDesRule": "case-insensitive-prefix-AR",
            "refDesSourceColumns": list(ARRAY_REF_DES_SOURCE_COLUMNS),
            "arrayResistanceSourceColumns": list(source_columns),
            "sourceColumnSelectionPolicy": (
                "first-configured-normalized-header-match-file-wide"
            ),
            "columnNormalization": "casefold-alphanumeric-only",
            "resistanceTokenRule": "explicit-OHM-unit-only",
            "edbResistanceInputPolicy": (
                "canonical-engineering-unit-pyedb-0.50.1"
            ),
        },
        "resolvedColumns": {
            "refDes": refdes_column,
            "arrayResistanceSource": resistance_source_column,
        },
        "bom": bom_snapshot(bom_path, job_root=job_root),
        "arrayCatalog": catalog,
        "arrayRuleResolution": resolution,
    }
