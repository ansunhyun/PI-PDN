"""공식 커패시터 목록의 동일 사양과 단일 항목 인접 단계를 판정한다."""
import math
import re


ADJUSTABLE = ('ratedVoltageV', 'tolerancePercent', 'temperatureRangeC', 'thicknessMm')


def component_specification_values(spec):
    unit = spec['capacitance']['unit'].replace('μ', 'u').replace('µ', 'u')
    tolerance = re.findall(r'[+-]?\d+(?:\.\d+)?', spec['capacitanceTolerance']['displayName'])
    temperature = re.findall(r'[+-]?\d+(?:\.\d+)?', spec['operaTemp']['value'])
    if not tolerance or len(temperature) != 2:
        raise ValueError('official_tolerance_or_temperature_missing')
    return {'capacitanceF': float(spec['capacitance']['value']) * {'pF': 1e-12, 'nF': 1e-9, 'uF': 1e-6}[unit],
        'ratedVoltageV': float(spec['ratedVoltageDc']['value']),
        'tolerancePercent': max(abs(float(v)) for v in tolerance),
        'dielectric': spec['tempChara']['value'].upper(),
        'sizeCodeMm': spec['sizeCodeInMm']['value'][:4],
        'lengthMm': float(spec['length']['value']), 'widthMm': float(spec['width']['value']),
        'thicknessMm': float(spec['thickness']['value']),
        'temperatureRangeC': [float(v) for v in temperature]}


def _equal(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-6)
    return a == b


def _order(field, value):
    if field == 'temperatureRangeC':
        return (value[1] - value[0], value[0], value[1])
    return value


def rank_bom_specifications(expected, rows, *, max_thickness_mm=None, catalog_only=False):
    """전체 관측 공식 목록에서 앞뒤 한 단계만 허용한다. 임의 ±비율은 없다.

    rows는 pn/spec을 갖는다. catalog_only는 파일 다운로드 전 두께 미확인 목록용이다.
    """
    fixed, rejected = [], []
    for row in rows:
        actual = row['spec']
        reasons = []
        for field in ('capacitanceF', 'dielectric', 'sizeCodeMm', 'lengthMm', 'widthMm'):
            if not _equal(expected.get(field), actual.get(field)):
                reasons.append(field + '_must_match')
        for field in ('capacitanceF', 'ratedVoltageV', 'tolerancePercent', 'lengthMm', 'widthMm'):
            value = actual.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                reasons.append(field + '_invalid')
        if not catalog_only and (not isinstance(actual.get('thicknessMm'), (int, float))
                or not math.isfinite(actual['thicknessMm']) or actual['thicknessMm'] <= 0):
            reasons.append('official_thickness_missing')
        if not expected.get('temperatureRangeC') or not actual.get('temperatureRangeC'):
            reasons.append('temperature_range_required')
        if reasons:
            rejected.append({'pn': row['pn'], 'reasons': reasons})
        else:
            fixed.append(row)
    selected = []
    for row in fixed:
        actual = row['spec']
        fields = [f for f in ADJUSTABLE if expected.get(f) is not None
                  and not (catalog_only and f == 'thicknessMm') and not _equal(expected[f], actual.get(f))]
        reasons, changes = [], []
        if len(fields) > 1:
            reasons.append('only_one_specification_may_change')
        for field in fields:
            # 다른 조정 항목은 BOM과 같은 제품군의 관측값으로만 이웃을 정의한다.
            levels = {_order(field, r['spec'][field]) for r in fixed
                if r.get('manufacturer') == row.get('manufacturer') and r['spec'].get(field) is not None and all(expected.get(f) is None
                    or catalog_only and f == 'thicknessMm' or _equal(expected[f], r['spec'].get(f))
                    for f in ADJUSTABLE if f != field)}
            origin, value = _order(field, expected[field]), _order(field, actual[field])
            below = max((v for v in levels if v < origin), default=None)
            above = min((v for v in levels if v > origin), default=None)
            if value not in (below, above):
                reasons.append(field + '_not_adjacent_official_level')
            changes.append({'field': field, 'bomValue': expected[field], 'modelValue': actual[field],
                'direction': 'above' if value > origin else 'below',
                'basis': 'adjacent_observed_official_catalog_level'})
        if actual['ratedVoltageV'] < expected['ratedVoltageV']:
            reasons.append('rated_voltage_below_bom_requirement')
        if actual['tolerancePercent'] > expected['tolerancePercent']:
            reasons.append('tolerance_wider_than_bom_requirement')
        low, high = actual['temperatureRangeC']
        if low > expected['temperatureRangeC'][0] or high < expected['temperatureRangeC'][1]:
            reasons.append('temperature_range_does_not_cover_bom')
        height = max_thickness_mm if max_thickness_mm is not None else expected.get('thicknessMm')
        if not catalog_only and height is not None and actual['thicknessMm'] > height:
            reasons.append('thickness_exceeds_confirmed_height')
        if reasons:
            rejected.append({'pn': row['pn'], 'reasons': reasons, 'changes': changes})
        else:
            selected.append({**row, 'matchTier': 'same_specification' if not fields else 'one_adjacent_specification',
                'changes': changes, 'unconfirmedBomFields': ['thicknessMm'] if expected.get('thicknessMm') is None else []})
    selected.sort(key=lambda r: (bool(r['changes']), ADJUSTABLE.index(r['changes'][0]['field']) if r['changes'] else -1, r['pn']))
    return selected, rejected
