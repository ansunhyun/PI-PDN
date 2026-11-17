"""원 BOM 품번과 위치별 해석 모델을 연결해 JSON·CSV로 저장한다."""
import csv
import json
import copy
import hashlib
import os
import io
from pathlib import Path


def model_mapping_record(part, scope, status, folder=None, replacement=None, message=''):
    record = {'lgePn': part.lge_pn, 'originalPn': part.raw_maker_pn or part.maker_pn,
              'originalManufacturer': part.manufacturer, 'sourceSpec': part.source_spec,
              'bomManufacturerText': part.raw_manufacturer,
              'isBom': part.is_bom, 'status': status, 'scope': scope, 'message': message,
              'selectedPn': None, 'selectedManufacturer': None, 'modelVariants': [], 'candidateModels': []}
    if folder:
        pn = replacement.selected_pn if replacement else part.maker_pn
        source_path = Path(folder) / f'{pn}.source.json'
        source = json.loads(source_path.read_text(encoding='utf-8')) if source_path.exists() else {}
        record.update(selectedPn=pn, selectedManufacturer=source.get('manufacturer', part.manufacturer),
                      selectionMode=replacement.selection_mode if replacement else 'exact_existing')
        folders = [Path(folder)]
        approval = None
        if replacement and replacement.selection_mode in ('characteristic_substitute', 'bom_spec_substitute'):
            approval = json.loads(Path(replacement.reason).read_text(encoding='utf-8'))
            record['approvalFile'] = replacement.reason
            record['selectedManufacturer'] = approval['selectedManufacturer']
            if replacement.selection_mode == 'characteristic_substitute':
                record['approvedCases'] = next(c['pdnResults'] for c in approval['candidates'] if c['pn'] == pn)
                folders = sorted(Path(folder).parent.glob('DC*V_*degC')) or folders
            else:
                record.update(selectionBasis=approval['selectionBasis'], approvalScope=approval['approvalScope'],
                    specificationMatch=approval['specificationMatch'], specificationDifferences=approval['specificationDifferences'],
                    unconfirmedBomFields=approval['unconfirmedBomFields'], pdnValidationStatus='not_evaluated',
                    performanceSimilarityValidated=False)
                if approval.get('originalSpecificationEvidence'):
                    record.update(originalSpecificationEvidence=approval['originalSpecificationEvidence'],
                        originalSpecificationStatus=approval['originalSpecificationStatus'],
                        originalSpecificationLimitations=approval['originalSpecificationLimitations'],
                        resolvedOriginalManufacturer=approval['resolvedOriginalManufacturer'],
                        searchSpecification=approval['searchSpecification'])
        for variant in folders:
            s2p = variant / f'{pn}.s2p'
            if not s2p.exists():
                continue
            proof_path = s2p.with_suffix('.source.json')
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            record['modelVariants'].append({'s2pFile': str(s2p.resolve()), 'rlcFile': str(s2p.with_suffix('.rlc').resolve()),
                'sourceFile': str(proof_path.resolve()), 's2pSha256': proof.get('sha256'),
                'conditions': proof.get('modelConditions'), 'sourceType': proof.get('sourceType')})
    elif 'report=' in message:
        report_path = Path(message.split('report=', 1)[1].split(';', 1)[0])
        if report_path.exists():
            comparison = json.loads(report_path.read_text(encoding='utf-8'))
            record['comparisonReport'] = str(report_path)
            record['candidateModels'] = [{'manufacturer': c.get('manufacturer'), 'pn': c['pn'],
                'status': c.get('status', 'candidate_not_approved'), 'models': c.get('models', [])}
                for c in [*comparison.get('candidates', []), *comparison.get('investigationOnlyCandidates', [])]]
    return record


def _position_variants(record, position):
    variants = record['modelVariants']
    if record.get('selectionMode') != 'characteristic_substitute':
        return variants
    conditions = [c['conditions'] for c in record.get('approvedCases', []) if c['rail'] == position.get('rail')]
    return [v for v in variants if v['conditions'] in conditions]


def _write_mapping_bytes(path, content, preserve_unchanged, write_stats):
    unchanged = preserve_unchanged and path.is_file() and path.read_bytes() == content
    if not unchanged:
        path.write_bytes(content)
    if write_stats is not None:
        key = 'unchangedMappings' if unchanged else 'writtenMappings'
        write_stats[key] = write_stats.get(key, 0) + 1


def write_model_mapping(anchor, records, policy, destination_dir=None,
                        preserve_unchanged=False, write_stats=None):
    anchor = Path(anchor)
    mapping_anchor = Path(destination_dir) / anchor.name if destination_dir else anchor
    manifest = mapping_anchor.with_suffix('.model-mapping.json')
    manifest.parent.mkdir(parents=True, exist_ok=True)
    data = {'analysisType': 'PDN', 'pmapFile': str(anchor), 'analysisSpec': policy.get('analysisSpecEvidence'),
            'pmapSaved': anchor.is_file(),
            'frequencyRangeHz': policy.get('frequencyRangeHz'), 'targetImpedanceOhm': policy.get('targetImpedanceOhm'),
            'peakLimitOhm': policy.get('peakLimitOhm'), 'records': records}
    for record in records:
        for variant in record.get('modelVariants', []):
            for field in ('s2pFile', 'rlcFile', 'sourceFile'):
                if variant.get(field):
                    variant[field + 'Relative'] = os.path.relpath(variant[field], manifest.parent).replace('\\', '/')
        record['bindings'] = [{'designator': p.get('designator'), 'pin': p.get('pin'), 'rail': p.get('rail'), 'model': v}
                              for p in record['scope'].get('pdnPlacements', []) for v in _position_variants(record, p)]
    _write_mapping_bytes(manifest, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'),
                         preserve_unchanged, write_stats)
    csv_path = mapping_anchor.with_suffix('.model-mapping.csv')
    fields = ['lgePn', 'originalManufacturer', 'originalPn', 'designator', 'pin', 'rail', 'status',
              'selectedManufacturer', 'selectedPn', 'conditions', 's2pFile', 'rlcFile', 'sourceFile', 'evidenceReport',
              'selectionBasis', 'specificationDifferences', 'unconfirmedBomFields', 'pdnValidationStatus',
              'originalSpecificationStatus', 'originalSpecificationEvidence', 'resolvedOriginalManufacturer',
              's2pFileRelative', 'rlcFileRelative', 'sourceFileRelative']
    with io.StringIO(newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            scope = record['scope']
            positions = [(p, record['status']) for p in scope.get('pdnPlacements', [])]
            positions += [(p, 'excluded_non_pdn') for p in scope.get('excludedPlacements', [])]
            positions += [(p, 'purpose_review') for p in scope.get('unresolvedPlacements', [])]
            if not positions:
                positions = [({}, record['status'])]
            for position, status in positions:
                for variant in (_position_variants(record, position) if status == record['status'] else []) or [{}]:
                    writer.writerow({**{k: record.get(k) for k in ('lgePn', 'originalManufacturer', 'originalPn')},
                        'designator': position.get('designator'), 'pin': position.get('pin'), 'rail': position.get('rail'), 'status': status,
                        'selectedManufacturer': record['selectedManufacturer'] if variant else None,
                        'selectedPn': record['selectedPn'] if variant else None,
                        'conditions': json.dumps(variant.get('conditions'), ensure_ascii=False) if variant else '',
                        **{k: variant.get(k) for k in ('s2pFile', 'rlcFile', 'sourceFile')},
                        **{k: variant.get(k) for k in ('s2pFileRelative', 'rlcFileRelative', 'sourceFileRelative')},
                        'evidenceReport': record.get('approvalFile') or record.get('comparisonReport'),
                        'selectionBasis': record.get('selectionBasis', record.get('selectionMode', '')),
                        'specificationDifferences': json.dumps(record.get('specificationDifferences', []), ensure_ascii=False),
                        'unconfirmedBomFields': json.dumps(record.get('unconfirmedBomFields', []), ensure_ascii=False),
                        'pdnValidationStatus': record.get('pdnValidationStatus', ''),
                        'originalSpecificationStatus': record.get('originalSpecificationStatus', ''),
                        'originalSpecificationEvidence': json.dumps(record.get('originalSpecificationEvidence', {}), ensure_ascii=False),
                        'resolvedOriginalManufacturer': record.get('resolvedOriginalManufacturer', record.get('originalManufacturer', ''))})
        csv_content = stream.getvalue().encode('utf-8-sig')
    _write_mapping_bytes(csv_path, csv_content, preserve_unchanged, write_stats)
    return manifest, csv_path


def save_mapping_with_models(mapping_paths, records, policy, library_roots=(), runtime_roots=()):
    """실제 선택 파일 옆과 라이브러리 최상위에 실행별 매핑을 저장한다."""
    primary = json.loads(Path(mapping_paths[0]).read_text(encoding='utf-8'))
    anchor = Path(primary['pmapFile'])
    source_folders = {Path(v['s2pFile']).resolve().parent for r in records
        for v in r.get('modelVariants', []) if Path(v['s2pFile']).is_file()}
    destinations = {folder: None for folder in source_folders}
    for root in library_roots:
        root = Path(root).resolve()
        if any(folder.is_relative_to(root) for folder in source_folders):
            destinations[root] = None
    failures = []
    for root in runtime_roots:
        root = Path(root).resolve()
        rebased = copy.deepcopy(records)
        matched_folders = set()
        for record in rebased:
            pn = record.get('selectedPn')
            if not pn:
                continue
            folder = root / pn
            for variant in record.get('modelVariants', []):
                s2p, rlc = folder / f'{pn}.s2p', folder / f'{pn}.rlc'
                if not s2p.is_file() or not rlc.is_file():
                    continue
                try:
                    if (hashlib.sha256(s2p.read_bytes()).hexdigest() != variant['s2pSha256']
                            or hashlib.sha256(rlc.read_bytes()).digest() != hashlib.sha256(Path(variant['rlcFile']).read_bytes()).digest()):
                        continue
                    variant.update(s2pFile=str(s2p), rlcFile=str(rlc))
                    proof = folder / f'{pn}.source.json'
                    if proof.is_file():
                        variant['sourceFile'] = str(proof)
                    matched_folders.add(folder)
                except OSError as error:
                    failures.append(f'{folder}: {error}')
        if matched_folders:
            destinations[root] = rebased
            for folder in matched_folders:
                destinations[folder] = rebased
    saved = []
    for destination, rebased in destinations.items():
        if destination == Path(mapping_paths[0]).resolve().parent:
            continue
        try:
            paths = write_model_mapping(anchor, copy.deepcopy(records if rebased is None else rebased),
                policy, destination_dir=destination)
            saved.extend(str(path.resolve()) for path in paths)
        except OSError as error:
            failures.append(f'{destination}: {error}')
    return saved, failures
