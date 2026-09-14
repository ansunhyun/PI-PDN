"""Customer-readable Array resistor application result.

The authoritative engineering evidence remains ``customer_component_manifest.json``
inside the Job work tree.  This module derives a small, deterministic public result
from that already verified record without exposing internal absolute paths.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Mapping


ARRAY_MODEL_RESULT_SCHEMA = "si-tdr-customer-array-model-result/1"
ARRAY_MODEL_RESULT_SUFFIX = "_Array_Model_Result.json"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_CMP_STATES = {
    "A": "same-as-bom",
    "B": "missing-or-unreadable",
    "C": "different-from-bom",
}


class ArrayModelResultError(ValueError):
    """Raised when a customer Array result cannot be derived safely."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArrayModelResultError(f"{label} must be an object")
    return value


def _non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArrayModelResultError(f"{label} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArrayModelResultError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ArrayModelResultError(f"{label} must be a finite positive number")
    return result


def _component_map(value: Any, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ArrayModelResultError(f"{label} must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    folded: dict[str, str] = {}
    for index, raw in enumerate(value):
        item = _mapping(raw, f"{label}[{index}]")
        name = _non_empty_text(item.get("component"), f"{label}[{index}].component")
        prior = folded.get(name.casefold())
        if prior is not None:
            raise ArrayModelResultError(
                f"{label} has a duplicate component: {prior!r}, {name!r}"
            )
        folded[name.casefold()] = name
        result[name] = item
    return result


def _pin_pair_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ArrayModelResultError(f"{label} must contain PinPairs")
    result: list[str] = []
    pins_seen: set[str] = set()
    pairs_seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or any(not isinstance(pin, str) or not pin.strip() for pin in raw)
        ):
            raise ArrayModelResultError(f"{label}[{index}] must contain two pins")
        first, second = (raw[0].strip(), raw[1].strip())
        pair = (first, second)
        if pair in pairs_seen or first in pins_seen or second in pins_seen:
            raise ArrayModelResultError(f"{label} contains duplicate PinPair coverage")
        pairs_seen.add(pair)
        pins_seen.update(pair)
        result.append(f"{first}-{second}")
    return result


def _matched_tokens(selection: Mapping[str, Any], label: str) -> list[str]:
    raw_tokens = selection.get("sourceTokens")
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise ArrayModelResultError(f"{label}.sourceTokens is empty")
    tokens: list[str] = []
    for index, raw in enumerate(raw_tokens):
        token = _mapping(raw, f"{label}.sourceTokens[{index}]")
        text = _non_empty_text(
            token.get("token"), f"{label}.sourceTokens[{index}].token"
        )
        if text not in tokens:
            tokens.append(text)
    return tokens


def _read_back_summary(
    evidence: Mapping[str, Any],
    *,
    expected_pin_pairs: list[str],
    expected_resistance: float,
    label: str,
) -> dict[str, Any]:
    if evidence.get("status") != "verified" or evidence.get("afterEqualsReadBack") is not True:
        raise ArrayModelResultError(f"{label} is not save/reopen verified")
    state = _mapping(evidence.get("readBackState"), f"{label}.readBackState")
    if state.get("modelType") != "RLC":
        raise ArrayModelResultError(f"{label} modelType is not RLC")
    raw_pairs = state.get("pinPairs")
    if not isinstance(raw_pairs, list) or len(raw_pairs) != len(expected_pin_pairs):
        raise ArrayModelResultError(f"{label} read-back PinPair count differs")
    observed_pairs: list[str] = []
    observed_value_strings: list[str] = []
    for index, raw in enumerate(raw_pairs):
        pair = _mapping(raw, f"{label}.readBackState.pinPairs[{index}]")
        first = _non_empty_text(pair.get("firstPin"), f"{label}.firstPin")
        second = _non_empty_text(pair.get("secondPin"), f"{label}.secondPin")
        observed_pairs.append(f"{first}-{second}")
        enabled = _mapping(pair.get("rlcEnable"), f"{label}.rlcEnable")
        values = _mapping(pair.get("rlcValues"), f"{label}.rlcValues")
        if (
            enabled.get("resistance") is not True
            or enabled.get("inductance") is not False
            or enabled.get("capacitance") is not False
            or _finite_number(values.get("resistance"), f"{label}.resistance")
            != expected_resistance
            or _finite_number(values.get("inductance"), f"{label}.inductance") != 0.0
            or _finite_number(values.get("capacitance"), f"{label}.capacitance") != 0.0
        ):
            raise ArrayModelResultError(f"{label} read-back is not the expected R-only model")
        value_string = _mapping(
            pair.get("resistanceValueString"), f"{label}.resistanceValueString"
        )
        text = _non_empty_text(value_string.get("value"), f"{label}.valueString")
        if text not in observed_value_strings:
            observed_value_strings.append(text)
    if observed_pairs != expected_pin_pairs:
        raise ArrayModelResultError(f"{label} read-back PinPair order differs")
    return {
        "status": "verified",
        "modelType": "RLC",
        "resistanceOhm": expected_resistance,
        "observedValueStrings": observed_value_strings,
        "resistanceEnabled": True,
        "inductanceEnabled": False,
        "capacitanceEnabled": False,
    }


def build_customer_array_model_result(
    component_manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Derive the public customer summary, or ``None`` when no Array was used."""

    if (
        component_manifest.get("status") != "ok"
        or component_manifest.get("mode") != "customer-strict"
    ):
        raise ArrayModelResultError("component manifest is not a successful strict record")
    batch_id = _non_empty_text(component_manifest.get("batchId"), "batchId")
    modeled = component_manifest.get("modeledArrayComponents")
    if not isinstance(modeled, list) or any(
        not isinstance(value, str) or not value.strip() for value in modeled
    ):
        raise ArrayModelResultError("modeledArrayComponents must be an array of RefDes")
    folded_modeled = [value.strip().casefold() for value in modeled]
    if len(folded_modeled) != len(set(folded_modeled)):
        raise ArrayModelResultError("modeledArrayComponents contains duplicates")
    if not modeled:
        return None

    source = _mapping(component_manifest.get("bomSnapshot"), "bomSnapshot")
    source_path = _non_empty_text(
        source.get("jobRelativePath") or source.get("path"), "bomSnapshot path"
    )
    source_hash = _non_empty_text(source.get("sha256"), "bomSnapshot.sha256")
    if _SHA256_RE.fullmatch(source_hash) is None:
        raise ArrayModelResultError("bomSnapshot.sha256 is invalid")

    applied_by_name = _component_map(component_manifest.get("components"), "components")
    read_back = _mapping(component_manifest.get("readBack"), "readBack")
    if read_back.get("status") != "verified" or read_back.get("saveReopenVerified") is not True:
        raise ArrayModelResultError("component read-back is not verified after save/reopen")
    read_back_by_name = _component_map(
        read_back.get("components"), "readBack.components"
    )

    modeled_names = sorted((value.strip() for value in modeled), key=str.casefold)
    result_components: list[dict[str, Any]] = []
    for refdes in modeled_names:
        applied = applied_by_name.get(refdes)
        evidence = read_back_by_name.get(refdes)
        if applied is None or evidence is None:
            raise ArrayModelResultError(f"Array {refdes} lacks apply/read-back coverage")
        verification = _mapping(applied.get("verification"), f"Array {refdes}.verification")
        selection = _mapping(evidence.get("bomSelection"), f"Array {refdes}.bomSelection")
        if (
            verification.get("action") != "configured-source-column-reapplied"
            or evidence.get("action") != "configured-source-column-reapplied"
            or evidence.get("component") != refdes
            or selection.get("refDes") != refdes
        ):
            raise ArrayModelResultError(f"Array {refdes} application identity differs")
        cmp_case = verification.get("cmpCase")
        if cmp_case not in _CMP_STATES:
            raise ArrayModelResultError(f"Array {refdes} CMP Case is invalid")
        cmp_resistance = verification.get("cmpResistanceOhm")
        if cmp_resistance is not None:
            cmp_resistance = _finite_number(
                cmp_resistance, f"Array {refdes}.cmpResistanceOhm", positive=True
            )
        resistance = _finite_number(
            evidence.get("resistanceOhm"),
            f"Array {refdes}.resistanceOhm",
            positive=True,
        )
        if _finite_number(
            selection.get("resistanceOhm"),
            f"Array {refdes}.bomSelection.resistanceOhm",
            positive=True,
        ) != resistance:
            raise ArrayModelResultError(f"Array {refdes} BOM/read-back resistance differs")
        edb_input = _non_empty_text(
            evidence.get("edbResistanceInput"), f"Array {refdes}.edbResistanceInput"
        )
        if selection.get("edbResistanceInput") != edb_input:
            raise ArrayModelResultError(f"Array {refdes} EDB input differs from BOM selection")
        pin_count = evidence.get("edbPinCount")
        if isinstance(pin_count, bool) or pin_count not in {4, 8}:
            raise ArrayModelResultError(f"Array {refdes} pin count must be 4 or 8")
        pin_pairs = _pin_pair_strings(
            evidence.get("arrayPinMap"), f"Array {refdes}.arrayPinMap"
        )
        read_back_summary = _read_back_summary(
            evidence,
            expected_pin_pairs=pin_pairs,
            expected_resistance=resistance,
            label=f"Array {refdes}",
        )
        result_components.append(
            {
                "refDes": refdes,
                "cmpObservation": {
                    "case": cmp_case,
                    "state": _CMP_STATES[cmp_case],
                    "resistanceOhm": cmp_resistance,
                },
                "bom": {
                    "sourceColumn": _non_empty_text(
                        selection.get("sourceColumn"),
                        f"Array {refdes}.sourceColumn",
                    ),
                    "matchedTokens": _matched_tokens(
                        selection, f"Array {refdes}.bomSelection"
                    ),
                    "resistanceOhm": resistance,
                },
                "appliedModel": {
                    "type": "R-only PinPair",
                    "edbInput": edb_input,
                    "pinCount": pin_count,
                    "pinPairs": pin_pairs,
                },
                "saveReopenVerification": read_back_summary,
            }
        )

    return {
        "schema": ARRAY_MODEL_RESULT_SCHEMA,
        "status": "verified",
        "batchId": batch_id,
        "sourceFile": {
            "name": Path(source_path).name,
            "sha256": source_hash.casefold(),
        },
        "componentCount": len(result_components),
        "components": result_components,
    }


def validate_customer_array_model_result(
    payload: Mapping[str, Any],
    *,
    component_manifest: Mapping[str, Any],
    expected_batch_id: str,
) -> dict[str, Any]:
    """Require the public payload to equal the deterministic manifest projection."""

    expected = build_customer_array_model_result(component_manifest)
    if expected is None:
        raise ArrayModelResultError(
            f"batch {expected_batch_id} has no modeled Array customer result"
        )
    if expected.get("batchId") != expected_batch_id:
        raise ArrayModelResultError("Array result Batch identity differs")
    if dict(payload) != expected:
        raise ArrayModelResultError("public Array result differs from the verified manifest")
    return expected
