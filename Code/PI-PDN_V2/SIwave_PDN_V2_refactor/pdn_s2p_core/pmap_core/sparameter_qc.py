import math
import re
from pathlib import Path
from typing import Any

from pdn_s2p_core.config import MODEL_FILE_ENCODING


_SCALE_MAP = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}


def _freq_to_hz(value: float, unit: str) -> float:
    return value * _SCALE_MAP.get(unit.lower(), 1.0)


def _extract_snp_port_count(path: Path) -> int:
    match = re.search(r"\.s(\d+)p$", path.name.lower())
    return int(match.group(1)) if match else 0


def _flag_enabled_in_comments(comment_blob: str, keyword: str) -> bool:
    patterns = (
        rf"\b{keyword}\b[^\n\r]*\b(on|true|enabled|enforced|pass|passed)\b",
        rf"\b{keyword}_(on|enabled|enforced)\b",
    )
    lowered = comment_blob.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _pair_to_complex(fmt: str, first: float, second: float) -> complex:
    if fmt == "ri":
        return complex(float(first), float(second))

    phase_rad = math.radians(float(second))
    if fmt == "ma":
        mag = float(first)
    elif fmt == "db":
        mag = 10 ** (float(first) / 20.0)
    else:
        raise ValueError(f"unsupported_touchstone_format({fmt})")

    return complex(mag * math.cos(phase_rad), mag * math.sin(phase_rad))


def read_s2p_qc_metadata(
    s2p_path: Path,
    *,
    dc_imag_tolerance: float,
    low_freq_region_max_hz: float = 5_000_000,
) -> dict[str, Any] | None:
    if not s2p_path.exists():
        return None

    port_count = _extract_snp_port_count(s2p_path)
    if port_count <= 0:
        return None

    option_unit = "hz"
    option_param = "s"
    option_format = "ri"
    option_reference = None
    values_per_point = 1 + (2 * (port_count**2))
    data_rows: list[list[float]] = []
    comments: list[str] = []
    line_shape_ok = True

    try:
        with open(s2p_path, "r", encoding=MODEL_FILE_ENCODING, errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue

                if "!" in line:
                    comment = line.split("!", 1)[1].strip()
                    if comment:
                        comments.append(comment)

                no_comment = line.split("!", 1)[0].strip()
                if not no_comment:
                    continue

                if no_comment.startswith("#"):
                    parts = no_comment[1:].strip().lower().split()
                    if parts:
                        option_unit = parts[0]
                    if len(parts) >= 2:
                        option_param = parts[1]
                    if len(parts) >= 3:
                        option_format = parts[2]
                    if "r" in parts:
                        idx = parts.index("r")
                        if idx + 1 < len(parts):
                            try:
                                option_reference = float(parts[idx + 1])
                            except ValueError:
                                option_reference = None
                    continue

                numeric_row = []
                for token in no_comment.split():
                    try:
                        numeric_row.append(float(token))
                    except ValueError:
                        line_shape_ok = False
                if len(numeric_row) != values_per_point:
                    line_shape_ok = False
                    continue
                data_rows.append(numeric_row)
    except OSError:
        return None

    if not data_rows:
        return None

    min_freq_hz = float("inf")
    max_freq_hz = -1.0
    points = 0
    monotonic = True
    prev_freq_hz = None

    has_dc_point = False
    dc_imag_max = 0.0
    low_freq_point_count = 0

    first_non_dc_row = None
    dc_row = None

    try:
        fmt = option_format.lower()
        for row in data_rows:
            freq_hz = _freq_to_hz(row[0], option_unit)
            if prev_freq_hz is not None and freq_hz < prev_freq_hz:
                monotonic = False
            prev_freq_hz = freq_hz

            min_freq_hz = min(min_freq_hz, freq_hz)
            max_freq_hz = max(max_freq_hz, freq_hz)
            points += 1

            if freq_hz <= low_freq_region_max_hz:
                low_freq_point_count += 1

            if abs(freq_hz) <= dc_imag_tolerance:
                has_dc_point = True
                dc_row = row
                for pair_idx in range(1, values_per_point, 2):
                    c = _pair_to_complex(fmt, row[pair_idx], row[pair_idx + 1])
                    dc_imag_max = max(dc_imag_max, abs(c.imag))
            elif first_non_dc_row is None:
                first_non_dc_row = row

        max_lowfreq_mag_delta = 0.0
        if dc_row is not None and first_non_dc_row is not None:
            for pair_idx in range(1, values_per_point, 2):
                dc_c = _pair_to_complex(fmt, dc_row[pair_idx], dc_row[pair_idx + 1])
                lf_c = _pair_to_complex(fmt, first_non_dc_row[pair_idx], first_non_dc_row[pair_idx + 1])
                max_lowfreq_mag_delta = max(max_lowfreq_mag_delta, abs(abs(lf_c) - abs(dc_c)))
    except (ValueError, OverflowError):
        return None

    if points <= 0 or min_freq_hz == float("inf"):
        return None

    comment_blob = "\n".join(comments)
    return {
        "ports": port_count,
        "min_freq_hz": min_freq_hz,
        "max_freq_hz": max_freq_hz,
        "points": points,
        "monotonic": monotonic,
        "has_dc_point": has_dc_point,
        "dc_imag_max": dc_imag_max,
        "option_param": option_param,
        "data_format": option_format.lower(),
        "reference_ohm": option_reference,
        "low_freq_point_count": low_freq_point_count,
        "line_shape_ok": line_shape_ok,
        "max_lowfreq_mag_delta": max_lowfreq_mag_delta,
        "has_passivity_tag": _flag_enabled_in_comments(comment_blob, "passivity"),
        "has_causality_tag": _flag_enabled_in_comments(comment_blob, "causality"),
        "has_reciprocity_tag": _flag_enabled_in_comments(comment_blob, "reciprocity"),
    }

def classify_s2p_issue(s2p_path: Path) -> str:
    if not s2p_path.exists():
        return "missing_sparameter_file"

    has_option_line = False
    has_numeric_data = False
    parse_failed = False

    try:
        with open(s2p_path, "r", encoding=MODEL_FILE_ENCODING, errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                no_comment = line.split("!", 1)[0].strip()
                if not no_comment:
                    continue
                if no_comment.startswith("#"):
                    has_option_line = True
                    continue
                try:
                    tokens = [float(token) for token in no_comment.split()]
                    if tokens:
                        has_numeric_data = True
                except ValueError:
                    parse_failed = True
    except OSError:
        return "missing_or_invalid_sparameter"

    if has_option_line and (not has_numeric_data):
        return "header_only_sparameter"
    if parse_failed and (not has_numeric_data):
        return "invalid_numeric_tokens"
    if not has_numeric_data:
        return "missing_or_invalid_sparameter"
    return "missing_or_invalid_sparameter"
