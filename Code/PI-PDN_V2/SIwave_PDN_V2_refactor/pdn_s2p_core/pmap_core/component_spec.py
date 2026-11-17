"""Technical Spec에서 대체 모델 검색에 사용할 커패시터 사양을 읽는다."""
import re


def electrolytic_specification(part):
    """전해콘덴서의 BOM 사양을 기록한다. 임피던스 정격을 ESR/ESL로 변환하지 않는다."""
    text = part.product_characteristics or part.source_spec
    voltage = re.search(r'\b(\d+(?:\.\d+)?)\s*V\b', text, re.I)
    dimensions = re.search(r'\b(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\s*MM\b', text, re.I)
    temperature = re.search(r'([+-]?\d+)\s*TO\s*([+-]?\d+)\s*C', text, re.I)
    ripple = re.search(r'(\d+(?:\.\d+)?)\s*mA\b', text, re.I)
    life = re.search(r'(\d+)\s*HR\b', text, re.I)
    tolerance = re.search(r'([+-]?\d+)\s*TO\s*([+-]?\d+)\s*%', text, re.I)
    return {'componentFamily': 'aluminum_electrolytic', 'capacitanceF': part.nominal_capacitance_f,
            'ratedVoltageV': float(voltage[1]) if voltage else None,
            'diameterMm': float(dimensions[1]) if dimensions else None,
            'heightMm': float(dimensions[2]) if dimensions else None,
            'temperatureRangeC': [float(temperature[1]), float(temperature[2])] if temperature else None,
            'toleranceRangePercent': [float(tolerance[1]), float(tolerance[2])] if tolerance else None,
            'ratedRippleCurrentA': float(ripple[1]) / 1000 if ripple else None,
            'loadLifeHours': int(life[1]) if life else None, 'sourceSpec': part.source_spec}


def capacitor_specification(part, settings=None, supplemental=None):
    settings = settings or {}
    supplemental = supplemental or {}
    text = part.product_characteristics or part.source_spec
    voltage = re.search(r'\b(\d+(?:\.\d+)?)\s*V\b', text, re.I)
    tolerance = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
    dielectric = re.search(r'\b(?:X7R|X5R|C0G|NP0|X7S|X6S|X5S)\b', text, re.I)
    size = re.search(r'\b(\d{4})\s+(?:R/TP|TP)\b', text)
    temperature = re.search(r'([+-]?\d+)\s*(?:TO|~|～|/|\.\.)\s*([+-]?\d+)\s*C', text, re.I)
    capacitance = part.nominal_capacitance_f or supplemental.get('capacitanceF')
    rated_voltage = float(voltage[1]) if voltage else supplemental.get('ratedVoltageV')
    tolerance_percent = float(tolerance[1]) if tolerance else supplemental.get('tolerancePercent')
    dielectric_value = dielectric[0].upper() if dielectric else supplemental.get('dielectric')
    size_code = size[1] if size else supplemental.get('sizeCodeMm')
    if not all((capacitance, rated_voltage, tolerance_percent, dielectric_value, size_code)):
        raise ValueError('bom_capacitance_voltage_tolerance_dielectric_size_required')
    # 삼성 공식 목록의 2012 제품과 해당 S2P 본문은 폭 1.25mm를 명시한다.
    # 크기 코드의 '12'를 실제 폭 1.2mm로 해석하면 같은 크기의 제품도 제외된다.
    width_mm = 1.25 if size_code == '2012' else int(size_code[2:]) / 10
    return {'capacitanceF': capacitance, 'ratedVoltageV': rated_voltage,
            'tolerancePercent': tolerance_percent, 'dielectric': dielectric_value,
            'sizeCodeMm': size_code,
            'lengthMm': int(size_code[:2]) / 10 if size else supplemental.get('lengthMm', int(size_code[:2]) / 10),
            'widthMm': width_mm if size else supplemental.get('widthMm', width_mm),
            'thicknessMm': settings.get('thicknessMm', supplemental.get('thicknessMm')),
            'temperatureRangeC': [float(temperature[1]), float(temperature[2])] if temperature else supplemental.get('temperatureRangeC')}


def cross_vendor_specification_reasons(expected, actual):
    """PN 접두어가 아닌 공식 특성을 대조한다. 미확인 사양은 승인하지 않는다."""
    reasons = []
    tolerance = re.search(r'(\d+(?:\.\d+)?)\s*%', actual.get('capacitanceTolerance', {}).get('displayName', ''))
    if not tolerance or float(tolerance[1]) != expected['tolerancePercent']:
        reasons.append('tolerance_mismatch_or_missing')
    temperature_text = actual.get('operaTemp', {}).get('value', '')
    temperatures = re.findall(r'[+-]?\d+(?:\.\d+)?', temperature_text)
    if (expected['temperatureRangeC'] is None or len(temperatures) != 2
            or [float(v) for v in temperatures] != expected['temperatureRangeC']):
        reasons.append('temperature_range_mismatch_or_missing')
    if expected['thicknessMm'] is None:
        reasons.append('bom_thickness_required_for_cross_vendor_approval')
    return reasons
