"""PDN 해석 Spec을 읽는다. 기존 대체 조건 JSON도 같은 입력으로 사용할 수 있다."""
import hashlib
import copy
import csv
import io
import json
import math
import re
from pathlib import Path


def _reference_spec(path, policy):
    payload = path.read_bytes()
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeError:
        text = payload.decode('cp949')
    rows = list(csv.DictReader(io.StringIO(text)))
    required = {'Designator', 'Net_name', 'Pin_number', 'Frequency_start(MHz)', 'Frequency_end(MHz)', 'Spec(ohm)'}
    if not rows or not required.issubset(rows[0]):
        raise ValueError('reference CSV의 Net_name·핀·주파수·Spec 열을 확인하세요.')
    existing = {r['rail']: r for r in policy.get('railCatalog', [])}
    rails = []
    for row in rows:
        name = row['Net_name'].strip()
        if not name:
            continue
        limit = re.fullmatch(r'<\s*(\d+(?:\.\d+)?)', row['Spec(ohm)'].strip())
        band = [float(row['Frequency_start(MHz)']) * 1e6, float(row['Frequency_end(MHz)']) * 1e6]
        if not limit or not 0 < band[0] < band[1] or float(limit[1]) <= 0:
            raise ValueError(f'{name}: 주파수 범위 또는 <임피던스 기준이 잘못되었습니다.')
        # 실제 전압은 기존 Spec의 근거만 유지한다. Net 이름에서 새로 추정하지 않는다.
        rails.append({**existing.get(name, {}), 'rail': name, 'sourceDesignator': row['Designator'].strip(),
                      'sourcePin': row['Pin_number'].strip(), 'frequencyRangeHz': band,
                      'specification': row['Spec(ohm)'].strip(), 'targetImpedanceOhm': float(limit[1])})
    if not rails:
        raise ValueError('reference CSV에 해석 대상 net이 없습니다.')
    bands = {tuple(r['frequencyRangeHz']) for r in rails}
    limits = {r['targetImpedanceOhm'] for r in rails}
    if len(bands) != 1 or len(limits) != 1:
        raise ValueError('전원별 주파수·한계가 다른 CSV는 전원별 해석 Spec으로 분리하세요.')
    policy.update(analysisType='PDN', targetRails=list(dict.fromkeys(r['rail'] for r in rails)),
                  railCatalog=rails, frequencyRangeHz=list(next(iter(bands))),
                  targetImpedanceOhm=next(iter(limits)), peakLimitOhm=next(iter(limits)))
    policy['referenceSpecEvidence'] = {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(),
                                      'columnsUsed': sorted(required), 'priorResultsUsedForApproval': False}
    return policy


def load_analysis_spec(path, base_policy=None):
    path = Path(path).resolve()
    payload = path.read_bytes()
    if path.suffix.lower() == '.csv':
        policy = _reference_spec(path, copy.deepcopy(base_policy or {}))
    else:
        policy = json.loads(payload.decode('utf-8-sig'))
        if not isinstance(policy, dict):
            raise ValueError('해석 Spec은 JSON 객체여야 합니다.')
        if policy.get('referenceSpecFile'):
            policy = _reference_spec((path.parent / policy['referenceSpecFile']).resolve(), policy)
    if not isinstance(policy, dict) or not isinstance(policy.get('parts', {}), dict):
        raise ValueError('해석 Spec의 parts는 JSON 객체여야 합니다.')
    if policy.get('analysisType', 'PDN') != 'PDN':
        raise ValueError('이번 앱의 해석 Spec은 analysisType=PDN이어야 합니다.')
    policy['analysisType'] = 'PDN'
    for key in ('targetRails', 'signalNets', 'excludedDesignators'):
        if key in policy and (not isinstance(policy[key], list) or any(not isinstance(v, str) for v in policy[key])):
            raise ValueError(f'{key}는 이름 문자열 목록이어야 합니다.')
    if 'targetRails' in policy and not policy['targetRails']:
        raise ValueError('targetRails가 비어 있습니다. 대상 전원을 지정하거나 이 필드를 생략하세요.')
    frequency = policy.get('frequencyRangeHz', [100_000, 1_000_000_000])
    if (not isinstance(frequency, list) or len(frequency) != 2
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in frequency)
            or not 0 < frequency[0] < frequency[1]):
        raise ValueError('frequencyRangeHz는 양수인 시작·끝 주파수여야 합니다.')
    for item in policy.get('evidence', []):
        if item.get('path'):
            item['path'] = str((path.parent / item['path']).resolve())
    for rail in policy.get('railCatalog', []):
        proof = rail.get('voltageEvidence', {})
        if proof.get('path'):
            proof['path'] = str((path.parent / proof['path']).resolve())
    for settings in policy.get('parts', {}).values():
        specification = settings.get('originalSpecification', {})
        if specification.get('evidenceFile'):
            specification['evidenceFile'] = str((path.parent / specification['evidenceFile']).resolve())
        for case in settings.get('cases', []):
            for proof in case.get('candidateResults', {}).values():
                if 'pdnCsv' in proof:
                    proof['pdnCsv'] = str((path.parent / proof['pdnCsv']).resolve())
        for pn, folders in settings.get('modelVariantFolders', {}).items():
            settings['modelVariantFolders'][pn] = [str((path.parent / folder).resolve()) for folder in folders]
    policy['officialModelLibraryDirs'] = [str((path.parent / folder).resolve()) for folder in policy.get('officialModelLibraryDirs', [])]
    policy['analysisSpecEvidence'] = {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest()}
    if policy.get('referenceSpecEvidence'):
        policy['analysisSpecEvidence']['referenceSpec'] = policy['referenceSpecEvidence']
    return policy
