"""Electrical PASS/NG at the existing native report's central X marker.

The validated waveform CSV is sampled at the displayed midpoint. Exact samples
are preferred; otherwise linear interpolation between adjacent samples is used.
No extrapolation, rounding before comparison, or whole-waveform extrema test.
"""
from bisect import bisect_left
import csv
import hashlib
import math
from pathlib import Path


def finite(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a marker/spec value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("marker/spec value must be finite")
    return result


def evaluate_center_markers(record, waveform: Path, channels):
    names = record["traceNames"]
    if not names or len(set(names)) != len(names):
        raise ValueError("marker evaluation requires unique traces")
    with waveform.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows.pop(0) != ["Time [ps]", *[f"{n} [ohm]" for n in names]]:
        raise ValueError("marker waveform header differs")
    if len(rows) != record["sampleCount"] or not rows:
        raise ValueError("marker waveform sample count differs")
    data = []
    for row in rows:
        if len(row) != len(names) + 1:
            raise ValueError("marker waveform column count differs")
        data.append([finite(v) for v in row])
    times = [r[0] for r in data]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("marker waveform time must increase")
    configured = {c["name"]: c for c in channels}
    if not configured or len(configured) != len(channels):
        raise ValueError("marker channels must be unique and nonempty")
    digest = hashlib.sha256(waveform.read_bytes()).hexdigest()
    result = {}
    for report in record["reports"]:
        native = report["strictNativeReport"]
        bounds = native["view"]["xAxisPs"]
        x0, x1 = finite(bounds["min"]), finite(bounds["max"])
        marker = native["xMarker"]
        x = finite(marker["xPs"])
        if x0 >= x1 or x != (x0 + x1) / 2 or marker["count"] != 1:
            raise ValueError("existing central marker evidence differs")
        if not times[0] <= x <= times[-1]:
            raise ValueError("marker is outside solved waveform; no extrapolation")
        index = bisect_left(times, x)
        exact = times[index] == x
        report_channels, traces = report["channels"], report["traceNames"]
        if len(report_channels) != len(traces) or not traces:
            raise ValueError("marker report channel/trace mapping differs")
        for channel, trace in zip(report_channels, traces):
            if channel in result or channel not in configured or trace not in names:
                raise ValueError("duplicate/unknown marker channel or trace")
            column = names.index(trace) + 1
            value = data[index][column] if exact else (
                data[index - 1][column] + (data[index][column] - data[index - 1][column])
                * (x - times[index - 1]) / (times[index] - times[index - 1]))
            value = finite(value)
            limits = configured[channel].get("targetRangeOhm") or configured[channel].get("targetBandOhm")
            low, high = finite(limits["lower"]), finite(limits["upper"])
            if low > high:
                raise ValueError("invalid Target Range")
            result[channel] = {
                "evaluationStatus": "PASS" if low <= value <= high else "NG",
                "evaluationReason": "center-marker-inclusive-target-range",
                "MarkerTimePs": x, "MarkerOhm": value,
                "markerEvaluation": {"schema": "si-tdr-center-marker-evaluation/1",
                    "reportName": report["name"], "traceName": trace,
                    "waveformSha256": digest,
                    "sampling": "exact-sample" if exact else "linear-interpolation",
                    "lowerOhm": low, "upperOhm": high, "boundsInclusive": True},
            }
    if set(result) != set(configured):
        raise ValueError("marker evaluation is missing configured channels")
    return result


def aggregate_status(rows):
    values = [r["evaluationStatus"] for r in rows]
    if not values or any(v not in {"PASS", "NG"} for v in values):
        raise ValueError("incomplete electrical evaluation")
    return "NG" if "NG" in values else "PASS"
