from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .aedb_lookup import ChannelPinLookupReport, PinLookupRecord, PinLookupRequest
from .direction import (
    PATH_ENDPOINT_TO_START,
    PATH_START_TO_ENDPOINT,
    DirectionResolutionError,
    direction_candidate,
    normalize_measurement_direction,
    require_resolved_direction,
    resolve_measurement_direction,
)
from .target_band import (
    LEGACY_REFERENCE_IMPEDANCE_KEY,
    REFERENCE_IMPEDANCE_KEY,
    TARGET_RANGE_KEY,
    TdrImpedanceSchemaError,
    merge_tdr_setting_overrides,
    resolve_reference_impedance,
    resolve_target_band,
)
from .targets import OPTIONAL_COLUMNS, REQUIRED_COLUMNS, ChannelTarget


MINIMAL_REQUIRED_COLUMNS = ("ic_refdes", "pos_pin", "neg_pin")
SUPPORTED_TDR_SETTING_KEYS = {
    REFERENCE_IMPEDANCE_KEY,
    LEGACY_REFERENCE_IMPEDANCE_KEY,
    TARGET_RANGE_KEY,
    "riseTimePs",
    "differentialBridgeOhm",
    "pulseRepetition",
    "pulseWidth",
    "timeDelay",
}


class MinimalInputValidationError(ValueError):
    pass


def _clean(value: Any) -> str:
    return str(value or "").strip()


def canonical_target_key(ic_refdes: str, pos_pin: str, neg_pin: str) -> str:
    return f"{_clean(ic_refdes)}:{_clean(pos_pin)}:{_clean(neg_pin)}".upper()


@dataclass(frozen=True)
class MinimalChannelInput:
    ic_refdes: str
    pos_pin: str
    neg_pin: str
    line_number: int

    @property
    def target_key(self) -> str:
        return canonical_target_key(self.ic_refdes, self.pos_pin, self.neg_pin)

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetKey": self.target_key,
            "lineNumber": self.line_number,
            "icRefdes": self.ic_refdes,
            "positivePin": self.pos_pin,
            "negativePin": self.neg_pin,
        }


@dataclass(frozen=True)
class PolaritySuffixPair:
    id: str
    positive: str
    negative: str


@dataclass(frozen=True)
class PolarityRegexPair:
    id: str
    positive_pattern: str
    negative_pattern: str
    pair_base_template: str


@dataclass(frozen=True)
class InferenceRule:
    id: str
    priority: int
    pair_base_pattern: str
    output: dict[str, Any]
    tdr_template_id: str | None = None
    syz_template_id: str | None = None

    def fullmatch(self, pair_base: str) -> re.Match[str] | None:
        return re.fullmatch(self.pair_base_pattern, pair_base, flags=re.IGNORECASE)


@dataclass(frozen=True)
class TdrTemplate:
    id: str
    priority: int
    match: dict[str, Any]
    settings: dict[str, Any]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class SyzTemplate:
    id: str
    frequency_sweep: dict[str, Any]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class MinimalInputProfile:
    version: int
    profile_id: str
    source_path: str
    polarity_suffix_pairs: tuple[PolaritySuffixPair, ...]
    polarity_regex_pairs: tuple[PolarityRegexPair, ...]
    inference_rules: tuple[InferenceRule, ...]
    tdr_templates: tuple[TdrTemplate, ...]
    syz_templates: tuple[SyzTemplate, ...]

    def rule_by_id(self, rule_id: str) -> InferenceRule | None:
        return next((rule for rule in self.inference_rules if rule.id == rule_id), None)

    def template_by_id(self, template_id: str) -> TdrTemplate | None:
        return next((template for template in self.tdr_templates if template.id == template_id), None)

    def syz_template_by_id(self, template_id: str) -> SyzTemplate | None:
        return next((template for template in self.syz_templates if template.id == template_id), None)


@dataclass(frozen=True)
class TargetOverride:
    target_key: str
    reason: str
    differential_pair_base: str | None = None
    inference_rule_id: str | None = None
    tdr_template_id: str | None = None
    syz_template_id: str | None = None
    interface: str | None = None
    channel: str | None = None
    report_group: str | None = None
    measurement_direction: str | None = None
    endpoint_refdes: str | None = None
    endpoint_pos_pin: str | None = None
    endpoint_neg_pin: str | None = None
    tdr: dict[str, Any] | None = None


class MinimalTargetOverrides:
    def __init__(self, overrides: list[TargetOverride] | None = None) -> None:
        self._by_key: dict[str, TargetOverride] = {}
        for override in overrides or []:
            if override.target_key in self._by_key:
                raise MinimalInputValidationError(f"duplicate override targetKey: {override.target_key}")
            self._by_key[override.target_key] = override

    @property
    def target_keys(self) -> set[str]:
        return set(self._by_key)

    def get(self, target_key: str) -> TargetOverride | None:
        return self._by_key.get(target_key.upper())


@dataclass(frozen=True)
class MinimalNormalizationResult:
    version: int
    profile_id: str
    profile_path: str
    source_csv: str
    inputs: list[MinimalChannelInput]
    targets: list[ChannelTarget]
    records: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    syz_template_id: str | None
    syz_frequency_sweep: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "profileId": self.profile_id,
            "profilePath": self.profile_path,
            "sourceCsv": self.source_csv,
            "summary": {
                "inputs": len(self.inputs),
                "normalized": len(self.targets),
                "unresolved": len(self.unresolved),
            },
            "records": self.records,
            "unresolved": self.unresolved,
            "syzTemplate": (
                {
                    "templateId": self.syz_template_id,
                    "frequencySweep": self.syz_frequency_sweep,
                }
                if self.syz_template_id
                else None
            ),
        }


def load_minimal_channel_inputs_csv(path: Path) -> list[MinimalChannelInput]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None:
            raise MinimalInputValidationError(f"CSV has no header: {path}")
        fieldnames = [_clean(field) for field in reader.fieldnames]
        missing = [field for field in MINIMAL_REQUIRED_COLUMNS if field not in fieldnames]
        unexpected = [field for field in fieldnames if field not in MINIMAL_REQUIRED_COLUMNS]
        if missing:
            raise MinimalInputValidationError(f"minimal CSV header missing required columns: {missing}")
        if unexpected:
            raise MinimalInputValidationError(
                f"minimal CSV contains non-minimal columns: {unexpected}; use a separate override JSON"
            )

        inputs: list[MinimalChannelInput] = []
        for row in reader:
            if not any(_clean(value) for value in row.values()):
                continue
            values = {field: _clean(row.get(field)) for field in MINIMAL_REQUIRED_COLUMNS}
            missing_values = [field for field, value in values.items() if not value]
            if missing_values:
                raise MinimalInputValidationError(
                    f"CSV line {reader.line_num}: missing required values: {missing_values}"
                )
            inputs.append(
                MinimalChannelInput(
                    ic_refdes=values["ic_refdes"],
                    pos_pin=values["pos_pin"],
                    neg_pin=values["neg_pin"],
                    line_number=reader.line_num,
                )
            )

    keys = [item.target_key for item in inputs]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise MinimalInputValidationError(f"duplicate minimal targets: {duplicates}")
    if not inputs:
        raise MinimalInputValidationError(f"minimal CSV has no target rows: {path}")
    return inputs


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MinimalInputValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MinimalInputValidationError(f"JSON root must be an object: {path}")
    return payload


def _unique_ids(items: list[dict[str, Any]], *, where: str) -> None:
    ids = [_clean(item.get("id")) for item in items]
    missing = [index for index, item_id in enumerate(ids) if not item_id]
    if missing:
        raise MinimalInputValidationError(f"{where} entries missing id at indexes: {missing}")
    duplicates = sorted(item_id for item_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise MinimalInputValidationError(f"duplicate {where} ids: {duplicates}")


def _validate_tdr_settings(settings: dict[str, Any], *, where: str) -> None:
    unknown = sorted(set(settings) - SUPPORTED_TDR_SETTING_KEYS)
    if unknown:
        raise MinimalInputValidationError(f"unsupported TDR settings in {where}: {unknown}")
    try:
        resolve_reference_impedance(settings, where=where)
        resolve_target_band(settings, where=where)
    except TdrImpedanceSchemaError as exc:
        raise MinimalInputValidationError(str(exc)) from exc
    for key in ("riseTimePs", "differentialBridgeOhm"):
        if key not in settings:
            continue
        try:
            value = float(settings[key])
        except (TypeError, ValueError) as exc:
            raise MinimalInputValidationError(f"{where}.{key} must be numeric") from exc
        if value <= 0:
            raise MinimalInputValidationError(f"{where}.{key} must be positive")


def _validate_syz_frequency_sweep(sweep: dict[str, Any], *, where: str) -> None:
    if not isinstance(sweep, dict):
        raise MinimalInputValidationError(f"{where} must be an object")
    ranges = sweep.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise MinimalInputValidationError(f"{where}.ranges must be a non-empty list")
    for index, row in enumerate(ranges):
        if not isinstance(row, list) or len(row) != 4:
            raise MinimalInputValidationError(f"{where}.ranges[{index}] must contain 4 items")
        mode, start, stop, count_or_step = row
        if _clean(mode).casefold() not in {"linear count", "log scale", "linear scale"}:
            raise MinimalInputValidationError(f"{where}.ranges[{index}] has unsupported mode {mode!r}")
        if (
            start is None
            or stop is None
            or (isinstance(start, str) and not start.strip())
            or (isinstance(stop, str) and not stop.strip())
        ):
            raise MinimalInputValidationError(f"{where}.ranges[{index}] requires start and stop")
        try:
            numeric = float(count_or_step)
        except (TypeError, ValueError) as exc:
            raise MinimalInputValidationError(
                f"{where}.ranges[{index}] count/step must be numeric"
            ) from exc
        if numeric <= 0:
            raise MinimalInputValidationError(f"{where}.ranges[{index}] count/step must be positive")


def load_minimal_input_profile(path: Path) -> MinimalInputProfile:
    payload = _read_json_object(path)
    if int(payload.get("version") or 0) != 1:
        raise MinimalInputValidationError(f"unsupported minimal input profile version: {payload.get('version')!r}")
    profile_id = _clean(payload.get("profileId"))
    if not profile_id:
        raise MinimalInputValidationError("minimal input profile missing profileId")
    raw_suffixes = payload.get("polaritySuffixPairs") or []
    raw_regex_pairs = payload.get("polarityRegexPairs") or []
    raw_rules = payload.get("inferenceRules") or []
    raw_templates = payload.get("tdrTemplates") or []
    raw_syz_templates = payload.get("syzTemplates") or []
    if not all(
        isinstance(items, list)
        for items in (raw_suffixes, raw_regex_pairs, raw_rules, raw_templates, raw_syz_templates)
    ):
        raise MinimalInputValidationError("profile suffix/rule/template sections must be lists")
    if not all(
        isinstance(item, dict)
        for items in (raw_suffixes, raw_regex_pairs, raw_rules, raw_templates, raw_syz_templates)
        for item in items
    ):
        raise MinimalInputValidationError("profile suffix/rule/template entries must be objects")
    _unique_ids(raw_suffixes, where="polaritySuffixPairs")
    _unique_ids(raw_regex_pairs, where="polarityRegexPairs")
    _unique_ids(raw_rules, where="inferenceRules")
    _unique_ids(raw_templates, where="tdrTemplates")
    _unique_ids(raw_syz_templates, where="syzTemplates")

    suffixes: list[PolaritySuffixPair] = []
    for raw in raw_suffixes:
        positive = _clean(raw.get("positive"))
        negative = _clean(raw.get("negative"))
        if not positive or not negative or positive.casefold() == negative.casefold():
            raise MinimalInputValidationError(f"invalid polarity suffix pair: {raw!r}")
        suffixes.append(PolaritySuffixPair(id=_clean(raw["id"]), positive=positive, negative=negative))
    regex_pairs: list[PolarityRegexPair] = []
    for raw in raw_regex_pairs:
        positive_pattern = _clean(raw.get("positivePattern"))
        negative_pattern = _clean(raw.get("negativePattern"))
        pair_base_template = _clean(raw.get("pairBaseTemplate"))
        if not positive_pattern or not negative_pattern or not pair_base_template:
            raise MinimalInputValidationError(f"invalid polarity regex pair: {raw!r}")
        try:
            re.compile(positive_pattern, flags=re.IGNORECASE)
            re.compile(negative_pattern, flags=re.IGNORECASE)
        except re.error as exc:
            raise MinimalInputValidationError(
                f"invalid polarity regex pair {raw.get('id')}: {exc}"
            ) from exc
        regex_pairs.append(
            PolarityRegexPair(
                id=_clean(raw["id"]),
                positive_pattern=positive_pattern,
                negative_pattern=negative_pattern,
                pair_base_template=pair_base_template,
            )
        )
    if not suffixes and not regex_pairs:
        raise MinimalInputValidationError(
            "profile requires at least one polaritySuffixPair or polarityRegexPair"
        )

    rules: list[InferenceRule] = []
    for raw in raw_rules:
        pattern = _clean(raw.get("pairBasePattern"))
        output = raw.get("output") or {}
        if not pattern or not isinstance(output, dict):
            raise MinimalInputValidationError(f"invalid inference rule: {raw!r}")
        try:
            re.compile(pattern, flags=re.IGNORECASE)
        except re.error as exc:
            raise MinimalInputValidationError(f"invalid pairBasePattern for {raw.get('id')}: {exc}") from exc
        try:
            if "signalDirection" in output:
                raise MinimalInputValidationError(
                    f"inferenceRules.{raw.get('id')}.output.signalDirection is not "
                    "part of the core model; keep RX/TX naming in interface, channel, "
                    "or reportGroup"
                )
            if _clean(output.get("measurementDirection")):
                normalize_measurement_direction(
                    output.get("measurementDirection"),
                    where=f"inferenceRules.{raw.get('id')}.output.measurementDirection",
                )
        except DirectionResolutionError as exc:
            raise MinimalInputValidationError(str(exc)) from exc
        rules.append(
            InferenceRule(
                id=_clean(raw["id"]),
                priority=int(raw.get("priority") or 0),
                pair_base_pattern=pattern,
                output=dict(output),
                tdr_template_id=_clean(raw.get("tdrTemplateId")) or None,
                syz_template_id=_clean(raw.get("syzTemplateId")) or None,
            )
        )
    if not rules:
        raise MinimalInputValidationError("profile requires at least one inferenceRule")

    templates: list[TdrTemplate] = []
    for raw in raw_templates:
        match = raw.get("match") or {}
        settings = raw.get("settings") or {}
        provenance = raw.get("provenance") or {}
        if not isinstance(match, dict) or not isinstance(settings, dict) or not isinstance(provenance, dict):
            raise MinimalInputValidationError(f"invalid TDR template: {raw!r}")
        pattern = _clean(match.get("interfacePattern"))
        if pattern:
            try:
                re.compile(pattern, flags=re.IGNORECASE)
            except re.error as exc:
                raise MinimalInputValidationError(
                    f"invalid interfacePattern for template {raw.get('id')}: {exc}"
                ) from exc
        _validate_tdr_settings(settings, where=f"tdrTemplates.{raw.get('id')}.settings")
        templates.append(
            TdrTemplate(
                id=_clean(raw["id"]),
                priority=int(raw.get("priority") or 0),
                match=dict(match),
                settings=dict(settings),
                provenance=dict(provenance),
            )
        )
    if not templates:
        raise MinimalInputValidationError("profile requires at least one tdrTemplate")
    template_ids = {template.id for template in templates}
    missing_rule_templates = sorted(
        rule.tdr_template_id
        for rule in rules
        if rule.tdr_template_id and rule.tdr_template_id not in template_ids
    )
    if missing_rule_templates:
        raise MinimalInputValidationError(
            f"inference rules reference missing TDR templates: {missing_rule_templates}"
        )

    syz_templates: list[SyzTemplate] = []
    for raw in raw_syz_templates:
        frequency_sweep = raw.get("frequencySweep") or {}
        provenance = raw.get("provenance") or {}
        if not isinstance(provenance, dict):
            raise MinimalInputValidationError(f"invalid SYZ template provenance: {raw!r}")
        _validate_syz_frequency_sweep(
            frequency_sweep,
            where=f"syzTemplates.{raw.get('id')}.frequencySweep",
        )
        syz_templates.append(
            SyzTemplate(
                id=_clean(raw["id"]),
                frequency_sweep=dict(frequency_sweep),
                provenance=dict(provenance),
            )
        )
    syz_template_ids = {template.id for template in syz_templates}
    missing_rule_syz_templates = sorted(
        rule.syz_template_id
        for rule in rules
        if rule.syz_template_id and rule.syz_template_id not in syz_template_ids
    )
    if missing_rule_syz_templates:
        raise MinimalInputValidationError(
            f"inference rules reference missing SYZ templates: {missing_rule_syz_templates}"
        )
    if syz_templates:
        missing_rule_syz_ids = sorted(rule.id for rule in rules if not rule.syz_template_id)
        if missing_rule_syz_ids:
            raise MinimalInputValidationError(
                f"inference rules missing syzTemplateId: {missing_rule_syz_ids}"
            )

    return MinimalInputProfile(
        version=1,
        profile_id=profile_id,
        source_path=str(path),
        polarity_suffix_pairs=tuple(suffixes),
        polarity_regex_pairs=tuple(regex_pairs),
        inference_rules=tuple(rules),
        tdr_templates=tuple(templates),
        syz_templates=tuple(syz_templates),
    )


def load_minimal_target_overrides(path: Path | None) -> MinimalTargetOverrides:
    if path is None:
        return MinimalTargetOverrides()
    payload = _read_json_object(path)
    if int(payload.get("version") or 0) != 1:
        raise MinimalInputValidationError(f"unsupported override version: {payload.get('version')!r}")
    raw_targets = payload.get("targets") or []
    if not isinstance(raw_targets, list):
        raise MinimalInputValidationError("override targets must be a list")

    overrides: list[TargetOverride] = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise MinimalInputValidationError("override target entries must be objects")
        allowed_fields = {
            "targetKey",
            "reason",
            "differentialPairBase",
            "inferenceRuleId",
            "tdrTemplateId",
            "syzTemplateId",
            "interface",
            "channel",
            "reportGroup",
            "measurementDirection",
            "endpointRefdes",
            "endpointPosPin",
            "endpointNegPin",
            "tdr",
        }
        unknown_fields = sorted(set(raw) - allowed_fields)
        if unknown_fields:
            raise MinimalInputValidationError(f"override contains unsupported fields: {unknown_fields}")
        target_key = _clean(raw.get("targetKey")).upper()
        reason = _clean(raw.get("reason"))
        if not target_key or not reason:
            raise MinimalInputValidationError("every override requires targetKey and reason")
        endpoint_values = [
            _clean(raw.get("endpointRefdes")),
            _clean(raw.get("endpointPosPin")),
            _clean(raw.get("endpointNegPin")),
        ]
        if any(endpoint_values) and not all(endpoint_values):
            raise MinimalInputValidationError(
                f"override {target_key}: endpointRefdes/endpointPosPin/endpointNegPin must be provided together"
            )
        tdr = raw.get("tdr") or {}
        if not isinstance(tdr, dict):
            raise MinimalInputValidationError(f"override {target_key}: tdr must be an object")
        _validate_tdr_settings(tdr, where=f"override {target_key}.tdr")
        try:
            measurement_direction = (
                normalize_measurement_direction(
                    raw.get("measurementDirection"),
                    where=f"override {target_key}.measurementDirection",
                )
                if _clean(raw.get("measurementDirection"))
                else None
            )
        except DirectionResolutionError as exc:
            raise MinimalInputValidationError(str(exc)) from exc
        overrides.append(
            TargetOverride(
                target_key=target_key,
                reason=reason,
                differential_pair_base=_clean(raw.get("differentialPairBase")) or None,
                inference_rule_id=_clean(raw.get("inferenceRuleId")) or None,
                tdr_template_id=_clean(raw.get("tdrTemplateId")) or None,
                syz_template_id=_clean(raw.get("syzTemplateId")) or None,
                interface=_clean(raw.get("interface")) or None,
                channel=_clean(raw.get("channel")) or None,
                report_group=_clean(raw.get("reportGroup")) or None,
                measurement_direction=measurement_direction,
                endpoint_refdes=endpoint_values[0] or None,
                endpoint_pos_pin=endpoint_values[1] or None,
                endpoint_neg_pin=endpoint_values[2] or None,
                tdr=dict(tdr) or None,
            )
        )
    return MinimalTargetOverrides(overrides)


def build_minimal_input_pin_requests(inputs: list[MinimalChannelInput]) -> list[PinLookupRequest]:
    requests: list[PinLookupRequest] = []
    for item in inputs:
        requests.extend(
            [
                PinLookupRequest(item.target_key, "positive", item.ic_refdes, item.pos_pin),
                PinLookupRequest(item.target_key, "negative", item.ic_refdes, item.neg_pin),
            ]
        )
    return requests


def build_minimal_discovery_targets(
    inputs: list[MinimalChannelInput],
) -> tuple[list[ChannelTarget], dict[str, str]]:
    targets: list[ChannelTarget] = []
    target_keys_by_channel: dict[str, str] = {}
    for index, item in enumerate(inputs, start=1):
        target = ChannelTarget(
            interface="MINIMAL_DISCOVERY",
            channel=f"T{index:04d}",
            signal_type="differential",
            ic_refdes=item.ic_refdes,
            pos_pin=item.pos_pin,
            neg_pin=item.neg_pin,
            report_group="MINIMAL_DISCOVERY",
            notes=f"minimalTargetKey={item.target_key}",
        )
        targets.append(target)
        target_keys_by_channel[target.name] = item.target_key
    return targets, target_keys_by_channel


def build_minimal_path_net_evidence(
    path_result: Any,
    target_keys_by_channel: dict[str, str],
) -> dict[str, dict[str, list[str]]]:
    evidence: dict[str, dict[str, list[str]]] = {}
    for path in path_result.paths:
        target_key = target_keys_by_channel.get(str(path.channel))
        polarity = str(path.polarity).casefold()
        if not target_key or polarity not in {"positive", "negative"}:
            continue
        bucket = evidence.setdefault(target_key, {"positive": [], "negative": []})[polarity]
        candidates = [getattr(path, "start_net", None), getattr(path, "end_net", None)]
        for step in getattr(path, "steps", []) or []:
            candidates.append(getattr(step, "net", None))
            action = str(getattr(step, "action", "") or "")
            if "_to " in action:
                candidates.append(action.rsplit("_to ", 1)[-1])
        for candidate in candidates:
            net = _clean(candidate)
            if net and net.casefold() not in {item.casefold() for item in bucket}:
                bucket.append(net)
    return evidence


def _pair_base(
    positive_net: str,
    negative_net: str,
    suffix_pairs: tuple[PolaritySuffixPair, ...],
    regex_pairs: tuple[PolarityRegexPair, ...] = (),
) -> tuple[str, PolaritySuffixPair | PolarityRegexPair] | None:
    for suffix in suffix_pairs:
        if not positive_net.casefold().endswith(suffix.positive.casefold()):
            continue
        if not negative_net.casefold().endswith(suffix.negative.casefold()):
            continue
        positive_base = positive_net[: len(positive_net) - len(suffix.positive)]
        negative_base = negative_net[: len(negative_net) - len(suffix.negative)]
        if positive_base and positive_base.casefold() == negative_base.casefold():
            return positive_base, suffix
    for regex_pair in regex_pairs:
        positive_match = re.fullmatch(
            regex_pair.positive_pattern, positive_net, flags=re.IGNORECASE
        )
        negative_match = re.fullmatch(
            regex_pair.negative_pattern, negative_net, flags=re.IGNORECASE
        )
        if positive_match is None or negative_match is None:
            continue
        positive_groups = {
            key: str(value)
            for key, value in positive_match.groupdict().items()
            if value is not None
        }
        negative_groups = {
            key: str(value)
            for key, value in negative_match.groupdict().items()
            if value is not None
        }
        shared = set(positive_groups) & set(negative_groups)
        if any(
            positive_groups[key].casefold() != negative_groups[key].casefold()
            for key in shared
        ):
            continue
        groups = dict(negative_groups)
        groups.update(positive_groups)
        try:
            pair_base = regex_pair.pair_base_template.format(**groups).strip()
        except KeyError as exc:
            raise MinimalInputValidationError(
                f"polarity regex pair {regex_pair.id} pairBaseTemplate references missing capture {exc}"
            ) from exc
        if pair_base:
            return pair_base, regex_pair
    return None


def _format_rule_value(value: Any, groups: dict[str, str], *, rule_id: str, field: str) -> str:
    try:
        return str(value).format(**groups)
    except KeyError as exc:
        raise MinimalInputValidationError(
            f"inference rule {rule_id} output.{field} references missing capture {exc}"
        ) from exc


def _rule_output(rule: InferenceRule, match: re.Match[str]) -> tuple[str, str, str]:
    groups = {key: str(value) for key, value in match.groupdict().items() if value is not None}
    interface = _format_rule_value(rule.output.get("interface", ""), groups, rule_id=rule.id, field="interface")
    raw_channel = _format_rule_value(rule.output.get("channel", ""), groups, rule_id=rule.id, field="channel")
    legacy_channel_map = rule.output.get("channelMap")
    channel_name_map = rule.output.get("channelNameMap")
    if legacy_channel_map and channel_name_map and legacy_channel_map != channel_name_map:
        raise MinimalInputValidationError(
            f"inference rule {rule.id} defines conflicting channelMap and channelNameMap"
        )
    channel_map = channel_name_map or legacy_channel_map or {}
    if not isinstance(channel_map, dict):
        raise MinimalInputValidationError(
            f"inference rule {rule.id} output.channelNameMap must be an object"
        )
    mapped_channel = channel_map.get(raw_channel)
    if mapped_channel is None:
        mapped_channel = next(
            (value for key, value in channel_map.items() if str(key).casefold() == raw_channel.casefold()),
            raw_channel,
        )
    channel = str(mapped_channel)
    report_group = _format_rule_value(
        rule.output.get("reportGroup", interface), groups, rule_id=rule.id, field="reportGroup"
    )
    return interface.strip(), channel.strip(), report_group.strip()


def _rule_port_names(rule: InferenceRule, match: re.Match[str]) -> dict[str, str]:
    raw_port_names = rule.output.get("portNames") or {}
    if not isinstance(raw_port_names, dict):
        raise MinimalInputValidationError(
            f"inference rule {rule.id} output.portNames must be an object"
        )
    allowed = {"nearPositive", "nearNegative", "farPositive", "farNegative"}
    unknown = sorted(set(raw_port_names) - allowed)
    if unknown:
        raise MinimalInputValidationError(
            f"inference rule {rule.id} output.portNames has unsupported fields: {unknown}"
        )
    groups = {key: str(value) for key, value in match.groupdict().items() if value is not None}
    return {
        key: _format_rule_value(value, groups, rule_id=rule.id, field=f"portNames.{key}").strip()
        for key, value in raw_port_names.items()
    }


def _rule_measurement_direction(
    rule: InferenceRule,
    match: re.Match[str],
) -> str | None:
    groups = {
        key: str(value)
        for key, value in match.groupdict().items()
        if value is not None
    }
    raw_measurement = rule.output.get("measurementDirection")
    return (
        normalize_measurement_direction(
            _format_rule_value(
                raw_measurement,
                groups,
                rule_id=rule.id,
                field="measurementDirection",
            ),
            where=f"inference rule {rule.id}.measurementDirection",
        )
        if _clean(raw_measurement)
        else None
    )


def _template_match_score(template: TdrTemplate, interface: str) -> tuple[int, int] | None:
    exact = _clean(template.match.get("interface"))
    pattern = _clean(template.match.get("interfacePattern"))
    if exact:
        if exact.casefold() != interface.casefold():
            return None
        return template.priority, 1
    if pattern:
        if re.fullmatch(pattern, interface, flags=re.IGNORECASE) is None:
            return None
        return template.priority, 0
    return template.priority, 0


def _pin_record_dict(record: PinLookupRecord | None) -> dict[str, Any]:
    if record is None:
        return {"found": False, "error": "lookup record missing"}
    return {
        "component": record.component,
        "pin": record.pin,
        "found": record.found,
        "net": record.net,
        "position": record.position,
        "error": record.error,
    }


def _number_string(value: Any) -> str:
    numeric = float(value)
    return f"{numeric:g}"


def _target_from_values(
    item: MinimalChannelInput,
    *,
    interface: str,
    channel: str,
    report_group: str,
    template: TdrTemplate,
    syz_template: SyzTemplate | None,
    settings: dict[str, Any],
    port_names: dict[str, str],
    override: TargetOverride | None,
    rule_id: str | None,
    measurement_direction: str,
    measurement_direction_source: str,
    measurement_direction_reason: str | None,
) -> ChannelTarget:
    endpoint_refdes = override.endpoint_refdes if override else None
    endpoint_pos_pin = override.endpoint_pos_pin if override else None
    endpoint_neg_pin = override.endpoint_neg_pin if override else None
    note_parts = [f"minimalTargetKey={item.target_key}", f"tdrTemplate={template.id}"]
    if syz_template:
        note_parts.append(f"syzTemplate={syz_template.id}")
    if rule_id:
        note_parts.append(f"inferenceRule={rule_id}")
    if override:
        note_parts.append(f"overrideReason={override.reason}")

    reference_impedance = resolve_reference_impedance(
        settings,
        where=f"resolved target {item.target_key}",
    )
    target_band = resolve_target_band(
        settings,
        where=f"resolved target {item.target_key}",
    )

    return ChannelTarget(
        interface=interface,
        channel=channel,
        signal_type="differential",
        ic_refdes=item.ic_refdes,
        pos_pin=item.pos_pin,
        neg_pin=item.neg_pin,
        report_group=report_group,
        endpoint_refdes=endpoint_refdes or "",
        endpoint_pos_pin=endpoint_pos_pin or "",
        endpoint_neg_pin=endpoint_neg_pin or "",
        near_pos_port=port_names.get("nearPositive", ""),
        near_neg_port=port_names.get("nearNegative", ""),
        far_pos_port=port_names.get("farPositive", ""),
        far_neg_port=port_names.get("farNegative", ""),
        measurement_direction=measurement_direction,
        measurement_direction_source=measurement_direction_source,
        measurement_direction_reason=measurement_direction_reason or "",
        reference_impedance_ohm=(
            _number_string(reference_impedance.value_ohm)
            if reference_impedance.value_ohm is not None
            else ""
        ),
        target_lower_ohm=(
            _number_string(target_band.lower_ohm) if target_band.configured else ""
        ),
        target_upper_ohm=(
            _number_string(target_band.upper_ohm) if target_band.configured else ""
        ),
        target_band_status=(
            ("configured" if target_band.configured else "not-configured")
            if TARGET_RANGE_KEY in settings
            else ""
        ),
        target_band_reason=(
            target_band.reason or "" if TARGET_RANGE_KEY in settings else ""
        ),
        target_band_source=(
            target_band.source or "" if TARGET_RANGE_KEY in settings else ""
        ),
        rise_time_ps=_number_string(settings["riseTimePs"]) if "riseTimePs" in settings else "",
        differential_bridge_ohm=(
            _number_string(settings["differentialBridgeOhm"])
            if "differentialBridgeOhm" in settings
            else ""
        ),
        pulse_repetition=_clean(settings.get("pulseRepetition")),
        pulse_width=_clean(settings.get("pulseWidth")),
        time_delay=_clean(settings.get("timeDelay")),
        notes="; ".join(note_parts),
    )


def normalize_minimal_inputs(
    inputs: list[MinimalChannelInput],
    pin_lookup: ChannelPinLookupReport,
    profile: MinimalInputProfile,
    *,
    source_csv: Path | str,
    overrides: MinimalTargetOverrides | None = None,
    path_net_evidence: dict[str, dict[str, list[str]]] | None = None,
) -> MinimalNormalizationResult:
    overrides = overrides or MinimalTargetOverrides()
    path_net_evidence = path_net_evidence or {}
    input_keys = {item.target_key for item in inputs}
    unknown_overrides = sorted(overrides.target_keys - input_keys)
    if unknown_overrides:
        raise MinimalInputValidationError(f"override targetKey not present in minimal CSV: {unknown_overrides}")

    observations = {
        (record.channel.upper(), record.polarity.casefold()): record for record in pin_lookup.records
    }
    records: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    tentative: list[tuple[ChannelTarget, dict[str, Any]]] = []

    def mark_unresolved(
        record: dict[str, Any],
        *,
        stage: str,
        code: str,
        message: str,
        required_override: list[str] | None = None,
    ) -> None:
        item = {
            "targetKey": record["targetKey"],
            "stage": stage,
            "code": code,
            "message": message,
            "requiredOverride": required_override or [],
        }
        record["status"] = "unresolved"
        record["normalizedTarget"] = None
        record["unresolved"] = item
        unresolved.append(item)

    for item in inputs:
        override = overrides.get(item.target_key)
        positive = observations.get((item.target_key, "positive"))
        negative = observations.get((item.target_key, "negative"))
        record: dict[str, Any] = {
            "targetKey": item.target_key,
            "input": item.to_dict(),
            "pinObservations": {
                "positive": _pin_record_dict(positive),
                "negative": _pin_record_dict(negative),
            },
            "override": (
                {"applied": True, "reason": override.reason, "targetKey": override.target_key}
                if override
                else {"applied": False}
            ),
            "pathNetEvidence": path_net_evidence.get(
                item.target_key, {"positive": [], "negative": []}
            ),
        }
        records.append(record)

        failed_records = [candidate for candidate in (positive, negative) if candidate is None or not candidate.found]
        if failed_records:
            mark_unresolved(
                record,
                stage="pin_lookup",
                code="pin_lookup_failed",
                message="positive or negative pin could not be resolved in AEDB",
            )
            continue
        assert positive is not None and negative is not None
        if not positive.net or not negative.net:
            mark_unresolved(
                record,
                stage="pin_lookup",
                code="pin_net_missing",
                message="positive or negative pin has no net",
            )
            continue
        if positive.net.casefold() == negative.net.casefold():
            mark_unresolved(
                record,
                stage="differential_pair",
                code="same_net_not_differential",
                message=f"positive and negative pins resolve to the same net {positive.net!r}",
            )
            continue

        evidence = path_net_evidence.get(item.target_key) or {}
        positive_nets = [positive.net, *(evidence.get("positive") or [])]
        negative_nets = [negative.net, *(evidence.get("negative") or [])]
        candidates_by_base: dict[str, dict[str, Any]] = {}
        for positive_candidate in positive_nets:
            for negative_candidate in negative_nets:
                pair_result = _pair_base(
                    positive_candidate,
                    negative_candidate,
                    profile.polarity_suffix_pairs,
                    profile.polarity_regex_pairs,
                )
                if pair_result is None:
                    continue
                candidate_base, polarity_rule = pair_result
                key = candidate_base.casefold()
                source = (
                    "pin_nets"
                    if positive_candidate == positive.net and negative_candidate == negative.net
                    else "channel_path_evidence"
                )
                existing = candidates_by_base.get(key)
                if existing is None or (source == "pin_nets" and existing["source"] != "pin_nets"):
                    candidates_by_base[key] = {
                        "base": candidate_base,
                        "polarityRule": polarity_rule,
                        "positiveNet": positive_candidate,
                        "negativeNet": negative_candidate,
                        "source": source,
                    }
        pair_candidates = list(candidates_by_base.values())
        supported_pair_candidates = [
            candidate
            for candidate in pair_candidates
            if any(rule.fullmatch(candidate["base"]) for rule in profile.inference_rules)
        ]
        selectable = supported_pair_candidates or pair_candidates
        record["differentialPairCandidates"] = [
            {
                "base": candidate["base"],
                "polarityRuleId": candidate["polarityRule"].id,
                "positiveNet": candidate["positiveNet"],
                "negativeNet": candidate["negativeNet"],
                "source": candidate["source"],
            }
            for candidate in selectable
        ]
        selected_pair = None
        if override and override.differential_pair_base:
            selected_pair = next(
                (
                    candidate
                    for candidate in selectable
                    if candidate["base"].casefold()
                    == override.differential_pair_base.casefold()
                ),
                None,
            )
            if selected_pair is None:
                selected_pair = {
                    "base": override.differential_pair_base,
                    "polarityRule": None,
                    "positiveNet": positive.net,
                    "negativeNet": negative.net,
                    "source": "override",
                }
        elif len(selectable) == 1:
            selected_pair = selectable[0]
        elif len(selectable) > 1:
            mark_unresolved(
                record,
                stage="differential_pair",
                code="ambiguous_differential_pair",
                message=(
                    "multiple P/N pair bases are supported by the selected profile: "
                    + ", ".join(sorted(candidate["base"] for candidate in selectable))
                ),
                required_override=["differentialPairBase", "reason"],
            )
            continue
        if selected_pair is None:
            mark_unresolved(
                record,
                stage="differential_pair",
                code="differential_pair_unresolved",
                message=f"nets do not form an allowed P/N pair: {positive.net!r}, {negative.net!r}",
                required_override=["differentialPairBase", "reason"],
            )
            continue
        pair_base = str(selected_pair["base"])
        polarity_rule = selected_pair["polarityRule"]
        record["differentialPair"] = {
            "status": "resolved",
            "base": pair_base,
            "source": selected_pair["source"],
            "polarityRuleId": polarity_rule.id if polarity_rule else None,
            "positiveNet": selected_pair["positiveNet"],
            "negativeNet": selected_pair["negativeNet"],
        }

        matched_rules = [
            (rule, match)
            for rule in profile.inference_rules
            if (match := rule.fullmatch(pair_base)) is not None
        ]
        record["inferenceCandidates"] = [
            {"ruleId": rule.id, "priority": rule.priority} for rule, _match in matched_rules
        ]
        selected_rule: InferenceRule | None = None
        selected_match: re.Match[str] | None = None

        direct_override = bool(override and override.interface and override.channel)
        if override and override.inference_rule_id:
            selected_rule = profile.rule_by_id(override.inference_rule_id)
            selected_match = selected_rule.fullmatch(pair_base) if selected_rule else None
            if selected_rule is None or selected_match is None:
                mark_unresolved(
                    record,
                    stage="inference",
                    code="invalid_inference_rule_override",
                    message=(
                        f"override inferenceRuleId {override.inference_rule_id!r} is missing or does not match {pair_base!r}"
                    ),
                    required_override=["valid inferenceRuleId or explicit interface+channel"],
                )
                continue
        elif not direct_override:
            if not matched_rules:
                mark_unresolved(
                    record,
                    stage="inference",
                    code="no_inference_rule",
                    message=f"no inference rule matches pair base {pair_base!r}",
                    required_override=[
                        "interface",
                        "channel",
                        "tdrTemplateId",
                        "syzTemplateId",
                        "reason",
                    ],
                )
                continue
            highest_priority = max(rule.priority for rule, _match in matched_rules)
            top_rules = [(rule, match) for rule, match in matched_rules if rule.priority == highest_priority]
            if len(top_rules) != 1:
                mark_unresolved(
                    record,
                    stage="inference",
                    code="ambiguous_inference_rule",
                    message=f"multiple inference rules share priority {highest_priority} for {pair_base!r}",
                    required_override=["inferenceRuleId", "reason"],
                )
                continue
            selected_rule, selected_match = top_rules[0]

        if selected_rule is not None and selected_match is not None:
            interface, channel, report_group = _rule_output(selected_rule, selected_match)
            port_names = _rule_port_names(selected_rule, selected_match)
            profile_measurement_direction = _rule_measurement_direction(
                selected_rule, selected_match
            )
            rule_tdr_template_id = selected_rule.tdr_template_id
            rule_syz_template_id = selected_rule.syz_template_id
        else:
            interface, channel, report_group = "", "", ""
            port_names = {}
            profile_measurement_direction = None
            rule_tdr_template_id = None
            rule_syz_template_id = None
        if override:
            interface = override.interface or interface
            channel = override.channel or channel
            report_group = override.report_group or report_group
        report_group = report_group or interface
        if not interface or not channel or not report_group:
            mark_unresolved(
                record,
                stage="inference",
                code="inference_fields_missing",
                message="interface, channel, or report group is empty after rule/override resolution",
                required_override=["interface", "channel", "reportGroup", "reason"],
            )
            continue
        record["inference"] = {
            "status": "resolved",
            "source": "override" if direct_override else "profile_rule",
            "ruleId": selected_rule.id if selected_rule else None,
            "interface": interface,
            "channel": channel,
            "reportGroup": report_group,
            "portNames": port_names,
        }

        if len(port_names) == 4:
            compatibility_direction = PATH_START_TO_ENDPOINT
            compatibility_source = "legacy_profile_port_roles_adapter"
            compatibility_reason = (
                "existing profile near/far port names were assigned to path start/endpoint"
            )
        elif override and override.endpoint_refdes:
            compatibility_direction = PATH_START_TO_ENDPOINT
            compatibility_source = "legacy_profile_endpoint_adapter"
            compatibility_reason = (
                "existing endpoint override rows measured from path start"
            )
        else:
            compatibility_direction = PATH_ENDPOINT_TO_START
            compatibility_source = "legacy_profile_auto_endpoint_adapter"
            compatibility_reason = (
                "existing auto-endpoint rows measured from the discovered endpoint"
            )
        try:
            direction_resolution = resolve_measurement_direction(
                [
                    direction_candidate(
                        override.measurement_direction if override else None,
                        source="override",
                        priority=400,
                        reason=override.reason if override else None,
                    ),
                    direction_candidate(
                        profile_measurement_direction,
                        source=f"profile:{profile.profile_id}:{selected_rule.id if selected_rule else 'direct'}",
                        priority=200,
                    ),
                    direction_candidate(
                        compatibility_direction,
                        source=compatibility_source,
                        priority=100,
                        reason=compatibility_reason,
                    ),
                ]
            )
            measurement_direction = require_resolved_direction(
                direction_resolution,
                where=f"target {item.target_key}",
            )
        except DirectionResolutionError as exc:
            mark_unresolved(
                record,
                stage="measurement_direction",
                code="measurement_direction_unresolved",
                message=str(exc),
                required_override=["measurementDirection", "reason"],
            )
            continue
        record["direction"] = {
            "measurement": direction_resolution.to_dict(),
        }

        explicit_template_id = override.tdr_template_id if override else None
        selected_template: TdrTemplate | None = None
        template_source = ""
        if explicit_template_id or rule_tdr_template_id:
            selected_id = explicit_template_id or rule_tdr_template_id
            selected_template = profile.template_by_id(str(selected_id))
            template_source = "override" if explicit_template_id else "inference_rule"
            if selected_template is None:
                mark_unresolved(
                    record,
                    stage="tdr_template",
                    code="tdr_template_not_found",
                    message=f"selected TDR template does not exist: {selected_id!r}",
                    required_override=["valid tdrTemplateId", "reason"],
                )
                continue
        else:
            matches = [
                (template, score)
                for template in profile.tdr_templates
                if (score := _template_match_score(template, interface)) is not None
            ]
            record["tdrTemplateCandidates"] = [
                {"templateId": template.id, "priority": score[0], "exactInterface": bool(score[1])}
                for template, score in matches
            ]
            if matches:
                best_score = max(score for _template, score in matches)
                top_templates = [template for template, score in matches if score == best_score]
            else:
                top_templates = []
            if len(top_templates) != 1:
                code = "no_tdr_template" if not top_templates else "ambiguous_tdr_template"
                mark_unresolved(
                    record,
                    stage="tdr_template",
                    code=code,
                    message=f"TDR template selection is not unique for interface {interface!r}",
                    required_override=["tdrTemplateId", "reason"],
                )
                continue
            selected_template = top_templates[0]
            template_source = "interface_match"

        settings = dict(selected_template.settings)
        if override and override.tdr:
            settings = merge_tdr_setting_overrides(settings, override.tdr)
        _validate_tdr_settings(settings, where=f"resolved target {item.target_key}")
        record["tdrTemplate"] = {
            "status": "resolved",
            "templateId": selected_template.id,
            "source": template_source,
            "settings": settings,
            "provenance": selected_template.provenance,
        }

        selected_syz_template: SyzTemplate | None = None
        if profile.syz_templates:
            explicit_syz_template_id = override.syz_template_id if override else None
            selected_syz_template_id = explicit_syz_template_id or rule_syz_template_id
            if not selected_syz_template_id:
                mark_unresolved(
                    record,
                    stage="syz_template",
                    code="syz_template_not_selected",
                    message="the resolved target does not select a SYZ template",
                    required_override=["syzTemplateId", "reason"],
                )
                continue
            selected_syz_template = profile.syz_template_by_id(str(selected_syz_template_id))
            if selected_syz_template is None:
                mark_unresolved(
                    record,
                    stage="syz_template",
                    code="syz_template_not_found",
                    message=f"selected SYZ template does not exist: {selected_syz_template_id!r}",
                    required_override=["valid syzTemplateId", "reason"],
                )
                continue
            record["syzTemplate"] = {
                "status": "resolved",
                "templateId": selected_syz_template.id,
                "source": "override" if explicit_syz_template_id else "inference_rule",
                "frequencySweep": selected_syz_template.frequency_sweep,
                "provenance": selected_syz_template.provenance,
            }

        target = _target_from_values(
            item,
            interface=interface,
            channel=channel,
            report_group=report_group,
            template=selected_template,
            syz_template=selected_syz_template,
            settings=settings,
            port_names=port_names,
            override=override,
            rule_id=selected_rule.id if selected_rule else None,
            measurement_direction=measurement_direction,
            measurement_direction_source=direction_resolution.source or "",
            measurement_direction_reason=direction_resolution.reason,
        )
        record["status"] = "resolved"
        record["endpoint"] = {
            "resolution": "override" if target.endpoint_refdes else "pending_path_traversal",
            "component": target.endpoint_refdes or None,
            "positivePin": target.endpoint_pos_pin or None,
            "negativePin": target.endpoint_neg_pin or None,
        }
        record["normalizedTarget"] = asdict(target)
        tentative.append((target, record))

    name_counts = Counter(target.name for target, _record in tentative)
    duplicate_names = {name for name, count in name_counts.items() if count > 1}
    targets: list[ChannelTarget] = []
    for target, record in tentative:
        if target.name in duplicate_names:
            mark_unresolved(
                record,
                stage="normalization",
                code="duplicate_normalized_channel",
                message=f"multiple minimal inputs normalize to channel {target.name!r}",
                required_override=["correct pins/rules so each interface+channel is unique"],
            )
            continue
        targets.append(target)

    resolved_syz_template_ids = {
        str(record["syzTemplate"]["templateId"])
        for record in records
        if record.get("status") == "resolved" and record.get("syzTemplate")
    }
    if len(resolved_syz_template_ids) > 1:
        for record in records:
            if record.get("status") != "resolved":
                continue
            mark_unresolved(
                record,
                stage="syz_template",
                code="multiple_syz_templates_in_run",
                message=(
                    "one SIWave SYZ run requires one frequency sweep; split targets by "
                    f"syzTemplateId: {sorted(resolved_syz_template_ids)}"
                ),
                required_override=["split the selected CSV into one run per syzTemplateId"],
            )
        targets = []

    syz_template_id = next(iter(resolved_syz_template_ids), None) if targets else None
    selected_run_syz_template = (
        profile.syz_template_by_id(syz_template_id) if syz_template_id else None
    )

    return MinimalNormalizationResult(
        version=1,
        profile_id=profile.profile_id,
        profile_path=profile.source_path,
        source_csv=str(source_csv),
        inputs=inputs,
        targets=targets,
        records=records,
        unresolved=unresolved,
        syz_template_id=syz_template_id,
        syz_frequency_sweep=(
            dict(selected_run_syz_template.frequency_sweep) if selected_run_syz_template else None
        ),
    )


def write_minimal_normalization_report(result: MinimalNormalizationResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_normalized_channel_targets_csv(result: MinimalNormalizationResult, output_path: Path) -> None:
    fieldnames = [*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for target in result.targets:
            writer.writerow(asdict(target))
