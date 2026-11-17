"""BOM 부품의 NDF 접속과 근거가 있는 전원 조건을 연결한다."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path


def _verified_ndf(policy):
    for evidence in policy.get('evidence', []):
        path = Path(evidence.get('path', ''))
        if path.suffix.lower() != '.ndf':
            continue
        if policy.get('boardDataFolder') and path.resolve().parent != Path(policy['boardDataFolder']).resolve():
            raise ValueError('ndf_board_folder_mismatch_load_matching_board_conditions')
        payload = path.read_bytes()
        if evidence.get('sha256') != hashlib.sha256(payload).hexdigest():
            raise ValueError('ndf_evidence_sha256_mismatch')
        text = payload.decode('utf-8-sig', errors='replace')
        rows = []
        for statement in re.finditer(r'[^;]+;', text):
            fields = re.findall(r'"([^"]*)"', statement[0])
            if len(fields) >= 5:
                rows.append({'net': fields[0], 'lgePn': fields[1], 'designator': fields[3],
                             'pin': fields[4], 'ground': ': GROUND:' in statement[0],
                             'ndfLine': text.count('\n', 0, statement.start() + len(statement[0]) - len(statement[0].lstrip())) + 1})
        return rows, evidence
    return [], None


def read_board_connections(policy):
    """검증된 보드 접속을 반환한다. 상위 실행 흐름에서 한 번 읽어 재사용할 수 있다."""
    return _verified_ndf(policy)


def resolve_pdn_scope(part, policy, connections=None):
    """Spec의 대상 전원과 실제 접속으로 위치별 PDN 범위를 판정한다."""
    result = {'status': 'purpose_review', 'pdnPlacements': [], 'excludedPlacements': [],
              'unresolvedPlacements': [], 'reasons': [], 'analysisType': 'PDN'}
    if not part.is_bom:
        # Inner Cap의 모델 확보 범위는 reference net으로 제한하지 않는다.
        result.update(status='pdn_target', downloadBasis='inner_cap_all_models')
        for position in part.pdn_placements:
            item = {**position, 'nets': [position['rail']], 'pins': []}
            reference_rails = {r['rail'] for r in policy.get('railCatalog', [])
                               if r.get('sourceDesignator') == position.get('designator')
                               and r.get('sourcePin') == position.get('pin')}
            if len(reference_rails) == 1:
                item.update(inputNet=position['rail'], rail=next(iter(reference_rails)),
                            railMappingBasis='exact_reference_designator_and_pin')
            elif len(reference_rails) > 1:
                item['railMappingBasis'] = 'ambiguous_reference_pin_original_net_preserved'
            result['pdnPlacements'].append(item)
        return result
    try:
        rows, evidence = connections if connections is not None else _verified_ndf(policy)
    except (OSError, ValueError) as error:
        result['reasons'].append(str(error))
        return result
    result['connectionEvidence'] = evidence
    key = part.lge_pn or part.raw_maker_pn or part.maker_pn
    settings = policy.get('parts', {}).get(key, {})
    explicit_targets = set(policy.get('targetRails', []))
    targets = explicit_targets or {r['rail'] for r in policy.get('railCatalog', []) if r.get('rail')}
    # 공식 참조의 전원 핀과 같은 실제 NDF 네트도 대상 전원으로 연결한다.
    for rail in policy.get('railCatalog', []):
        if rail.get('rail') in targets:
            targets.update(r['net'] for r in rows if r['designator'] == rail.get('sourceDesignator') and r['pin'] == rail.get('sourcePin'))
    cases = [c for c in settings.get('cases', []) if not explicit_targets or c.get('rail') in targets]
    targets.update(c['rail'] for c in cases if c.get('rail'))
    placements = {}
    if part.lge_pn:
        for row in rows:
            if row['lgePn'] == part.lge_pn:
                placements.setdefault(row['designator'], []).append(row)
    overrides = settings.get('placementRoles', {})
    for name, pins in sorted(placements.items()):
        nets = {p['net'] for p in pins}
        live = [p for p in pins if not p['ground'] and p['net'] != 'GND']
        grounded = [p for p in pins if p not in live]
        item = {'designator': name, 'nets': sorted(nets), 'pins': pins}
        role = overrides.get(name)
        if name in policy.get('excludedDesignators', []) or role == 'signal' or (live and all(p['net'] in policy.get('signalNets', []) for p in live)):
            item['reason'] = 'explicit_signal_or_excluded_designator'
            result['excludedPlacements'].append(item)
        elif role == 'pdn' or any(name in c.get('designators', []) for c in cases) or (len(pins) == 2 and len(live) == len(grounded) == 1 and live[0]['net'] in targets):
            item['rail'] = live[0]['net'] if len(live) == 1 else next((c['rail'] for c in cases if name in c.get('designators', [])), None)
            result['pdnPlacements'].append(item)
        elif explicit_targets and not nets.intersection(targets):
            item['reason'] = 'outside_spec_target_rails'
            result['excludedPlacements'].append(item)
        else:
            item['reason'] = 'pdn_purpose_or_connection_unconfirmed'
            result['unresolvedPlacements'].append(item)
    if not placements and cases:
        result['pdnPlacements'] = [{'designator': name, 'rail': c['rail'], 'pins': [], 'nets': [c['rail']],
                                    'reason': 'explicit_spec_case'} for c in cases for name in (c.get('designators') or [None])]
    if result['pdnPlacements']:
        result['status'] = 'pdn_target'
    elif result['excludedPlacements'] and not result['unresolvedPlacements']:
        result['status'] = 'excluded_non_pdn'
    else:
        result['reasons'].append('pdn_purpose_or_placement_required')
    return result


def resolve_part_conditions(part, policy):
    """확인된 전원-접지 연결만 자동 지정한다. 신호/BYP/AVS는 명시 조건을 요구한다."""
    resolved = copy.deepcopy(policy)
    key = part.lge_pn or part.raw_maker_pn or part.maker_pn
    settings = resolved.setdefault('parts', {}).setdefault(key, {})
    if settings.get('thicknessMm') is None:
        thickness = read_bom_thickness(part)
        if thickness is not None:
            settings['thicknessMm'] = thickness
            settings['thicknessEvidence'] = {'sourceType': 'explicit_bom_component_thickness',
                                             'sourceSpec': part.source_spec}
        specification = settings.get('originalSpecification', {})
        if thickness is None and specification:
            evidence_path = Path(specification.get('evidenceFile', ''))
            try:
                if (specification['partNumber'] != (part.raw_maker_pn or part.maker_pn)
                        or specification['manufacturer'] != part.manufacturer
                        or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != specification['evidenceSha256']):
                    raise ValueError('original_specification_identity_or_hash_mismatch')
                thickness = specification['thicknessMm']
                if not isinstance(thickness, (int, float)) or not math.isfinite(thickness) or thickness <= 0:
                    raise ValueError('original_specification_thickness_invalid')
                settings['thicknessMm'] = thickness
                settings['thicknessEvidence'] = specification
            except (OSError, ValueError, KeyError) as error:
                settings['thicknessEvidenceFailure'] = str(error)
    resolution = {'sourceType': 'ndf_connections_and_verified_rail_catalog', 'unresolved': [],
                  'automaticCases': [], 'status': 'explicit_conditions' if settings.get('cases') else 'pending'}
    settings['conditionResolution'] = resolution
    try:
        rows, evidence = _verified_ndf(resolved)
    except (ValueError, OSError) as error:
        resolution['unresolved'].append(str(error))
        settings['conditionResolutionReasons'] = [str(error)]
        return resolved
    if not evidence:
        return resolved
    resolution['evidence'] = evidence
    placements = {}
    for row in rows:
        if row['lgePn'] == part.lge_pn:
            placements.setdefault(row['designator'], []).append(row)
    settings['placementEvidence'] = [{'designator': name, 'pins': pins} for name, pins in sorted(placements.items())]
    if policy.get('analysisType') == 'PDN':
        scope = settings.get('pdnScope') or resolve_pdn_scope(part, policy, (rows, evidence))
        settings['pdnScope'] = scope
        allowed = {p['designator'] for p in scope['pdnPlacements']}
        placements = {name: pins for name, pins in placements.items() if name in allowed}
        settings['placementEvidence'] = [{'designator': name, 'pins': pins} for name, pins in sorted(placements.items())]
        if settings.get('cases'):
            filtered = []
            for case in settings['cases']:
                allowed_rails = {p.get('rail') for p in scope['pdnPlacements']} | set(policy.get('targetRails', []))
                if policy.get('targetRails') and case['rail'] not in allowed_rails:
                    continue
                selected = [name for name in case.get('designators', []) if name in allowed]
                if case.get('designators') and not selected:
                    continue
                filtered.append({**case, 'designators': selected})
            settings['cases'] = filtered
    if settings.get('cases'):
        return resolved  # 명시된 실제 조건 및 모델별 PDN 결과를 자동 값으로 덮어쓰지 않는다.
    rail_map = {}
    for rail in resolved.get('railCatalog', []):
        voltage_evidence = rail.get('voltageEvidence')
        if voltage_evidence:
            try:
                if hashlib.sha256(Path(voltage_evidence['path']).read_bytes()).hexdigest() != voltage_evidence['sha256']:
                    raise ValueError('voltage_evidence_sha256_mismatch')
            except (OSError, KeyError, ValueError) as error:
                resolution['unresolved'].append(f"{rail.get('rail')}: {error}")
                continue
        nets = {rail.get('rail')}
        sources = [r for r in rows if r['designator'] == rail.get('sourceDesignator')
                   and r['pin'] == rail.get('sourcePin')]
        if not sources:
            # 참조번호를 숫자 패턴으로 바꾸지 않는다. 네트명과 물리 핀이 모두 같은
            # 유일한 NDF 행만 원본 reference의 전원 근거에 연결한다.
            exact_sources = [r for r in rows if r['net'] == rail.get('rail') and r['pin'] == rail.get('sourcePin')]
            if len(exact_sources) == 1:
                sources = exact_sources
                resolution.setdefault('railSourceMappings', []).append({
                    'referenceDesignator': rail.get('sourceDesignator'), 'ndfDesignator': sources[0]['designator'],
                    'pin': sources[0]['pin'], 'net': sources[0]['net'], 'ndfLine': sources[0]['ndfLine']})
        nets.update(r['net'] for r in sources)
        for net in nets - {None}:
            rail_map.setdefault(net, []).append(rail)
    cases = {}
    for designator, pins in sorted(placements.items()):
        grounded = [p for p in pins if p['ground'] or p['net'] == 'GND']
        live = [p for p in pins if p not in grounded]
        if len(pins) != 2 or len(grounded) != 1 or len(live) != 1:
            resolution['unresolved'].append(f'{designator}: two_terminal_rail_to_ground_connection_required')
            continue
        net = live[0]['net']
        rails = rail_map.get(net, [])
        voltages = {r.get('nominalVoltageV') for r in rails}
        if len(voltages) != 1 or None in voltages:
            resolution['unresolved'].append(f'{designator}/{net}: verified_operating_voltage_required')
            continue
        voltage = next(iter(voltages))
        if not isinstance(voltage, (int, float)) or not math.isfinite(voltage) or voltage < 0:
            resolution['unresolved'].append(f'{designator}/{net}: invalid_operating_voltage')
            continue
        if resolved.get('avsEvidence') and voltage == 0.9:
            resolution['unresolved'].append(f'{designator}/{net}: avs_location_and_voltage_range_required')
            continue
        case = cases.setdefault(net, {'rail': net, 'voltageV': voltage,
                'temperatureC': resolved.get('temperatureC', 25), 'candidateResults': {},
                'sourceType': 'verified_rail_to_ground_ndf_assignment', 'designators': []})
        case['voltageEvidence'] = [r.get('voltageEvidence', {'sourceType': 'verified_reference_rail_catalog',
                                  'rail': r['rail'], 'sourcePin': r.get('sourcePin')}) for r in rails]
        case['designators'].append(designator)
    if not part.is_bom:
        for position in settings.get('pdnScope', {}).get('pdnPlacements', []):
            net = position['rail']
            rails = rail_map.get(net, [])
            voltages = {r.get('nominalVoltageV') for r in rails}
            if len(voltages) != 1 or None in voltages:
                resolution['unresolved'].append(f"{position['designator']}/{net}: verified_operating_voltage_required")
                continue
            voltage = next(iter(voltages))
            if resolved.get('avsEvidence') and voltage == 0.9:
                resolution['unresolved'].append(f"{position['designator']}/{net}: avs_location_and_voltage_range_required")
                continue
            case = cases.setdefault(net, {'rail': net, 'voltageV': voltage,
                'temperatureC': resolved.get('temperatureC', 25), 'candidateResults': {},
                'sourceType': 'inner_cap_and_reference_pin_assignment', 'designators': []})
            if position['designator'] not in case['designators']:
                case['designators'].append(position['designator'])
            case.setdefault('innerPinAssignments', []).append(position)
    settings['cases'] = list(cases.values())
    resolution['automaticCases'] = list(cases.values())
    if not placements and not settings.get('pdnScope', {}).get('pdnPlacements'):
        resolution['unresolved'].append('exact_lge_pn_placement_not_found')
    settings['conditionResolutionReasons'] = resolution['unresolved']
    resolution['status'] = 'resolved' if cases and not resolution['unresolved'] else 'pending'
    return resolved


def read_bom_thickness(part):
    # 1005 등의 평면 크기 및 PCB의 1.6T를 부품 두께로 해석하지 않는다.
    match = re.search(r'(?:thickness|두께|\bT\s*=)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*mm\b',
                      part.product_characteristics or part.source_spec, re.I)
    return float(match[1]) if match else None
