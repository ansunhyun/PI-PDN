"""Series passive electrical model installation for the SI-TDR pipeline.

Two-layer structure (docs/si-tdr-series-model-two-layer-design-2026-07-07.md):
- BOM group / legacy library facts: matched components -> pin-pair topology + model
- seriesTreatment policy: actual/short/unresolved by BOM group, with legacy
  type and part/refdes adapters

Applied only to components recorded as traversed by the channel path report
(series_* / array_component steps), never board-wide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .part_library import load_part_library_entries


SERIES_STEP_TYPES = {
    "series_resistor": "resistor",
    "series_capacitor": "capacitor",
    "series_inductor": "inductor",
}
ARRAY_STEP_KIND = "array_component"

# resistor/inductor actual is customer-practice-backed (HDMI 5.1 ohm, HDMI_USB 2.2 ohm
# inherited); capacitor short is still a hypothesis pending the customer treatment table,
# so the built-in default blocks it explicitly instead of guessing.
TREATMENT_DEFAULTS = {"resistor": "actual", "inductor": "actual", "capacitor": "unresolved"}
VALID_TREATMENTS = {"actual", "short", "unresolved"}
INHERITED_TYPES = {"resistor", "capacitor", "inductor"}


def _normalize_key(value: str | None) -> str:
    return (value or "").strip().casefold()


def load_part_library(path: Path) -> dict[str, dict[str, Any]]:
    """Load legacy or proposed shared part-library facts by normalized identifiers."""
    by_key: dict[str, dict[str, Any]] = {}
    for entry in load_part_library_entries(path):
        keys = [entry.get("part_no"), entry.get("part_name"), *(entry.get("aliases") or [])]
        for key in keys:
            normalized = _normalize_key(str(key) if key is not None else None)
            if normalized:
                by_key[normalized] = entry
    return by_key


@dataclass
class PathSeriesComponent:
    refdes: str
    kinds: set[str] = field(default_factory=set)
    part_name: str | None = None
    channels: list[dict[str, str]] = field(default_factory=list)

    def series_type(self) -> str | None:
        for kind in sorted(self.kinds):
            if kind in SERIES_STEP_TYPES:
                return SERIES_STEP_TYPES[kind]
        return None

    @property
    def is_array(self) -> bool:
        return ARRAY_STEP_KIND in self.kinds


def collect_path_series_components(report_payload: dict[str, Any]) -> list[PathSeriesComponent]:
    """Collect traversed series/array components from a channel path report payload."""
    by_refdes: dict[str, PathSeriesComponent] = {}
    for path in report_payload.get("paths") or []:
        status = str(path.get("status") or "")
        if not status.startswith("resolved"):
            continue
        channel_key = {"channel": str(path.get("channel")), "polarity": str(path.get("polarity"))}
        for step in path.get("steps") or []:
            kind = str(step.get("kind") or "")
            if kind not in SERIES_STEP_TYPES and kind != ARRAY_STEP_KIND:
                continue
            refdes = str(step.get("component") or "")
            if not refdes:
                continue
            item = by_refdes.setdefault(refdes, PathSeriesComponent(refdes=refdes))
            item.kinds.add(kind)
            if step.get("part_name"):
                item.part_name = str(step["part_name"])
            if channel_key not in item.channels:
                item.channels.append(channel_key)
    return [by_refdes[refdes] for refdes in sorted(by_refdes)]


def validate_treatment_config(treatment_config: dict[str, Any]) -> None:
    if not isinstance(treatment_config, dict):
        raise ValueError("seriesTreatment must be an object")
    unknown = sorted(
        set(treatment_config) - {"default", "byGroup", "byType", "overrides"}
    )
    if unknown:
        raise ValueError(f"seriesTreatment has unsupported fields: {unknown}")
    if "default" in treatment_config:
        if not isinstance(treatment_config.get("default"), str):
            raise ValueError("seriesTreatment.default must be a string")
        _validate_treatment_value(
            str(treatment_config.get("default")),
            "seriesTreatment.default",
        )
    by_group = treatment_config.get("byGroup") or {}
    if not isinstance(by_group, dict):
        raise ValueError("seriesTreatment.byGroup must be an object")
    for group_name, treatment in by_group.items():
        if not isinstance(group_name, str) or not group_name.strip():
            raise ValueError("seriesTreatment.byGroup names must be non-empty strings")
        if not isinstance(treatment, str):
            raise ValueError(
                f"seriesTreatment.byGroup.{group_name} must be a string"
            )
        _validate_treatment_value(
            str(treatment),
            f"seriesTreatment.byGroup.{group_name}",
        )
    by_type = treatment_config.get("byType") or {}
    if not isinstance(by_type, dict):
        raise ValueError("seriesTreatment.byType must be an object")
    for type_name, treatment in by_type.items():
        if not isinstance(treatment, str):
            raise ValueError(f"seriesTreatment.byType.{type_name} must be a string")
        _validate_treatment_value(str(treatment), f"seriesTreatment.byType.{type_name}")
    overrides = treatment_config.get("overrides") or []
    if not isinstance(overrides, list) or not all(
        isinstance(override, dict) for override in overrides
    ):
        raise ValueError("seriesTreatment.overrides must be an object list")
    for override in overrides:
        _validate_treatment_value(str(override.get("treatment")), f"seriesTreatment.overrides for {override}")


def _validate_treatment_value(treatment: str, where: str) -> None:
    if treatment == "open":
        raise ValueError(
            f"treatment 'open' is not implemented ({where}); "
            "the SYZ meaning of a removed model (open vs short) is unconfirmed with the customer"
        )
    if treatment not in VALID_TREATMENTS:
        raise ValueError(f"unsupported treatment {treatment!r} ({where}); valid: {sorted(VALID_TREATMENTS)}")


def resolve_treatment(
    *,
    model_type: str | None,
    refdes: str,
    part_keys: set[str],
    groups: set[str] | None = None,
    treatment_config: dict[str, Any],
) -> str | None:
    """Resolve one component: legacy override > BOM group > type > default.

    Returns None when the type is unknown and no override matches (caller reports unresolved).
    """
    part_override: str | None = None
    for override in treatment_config.get("overrides") or []:
        treatment = str(override.get("treatment") or "")
        if _normalize_key(override.get("refdes")) == _normalize_key(refdes) and override.get("refdes"):
            return treatment
        override_part = override.get("part_no") or override.get("part_name")
        if override_part and _normalize_key(str(override_part)) in part_keys:
            part_override = treatment
    if part_override:
        return part_override

    group_treatments = {
        str(treatment)
        for group, treatment in (treatment_config.get("byGroup") or {}).items()
        if _normalize_key(str(group))
        in {_normalize_key(value) for value in (groups or set())}
    }
    if len(group_treatments) > 1:
        raise ValueError(
            f"conflicting series treatments for {refdes}: {sorted(group_treatments)}"
        )
    if group_treatments:
        return next(iter(group_treatments))

    if model_type is None:
        default = treatment_config.get("default")
        return str(default) if default is not None else None
    by_type = treatment_config.get("byType") or {}
    if model_type in by_type:
        return str(by_type[model_type])
    if treatment_config.get("default") is not None:
        return str(treatment_config["default"])
    return TREATMENT_DEFAULTS.get(model_type)


def _component_part(comp: object) -> str:
    return str(getattr(comp, "component_def", None) or getattr(comp, "partname", None) or "")


def _edb_components(edb: object) -> dict[str, object]:
    comps = getattr(edb.components, "instances", None)
    if isinstance(comps, dict) and comps:
        return comps
    return edb.components.components


def _inherited_values(comp: object) -> dict[str, Any]:
    """Return the ANF/CMP-inherited RLC values, if the component carries a usable model."""
    if str(getattr(comp, "type", "")).casefold() not in INHERITED_TYPES:
        return {}
    values: dict[str, Any] = {}
    for attr, key in (("res_value", "r"), ("cap_value", "c"), ("ind_value", "l")):
        try:
            value = getattr(comp, attr)
        except Exception:
            continue
        if value is None or value == "":
            continue
        values[key] = value
    return values


def _assign_pin_pair_rlc(comp: object, pin_pairs: list[tuple[str, str]], r_ohm: float) -> None:
    """Install the validated series model: pin-pair RLC on the component + type=Resistor.

    SIWave only translates RLC models for R/L/C-typed components (type=Other is ignored
    regardless of the model), and pyedb's assign_rlc_model pairs consecutive pins, which
    is wrong for multi-pin arrays — hence the explicit PinPairModel construction.
    """
    if str(getattr(comp, "type", None)) != "Resistor":
        comp.type = "Resistor"
    pp_model = comp._edb.cell.hierarchy._hierarchy.PinPairModel()
    r_val = comp._get_edb_value(r_ohm)
    zero = comp._get_edb_value(0)
    for pin_a, pin_b in pin_pairs:
        pin_pair = comp._edb.utility.utility.PinPair(pin_a, pin_b)
        rlc = comp._edb.utility.utility.Rlc(r_val, True, zero, False, zero, False, False)
        pp_model.SetPinPairRlc(pin_pair, rlc)
    if not comp._set_model(pp_model):
        raise RuntimeError("_set_model failed")


def _resolve_pin_pairs(comp: object, library_entry: dict[str, Any] | None) -> list[tuple[str, str]] | None:
    if library_entry and library_entry.get("pin_pairs"):
        return [(str(a), str(b)) for a, b in library_entry["pin_pairs"]]
    pins = sorted(getattr(comp, "pins", {}) or {})
    if len(pins) == 2:
        return [(pins[0], pins[1])]
    return None


def apply_series_models(
    edb: object,
    *,
    report_payload: dict[str, Any],
    part_library: dict[str, dict[str, Any]],
    treatment_config: dict[str, Any],
) -> dict[str, Any]:
    """Apply series electrical models to path-traversed components on an open EDB.

    Value resolution for treatment=actual: inherited EDB model (2-pin) first,
    then part library model, otherwise unresolved. The library never overrides
    an inherited value; exceptions go through seriesTreatment.overrides.
    """
    validate_treatment_config(treatment_config)

    components = collect_path_series_components(report_payload)
    edb_comps = _edb_components(edb)
    bom_group_mode = any(
        entry.get("groups")
        for entry in part_library.values()
    )

    installed: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    def mark_unresolved(item: PathSeriesComponent, reason: str, treatment: str | None) -> None:
        unresolved.append(
            {
                "component": item.refdes,
                "part": item.part_name,
                "treatment": treatment,
                "reason": reason,
                "channels": item.channels,
            }
        )

    for item in components:
        comp = edb_comps.get(item.refdes)
        if comp is None:
            mark_unresolved(item, "component not found in EDB", None)
            continue

        part = _component_part(comp) or (item.part_name or "")
        candidate_keys = [
            key
            for key in (
                _normalize_key(item.refdes),
                _normalize_key(part),
                _normalize_key(item.part_name),
            )
            if key
        ]
        part_keys = set(candidate_keys)
        library_entry = None
        for key in candidate_keys:
            if key in part_library:
                library_entry = part_library[key]
                break
        if bom_group_mode and library_entry is None:
            mark_unresolved(
                item,
                "component is not registered by any BOM.compProp group",
                None,
            )
            continue
        groups = {
            str(group)
            for group in (library_entry or {}).get("groups") or []
            if str(group).strip()
        }

        if item.is_array:
            model_type = str((library_entry or {}).get("model", {}).get("type") or "") or None
        else:
            model_type = item.series_type()

        treatment = resolve_treatment(
            model_type=model_type,
            refdes=item.refdes,
            part_keys=part_keys,
            groups=groups,
            treatment_config=treatment_config,
        )
        if treatment is None:
            mark_unresolved(item, f"treatment undecidable: unknown model type for part {part!r}", None)
            continue

        if treatment == "unresolved":
            mark_unresolved(item, "treatment policy is unresolved (pending customer confirmation)", treatment)
            continue

        if treatment == "actual":
            values = {} if item.is_array else _inherited_values(comp)
            if values:
                inherited.append(
                    {
                        "component": item.refdes,
                        "part": part,
                        "treatment": treatment,
                        "values": {key: str(value) for key, value in values.items()},
                        "channels": item.channels,
                    }
                )
                continue
            model = (library_entry or {}).get("model") or {}
            if model.get("type") != "resistor" or model.get("r_ohm") is None:
                reason = (
                    "no value source: no inherited model and no part library resistor model"
                    if not model
                    else f"unsupported part library model type: {model.get('type')}"
                )
                mark_unresolved(item, reason, treatment)
                continue
            r_ohm = float(model["r_ohm"])
        else:  # short
            r_ohm = 0.0

        pin_pairs = _resolve_pin_pairs(comp, library_entry)
        if pin_pairs is None:
            mark_unresolved(item, "pin pairs unknown: no part library pin_pairs and not a 2-pin component", treatment)
            continue
        comp_pins = getattr(comp, "pins", {}) or {}
        missing = [pin for pair in pin_pairs for pin in pair if pin not in comp_pins]
        if missing:
            mark_unresolved(item, f"pins missing on component: {missing}", treatment)
            continue

        try:
            _assign_pin_pair_rlc(comp, pin_pairs, r_ohm)
        except Exception as exc:
            mark_unresolved(item, f"model install failed: {type(exc).__name__}: {exc}", treatment)
            continue
        def _pin_net(pin_name: str) -> str | None:
            pin = comp_pins.get(pin_name)
            net = getattr(pin, "net_name", None)
            return str(net) if net else None

        installed.append(
            {
                "method": "pin-pair-rlc-model",
                "component": item.refdes,
                "part": part,
                "treatment": treatment,
                "rOhm": r_ohm,
                "pinPairs": [[a, b] for a, b in pin_pairs],
                "nets": [[_pin_net(a), _pin_net(b)] for a, b in pin_pairs],
                "channels": item.channels,
            }
        )

    skipped_channels: list[dict[str, str]] = []
    for entry in unresolved:
        for channel in entry["channels"]:
            if channel not in skipped_channels:
                skipped_channels.append(channel)

    return {
        "pathSeriesComponentCount": len(components),
        "installed": installed,
        "inherited": inherited,
        "unresolved": unresolved,
        "skippedChannels": skipped_channels,
    }
