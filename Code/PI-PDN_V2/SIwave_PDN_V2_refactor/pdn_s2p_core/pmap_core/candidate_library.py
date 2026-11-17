"""제조사에 관계없이 저장된 공식 모델의 사양 후보를 읽는다. QC는 상위 서비스에서 수행한다."""
import hashlib
import json
from pathlib import Path

from .vendors.base import VendorModelCandidate


def library_candidates(roots):
    found = {}
    for root in dict.fromkeys(Path(r) for r in roots):
        if not root.exists():
            continue
        for source in root.rglob('*.source.json'):
            if source.name.endswith('.rlc.source.json'):
                continue
            try:
                data = json.loads(source.read_text(encoding='utf-8'))
                pn = data['partNumber']
                s2p = source.parent / f'{pn}.s2p'
                if data.get('sourceType') not in {'official_vendor_download', 'official_vendor_manual_import'}:
                    continue
                if not s2p.exists() or data.get('sha256') != hashlib.sha256(s2p.read_bytes()).hexdigest():
                    continue
                specification = data.get('componentSpecification')
                specification_file = s2p.with_suffix('.specification.json')
                if specification_file.exists():
                    spec = json.loads(specification_file.read_text(encoding='utf-8'))
                    evidence = Path(spec['evidenceFile'])
                    if (spec['s2pSha256'] != data['sha256'] or spec['partNumber'] != pn
                            or spec['manufacturer'] != data['manufacturer']
                            or hashlib.sha256(evidence.read_bytes()).hexdigest() != spec['evidenceSha256']):
                        continue
                    specification = spec['specification']
                if specification:
                    found[(data['manufacturer'], pn)] = VendorModelCandidate(pn, data['manufacturer'],
                        data.get('modelPartNumber', pn), specification,
                        {'localModelFolder': str(source.parent.resolve()), 'sourceSha256': data['sha256'],
                         'discoveryType': 'stored_official_model_specification'})
            except (ValueError, KeyError, OSError, TypeError):
                continue
        for report in root.rglob('comparison.json'):
            try:
                data = json.loads(report.read_text(encoding='utf-8'))
                for item in data.get('candidateInventory', []):
                    folder = report.parent / 'models' / item['pn']
                    if (folder / f"{item['pn']}.s2p").exists():
                        found.setdefault((item['manufacturer'], item['pn']), VendorModelCandidate(
                            item['pn'], item['manufacturer'], item['modelPn'], item['specification'],
                            {**item.get('provenance', {}), 'localModelFolder': str(folder.resolve()),
                             'discoveryType': 'stored_official_candidate_report'}))
            except (ValueError, KeyError, OSError, TypeError):
                continue
    return list(found.values())
