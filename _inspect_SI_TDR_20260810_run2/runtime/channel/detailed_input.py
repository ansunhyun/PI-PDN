from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .direction import PATH_ENDPOINT_TO_START, PATH_START_TO_ENDPOINT
from .targets import ChannelTarget, OPTIONAL_COLUMNS, REQUIRED_COLUMNS


class DetailedInputValidationError(ValueError):
    pass


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _header_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean(value).lstrip("\ufeff").casefold())


HEADER_ALIASES: dict[str, set[str]] = {
    "snp_file": {"snpfile", "touchstonefile", "touchstonebasename"},
    "designator": {"designator", "icrefdes"},
    "pin": {"icpinno", "icpin", "pin", "pinnumber"},
    "interface": {"function", "interface"},
    "version": {"version", "interfaceversion"},
    "group": {"group", "groupid", "groupname", "channelgroup"},
    "polarity": {"polarity", "pm"},
    "signal": {"clkdata", "signal", "signalrole", "channeltype"},
    "direction": {"direction"},
    "target": {"targetimpedance", "targetimpedanceohm", "referenceimpedanceohm"},
    "minimum": {"minspec", "minspecohm", "targetlowerohm"},
    "maximum": {"maxspec", "maxspecohm", "targetupperohm"},
    "tdr_chart_name": {
        "tdrchartname",
        "chartname",
        "reportgroup",
    },
    "channel": {"channel", "channelid", "channelname", "lane", "laneid"},
    "net_name": {"netname"},
    "ic_pin_name": {"icpinname"},
}

REQUIRED_HEADER_KEYS = {
    "designator",
    "pin",
    "interface",
    "polarity",
    "direction",
    "target",
    "minimum",
    "maximum",
}

REQUIRED_ROW_KEYS = {
    "designator",
    "pin",
    "interface",
    "polarity",
    "direction",
    "target",
    "minimum",
    "maximum",
}

SAFE_OUTPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
TOUCHSTONE_EXTENSION = re.compile(r"\.s\d+p$", re.IGNORECASE)


def _canonical_header(token: str) -> str | None:
    for canonical, aliases in HEADER_ALIASES.items():
        if token in aliases:
            return canonical
    return None


def _header_mapping(row: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for index, value in enumerate(row):
        canonical = _canonical_header(_header_token(value))
        if canonical and canonical not in mapping:
            mapping[canonical] = index
    if not REQUIRED_HEADER_KEYS.issubset(mapping):
        return None
    if "signal" not in mapping and "channel" not in mapping:
        return None
    return mapping


def _cell(row: list[str], mapping: dict[str, int], key: str) -> str:
    index = mapping.get(key)
    if index is None or index >= len(row):
        return ""
    return _clean(row[index])


def _number_string(value: str, *, line_number: int, field: str) -> str:
    try:
        number = float(value)
    except ValueError as exc:
        raise DetailedInputValidationError(
            f"detailed CSV line {line_number}: {field} must be numeric: {value!r}"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise DetailedInputValidationError(
            f"detailed CSV line {line_number}: {field} must be positive: {value!r}"
        )
    return f"{number:g}"


@dataclass(frozen=True)
class DetailedConductorRow:
    line_number: int
    snp_file: str
    designator: str
    pin: str
    interface: str
    version: str
    group: str
    polarity: str
    signal: str
    explicit_channel: str
    tdr_chart_name: str
    direction: str
    target_impedance_ohm: str
    min_spec_ohm: str
    max_spec_ohm: str
    net_name: str = ""
    ic_pin_name: str = ""

    @property
    def requested_channel(self) -> str:
        return self.explicit_channel or self.signal


@dataclass(frozen=True)
class DetailedCsvInput:
    source_csv: str
    platform: str
    rows: list[DetailedConductorRow]


@dataclass(frozen=True)
class DetailedChannelPair:
    pair_index: int
    source_lines: tuple[int, int]
    snp_file: str
    designator: str
    interface: str
    version: str
    group: str
    tdr_chart_name: str
    grouping_source: str
    channel: str
    direction: str
    pos_pin: str
    neg_pin: str
    target_impedance_ohm: str
    min_spec_ohm: str
    max_spec_ohm: str
    positive_net_name: str = ""
    negative_net_name: str = ""
    positive_ic_pin_name: str = ""
    negative_ic_pin_name: str = ""

    @property
    def logical_group(self) -> str:
        return self.tdr_chart_name

    def to_channel_target(self, *, source_csv: str) -> ChannelTarget:
        measurement_direction = (
            PATH_START_TO_ENDPOINT
            if self.direction == "TX"
            else PATH_ENDPOINT_TO_START
        )
        target = ChannelTarget(
            interface=self.interface,
            interface_version=self.version,
            channel_group=self.group,
            channel=self.channel,
            signal_type="differential",
            ic_refdes=self.designator,
            pos_pin=self.pos_pin,
            neg_pin=self.neg_pin,
            report_group=self.logical_group,
            direction=self.direction,
            measurement_direction=measurement_direction,
            measurement_direction_source="customer_detailed_csv_direction",
            measurement_direction_reason=(
                f"Direction={self.direction} maps the listed Designator/pin "
                "to the TDR measurement near/far orientation"
            ),
            reference_impedance_ohm=self.target_impedance_ohm,
            target_lower_ohm=self.min_spec_ohm,
            target_upper_ohm=self.max_spec_ohm,
            target_band_status="configured",
            target_band_reason="customer-provided detailed CSV impedance bounds",
            target_band_source="customer_detailed_csv",
            notes=(
                f"detailedCsv={source_csv}; snpFile={self.snp_file}; "
                f"group={self.group}; tdrChartName={self.tdr_chart_name}; "
                f"groupingSource={self.grouping_source}; "
                f"sourceLines={self.source_lines[0]},{self.source_lines[1]}"
            ),
        )
        # Round-trip through the existing ChannelTarget boundary so defaults such
        # as measurement direction are identical for rich and legacy CSV inputs.
        return ChannelTarget.from_row(
            asdict(target),
            line_number=self.source_lines[0],
        )


@dataclass(frozen=True)
class DetailedSnpBatch:
    snp_file: str
    pairs: list[DetailedChannelPair]
    targets: list[ChannelTarget]

    def to_dict(self) -> dict[str, Any]:
        first_pair = self.pairs[0] if self.pairs else None
        return {
            "snpFile": self.snp_file,
            "groupingSource": (
                first_pair.grouping_source if first_pair is not None else None
            ),
            "groupingKey": (
                {
                    "function": first_pair.interface,
                    "version": first_pair.version or None,
                    "designator": first_pair.designator,
                    "group": first_pair.group,
                    "direction": first_pair.direction,
                }
                if first_pair is not None
                else None
            ),
            "tdrReportName": (
                first_pair.tdr_chart_name if first_pair is not None else None
            ),
            "pairIndexes": [pair.pair_index for pair in self.pairs],
            "targetNames": [target.name for target in self.targets],
            "pairCount": len(self.pairs),
            "targetCount": len(self.targets),
        }


@dataclass(frozen=True)
class DetailedNormalizationResult:
    source_csv: str
    platform: str
    rows: list[DetailedConductorRow]
    pairs: list[DetailedChannelPair]
    targets: list[ChannelTarget]
    batches: list[DetailedSnpBatch]
    records: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceCsv": self.source_csv,
            "platform": self.platform or None,
            "summary": {
                "conductorRows": len(self.rows),
                "pairRecords": len(self.records),
                "validPairs": len(self.pairs),
                "normalizedTargets": len(self.targets),
                "unresolvedPairs": len(self.unresolved),
                "snpFiles": len(self.batches),
            },
            "snpBatches": [batch.to_dict() for batch in self.batches],
            "records": self.records,
            "unresolved": self.unresolved,
            "normalizedTargets": [asdict(target) for target in self.targets],
        }

    def batch_for(self, snp_file: str) -> DetailedSnpBatch:
        requested = _clean(snp_file).casefold()
        for batch in self.batches:
            if batch.snp_file.casefold() == requested:
                return batch
        available = [batch.snp_file for batch in self.batches]
        raise DetailedInputValidationError(
            f"detailed CSV has no sNp batch={snp_file!r}; available={available}"
        )


def load_detailed_csv(path: Path) -> DetailedCsvInput:
    rows: list[DetailedConductorRow] = []
    platform = ""
    active_header: dict[str, int] | None = None
    saw_header = False

    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.reader(fp)
        for line_number, raw_row in enumerate(reader, start=1):
            row = [_clean(value) for value in raw_row]
            if not any(row):
                continue

            header = _header_mapping(row)
            if header is not None:
                active_header = header
                saw_header = True
                continue

            if active_header is None:
                if not platform:
                    platform = next((value for value in row if value), "")
                continue

            input_values = {
                key: _cell(row, active_header, key)
                for key in set(active_header).difference({"net_name", "ic_pin_name"})
            }
            if not any(input_values.values()):
                continue

            missing = [
                key for key in sorted(REQUIRED_ROW_KEYS) if not input_values.get(key)
            ]
            signal = input_values.get("signal", "")
            explicit_channel = input_values.get("channel", "")
            if not signal and not explicit_channel:
                missing.append("signal_or_channel")
            if missing:
                raise DetailedInputValidationError(
                    f"detailed CSV line {line_number}: missing required values: {missing}"
                )

            rows.append(
                DetailedConductorRow(
                    line_number=line_number,
                    snp_file=input_values.get("snp_file", ""),
                    designator=input_values["designator"],
                    pin=input_values["pin"],
                    interface=input_values["interface"],
                    version=input_values.get("version", ""),
                    group=input_values.get("group", ""),
                    polarity=input_values["polarity"].upper(),
                    signal=signal.upper(),
                    explicit_channel=explicit_channel.upper(),
                    tdr_chart_name=input_values.get("tdr_chart_name", ""),
                    direction=input_values["direction"].upper(),
                    target_impedance_ohm=_number_string(
                        input_values["target"],
                        line_number=line_number,
                        field="Target_Impedance",
                    ),
                    min_spec_ohm=_number_string(
                        input_values["minimum"],
                        line_number=line_number,
                        field="Min_Spec",
                    ),
                    max_spec_ohm=_number_string(
                        input_values["maximum"],
                        line_number=line_number,
                        field="Max_Spec",
                    ),
                    net_name=_cell(row, active_header, "net_name"),
                    ic_pin_name=_cell(row, active_header, "ic_pin_name"),
                )
            )

    if not saw_header:
        raise DetailedInputValidationError(f"detailed CSV has no supported header: {path}")
    if not rows:
        raise DetailedInputValidationError(f"detailed CSV has no target rows: {path}")
    return DetailedCsvInput(source_csv=str(path), platform=platform, rows=rows)


def _pair_issue(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **evidence}


def _same_value_issue(
    positive: DetailedConductorRow,
    negative: DetailedConductorRow,
    *,
    attribute: str,
    label: str,
) -> dict[str, Any] | None:
    first = getattr(positive, attribute)
    second = getattr(negative, attribute)
    if first == second:
        return None
    return _pair_issue(
        "pair_field_mismatch",
        f"paired rows have different {label}",
        field=label,
        values=[first, second],
    )


def _resolve_output_grouping(
    row: DetailedConductorRow,
) -> tuple[str, str, str, str, list[dict[str, Any]]]:
    """Resolve customer CSV grouping into the existing internal batch boundary.

    The final customer contract derives one sNp batch from
    Function + Version + Designator + Group + Direction and uses Group as the
    single report name inside that batch. The earlier explicit
    SNP_File/TDR_Chart_Name proposal remains a compatibility input contract.
    """

    issues: list[dict[str, Any]] = []
    has_explicit_snp = bool(row.snp_file)
    has_explicit_chart = bool(row.tdr_chart_name)
    if has_explicit_snp or has_explicit_chart:
        if not (has_explicit_snp and has_explicit_chart):
            issues.append(
                _pair_issue(
                    "incomplete_explicit_grouping",
                    "legacy explicit grouping requires both SNP_File and TDR_Chart_Name",
                    values={
                        "SNP_File": row.snp_file,
                        "TDR_Chart_Name": row.tdr_chart_name,
                    },
                )
            )
            return "", "", "", "legacy_explicit_columns", issues
        return (
            row.snp_file,
            row.tdr_chart_name,
            row.group or row.tdr_chart_name,
            "legacy_explicit_columns",
            issues,
        )

    if not row.group:
        issues.append(
            _pair_issue(
                "group_missing",
                "Group is required to derive the sNp and TDR output unit",
            )
        )
        return (
            "",
            "",
            "",
            "function_version_designator_group_direction",
            issues,
        )

    grouping_values = [
        row.interface,
        row.version,
        row.designator,
        row.group,
        row.direction,
    ]
    invalid_values = [
        value
        for value in grouping_values
        if value and SAFE_OUTPUT_NAME.fullmatch(value) is None
    ]
    if invalid_values:
        issues.append(
            _pair_issue(
                "invalid_grouping_value",
                "Function, Version, Designator, Group, and Direction must use "
                "only letters, numbers, dot, underscore, and hyphen",
                values=invalid_values,
            )
        )
        return (
            "",
            "",
            row.group,
            "function_version_designator_group_direction",
            issues,
        )

    snp_file = "_".join(value for value in grouping_values if value)
    return (
        snp_file,
        row.group,
        row.group,
        "function_version_designator_group_direction",
        issues,
    )


def normalize_detailed_input(csv_input: DetailedCsvInput) -> DetailedNormalizationResult:
    records: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    pairs: list[DetailedChannelPair] = []
    candidate_targets: list[tuple[int, DetailedChannelPair, ChannelTarget]] = []

    for offset in range(0, len(csv_input.rows), 2):
        pair_index = offset // 2 + 1
        if offset + 1 >= len(csv_input.rows):
            row = csv_input.rows[offset]
            record = {
                "pairIndex": pair_index,
                "sourceLines": [row.line_number],
                "status": "unresolved",
                "issues": [
                    _pair_issue(
                        "orphan_conductor_row",
                        "the final conductor row has no adjacent pair row",
                    )
                ],
            }
            records.append(record)
            unresolved.append(record)
            continue

        first = csv_input.rows[offset]
        second = csv_input.rows[offset + 1]
        source_lines = [first.line_number, second.line_number]
        issues: list[dict[str, Any]] = []

        polarity_rows = {first.polarity: first, second.polarity: second}
        if set(polarity_rows) != {"P", "M"}:
            issues.append(
                _pair_issue(
                    "invalid_pair_polarity",
                    "paired rows must contain exactly one P and one M polarity",
                    values=[first.polarity, second.polarity],
                )
            )

        for attribute, label in (
            ("snp_file", "SNP_File"),
            ("designator", "Designator"),
            ("interface", "Function"),
            ("version", "Version"),
            ("group", "Group"),
            ("tdr_chart_name", "TDR_Chart_Name"),
            ("direction", "Direction"),
            ("target_impedance_ohm", "Target_Impedance"),
            ("min_spec_ohm", "Min_Spec"),
            ("max_spec_ohm", "Max_Spec"),
        ):
            issue = _same_value_issue(first, second, attribute=attribute, label=label)
            if issue:
                issues.append(issue)

        first_channel = first.requested_channel
        second_channel = second.requested_channel
        if first_channel != second_channel:
            issues.append(
                _pair_issue(
                    "pair_field_mismatch",
                    "paired rows have different Channel/Lane values",
                    field="Channel",
                    values=[first_channel, second_channel],
                )
            )

        if first.direction not in {"TX", "RX"} or second.direction not in {"TX", "RX"}:
            issues.append(
                _pair_issue(
                    "invalid_direction",
                    "Direction must be TX or RX relative to the listed Designator/pin",
                    values=[first.direction, second.direction],
                )
            )

        if first.pin == second.pin:
            issues.append(
                _pair_issue(
                    "duplicate_pair_pin",
                    "paired rows must use different pins",
                    pin=first.pin,
                )
            )

        if not issues:
            lower = float(first.min_spec_ohm)
            target = float(first.target_impedance_ohm)
            upper = float(first.max_spec_ohm)
            if not lower <= target <= upper:
                issues.append(
                    _pair_issue(
                        "invalid_impedance_bounds",
                        "impedance bounds must satisfy Min_Spec <= Target_Impedance <= Max_Spec",
                        values={"minimum": lower, "target": target, "maximum": upper},
                    )
                )

        if issues:
            record = {
                "pairIndex": pair_index,
                "sourceLines": source_lines,
                "status": "unresolved",
                "issues": issues,
            }
            records.append(record)
            unresolved.append(record)
            continue

        positive = polarity_rows["P"]
        negative = polarity_rows["M"]
        channel = positive.requested_channel
        (
            snp_file,
            tdr_chart_name,
            group,
            grouping_source,
            grouping_issues,
        ) = _resolve_output_grouping(positive)
        pair = DetailedChannelPair(
            pair_index=pair_index,
            source_lines=(positive.line_number, negative.line_number),
            snp_file=snp_file,
            designator=positive.designator,
            interface=positive.interface,
            version=positive.version,
            group=group,
            tdr_chart_name=tdr_chart_name,
            grouping_source=grouping_source,
            channel=channel,
            direction=positive.direction,
            pos_pin=positive.pin,
            neg_pin=negative.pin,
            target_impedance_ohm=positive.target_impedance_ohm,
            min_spec_ohm=positive.min_spec_ohm,
            max_spec_ohm=positive.max_spec_ohm,
            positive_net_name=positive.net_name,
            negative_net_name=negative.net_name,
            positive_ic_pin_name=positive.ic_pin_name,
            negative_ic_pin_name=negative.ic_pin_name,
        )
        pairs.append(pair)

        mapping_issues: list[dict[str, Any]] = list(grouping_issues)
        if not pair.snp_file:
            if not grouping_issues:
                mapping_issues.append(
                    _pair_issue(
                        "snp_file_missing",
                        "an internal sNp batch name could not be resolved",
                    )
                )
        elif TOUCHSTONE_EXTENSION.search(pair.snp_file):
            mapping_issues.append(
                _pair_issue(
                    "snp_file_extension_not_allowed",
                    "SNP_File must omit the generated .sNp extension",
                    value=pair.snp_file,
                )
            )
        elif SAFE_OUTPUT_NAME.fullmatch(pair.snp_file) is None:
            mapping_issues.append(
                _pair_issue(
                    "invalid_snp_file",
                    "SNP_File must use only letters, numbers, dot, underscore, and hyphen",
                    value=pair.snp_file,
                )
            )
        if not pair.tdr_chart_name:
            if not grouping_issues:
                mapping_issues.append(
                    _pair_issue(
                        "tdr_chart_name_missing",
                        "an internal TDR report name could not be resolved",
                    )
                )
        elif SAFE_OUTPUT_NAME.fullmatch(pair.tdr_chart_name) is None:
            mapping_issues.append(
                _pair_issue(
                    "invalid_tdr_chart_name",
                    "TDR_Chart_Name must use only letters, numbers, dot, underscore, and hyphen",
                    value=pair.tdr_chart_name,
                )
            )
        if SAFE_OUTPUT_NAME.fullmatch(pair.channel) is None:
            mapping_issues.append(
                _pair_issue(
                    "invalid_channel_name",
                    "Channel_Name is required and must use only letters, numbers, "
                    "dot, underscore, and hyphen",
                    value=pair.channel,
                )
            )

        if mapping_issues:
            record = {
                "pairIndex": pair_index,
                "sourceLines": source_lines,
                "status": "unresolved",
                "pair": asdict(pair),
                "issues": mapping_issues,
            }
            records.append(record)
            unresolved.append(record)
            continue

        target = pair.to_channel_target(source_csv=csv_input.source_csv)
        candidate_targets.append((pair_index, pair, target))
        records.append(
            {
                "pairIndex": pair_index,
                "sourceLines": source_lines,
                "status": "normalized",
                "pair": asdict(pair),
                "normalizedTarget": asdict(target),
            }
        )

    duplicate_names = {
        key
        for key, count in Counter(
            (pair.snp_file.casefold(), target.name.casefold())
            for _, pair, target in candidate_targets
        ).items()
        if count > 1
    }
    targets: list[ChannelTarget] = []
    accepted_candidates: list[tuple[int, DetailedChannelPair, ChannelTarget]] = []
    if duplicate_names:
        duplicate_pair_indexes = {
            pair_index
            for pair_index, pair, target in candidate_targets
            if (pair.snp_file.casefold(), target.name.casefold()) in duplicate_names
        }
        duplicate_evidence_by_pair = {
            pair_index: {"snpFile": pair.snp_file, "targetName": target.name}
            for pair_index, pair, target in candidate_targets
            if (pair.snp_file.casefold(), target.name.casefold()) in duplicate_names
        }
        for record in records:
            if record["pairIndex"] not in duplicate_pair_indexes:
                continue
            record["status"] = "unresolved"
            record["issues"] = [
                _pair_issue(
                    "duplicate_normalized_channel",
                    "multiple detailed CSV pairs in one sNp batch normalize to the same channel",
                    **duplicate_evidence_by_pair[record["pairIndex"]],
                )
            ]
            record.pop("normalizedTarget", None)
            unresolved.append(record)
        accepted_candidates = [
            candidate
            for candidate in candidate_targets
            if (
                candidate[1].snp_file.casefold(),
                candidate[2].name.casefold(),
            )
            not in duplicate_names
        ]
        targets = [target for _, _, target in accepted_candidates]
    else:
        accepted_candidates = candidate_targets
        targets = [target for _, _, target in candidate_targets]

    batch_builders: dict[str, dict[str, Any]] = {}
    for _, pair, target in accepted_candidates:
        batch_key = pair.snp_file.casefold()
        batch = batch_builders.setdefault(
            batch_key,
            {"snp_file": pair.snp_file, "pairs": [], "targets": []},
        )
        batch["pairs"].append(pair)
        batch["targets"].append(target)
    batches = [
        DetailedSnpBatch(
            snp_file=batch["snp_file"],
            pairs=batch["pairs"],
            targets=batch["targets"],
        )
        for batch in batch_builders.values()
    ]

    return DetailedNormalizationResult(
        source_csv=csv_input.source_csv,
        platform=csv_input.platform,
        rows=csv_input.rows,
        pairs=pairs,
        targets=targets,
        batches=batches,
        records=records,
        unresolved=unresolved,
    )


def normalize_detailed_csv(path: Path) -> DetailedNormalizationResult:
    return normalize_detailed_input(load_detailed_csv(path))


def write_detailed_normalization_report(
    result: DetailedNormalizationResult,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_detailed_channel_targets_csv(
    result: DetailedNormalizationResult,
    output_path: Path,
    *,
    snp_file: str | None = None,
) -> None:
    targets = result.targets if snp_file is None else result.batch_for(snp_file).targets
    fieldnames = [*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for target in targets:
            writer.writerow(asdict(target))
