"""Strict customer Channel Path component handling for FB-06.

This module is intentionally separate from ``series_models``.  The maintained
customer contract has no external value override or ``seriesTreatment`` policy:

* every traversed two-pin Series R/C/L is shorted;
* an ``AR*`` RefDes resolves to resistance from one configured BOM column;
* every traversed Array gets a 4/8-pin fixed-map RLC model reapplied;
* components outside resolved Channel Paths are never changed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .array_resistor_bom import (
    ARRAY_BOM_POLICY,
    ArrayResistorBomError,
    automatic_array_pin_map,
    canonical_resistance_edb_input,
    is_array_refdes,
    normalize_resistance_edb_value_string,
    resistance_edb_value_string_matches,
    resistance_semantic_binding_sha256,
    resolve_array_resistor_bom,
)
from .series_models import ARRAY_STEP_KIND, SERIES_STEP_TYPES, _assign_pin_pair_rlc


CUSTOMER_COMPONENT_CONTRACT_SCHEMA = "si-tdr-customer-component-handling/6"
CUSTOMER_SERIES_ACTION = "short"
CUSTOMER_ARRAY_ACTION = "configured-source-column-reapplied"
CUSTOMER_COMPONENT_READBACK_SCHEMA = "si-tdr-customer-component-readback/5"
CUSTOMER_ARRAY_MODEL_STATE_SCHEMA = "si-tdr-pyedb-array-model-state/2"
CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY = (
    "pyedb-component-model-pin-pair-numeric-and-value-string-readback"
)
CUSTOMER_COMPONENT_READBACK_CAPABILITY = (
    "pyedb-component-pin-pair-rlc-numeric-and-value-string-readback"
)
CUSTOMER_COMPONENT_READBACK_DETECTION = "runtime-wrapper-property-inspection"
CUSTOMER_OFF_PATH_POLICY = "not-touched-by-dispatcher"
CUSTOMER_OFF_PATH_EVIDENCE_SCOPE = "resolved-channel-path-target-set-only"
_PARENT_NUMERIC_SUFFIX = re.compile(r"^(?P<parent>.+)_(?P<index>[0-9]+)$")


class CustomerComponentReadbackError(RuntimeError):
    """The installed EDB wrapper cannot prove the applied component state."""


def resolve_customer_array_catalog(
    administrator_config: Mapping[str, Any],
    *,
    bom_path: Path,
    administrator_config_path: Path,
    job_root: Path,
) -> dict[str, Any]:
    """Resolve Job BOM ``AR*`` rows and configured-column resistance evidence."""

    del administrator_config_path  # provenance lives in generatedRunConfig.
    if "seriesTreatment" in administrator_config:
        raise ValueError("customer component handling does not accept seriesTreatment")
    try:
        return resolve_array_resistor_bom(
            administrator_config,
            job_root=job_root,
            bom_path=bom_path,
        )
    except ArrayResistorBomError as exc:
        raise ValueError(str(exc)) from exc


def load_customer_array_catalog(
    administrator_config: Mapping[str, Any],
    *,
    bom_path: Path,
    administrator_config_path: Path,
    job_root: Path,
) -> list[dict[str, Any]]:
    """Return configured BOM-column-derived Array catalog records."""

    return resolve_customer_array_catalog(
        administrator_config,
        bom_path=bom_path,
        administrator_config_path=administrator_config_path,
        job_root=job_root,
    )["arrayCatalog"]


def build_customer_component_contract(
    *,
    array_catalog: list[dict[str, Any]],
    array_rule_resolution: list[dict[str, Any]] | None = None,
    bom_snapshot: Mapping[str, Any],
    selector: Mapping[str, Any],
    resolved_columns: Mapping[str, Any],
    bom_path: Path,
) -> dict[str, Any]:
    return {
        "schema": CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
        "policySource": "fixed customer administrator contract",
        "series": CUSTOMER_SERIES_ACTION,
        "array": "bom-configured-source-column-authoritative",
        "arrayPolicy": ARRAY_BOM_POLICY,
        "forbiddenCustomerInputs": [
            "arrayPinMap",
            "resistanceOhm",
            "value",
            "seriesTreatment",
        ],
        "bom": str(bom_path.resolve()),
        "bomSnapshot": deepcopy(dict(bom_snapshot)),
        "selector": deepcopy(dict(selector)),
        "resolvedColumns": deepcopy(dict(resolved_columns)),
        "arrayCatalog": deepcopy(array_catalog),
        "arrayRuleResolution": deepcopy(array_rule_resolution or []),
    }


def _array_by_designator(
    array_catalog: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_designator: dict[str, dict[str, Any]] = {}
    for entry in array_catalog:
        allowed = {
            "group", "status", "matchedDesignators", "model", "bomEvidence",
            "bomSelection",
        }
        if set(entry) - allowed:
            raise ValueError(
                "customer Array catalog contains unsupported fields: "
                f"{sorted(set(entry) - allowed)}"
            )
        status = str(entry.get("status") or "")
        if status not in {"resolved", "missing-resistance", "ambiguous-resistance"}:
            raise ValueError(
                f"customer Array catalog has unsupported status: {status!r}"
            )
        if status != "resolved":
            for refdes in entry.get("matchedDesignators") or []:
                key = str(refdes).strip().casefold()
                if not key or key in by_designator:
                    raise ValueError(
                        f"invalid or duplicate unresolved Array RefDes: {refdes!r}"
                    )
                by_designator[key] = entry
            continue
        model = entry.get("model")
        resistance = model.get("r_ohm") if isinstance(model, Mapping) else None
        edb_input = model.get("r_edb_input") if isinstance(model, Mapping) else None
        selection = entry.get("bomSelection")
        if (
            not isinstance(model, Mapping)
            or model.get("type") != "resistor"
            or isinstance(resistance, bool)
            or not isinstance(resistance, (int, float))
            or not math.isfinite(float(resistance))
            or float(resistance) <= 0.0
            or not isinstance(edb_input, str)
            or edb_input != canonical_resistance_edb_input(resistance)
            or not isinstance(selection, Mapping)
            or selection.get("resistanceOhm") != resistance
            or selection.get("edbResistanceInput") != edb_input
            or not isinstance(selection.get("sourceTokens"), list)
            or selection.get("resistanceSemanticSha256")
            != resistance_semantic_binding_sha256(
                source_tokens=list(selection.get("sourceTokens") or []),
                resistance_ohm=float(resistance),
                edb_resistance_input=edb_input,
            )
        ):
            raise ValueError(
                f"Array group {entry.get('group')!r} requires a semantically bound "
                "finite positive resistor model and canonical EDB input"
            )
        for refdes in entry.get("matchedDesignators") or []:
            key = str(refdes).strip().casefold()
            if not key:
                raise ValueError(f"Array group {entry.get('group')!r} has an empty RefDes")
            existing = by_designator.get(key)
            if existing is not None:
                raise ValueError(
                    "component matches multiple Array groups: "
                    f"RefDes={refdes}, groups={[existing.get('group'), entry.get('group')]}"
                )
            by_designator[key] = entry
    return by_designator


def _resolve_array_designator(
    arrays: Mapping[str, dict[str, Any]],
    refdes: str,
) -> tuple[dict[str, Any] | None, str | None, str | None, int | None]:
    exact = arrays.get(refdes.casefold())
    if exact is not None:
        return exact, refdes, "exact", None
    match = _PARENT_NUMERIC_SUFFIX.fullmatch(refdes)
    if match is None:
        return None, None, None, None
    parent = match.group("parent")
    inherited = arrays.get(parent.casefold())
    if inherited is None:
        return None, None, None, None
    return inherited, parent, "parent_numeric_suffix", int(match.group("index"))


def plan_customer_path_components(
    report_payload: Mapping[str, Any],
    *,
    array_catalog: list[dict[str, Any]],
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Classify only components traversed by resolved Channel Paths."""

    arrays = _array_by_designator(array_catalog)
    evidence_by_refdes: dict[str, dict[str, Any]] = {}
    for path_index, path in enumerate(report_payload.get("paths") or []):
        status = str(path.get("status") or "")
        if not status.startswith("resolved"):
            continue
        channel = str(path.get("channel") or "")
        polarity = str(path.get("polarity") or "")
        for step_index, step in enumerate(path.get("steps") or []):
            kind = str(step.get("kind") or "")
            if kind not in SERIES_STEP_TYPES and kind != ARRAY_STEP_KIND:
                continue
            refdes = str(step.get("component") or "").strip()
            if not refdes:
                raise ValueError(
                    f"resolved Channel Path contains a component step without RefDes: "
                    f"channel={channel}, polarity={polarity}, step={step_index}"
                )
            item = evidence_by_refdes.setdefault(
                refdes,
                {
                    "component": refdes,
                    "pathEvidence": [],
                    "kinds": set(),
                },
            )
            item["kinds"].add(kind)
            evidence = {
                "channel": channel,
                "polarity": polarity,
                "pathIndex": path_index,
                "stepIndex": step_index,
                "kind": kind,
                "net": step.get("net"),
                "traversalAction": step.get("action"),
            }
            if evidence not in item["pathEvidence"]:
                item["pathEvidence"].append(evidence)

    components: list[dict[str, Any]] = []
    for refdes in sorted(evidence_by_refdes, key=str.casefold):
        item = evidence_by_refdes[refdes]
        (
            array_entry,
            array_catalog_designator,
            array_designator_match,
            _,
        ) = (
            _resolve_array_designator(arrays, refdes)
        )
        kinds = sorted(item["kinds"])
        refdes_is_array = is_array_refdes(refdes)
        if ARRAY_STEP_KIND in kinds and not refdes_is_array:
            raise ValueError(
                "Channel Path classified a non-AR RefDes as an Array: "
                f"RefDes={refdes}, batch={batch_id!r}"
            )
        if refdes_is_array and array_entry is None:
            raise ValueError(
                "Channel Path reached an AR component without a matching BOM/PartList "
                f"row: RefDes={refdes}, batch={batch_id!r}"
            )
        is_array = refdes_is_array
        if is_array and array_entry is not None and array_entry.get("status") != "resolved":
            raise ValueError(
                "Channel Path reached an AR component whose selected BOM source column "
                f"resistance is not resolved: RefDes={refdes}, "
                f"status={array_entry.get('status')!r}"
            )
        action = CUSTOMER_ARRAY_ACTION if is_array else CUSTOMER_SERIES_ACTION
        components.append(
            {
                "component": refdes,
                "pathIncluded": True,
                "pathEvidence": item["pathEvidence"],
                "stepKinds": kinds,
                "isArray": is_array,
                "arrayGroup": array_entry.get("group") if array_entry else None,
                "bomSelection": deepcopy(array_entry.get("bomSelection")) if array_entry else None,
                "arrayCatalogDesignator": array_catalog_designator,
                "arrayDesignatorMatch": array_designator_match,
                "pinMapPolicy": "fixed-by-edb-pin-count" if is_array else None,
                "edbPinCount": None,
                "arrayPinMap": None,
                "arrayModel": deepcopy(array_entry.get("model")) if array_entry else None,
                "action": action,
            }
        )

    channels: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        for evidence in component["pathEvidence"]:
            channel_key = str(evidence["channel"])
            channel_item = {
                "component": component["component"],
                "polarity": evidence["polarity"],
                "isArray": component["isArray"],
                "action": component["action"],
                "pathIndex": evidence["pathIndex"],
                "stepIndex": evidence["stepIndex"],
            }
            if channel_item not in channels.setdefault(channel_key, []):
                channels[channel_key].append(channel_item)

    return {
        "schema": CUSTOMER_COMPONENT_CONTRACT_SCHEMA,
        "batchId": batch_id,
        "componentCount": len(components),
        "components": components,
        "channels": channels,
        "offPathPolicy": CUSTOMER_OFF_PATH_POLICY,
        "offPathEvidenceScope": CUSTOMER_OFF_PATH_EVIDENCE_SCOPE,
    }


def _component_instances(edb: object) -> dict[str, object]:
    instances = getattr(getattr(edb, "components", None), "instances", None)
    if isinstance(instances, dict):
        return instances
    components = getattr(getattr(edb, "components", None), "components", None)
    if isinstance(components, dict):
        return components
    raise ValueError("EDB component collection is unavailable")


def _pin_net(pin: object) -> str | None:
    value = getattr(pin, "net_name", None)
    return str(value) if value not in (None, "") else None


def _snapshot_component(
    component: object, *, require_array_model_state: bool = False
) -> dict[str, Any]:
    values: dict[str, str] = {}
    for attribute, name in (
        ("res_value", "resistance"),
        ("cap_value", "capacitance"),
        ("ind_value", "inductance"),
    ):
        try:
            value = getattr(component, attribute)
        except Exception:
            continue
        if value not in (None, ""):
            values[name] = str(value)
    pins = getattr(component, "pins", {}) or {}
    snapshot = {
        "componentType": str(getattr(component, "type", "")),
        "componentDefinition": str(
            getattr(component, "component_def", None)
            or getattr(component, "partname", None)
            or ""
        ),
        "importedValues": values,
        "pinNets": {
            str(pin_name): _pin_net(pin)
            for pin_name, pin in sorted(pins.items(), key=lambda item: str(item[0]))
        },
    }
    if require_array_model_state:
        snapshot["arrayModelState"] = _array_model_state(component)
    return snapshot


def _wrapper_visible_array_state(component: object) -> dict[str, Any]:
    """Capture CMP/PyEDB-visible state without pretending missing capability."""

    try:
        state = _array_model_state(component)
    except CustomerComponentReadbackError as exc:
        return {
            "status": "unavailable-or-incomplete",
            "capabilityIdentity": CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
            "reason": str(exc),
            "observedModelType": str(
                getattr(component, "model_type", "") or ""
            ),
        }
    return {
        "status": "captured",
        "capabilityIdentity": CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
        "state": state,
        "stateSha256": _model_state_sha256(state),
    }


def _cmp_case(
    visible: Mapping[str, Any],
    *,
    pin_pairs: list[list[str]],
    resistance_ohm: float,
    edb_resistance_input: str,
    refdes: str,
) -> tuple[str, list[str], float | None]:
    state = visible.get("state") if visible.get("status") == "captured" else None
    if not isinstance(state, Mapping):
        return "B", [str(visible.get("reason") or "model unavailable")], None
    value: float | None = None
    component_values = state.get("componentValues")
    if isinstance(component_values, Mapping):
        resistance = component_values.get("resistance")
        if isinstance(resistance, Mapping) and resistance.get("value") is not None:
            try:
                value = float(resistance["value"])
            except (TypeError, ValueError):
                value = None
    try:
        _validate_array_model_state(
            state,
            pin_pairs=pin_pairs,
            resistance_ohm=resistance_ohm,
            edb_resistance_input=edb_resistance_input,
            refdes=refdes,
        )
    except CustomerComponentReadbackError as exc:
        return "C", [str(exc)], value
    return "A", [], value


def apply_customer_path_components(
    edb: object,
    report_payload: Mapping[str, Any],
    *,
    array_catalog: list[dict[str, Any]],
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Apply the fixed customer action and return per-batch/channel evidence."""

    plan = plan_customer_path_components(
        report_payload,
        array_catalog=array_catalog,
        batch_id=batch_id,
    )
    edb_components = _component_instances(edb)
    by_folded_refdes = {str(name).casefold(): component for name, component in edb_components.items()}
    prepared: list[dict[str, Any]] = []
    for planned_item in plan["components"]:
        item = deepcopy(planned_item)
        refdes = str(item["component"])
        component = by_folded_refdes.get(refdes.casefold())
        if component is None:
            raise ValueError(f"Channel Path component is missing from EDB: {refdes}")
        before = _snapshot_component(component)
        pins = getattr(component, "pins", {}) or {}

        if item["isArray"]:
            before["wrapperVisibleModelState"] = _wrapper_visible_array_state(component)
            try:
                resolved_pin_map = automatic_array_pin_map(pins, refdes=refdes)
            except ArrayResistorBomError as exc:
                raise ValueError(str(exc)) from exc
            item["edbPinCount"] = len(pins)
            item["arrayPinMap"] = resolved_pin_map
            short_pin_pair = None
        else:
            pin_names = [str(name) for name in pins]
            if len(pin_names) != 2:
                raise ValueError(
                    "customer Series short requires exactly two EDB pins: "
                    f"RefDes={refdes}, pins={sorted(pin_names)}"
                )
            short_pin_pair = tuple(sorted(pin_names, key=str.casefold))
        prepared.append(
            {
                "plan": item,
                "componentObject": component,
                "before": before,
                "shortPinPair": short_pin_pair,
            }
        )

    applied: list[dict[str, Any]] = []
    for prepared_item in prepared:
        item = prepared_item["plan"]
        component = prepared_item["componentObject"]
        refdes = str(item["component"])
        before = prepared_item["before"]
        if item["isArray"]:
            pin_pairs = [tuple(pair) for pair in item["arrayPinMap"] or []]
            model = item.get("arrayModel") or {}
            resistance = float(model["r_ohm"])
            edb_resistance_input = str(model["r_edb_input"])
            bom_selection = item.get("bomSelection") or {}
            bom_tokens = deepcopy(bom_selection.get("sourceTokens") or [])
            semantic_sha256 = str(
                bom_selection.get("resistanceSemanticSha256") or ""
            )
            visible_before = before["wrapperVisibleModelState"]
            case, differences, cmp_value = _cmp_case(
                visible_before,
                pin_pairs=[list(pair) for pair in pin_pairs],
                resistance_ohm=resistance,
                edb_resistance_input=edb_resistance_input,
                refdes=refdes,
            )
            _assign_pin_pair_rlc(
                component,
                pin_pairs,
                resistance,
                r_edb_input=edb_resistance_input,
            )
            after = _snapshot_component(component, require_array_model_state=True)
            verification = {
                "result": "configured-source-column-model-reapplied",
                "method": "bom-configured-source-column-fixed-pin-pair-rlc",
                "arrayPinMapPinsExist": True,
                "pinPairs": [list(pair) for pair in pin_pairs],
                "bomResistanceTokens": bom_tokens,
                "resistanceOhm": resistance,
                "edbResistanceInput": edb_resistance_input,
                "resistanceSemanticSha256": semantic_sha256,
                "modelSource": "bom-configured-source-column",
                "bomSelection": deepcopy(item.get("bomSelection")),
                "edbPinCount": item.get("edbPinCount"),
                "cmpCase": case,
                "cmpResistanceOhm": cmp_value,
                "differences": differences,
                "conflictPolicy": "record-and-bom-reapply",
                "action": CUSTOMER_ARRAY_ACTION,
                "operationReceipt": {
                    "operation": "assign-pin-pair-rlc",
                    "target": refdes,
                    "pinPairs": [list(pair) for pair in pin_pairs],
                    "bomResistanceTokens": bom_tokens,
                    "resistanceOhm": resistance,
                    "edbResistanceInput": edb_resistance_input,
                    "resistanceSemanticSha256": semantic_sha256,
                    "wrapperPostApplyState": "captured",
                    "result": "completed",
                },
                "edbReadBack": "pending-save-reopen",
            }
        else:
            pin_pair = prepared_item["shortPinPair"]
            assert pin_pair is not None
            _assign_pin_pair_rlc(component, [pin_pair], 0.0)
            after = _snapshot_component(component)
            verification = {
                "result": "short-applied",
                "method": "zero-ohm-pin-pair-rlc",
                "pinPairs": [list(pin_pair)],
                "rOhm": 0.0,
            }

        applied.append(
            {
                **item,
                "before": before,
                "after": after,
                "verification": verification,
            }
        )

    channel_records: dict[str, list[dict[str, Any]]] = {}
    applied_by_refdes = {item["component"]: item for item in applied}
    for channel, channel_items in plan["channels"].items():
        channel_records[channel] = [
            {
                **item,
                "verification": applied_by_refdes[item["component"]]["verification"],
            }
            for item in channel_items
        ]
    return {
        **plan,
        "components": applied,
        "channels": channel_records,
        "modifiedComponents": [
            item["component"] for item in applied if item["action"] == CUSTOMER_SERIES_ACTION
        ],
        "modeledArrayComponents": [
            item["component"] for item in applied if item["action"] == CUSTOMER_ARRAY_ACTION
        ],
    }


def _readback_pin_name(value: object) -> str:
    for attribute in ("name", "pin_name", "GetName"):
        member = getattr(value, attribute, None)
        if member is None:
            continue
        try:
            resolved = member() if callable(member) else member
        except Exception:
            continue
        if resolved not in (None, ""):
            return str(resolved)
    return str(value)


def _readback_pin_pairs(component: object) -> list[object]:
    for attribute in ("pin_pairs", "_pin_pairs"):
        try:
            pairs = getattr(component, attribute)
        except Exception:
            continue
        if isinstance(pairs, Mapping):
            values = list(pairs.values())
        elif isinstance(pairs, (list, tuple)):
            values = list(pairs)
        else:
            continue
        if values:
            return values
    raise CustomerComponentReadbackError(
        "EDB component wrapper exposes no readable component.pin_pairs or component._pin_pairs"
    )


def _readback_pair_pin(pair: object, *, first: bool) -> str:
    names = (
        ("first_pin", "FirstPin", "first", "GetFirstPin")
        if first
        else ("second_pin", "SecondPin", "second", "GetSecondPin")
    )
    for source in (pair, getattr(pair, "_edb_pin_pair", None)):
        if source is None:
            continue
        for name in names:
            member = getattr(source, name, None)
            if member is None:
                continue
            try:
                value = member() if callable(member) else member
            except Exception:
                continue
            resolved = _readback_pin_name(value).strip()
            if resolved:
                return resolved
    side = "first" if first else "second"
    raise CustomerComponentReadbackError(
        f"EDB pin-pair wrapper does not expose the {side} pin name"
    )


def _readback_enable(value: object) -> tuple[bool, bool, bool]:
    if isinstance(value, Mapping):
        keys = (
            ("r", "resistance", "r_enabled", "resistance_enabled"),
            ("l", "inductance", "l_enabled", "inductance_enabled"),
            ("c", "capacitance", "c_enabled", "capacitance_enabled"),
        )
        resolved = []
        for candidates in keys:
            matches = [value[key] for key in candidates if key in value]
            if not matches:
                raise CustomerComponentReadbackError(
                    "EDB pin-pair rlc_enable mapping is incomplete"
                )
            resolved.append(bool(matches[0]))
        return tuple(resolved)  # type: ignore[return-value]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return bool(value[0]), bool(value[1]), bool(value[2])
    resolved_object: list[bool] = []
    for names in (
        ("r_enabled", "resistance_enabled"),
        ("l_enabled", "inductance_enabled"),
        ("c_enabled", "capacitance_enabled"),
    ):
        found = None
        for name in names:
            member = getattr(value, name, None)
            if member is not None:
                found = member() if callable(member) else member
                break
        if found is None:
            raise CustomerComponentReadbackError(
                "EDB pin-pair rlc_enable object is incomplete"
            )
        resolved_object.append(bool(found))
    return tuple(resolved_object)  # type: ignore[return-value]


def _numeric_ohms(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    for member_name in ("to_double", "ToDouble", "value"):
        member = getattr(value, member_name, None)
        if member is None:
            continue
        try:
            resolved = member() if callable(member) else member
            return float(resolved)
        except (TypeError, ValueError):
            continue
    text = str(value).strip().casefold().replace("ohms", "").replace("ohm", "")
    try:
        return float(text)
    except ValueError as exc:
        raise CustomerComponentReadbackError(
            f"EDB wrapper value is not a numeric resistance: {value!r}"
        ) from exc


def _readback_resistance(pair: object) -> float:
    values = getattr(pair, "rlc_values", None)
    candidates = (
        getattr(values, "resistance", None) if values is not None else None,
        getattr(pair, "resistance", None),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return _numeric_ohms(candidate)
        except CustomerComponentReadbackError:
            continue
    raise CustomerComponentReadbackError(
        "EDB pin-pair wrapper does not expose a numeric resistance"
    )


def _numeric_model_value(value: object, label: str) -> float | None:
    if value is None:
        return None
    number = _numeric_ohms(value)
    if not math.isfinite(number):
        raise CustomerComponentReadbackError(
            f"EDB wrapper {label} is not finite"
        )
    return number


def _value_to_string(value: object, label: str) -> dict[str, str]:
    """Require a wrapper/.NET EDB Value ``ToString()`` read-back."""

    for source_name, source in (
        (label, value),
        (f"{label}._edb_value", getattr(value, "_edb_value", None)),
        (f"{label}.edb_value", getattr(value, "edb_value", None)),
    ):
        if source is None:
            continue
        member = getattr(source, "ToString", None)
        if not callable(member):
            continue
        try:
            result = member()
        except Exception:
            continue
        text = str(result).strip()
        if text:
            return {"value": text, "source": f"{source_name}.ToString()"}
    raise CustomerComponentReadbackError(
        f"{label} does not expose a readable wrapper/.NET Value ToString()"
    )


def _pair_resistance_value_string(
    pair: object, exposed_value: object, exposed_label: str
) -> dict[str, str]:
    """Read the engineering string from wrapper value or raw PinPair RLC Value."""

    try:
        return _value_to_string(exposed_value, exposed_label)
    except CustomerComponentReadbackError:
        pass
    try:
        raw_rlc = getattr(pair, "_pin_pair_rlc")
    except Exception:
        raw_rlc = None
    raw_resistance = getattr(raw_rlc, "R", None) if raw_rlc is not None else None
    if raw_resistance is not None:
        return _value_to_string(
            raw_resistance, "pinPair._pin_pair_rlc.R"
        )
    raw_model = getattr(pair, "_edb_model", None)
    raw_pin_pair = getattr(pair, "_edb_pin_pair", None)
    getter = getattr(raw_model, "GetPinPairRlc", None)
    if callable(getter) and raw_pin_pair is not None:
        try:
            raw_rlc = getter(raw_pin_pair)
        except Exception:
            raw_rlc = None
        raw_resistance = (
            getattr(raw_rlc, "R", None) if raw_rlc is not None else None
        )
        if raw_resistance is not None:
            return _value_to_string(
                raw_resistance,
                "pinPair._edb_model.GetPinPairRlc(_edb_pin_pair).R",
            )
    raise CustomerComponentReadbackError(
        "EDB pin-pair wrapper/raw model does not expose resistance "
        "Value.ToString()"
    )


def _component_value_state(component: object, name: str) -> dict[str, Any]:
    if not hasattr(component, name):
        return {"available": False, "value": None}
    try:
        value = getattr(component, name)
        value = value() if callable(value) else value
    except Exception as exc:
        raise CustomerComponentReadbackError(
            f"EDB component wrapper cannot read {name}"
        ) from exc
    return {
        "available": True,
        "value": _numeric_model_value(value, f"component.{name}"),
    }


def _pair_rlc_state(pair: object) -> tuple[dict[str, Any], dict[str, str]]:
    values = getattr(pair, "rlc_values", None)
    if values is None:
        raise CustomerComponentReadbackError(
            "EDB pin-pair wrapper does not expose rlc_values"
        )
    if isinstance(values, (list, tuple)) and len(values) == 3:
        numeric = {
            field: _numeric_model_value(value, f"pinPair.rlc_values[{index}]")
            for index, (field, value) in enumerate(
                zip(("resistance", "inductance", "capacitance"), values)
            )
        }
        return numeric, _pair_resistance_value_string(
            pair, values[0], "pinPair.rlc_values[0]"
        )
    result: dict[str, Any] = {}
    for field in ("resistance", "inductance", "capacitance"):
        if not hasattr(values, field):
            raise CustomerComponentReadbackError(
                f"EDB pin-pair rlc_values does not expose {field}"
            )
        try:
            value = getattr(values, field)
            value = value() if callable(value) else value
        except Exception as exc:
            raise CustomerComponentReadbackError(
                f"EDB pin-pair wrapper cannot read rlc_values.{field}"
            ) from exc
        result[field] = _numeric_model_value(
            value, f"pinPair.rlc_values.{field}"
        )
    return result, _pair_resistance_value_string(
        pair,
        getattr(values, "resistance"),
        "pinPair.rlc_values.resistance",
    )


def _array_model_state(component: object) -> dict[str, Any]:
    """Capture the complete stable electrical state exposed by PyEDB wrappers.

    This is deliberately not described as the complete imported CAD model. It
    proves only the enumerated component values, pin nets, and pin-pair RLC
    state. Missing wrapper properties make strict Array model verification fail.
    """

    model_type = str(getattr(component, "model_type", "") or "").strip()
    if not model_type:
        raise CustomerComponentReadbackError(
            "EDB Array component wrapper does not expose model_type"
        )
    pins = getattr(component, "pins", None)
    if not isinstance(pins, Mapping) or not pins:
        raise CustomerComponentReadbackError(
            "EDB Array component wrapper does not expose a non-empty pins mapping"
        )
    pair_states: list[dict[str, Any]] = []
    for pair in _readback_pin_pairs(component):
        first = _readback_pair_pin(pair, first=True)
        second = _readback_pair_pin(pair, first=False)
        if not first or not second or first == second:
            raise CustomerComponentReadbackError(
                "EDB Array pin-pair endpoints are missing or identical"
            )
        enabled = _readback_enable(getattr(pair, "rlc_enable", None))
        rlc_values, resistance_value_string = _pair_rlc_state(pair)
        pair_states.append(
            {
                "firstPin": first,
                "secondPin": second,
                "rlcEnable": {
                    "resistance": enabled[0],
                    "inductance": enabled[1],
                    "capacitance": enabled[2],
                },
                "rlcValues": rlc_values,
                "resistanceValueString": resistance_value_string,
            }
        )
    pair_states.sort(
        key=lambda item: (
            str(item["firstPin"]).casefold(),
            str(item["secondPin"]).casefold(),
        )
    )
    pair_keys = [
        (item["firstPin"].casefold(), item["secondPin"].casefold())
        for item in pair_states
    ]
    if len(set(pair_keys)) != len(pair_keys):
        raise CustomerComponentReadbackError(
            "EDB Array component has duplicate readable pin pairs"
        )
    return {
        "schema": CUSTOMER_ARRAY_MODEL_STATE_SCHEMA,
        "capabilityIdentity": CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
        "capabilityDetection": CUSTOMER_COMPONENT_READBACK_DETECTION,
        "claimScope": "pyedb-wrapper-exposed-component-values-pin-nets-pin-pair-rlc-and-value-tostring-state",
        "fullImportedModelReadBack": "not-claimed",
        "modelType": model_type,
        "componentType": str(getattr(component, "type", "") or ""),
        "componentDefinition": str(
            getattr(component, "component_def", None)
            or getattr(component, "partname", None)
            or ""
        ),
        "componentValues": {
            "resistance": _component_value_state(component, "res_value"),
            "capacitance": _component_value_state(component, "cap_value"),
            "inductance": _component_value_state(component, "ind_value"),
        },
        "pinNets": {
            str(name): _pin_net(pin)
            for name, pin in sorted(pins.items(), key=lambda item: str(item[0]))
        },
        "pinPairs": pair_states,
    }


def _model_state_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _array_snapshot_semantic_projection(
    value: Mapping[str, Any], *, refdes: str
) -> dict[str, Any]:
    """Remove only the wrapper access route from an Array snapshot comparison.

    The engineering value returned by ``Value.ToString()`` is part of the
    electrical contract.  The wrapper/raw property used to reach that value is
    diagnostic evidence and may legitimately differ after save/reopen.
    """

    result = deepcopy(dict(value))
    state = result.get("arrayModelState")
    if not isinstance(state, Mapping):
        raise CustomerComponentReadbackError(
            f"Array model state is missing from the snapshot: {refdes}"
        )
    pairs = state.get("pinPairs")
    if not isinstance(pairs, list) or not pairs:
        raise CustomerComponentReadbackError(
            f"Array pin-pair state is missing from the snapshot: {refdes}"
        )
    for pair in pairs:
        if not isinstance(pair, Mapping):
            raise CustomerComponentReadbackError(
                f"Array pin-pair state is invalid in the snapshot: {refdes}"
            )
        resistance_string = pair.get("resistanceValueString")
        if (
            not isinstance(resistance_string, Mapping)
            or set(resistance_string) != {"value", "source"}
            or not str(resistance_string.get("value") or "").strip()
            or not str(resistance_string.get("source") or "").endswith(
                ".ToString()"
            )
        ):
            raise CustomerComponentReadbackError(
                f"Array resistance Value.ToString evidence is invalid: {refdes}"
            )
        resistance_string = dict(resistance_string)
        try:
            resistance_string["value"] = normalize_resistance_edb_value_string(
                resistance_string["value"]
            )
        except ArrayResistorBomError as exc:
            raise CustomerComponentReadbackError(str(exc)) from exc
        resistance_string.pop("source")
        pair["resistanceValueString"] = resistance_string
    return result


def _validate_array_model_state(
    state: Mapping[str, Any],
    *,
    pin_pairs: list[list[str]],
    resistance_ohm: float,
    edb_resistance_input: str,
    refdes: str,
) -> None:
    """Require the configured R-only model on every exact Array pin pair."""

    if str(state.get("modelType") or "").casefold() != "rlc":
        raise CustomerComponentReadbackError(
            f"Array model_type is not RLC after save/reopen: {refdes}"
        )
    if str(state.get("componentType") or "").casefold() != "resistor":
        raise CustomerComponentReadbackError(
            f"Array component type is not Resistor after save/reopen: {refdes}"
        )
    expected = {
        tuple(sorted((str(left), str(right)), key=str.casefold))
        for left, right in pin_pairs
    }
    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    for pair in state.get("pinPairs") or []:
        if not isinstance(pair, Mapping):
            raise CustomerComponentReadbackError(
                f"Array pin-pair state is not an object: {refdes}"
            )
        key = tuple(
            sorted(
                (str(pair.get("firstPin") or ""), str(pair.get("secondPin") or "")),
                key=str.casefold,
            )
        )
        if not all(key) or key in actual:
            raise CustomerComponentReadbackError(
                f"Array pin-pair state is missing or duplicated: {refdes}/{key}"
            )
        actual[key] = pair
    if set(actual) != expected:
        raise CustomerComponentReadbackError(
            f"Array configured pin pairs differ after save/reopen: {refdes}"
        )
    for key, pair in actual.items():
        enabled = pair.get("rlcEnable")
        values = pair.get("rlcValues")
        resistance_string = pair.get("resistanceValueString")
        if not isinstance(enabled, Mapping) or not isinstance(values, Mapping):
            raise CustomerComponentReadbackError(
                f"Array RLC state is incomplete after save/reopen: {refdes}/{key}"
            )
        if (
            enabled.get("resistance") is not True
            or enabled.get("inductance") is not False
            or enabled.get("capacitance") is not False
            or values.get("resistance") != resistance_ohm
            or values.get("inductance") != 0.0
            or values.get("capacitance") != 0.0
            or not isinstance(resistance_string, Mapping)
            or not resistance_edb_value_string_matches(
                edb_resistance_input, resistance_string.get("value")
            )
            or not str(resistance_string.get("source") or "").endswith(
                ".ToString()"
            )
        ):
            raise CustomerComponentReadbackError(
                f"Array RLC read-back differs from BOM-derived model: {refdes}/{key}"
            )


def read_back_customer_component_application(
    edb: object,
    applied_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove saved component state through detected PyEDB wrapper properties."""

    try:
        instances = _component_instances(edb)
    except ValueError as exc:
        raise CustomerComponentReadbackError(str(exc)) from exc
    by_name = {str(name).casefold(): value for name, value in instances.items()}
    evidence: list[dict[str, Any]] = []
    for item in applied_result.get("components") or []:
        if not isinstance(item, Mapping):
            raise CustomerComponentReadbackError("applied component record is invalid")
        refdes = str(item.get("component") or "")
        component = by_name.get(refdes.casefold())
        if component is None:
            raise CustomerComponentReadbackError(
                f"component is missing during saved-EDB read-back: {refdes}"
            )
        if item.get("isArray"):
            current = _snapshot_component(
                component, require_array_model_state=True
            )
            after = item.get("after")
            if not isinstance(after, Mapping) or (
                _array_snapshot_semantic_projection(current, refdes=refdes)
                != _array_snapshot_semantic_projection(after, refdes=refdes)
            ):
                raise CustomerComponentReadbackError(
                    f"Array component identity/pin state changed after save/reopen: {refdes}"
                )
            missing = [
                pin
                for pair in item.get("arrayPinMap") or []
                for pin in pair
                if pin not in (current.get("pinNets") or {})
            ]
            if missing:
                raise CustomerComponentReadbackError(
                    f"Array pins are missing after save/reopen: {refdes}/{missing}"
                )
            model = item.get("arrayModel")
            resistance = model.get("r_ohm") if isinstance(model, Mapping) else None
            edb_resistance_input = (
                model.get("r_edb_input") if isinstance(model, Mapping) else None
            )
            if (
                isinstance(resistance, bool)
                or not isinstance(resistance, (int, float))
                or not math.isfinite(float(resistance))
                or float(resistance) <= 0.0
                or not isinstance(edb_resistance_input, str)
                or edb_resistance_input
                != canonical_resistance_edb_input(resistance)
            ):
                raise CustomerComponentReadbackError(
                    f"Array BOM resistance evidence is invalid: {refdes}"
                )
            state = current["arrayModelState"]
            _validate_array_model_state(
                state,
                pin_pairs=deepcopy(item.get("arrayPinMap") or []),
                resistance_ohm=float(resistance),
                edb_resistance_input=edb_resistance_input,
                refdes=refdes,
            )
            selection = item.get("bomSelection") or {}
            evidence.append(
                {
                    "component": refdes,
                    "action": CUSTOMER_ARRAY_ACTION,
                    "status": "verified",
                    "arrayPinMap": deepcopy(item.get("arrayPinMap")),
                    "modelSource": "bom-configured-source-column",
                    "bomSelection": deepcopy(item.get("bomSelection")),
                    "bomResistanceTokens": deepcopy(
                        selection.get("sourceTokens") or []
                    ),
                    "edbPinCount": item.get("edbPinCount"),
                    "modelInstalled": True,
                    "resistanceOhm": float(resistance),
                    "edbResistanceInput": edb_resistance_input,
                    "resistanceSemanticSha256": selection.get(
                        "resistanceSemanticSha256"
                    ),
                    "afterEqualsReadBack": True,
                    "modelStateScope": CUSTOMER_ARRAY_MODEL_STATE_CAPABILITY,
                    "modelStateSha256": _model_state_sha256(state),
                    "readBackState": deepcopy(state),
                    "fullImportedModelReadBack": "not-claimed",
                }
            )
            continue

        model_type = str(getattr(component, "model_type", "") or "")
        if model_type.casefold() != "rlc":
            raise CustomerComponentReadbackError(
                f"Series model_type is not RLC after save/reopen: {refdes}/{model_type!r}"
            )
        expected_pairs = item.get("verification", {}).get("pinPairs") or []
        if len(expected_pairs) != 1:
            raise CustomerComponentReadbackError(
                f"Series expected pin-pair evidence differs: {refdes}"
            )
        expected_pair = sorted((str(value) for value in expected_pairs[0]), key=str.casefold)
        matching: list[dict[str, Any]] = []
        for pair in _readback_pin_pairs(component):
            actual_pair = sorted(
                (
                    _readback_pair_pin(pair, first=True),
                    _readback_pair_pin(pair, first=False),
                ),
                key=str.casefold,
            )
            if actual_pair != expected_pair:
                continue
            enable = _readback_enable(getattr(pair, "rlc_enable", None))
            resistance = _readback_resistance(pair)
            matching.append(
                {
                    "pinPair": actual_pair,
                    "resistanceOhm": resistance,
                    "rEnabled": enable[0],
                    "lEnabled": enable[1],
                    "cEnabled": enable[2],
                }
            )
        if len(matching) != 1:
            raise CustomerComponentReadbackError(
                f"Series exact pin pair was not uniquely readable after save/reopen: {refdes}"
            )
        state = matching[0]
        component_resistance = _numeric_ohms(getattr(component, "res_value", None))
        if (
            state["resistanceOhm"] != 0.0
            or component_resistance != 0.0
            or state["rEnabled"] is not True
            or state["lEnabled"] is not False
            or state["cEnabled"] is not False
        ):
            raise CustomerComponentReadbackError(
                f"Series RLC read-back is not exact zero-ohm R-only: {refdes}/{state}"
            )
        evidence.append(
            {
                "component": refdes,
                "action": CUSTOMER_SERIES_ACTION,
                "status": "verified",
                "modelType": "RLC",
                "componentResistanceOhm": component_resistance,
                **state,
            }
        )

    return {
        "schema": CUSTOMER_COMPONENT_READBACK_SCHEMA,
        "status": "verified",
        "capabilityIdentity": CUSTOMER_COMPONENT_READBACK_CAPABILITY,
        "capabilityDetection": CUSTOMER_COMPONENT_READBACK_DETECTION,
        "requiredWrapperProperties": [
            "component.model_type",
            "component.pin_pairs|component._pin_pairs",
            "component.res_value",
            "pinPair.first_pin|pinPair._edb_pin_pair.FirstPin",
            "pinPair.second_pin|pinPair._edb_pin_pair.SecondPin",
            "pinPair.rlc_values.resistance|pinPair.rlc_values[0]",
            "pinPair.rlc_enable",
            "array.component.model_type",
            "array.component.pin_pairs|array.component._pin_pairs",
            "array.pinPair.rlc_values named-fields|three-value-sequence",
            "array.pinPair.rlc_values.resistance|array.pinPair._pin_pair_rlc.R Value.ToString()",
        ],
        "saveReopenVerified": True,
        "componentCount": len(evidence),
        "components": evidence,
    }
