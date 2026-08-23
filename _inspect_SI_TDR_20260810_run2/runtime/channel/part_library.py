"""Normalize customer BOM group rules and legacy SI-TDR Part Libraries.

The maintained customer-facing contract is an SI-TDR-owned Config containing
``BOM.compProp``. Each group filters real BOM columns using literal contains or
an explicit regular expression, then contributes optional ``siTdr`` Array
facts. ``seriesTreatment.byGroup`` remains a separate Channel Path policy.

The older standalone ``array_parts`` and root ``partLibrary`` shapes remain
read-only adapters for delivered samples and regression. DCIR's own
``DCIR.BOM.compProp`` is not reinterpreted and the DCIR Config is not a runtime
dependency of this loader.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

try:
    from ..preprocess.bom import (
        DEFAULT_DESCRIPTION_COLUMNS,
        DEFAULT_DESIGNATOR_COLUMNS,
        DEFAULT_PART_NAME_COLUMNS,
        DEFAULT_PART_NUMBER_COLUMNS,
        DEFAULT_SYMBOL_COLUMNS,
        ReferencePreprocessError,
        parse_bom_rows,
    )
except ImportError:  # Support direct execution from the SI_TDR folder.
    from preprocess.bom import (  # type: ignore[no-redef]
        DEFAULT_DESCRIPTION_COLUMNS,
        DEFAULT_DESIGNATOR_COLUMNS,
        DEFAULT_PART_NAME_COLUMNS,
        DEFAULT_PART_NUMBER_COLUMNS,
        DEFAULT_SYMBOL_COLUMNS,
        ReferencePreprocessError,
        parse_bom_rows,
    )


class PartLibrarySchemaError(ValueError):
    """Raised when an explicitly supplied part-library file is incompatible."""


BOM_RULE_SCHEMA_VERSION = 1
MAX_REGEX_PATTERN_LENGTH = 2048
VALID_BOM_TREATMENTS = {"actual", "short", "unresolved"}


def _normalized_key(value: object | None) -> str:
    return str(value or "").strip().casefold()


def _normalized_column(value: object | None) -> str:
    return "".join(character for character in _normalized_key(value) if character.isalnum())


def _read_identity(entry: dict[str, Any], *, where: str) -> tuple[str | None, str | None, list[str]]:
    part_no = entry.get("part_no") if entry.get("part_no") is not None else entry.get("partNo")
    part_name = entry.get("part_name") if entry.get("part_name") is not None else entry.get("partName")
    if part_name is None:
        part_name = entry.get("component_def")  # Legacy array-mapping alias.
    aliases = entry.get("aliases") or []
    if not isinstance(aliases, list):
        raise PartLibrarySchemaError(f"{where}.aliases must be a list")

    normalized_part_no = str(part_no).strip() if part_no is not None else None
    normalized_part_name = str(part_name).strip() if part_name is not None else None
    normalized_aliases = sorted(
        {str(alias).strip() for alias in aliases if str(alias).strip()},
        key=str.casefold,
    )
    if not normalized_part_no and not normalized_part_name:
        raise PartLibrarySchemaError(f"{where} requires part_no or part_name")
    return normalized_part_no or None, normalized_part_name or None, normalized_aliases


def _read_pin_pairs(raw_pairs: object, *, where: str) -> list[list[str]]:
    if raw_pairs is None:
        return []
    if not isinstance(raw_pairs, list):
        raise PartLibrarySchemaError(f"{where} must be a list")

    pairs: list[list[str]] = []
    seen_pins: set[str] = set()
    for index, item in enumerate(raw_pairs):
        item_where = f"{where}[{index}]"
        if isinstance(item, dict):
            left = item.get("from") or item.get("a") or item.get("pin1")
            right = item.get("to") or item.get("b") or item.get("pin2")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            left, right = item
        else:
            raise PartLibrarySchemaError(f"{item_where} must contain exactly two pins")
        if left is None or right is None:
            raise PartLibrarySchemaError(f"{item_where} requires two non-null pins")
        left_text = str(left).strip()
        right_text = str(right).strip()
        if not left_text or not right_text or _normalized_key(left_text) == _normalized_key(right_text):
            raise PartLibrarySchemaError(f"{item_where} requires two distinct, non-empty pins")
        for pin in (left_text, right_text):
            normalized_pin = _normalized_key(pin)
            if normalized_pin in seen_pins:
                raise PartLibrarySchemaError(f"{item_where} reuses pin {pin!r}")
            seen_pins.add(normalized_pin)
        pairs.append([left_text, right_text])
    return pairs


def _read_model(raw_model: object, *, where: str) -> dict[str, Any]:
    if raw_model is None:
        return {}
    if not isinstance(raw_model, dict):
        raise PartLibrarySchemaError(f"{where} must be an object")

    model_type = str(raw_model.get("type") or "").strip().casefold()
    if not model_type:
        raise PartLibrarySchemaError(f"{where}.type is required when model is present")

    normalized: dict[str, Any] = {"type": model_type}
    legacy_r = raw_model.get("r_ohm")
    generic_value = raw_model.get("value")
    generic_unit = raw_model.get("unit")

    def resistor_number(value: object, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise PartLibrarySchemaError(f"{where}.{field} must be numeric") from None
        if not math.isfinite(number) or number < 0:
            raise PartLibrarySchemaError(f"{where}.{field} must be a finite, non-negative number")
        return number

    legacy_number = resistor_number(legacy_r, "r_ohm") if legacy_r is not None else None
    generic_number = (
        resistor_number(generic_value, "value")
        if generic_value is not None and model_type == "resistor"
        else None
    )

    if legacy_number is not None and generic_number is not None:
        if legacy_number != generic_number:
            raise PartLibrarySchemaError(f"{where} has conflicting r_ohm and value")

    if generic_value is not None:
        if generic_unit is None:
            raise PartLibrarySchemaError(f"{where}.unit is required with value")
        unit = str(generic_unit).strip().casefold()
        if model_type == "resistor" and unit not in {"ohm", "ohms", "ω"}:
            raise PartLibrarySchemaError(f"{where}.unit must be ohm for a resistor")
        if model_type != "resistor":
            # Phase 1 only has a validated resistor-array installer.  Preserve
            # the type so downstream reports it as unsupported, but do not
            # pretend a generic unit/value has been normalized electrically.
            normalized.update({"value": generic_value, "unit": str(generic_unit).strip()})

    resistor_value = legacy_number if legacy_number is not None else generic_number
    if model_type == "resistor" and resistor_value is not None:
        normalized["r_ohm"] = resistor_value
    return normalized


def _normalize_entry(
    entry: object,
    *,
    where: str,
    si_tdr_nested: bool,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        raise PartLibrarySchemaError(f"{where} must be an object")

    part_no, part_name, aliases = _read_identity(entry, where=where)
    facts: dict[str, Any]
    if si_tdr_nested:
        snake = entry.get("si_tdr")
        camel = entry.get("siTdr")
        if snake is not None and camel is not None:
            raise PartLibrarySchemaError(f"{where} cannot define both si_tdr and siTdr")
        raw_facts = snake if snake is not None else camel
        if raw_facts is None:
            return None  # A shared file may contain parts used only by DCIR/another system.
        if not isinstance(raw_facts, dict):
            raise PartLibrarySchemaError(f"{where}.siTdr must be an object")
        facts = raw_facts
    else:
        facts = entry

    raw_pairs = facts.get("pin_pairs") if facts.get("pin_pairs") is not None else facts.get("pinPairs")
    normalized: dict[str, Any] = {
        "part_no": part_no,
        "part_name": part_name,
        "aliases": aliases,
        "pin_pairs": _read_pin_pairs(raw_pairs, where=f"{where}.pin_pairs"),
    }
    model = _read_model(facts.get("model"), where=f"{where}.model")
    if model:
        normalized["model"] = model
    return normalized


def _entry_keys(entry: dict[str, Any]) -> set[str]:
    return {
        key
        for key in (
            _normalized_key(entry.get("part_no")),
            _normalized_key(entry.get("part_name")),
            *(_normalized_key(alias) for alias in entry.get("aliases") or []),
        )
        if key
    }


def _merge_entries(entries: list[dict[str, Any]], *, source: str) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for entry in entries:
        keys = _entry_keys(entry)
        matches = {id(by_key[key]): by_key[key] for key in keys if key in by_key}
        if matches:
            if len(matches) > 1 or any(existing != entry for existing in matches.values()):
                conflict_keys = sorted(key for key in keys if key in by_key)
                raise PartLibrarySchemaError(
                    f"{source}: conflicting part-library entries for keys {conflict_keys}"
                )
            continue  # Identical legacy/shared definitions are migration-safe.
        merged.append(entry)
        for key in keys:
            by_key[key] = entry
    return merged


def _resolve_bom_path(
    payload: dict[str, Any],
    bom_config: dict[str, Any],
    *,
    config_path: Path,
) -> Path:
    candidates: list[tuple[str, object]] = []
    if bom_config.get("path") is not None:
        candidates.append(("BOM.path", bom_config.get("path")))
    preprocessing = payload.get("preprocessing") or {}
    if not isinstance(preprocessing, dict):
        raise PartLibrarySchemaError(f"{config_path}: preprocessing must be an object")
    inputs = preprocessing.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise PartLibrarySchemaError(f"{config_path}: preprocessing.inputs must be an object")
    if inputs.get("bom") is not None:
        candidates.append(("preprocessing.inputs.bom", inputs.get("bom")))
    if not candidates:
        raise PartLibrarySchemaError(
            f"{config_path}: BOM rules require BOM.path or preprocessing.inputs.bom"
        )

    resolved: list[tuple[str, Path]] = []
    for label, raw_path in candidates:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PartLibrarySchemaError(f"{config_path}: {label} must be a non-empty path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = (config_path.parent / path).resolve()
        else:
            path = path.resolve()
        resolved.append((label, path))
    unique_paths = {path for _, path in resolved}
    if len(unique_paths) != 1:
        details = ", ".join(f"{label}={path}" for label, path in resolved)
        raise PartLibrarySchemaError(f"{config_path}: conflicting BOM paths: {details}")
    return resolved[0][1]


def _column_index(rows: list[dict[str, str]], *, source: str) -> dict[str, str]:
    if not rows:
        raise PartLibrarySchemaError(f"{source}: BOM contains no rows")
    result: dict[str, str] = {}
    for column in rows[0]:
        normalized = _normalized_column(column)
        if not normalized:
            continue
        if normalized in result:
            raise PartLibrarySchemaError(
                f"{source}: duplicate normalized BOM columns {result[normalized]!r} and {column!r}"
            )
        result[normalized] = column
    return result


def _find_column(
    columns: dict[str, str],
    candidates: tuple[str, ...] | list[str],
) -> str | None:
    for candidate in candidates:
        column = columns.get(_normalized_column(candidate))
        if column is not None:
            return column
    return None


def _literal_values(raw: object, *, where: str) -> list[str]:
    values = raw if isinstance(raw, list) else [raw]
    if not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise PartLibrarySchemaError(
            f"{where} must be a non-empty string or list of non-empty strings"
        )
    return [str(value).strip() for value in values]


def _matches_filter(cell_value: str, rule: object, *, where: str) -> bool:
    if isinstance(rule, (str, list)):
        values = _literal_values(rule, where=where)
        folded = cell_value.casefold()
        return any(value.casefold() in folded for value in values)
    if not isinstance(rule, dict) or set(rule) != {"regex"}:
        raise PartLibrarySchemaError(
            f"{where} must be a string, string list, or {{\"regex\": \"...\"}}"
        )
    pattern = rule.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise PartLibrarySchemaError(f"{where}.regex must be a non-empty string")
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        raise PartLibrarySchemaError(
            f"{where}.regex exceeds {MAX_REGEX_PATTERN_LENGTH} characters"
        )
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise PartLibrarySchemaError(f"{where}.regex is invalid: {exc}") from exc
    return compiled.search(cell_value) is not None


def _row_values(rows: list[dict[str, str]], column: str | None) -> set[str]:
    if column is None:
        return set()
    return {str(row.get(column) or "").strip() for row in rows if str(row.get(column) or "").strip()}


def _split_designator_cell(raw: object) -> list[str]:
    return [
        token.strip().strip("'").strip('"')
        for token in str(raw).replace(";", ",").split(",")
        if token.strip().strip("'").strip('"')
    ]


def _normalize_bom_comp_prop_payload(
    payload: dict[str, Any],
    *,
    source: str,
    config_path: Path,
) -> list[dict[str, Any]]:
    bom_config = payload.get("BOM")
    if not isinstance(bom_config, dict):
        raise PartLibrarySchemaError(f"{source}.BOM must be an object")
    schema_version = bom_config.get("schemaVersion")
    if schema_version != BOM_RULE_SCHEMA_VERSION:
        raise PartLibrarySchemaError(
            f"{source}: unsupported BOM.schemaVersion {schema_version!r}; "
            f"supported: {BOM_RULE_SCHEMA_VERSION}"
        )
    comp_prop = bom_config.get("compProp")
    if not isinstance(comp_prop, dict) or not comp_prop:
        raise PartLibrarySchemaError(f"{source}.BOM.compProp must be a non-empty object")

    bom_path = _resolve_bom_path(payload, bom_config, config_path=config_path)
    try:
        rows = parse_bom_rows(bom_path)
    except ReferencePreprocessError as exc:
        raise PartLibrarySchemaError(f"{source}: {exc}") from exc
    columns = _column_index(rows, source=str(bom_path))

    col_key = bom_config.get("colKey") or []
    if not isinstance(col_key, list) or not all(
        isinstance(column, str) and column.strip() for column in col_key
    ):
        raise PartLibrarySchemaError(f"{source}.BOM.colKey must be a string list")
    for required in col_key:
        if _normalized_column(required) not in columns:
            raise PartLibrarySchemaError(
                f"{source}.BOM.colKey column {required!r} is missing from {bom_path}"
            )

    designator_column = _find_column(columns, list(DEFAULT_DESIGNATOR_COLUMNS))
    if designator_column is None:
        raise PartLibrarySchemaError(f"{source}: BOM Designator column is missing")
    part_number_column = _find_column(columns, list(DEFAULT_PART_NUMBER_COLUMNS))
    part_name_column = _find_column(columns, list(DEFAULT_PART_NAME_COLUMNS))
    symbol_column = _find_column(columns, list(DEFAULT_SYMBOL_COLUMNS))
    description_column = _find_column(columns, list(DEFAULT_DESCRIPTION_COLUMNS))

    identity_columns = [
        column
        for column in (
            part_number_column,
            part_name_column,
            symbol_column,
            description_column,
        )
        if column is not None
    ]
    identity_by_designator: dict[str, dict[str, str]] = {}
    for row in rows:
        fingerprint = {column: str(row.get(column) or "").strip() for column in identity_columns}
        for designator in _split_designator_cell(row.get(designator_column, "")):
            existing = identity_by_designator.get(_normalized_key(designator))
            if existing is not None and existing != fingerprint:
                raise PartLibrarySchemaError(
                    f"{source}: BOM Designator {designator!r} has conflicting identity rows"
                )
            identity_by_designator[_normalized_key(designator)] = fingerprint

    raw_treatment = payload.get("seriesTreatment")
    treatment = {} if raw_treatment is None else raw_treatment
    if not isinstance(treatment, dict):
        raise PartLibrarySchemaError(f"{source}.seriesTreatment must be an object")
    unknown_treatment_fields = sorted(set(treatment) - {"default", "byGroup"})
    if unknown_treatment_fields:
        raise PartLibrarySchemaError(
            f"{source}.seriesTreatment has unsupported BOM-rule fields: "
            f"{unknown_treatment_fields}"
        )
    if "default" in treatment:
        default_treatment = treatment.get("default")
        if (
            not isinstance(default_treatment, str)
            or default_treatment not in VALID_BOM_TREATMENTS
        ):
            raise PartLibrarySchemaError(
                f"{source}.seriesTreatment.default must be one of "
                f"{sorted(VALID_BOM_TREATMENTS)}"
            )
    by_group = treatment.get("byGroup") or {}
    if not isinstance(by_group, dict):
        raise PartLibrarySchemaError(f"{source}.seriesTreatment.byGroup must be an object")
    for group, group_treatment in by_group.items():
        if not isinstance(group, str) or not group.strip():
            raise PartLibrarySchemaError(
                f"{source}.seriesTreatment.byGroup names must be non-empty strings"
            )
        if (
            not isinstance(group_treatment, str)
            or group_treatment not in VALID_BOM_TREATMENTS
        ):
            raise PartLibrarySchemaError(
                f"{source}.seriesTreatment.byGroup.{group} must be one of "
                f"{sorted(VALID_BOM_TREATMENTS)}"
            )
    unknown_groups = sorted(set(str(group) for group in by_group) - set(comp_prop))
    if unknown_groups:
        raise PartLibrarySchemaError(
            f"{source}.seriesTreatment.byGroup references unknown groups: {unknown_groups}"
        )

    entries: list[dict[str, Any]] = []
    for group_name, raw_group in comp_prop.items():
        where = f"{source}.BOM.compProp.{group_name}"
        if not isinstance(group_name, str) or not group_name.strip():
            raise PartLibrarySchemaError(f"{source}.BOM.compProp group names must be non-empty")
        if not isinstance(raw_group, dict):
            raise PartLibrarySchemaError(f"{where} must be an object")
        snake = raw_group.get("si_tdr")
        camel = raw_group.get("siTdr")
        if snake is not None and camel is not None:
            raise PartLibrarySchemaError(f"{where} cannot define both siTdr and si_tdr")
        raw_facts = snake if snake is not None else camel
        filters = {
            key: value
            for key, value in raw_group.items()
            if key not in {"siTdr", "si_tdr"}
        }
        if not filters:
            raise PartLibrarySchemaError(f"{where} requires at least one BOM column filter")

        resolved_filters: list[tuple[str, object]] = []
        for requested_column, rule in filters.items():
            column = columns.get(_normalized_column(requested_column))
            if column is None:
                raise PartLibrarySchemaError(
                    f"{where} filter column {requested_column!r} is missing from {bom_path}"
                )
            # Validate every filter even when an earlier column rejects a row.
            _matches_filter("", rule, where=f"{where}.{requested_column}")
            resolved_filters.append((column, rule))

        matched_rows = [
            row
            for row in rows
            if all(
                _matches_filter(
                    str(row.get(column) or ""),
                    rule,
                    where=f"{where}.{column}",
                )
                for column, rule in resolved_filters
            )
        ]
        if not matched_rows:
            raise PartLibrarySchemaError(f"{where} matched no BOM rows in {bom_path}")

        designators = sorted(
            {
                designator
                for row in matched_rows
                for designator in _split_designator_cell(row.get(designator_column, ""))
            },
            key=str.casefold,
        )
        if not designators:
            raise PartLibrarySchemaError(f"{where} matched rows without Designators")

        facts: dict[str, Any] = {}
        if raw_facts is not None:
            if not isinstance(raw_facts, dict):
                raise PartLibrarySchemaError(f"{where}.siTdr must be an object")
            unknown_facts = sorted(set(raw_facts) - {"arrayPinMap", "resistanceOhm"})
            if unknown_facts:
                raise PartLibrarySchemaError(
                    f"{where}.siTdr has unsupported fields: {unknown_facts}"
                )
            pin_pairs = _read_pin_pairs(
                raw_facts.get("arrayPinMap"),
                where=f"{where}.siTdr.arrayPinMap",
            )
            if pin_pairs:
                facts["pin_pairs"] = pin_pairs
            if raw_facts.get("resistanceOhm") is not None:
                facts["model"] = _read_model(
                    {
                        "type": "resistor",
                        "value": raw_facts.get("resistanceOhm"),
                        "unit": "ohm",
                    },
                    where=f"{where}.siTdr.resistanceOhm",
                )

        identity_values = set(designators)
        identity_values.update(_row_values(matched_rows, part_number_column))
        identity_values.update(_row_values(matched_rows, part_name_column))
        identity_values.update(_row_values(matched_rows, symbol_column))
        part_numbers = sorted(_row_values(matched_rows, part_number_column), key=str.casefold)
        entry = {
            "part_no": part_numbers[0] if len(part_numbers) == 1 else None,
            "part_name": group_name,
            "aliases": sorted(identity_values, key=str.casefold),
            "groups": [group_name],
            "pin_pairs": facts.get("pin_pairs", []),
            "bom_match": {
                "source": str(bom_path),
                "filters": [column for column, _ in resolved_filters],
                "matchedDesignators": designators,
                "partNumbers": part_numbers,
                "partNames": sorted(_row_values(matched_rows, part_name_column), key=str.casefold),
                "symbols": sorted(_row_values(matched_rows, symbol_column), key=str.casefold),
                "descriptions": sorted(_row_values(matched_rows, description_column), key=str.casefold),
            },
        }
        if facts.get("model"):
            entry["model"] = facts["model"]
        if facts.get("model") and not facts.get("pin_pairs"):
            raise PartLibrarySchemaError(
                f"{where}.siTdr.resistanceOhm currently requires arrayPinMap"
            )
        effective_treatment = by_group.get(group_name, treatment.get("default"))
        if (
            facts.get("pin_pairs")
            and effective_treatment == "actual"
            and not facts.get("model")
        ):
            raise PartLibrarySchemaError(
                f"{where} treatment actual requires siTdr.resistanceOhm for an Array"
            )
        entries.append(entry)
    return entries


def normalize_part_library_payload(
    payload: object,
    *,
    source: str = "<memory>",
    source_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return canonical SI-TDR facts from BOM rules or a legacy library.

    Canonical entries use ``part_no``, ``part_name``, ``aliases``, ``groups``,
    ``pin_pairs`` and the existing resistor model shape ``r_ohm``. Supplying
    multiple legacy source sections is accepted only when overlapping entries
    normalize to identical facts. BOM rules cannot be mixed with a legacy
    source in the same file; conflicts and ambiguous source ownership fail closed.
    """
    if not isinstance(payload, dict):
        raise PartLibrarySchemaError(f"{source}: root must be an object")

    raw_entries: list[dict[str, Any]] = []
    found_section = False

    has_bom_rules = payload.get("BOM") is not None
    has_legacy_source = any(
        payload.get(name) is not None
        for name in ("array_parts", "arrayParts", "partLibrary", "part_library")
    )
    if has_bom_rules and has_legacy_source:
        raise PartLibrarySchemaError(
            f"{source}: BOM.compProp rules cannot be combined with a legacy Part Library section"
        )

    if has_bom_rules:
        found_section = True
        if source_path is None:
            raise PartLibrarySchemaError(
                f"{source}: BOM.compProp rules require a source path to resolve the BOM file"
            )
        raw_entries.extend(
            _normalize_bom_comp_prop_payload(
                payload,
                source=source,
                config_path=source_path.resolve(),
            )
        )

    legacy_snake = payload.get("array_parts")
    legacy_camel = payload.get("arrayParts")
    if legacy_snake is not None and legacy_camel is not None:
        raise PartLibrarySchemaError(f"{source}: cannot define both array_parts and arrayParts")
    legacy = legacy_snake if legacy_snake is not None else legacy_camel
    if legacy is not None:
        found_section = True
        if not isinstance(legacy, list):
            raise PartLibrarySchemaError(f"{source}: array_parts must be a list")
        for index, entry in enumerate(legacy):
            normalized = _normalize_entry(
                entry,
                where=f"{source}.array_parts[{index}]",
                si_tdr_nested=False,
            )
            assert normalized is not None
            raw_entries.append(normalized)

    shared_camel = payload.get("partLibrary")
    shared_snake = payload.get("part_library")
    if shared_camel is not None and shared_snake is not None:
        raise PartLibrarySchemaError(f"{source}: cannot define both partLibrary and part_library")
    shared = shared_camel if shared_camel is not None else shared_snake
    if shared is not None:
        found_section = True
        if not isinstance(shared, dict):
            raise PartLibrarySchemaError(f"{source}: partLibrary must be an object")
        schema_version = shared.get("schemaVersion", shared.get("schema_version"))
        if schema_version != 1:
            raise PartLibrarySchemaError(
                f"{source}: unsupported partLibrary.schemaVersion {schema_version!r}; supported: 1"
            )
        parts = shared.get("parts")
        if not isinstance(parts, list):
            raise PartLibrarySchemaError(f"{source}: partLibrary.parts must be a list")
        for index, entry in enumerate(parts):
            normalized = _normalize_entry(
                entry,
                where=f"{source}.partLibrary.parts[{index}]",
                si_tdr_nested=True,
            )
            if normalized is not None:
                raw_entries.append(normalized)

    if not found_section:
        raise PartLibrarySchemaError(
            f"{source}: no supported part-library section; expected array_parts or partLibrary"
        )
    return _merge_entries(raw_entries, source=source)


def load_part_library_entries(path: Path) -> list[dict[str, Any]]:
    """Load and normalize an explicitly selected part-library/config file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PartLibrarySchemaError(f"part-library file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise PartLibrarySchemaError(f"invalid JSON in part-library file {path}: {exc}") from exc
    return normalize_part_library_payload(
        payload,
        source=str(path),
        source_path=path,
    )
