"""원부품의 보완 사양과 저장된 공식 자료 근거를 읽는다. 대체 PN은 저장하지 않는다."""
import hashlib
import json
from pathlib import Path


def read_original_specification(path, requested_pn, manufacturer=''):
    path = Path(path)
    if not path.exists():
        return None
    catalog = json.loads(path.read_text(encoding='utf-8'))
    entry = catalog.get('parts', {}).get(requested_pn)
    if entry is None:
        return None
    if entry['partNumber'] != requested_pn:
        raise ValueError('original_specification_pn_mismatch')
    if manufacturer and manufacturer != entry['manufacturer']:
        raise ValueError('original_specification_manufacturer_conflict')
    if entry['specificationStatus'] not in ('official_exact_datasheet', 'inferred_from_official_series_and_code_documents'):
        raise ValueError('documented_original_specification_required')
    if not entry.get('evidence'):
        raise ValueError('original_specification_evidence_required')
    evidence = []
    for item in entry['evidence']:
        file = (path.parent / item['file']).resolve()
        if hashlib.sha256(file.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('original_specification_evidence_hash_mismatch')
        evidence.append({**item, 'file': str(file)})
    return {**entry, 'catalogFile': str(path.resolve()),
        'catalogSha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence': evidence}
