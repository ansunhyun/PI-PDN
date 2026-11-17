"""특성 기반 후보 비교. 제조사 요청·파일 승인·GUI와 독립적인 PDN 판정이다."""
from __future__ import annotations

import csv
import json
import hashlib
import math
from pathlib import Path

from .netlist import rlc_impedance_samples, validate_rlc_sidecar


def _impedance_curve(path, frequencies, impedances):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['frequency_hz', 'z_real_ohm', 'z_imag_ohm', 'esr_ohm'])
        writer.writerows((f, z.real, z.imag, z.real) for f, z in zip(frequencies, impedances))
    return str(path)


def _s2p_impedances(path, frequencies):
    # 공식 직렬 2단자 모델과 RLC의 네 S값 대조가 통과한 파일에 적용한다.
    # 데이터 제공 대역 안에서만 보간한다. 원본 S2P를 생성/변경하지 않는다.
    import numpy as np
    import skrf
    network = skrf.Network(str(path))
    mask = network.f > 0
    fs = network.f[mask]
    if not len(fs) or frequencies[0] < fs[0] or frequencies[-1] > fs[-1]:
        raise ValueError('s2p_interpolation_outside_provided_band')
    if not np.allclose(network.z0[:, 0], network.z0[:, 1]):
        raise ValueError('series_s2p_requires_equal_port_reference_impedances')
    s21 = network.s[mask, 1, 0]
    reference = network.z0[mask, 0]
    if np.any(np.abs(s21) == 0):
        raise ValueError('series_s2p_zero_transmission')
    impedance = 2 * reference * (1 - s21) / s21
    xs, targets = np.log(fs), np.log(frequencies)
    return list(np.interp(targets, xs, impedance.real) + 1j * np.interp(targets, xs, impedance.imag))


def _trace(path: Path, low: float, high: float):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = [(float(r["frequency_hz"]), complex(float(r["z_real_ohm"]), float(r["z_imag_ohm"])))
                for r in csv.DictReader(stream)]
    if (len(rows) < 3 or any(not all(math.isfinite(v) for v in (f, z.real, z.imag)) or f <= 0 for f, z in rows)
            or any(a[0] >= b[0] for a, b in zip(rows, rows[1:]))
            or rows[0][0] > low or rows[-1][0] < high):
        raise ValueError("pdn_trace_invalid_or_incomplete_band")
    # 한계 주파수 바깥의 큰 값이 필터링되어 통과하지 않도록 경계점도 요구한다.
    if not any(f == low for f, _ in rows) or not any(f == high for f, _ in rows):
        raise ValueError("pdn_trace_band_boundary_samples_required")
    band = [(f, abs(z)) for f, z in rows if low <= f <= high]
    if len(band) < 3:
        raise ValueError("pdn_trace_insufficient_band_points")
    maximum = max(band, key=lambda point: point[1])
    peaks = [b for a, b, c in zip(band, band[1:], band[2:]) if b[1] > a[1] and b[1] >= c[1]]
    return {"points": len(band), "maxImpedanceOhm": maximum[1], "maxFrequencyHz": maximum[0],
            "antiresonancePeaks": [{"frequencyHz": f, "impedanceOhm": z} for f, z in peaks],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "path": str(path)}


def model_coverage_reasons(variant: dict, policy: dict) -> list[str]:
    """파일 쌍의 조건·대역 적합성. 보드 PDN 결과의 승인과는 구분한다."""
    low, high = policy.get('frequencyRangeHz', [100_000, 1_000_000_000])
    reasons = []
    if any(variant['conditions'].get(k) is None for k in ('dcBiasV', 'temperatureC')):
        reasons.append('official_model_bias_temperature_not_stated')
    sidecar = Path(variant['rlc']).with_suffix('.rlc.source.json')
    source = json.loads(sidecar.read_text(encoding='utf-8')) if sidecar.exists() else {}
    derived = source.get('sourceType') == 'official_s2p_derived_rlc'
    missing_low = variant['frequencyRangeHz'][0] > low
    separate = missing_low and policy.get('allowSeparateBandAnalysis', False) and not derived
    if (missing_low and not separate) or variant['frequencyRangeHz'][1] < high:
        reasons.append(f"candidate_model_band_incomplete; available={variant['frequencyRangeHz']}; required={[low, high]}")
    if derived and missing_low:
        reasons.append('derived_rlc_extrapolation_not_allowed')
    return reasons


def candidate_model_coverage_reasons(candidate: dict, policy: dict) -> list[str]:
    """동일 제조사 우선 검색을 종료할 수 있는 모델인지 확인한다."""
    reasons = []
    valid = []
    for variant in candidate['variants']:
        issues = model_coverage_reasons(variant, policy)
        reasons.extend(issues)
        if not issues:
            valid.append(variant)
    if not valid:
        reasons.append('no_condition_and_band_qualified_model')
    for case in policy.get('cases', []):
        conditions = {'dcBiasV': case.get('voltageV'), 'temperatureC': case.get('temperatureC')}
        if not any(v['conditions'] == conditions for v in valid):
            reasons.append(f"{case.get('rail', 'unassigned')}: operating_condition_model_required({conditions})")
    return reasons


def compare_characteristic_candidates(candidates: list[dict], policy: dict, output: Path) -> dict:
    """각 사용 조건의 공식 모델과 해당 모델로 해석한 PDN 곡선을 모두 요구한다.

    조건별 후보 전체가 한계를 통과하면 최대 임피던스 여유가 가장 작은 후보를
    보수적으로 선택한다. 누락 조건·모델 또는 후보에 따라 PASS/FAIL이 바뀌면 보류한다.
    """
    low, high = policy.get("frequencyRangeHz", [100_000, 1_000_000_000])
    output.mkdir(parents=True, exist_ok=True)
    target, peak_limit = policy.get("targetImpedanceOhm", 10), policy.get("peakLimitOhm", 10)
    if not (0 < low < high and all(math.isfinite(v) and v > 0 for v in (low, high, target, peak_limit))):
        raise ValueError("invalid_pdn_limits")
    result = {"status": "pending", "method": "all_candidate_all_operating_case_pdn_sweep",
              "frequencyRangeHz": [low, high], "targetImpedanceOhm": target, "peakLimitOhm": peak_limit,
              "candidates": [], "cases": policy.get("cases", []), "reasons": []}
    curves = []
    for candidate in candidates:
        entry = {"pn": candidate["pn"], "manufacturer": candidate["manufacturer"], "models": [], "pdnResults": []}
        for variant in candidate["variants"]:
            try:
                rlc_source = json.loads(Path(variant['rlc']).with_suffix('.rlc.source.json').read_text(encoding='utf-8')) if Path(variant['rlc']).with_suffix('.rlc.source.json').exists() else {}
                derived = rlc_source.get('sourceType') == 'official_s2p_derived_rlc'
                result['reasons'].extend(f"{candidate['pn']}: {reason}" for reason in model_coverage_reasons(variant, policy))
                missing_low = variant['frequencyRangeHz'][0] > low
                separate_bands = missing_low and policy.get('allowSeparateBandAnalysis', False) and not derived
                start, end = max(low, variant["frequencyRangeHz"][0]), min(high, variant["frequencyRangeHz"][1])
                if start >= end:
                    raise ValueError("candidate_model_has_no_comparison_band")
                model_frequencies = [start * (end / start) ** (i / 400) for i in range(401)]
                model_frequencies[0], model_frequencies[-1] = start, end
                zs = rlc_impedance_samples(variant["rlc"].read_bytes(), model_frequencies)
                resonance = min(zip(model_frequencies, zs), key=lambda pair: abs(pair[1]))
                model = {"conditions": variant["conditions"], "s2pSha256": variant["s2pSha256"],
                         "rlcSha256": variant["rlcSha256"], "minImpedanceFrequencyHz": resonance[0],
                         "availableFrequencyRangeHz": variant["frequencyRangeHz"], "comparisonFrequencyRangeHz": [start, end],
                         "impedanceAtMinimumOhm": abs(resonance[1]), "esrAtMinimumOhm": resonance[1].real}
                model['s2pProvidedFrequencyRangeHz'] = variant['frequencyRangeHz']
                model['rlcSourceType'] = rlc_source.get('sourceType', 'unrecorded')
                stem = f"{candidate['pn']}-v{variant['conditions']['dcBiasV']}-t{variant['conditions']['temperatureC']}"
                model['s2pComparisonCurve'] = _impedance_curve(output / f'{stem}-s2p.csv', model_frequencies,
                                                              _s2p_impedances(variant['s2p'], model_frequencies))
                model['analysisBandBinding'] = {'mode': 's2p_provided_band', 'frequencyRangeHz': [low, high]}
                if separate_bands:
                    overlap = validate_rlc_sidecar(Path(variant['s2p']), Path(variant['rlc']))
                    if overlap is None:
                        raise ValueError('official_rlc_overlap_validation_required_for_low_band')
                    low_frequencies = [low * (start / low) ** (i / 400) for i in range(401)]
                    low_frequencies[0], low_frequencies[-1] = low, start
                    model['rlcLowFrequencyCurve'] = _impedance_curve(output / f'{stem}-rlc-low.csv', low_frequencies,
                                         rlc_impedance_samples(variant['rlc'].read_bytes(), low_frequencies))
                    model['rlcLowFrequencySourceType'] = 'official_netlist_circuit_evaluation_outside_s2p_band'
                    model['rlcOverlapValidation'] = overlap
                    model['analysisBandBinding'] = {'mode': 'rlc_low_s2p_high', 'frequencyRangeHz': [low, high],
                        'transitionFrequencyHz': start, 's2pProvidedFrequencyRangeHz': variant['frequencyRangeHz']}
                if zs[0].imag < 0:
                    model["effectiveCapacitanceAtBandStartF"] = -1 / (2 * math.pi * start * zs[0].imag)
                    model["effectiveCapacitanceFrequencyHz"] = start
                curve = output / f"{candidate['pn']}-v{variant['conditions']['dcBiasV']}-t{variant['conditions']['temperatureC']}.csv"
                with curve.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["frequency_hz", "z_real_ohm", "z_imag_ohm", "esr_ohm"])
                    writer.writerows((f, z.real, z.imag, z.real) for f, z in zip(model_frequencies, zs))
                model["comparisonCurve"] = str(curve)
                entry["models"].append(model)
                curves.append((candidate["pn"], variant["conditions"], model_frequencies, variant["rlc"]))
            except (ValueError, OSError) as error:
                result["reasons"].append(f"{candidate['pn']}: {error}")
        # 조건별 공식 모델로 계산한 용량 지표이다. DC0V의 단순 축척은 사용하지 않는다.
        reference = next((m for m in entry["models"] if m["conditions"]["dcBiasV"] == 0), None)
        if reference and "effectiveCapacitanceAtBandStartF" in reference:
            for model in entry["models"]:
                if (model["conditions"]["temperatureC"] == reference["conditions"]["temperatureC"]
                        and model.get("effectiveCapacitanceFrequencyHz") == reference.get("effectiveCapacitanceFrequencyHz")
                        and "effectiveCapacitanceAtBandStartF" in model):
                    model["capacitanceRatioVs0VAtBandStart"] = model["effectiveCapacitanceAtBandStartF"] / reference["effectiveCapacitanceAtBandStartF"]
        for case in result["cases"]:
            label = case.get("rail", "unassigned")
            conditions = {"dcBiasV": case.get("voltageV"), "temperatureC": case.get("temperatureC")}
            matches = [m for m in entry["models"] if m["conditions"] == conditions]
            if len(matches) != 1:
                result["reasons"].append(f"{candidate['pn']}/{label}: operating_condition_model_required({conditions}); DC0V_not_extrapolated")
                continue
            model = matches[0]
            proof = case.get("candidateResults", {}).get(candidate["pn"], {})
            if any(proof.get(key) != model[key] for key in ("s2pSha256", "rlcSha256")):
                result["reasons"].append(f"{candidate['pn']}/{label}: pdn_result_for_exact_model_hashes_required")
                continue
            if (model['analysisBandBinding']['mode'] == 'rlc_low_s2p_high'
                    and proof.get('analysisBandBinding') != model['analysisBandBinding']):
                result['reasons'].append(f"{candidate['pn']}/{label}: separate_band_pdn_result_binding_required")
                continue
            try:
                trace = _trace(Path(proof["pdnCsv"]), low, high)
                peak = max([p["impedanceOhm"] for p in trace["antiresonancePeaks"]] or [0])
                passed = trace["maxImpedanceOhm"] < target and peak < peak_limit
                entry["pdnResults"].append({"rail": label, "conditions": conditions, **trace,
                    'analysisBandBinding': model['analysisBandBinding'],
                    "pass": passed, "limitRatio": max(trace["maxImpedanceOhm"] / target, peak / peak_limit)})
                if not passed:
                    result["reasons"].append(f"{candidate['pn']}/{label}: pdn_limit_exceeded")
            except (ValueError, OSError, KeyError) as error:
                result["reasons"].append(f"{candidate['pn']}/{label}: {error}")
        result["candidates"].append(entry)
    spreads = []
    for i, (pn_a, condition_a, frequencies_a, rlc_a) in enumerate(curves):
        for pn_b, condition_b, frequencies_b, rlc_b in curves[i + 1:]:
            if pn_a != pn_b and condition_a == condition_b:
                start, end = max(frequencies_a[0], frequencies_b[0]), min(frequencies_a[-1], frequencies_b[-1])
                if start >= end:
                    continue
                shared = [start * (end / start) ** (j / 400) for j in range(401)]
                shared[0], shared[-1] = start, end
                zs_a, zs_b = rlc_impedance_samples(rlc_a.read_bytes(), shared), rlc_impedance_samples(rlc_b.read_bytes(), shared)
                spreads.append({"pnA": pn_a, "pnB": pn_b, "conditions": condition_a, "comparisonFrequencyRangeHz": [start, end],
                    "maxRelativeComplexImpedanceDifference": max(abs(a - b) / max(abs(a), abs(b), 1e-30) for a, b in zip(zs_a, zs_b))})
    result["pairwiseDifferences"] = spreads
    if not candidates:
        result["reasons"].append("no_specification_matched_official_pairs")
    if not result["cases"]:
        result["reasons"].append("rail_operating_conditions_and_pdn_results_required")
    if not result["reasons"]:
        selected = max(result["candidates"], key=lambda c: (max(r["limitRatio"] for r in c["pdnResults"]), c["pn"]))
        result.update(status="approved_characteristic_substitute", selectedPn=selected["pn"],
                      reason="all_candidates_pass_all_cases; conservative_smallest_pdn_margin_candidate_selected")
    return result
