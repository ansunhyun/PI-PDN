"""GUI 없는 모델 확보·대체·검증·매핑 실행 진입점."""
import copy
import hashlib
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .config import ORIGINAL_SPECIFICATION_PATH, VENDOR_URL_TEMPLATES_JSON
from .pmap_core.analysis_spec import load_analysis_spec
from .pmap_core.model_mapping import model_mapping_record, write_model_mapping, save_mapping_with_models
from .pmap_core.model_service import ModelDownloader
from .pmap_core.models import PartEntry
from .pmap_core.operating_conditions import read_board_connections, resolve_pdn_scope
from .pmap_core.part_rules import identify_bom_part, get_component_info, normalize_maker_pn, manufacturer_from_text
from .pmap_core.vendor_download import VendorS2PDownloader, VendorDownloadFailure


def part_from_bom(lge_pn, technical_spec, manufacturer_text=''):
    """BOM Technical Spec과 Maker를 기존 제조사·품번 판정 규칙으로 해석한다."""
    return PartEntry(lge_pn=lge_pn, is_bom=True,
        **identify_bom_part(technical_spec, manufacturer_text))


class _StoredModelsOnly:
    """공식 저장 모델만 사용한다. 외부 HTTP를 호출하지 않는다."""
    timeout_sec = 20
    last_failure = None
    last_candidate_rejections = []

    def download_exact(self, pn, maker, folder):
        self.last_failure = VendorDownloadFailure('exact_pn_not_found', pn, maker, 'stored_models_only')
        return None

    def discover_same_vendor_candidates(self, specification, manufacturer):
        return [], ['live_network_not_executed']

    def discover_cross_vendor_candidates(self, specification, manufacturer):
        return [], ['live_network_not_executed']


UNAVAILABLE_CODES = {'exact_pn_not_found', 'official_s2p_link_not_found', 'official_pair_not_validated',
    'official_sparameter_investigation_required', 'unsupported_vendor', 'automation_capture_required',
    'empty_pn_cross_response', 'official_model_unavailable', 'official_model_link_not_found',
    'official_order_pn_resolution_required', 'official_pn_relationship_review_required', 'pn_classification_unresolved'}


def generate_models(parts, output_dir, *, analysis_spec=None, ndf_file=None, policy=None,
                    library_dirs=(), runtime_dirs=(), allow_network=True, filter_pdn_scope=True,
                    original_specification_catalog=None, batch_name='PDN_models', log=None):
    """PartEntry 또는 같은 키를 가진 dict 목록에서 공식 모델과 매핑을 확보한다.

    기본적으로 BOM은 Spec/NDF의 PDN net으로 제한하고 Inner Cap은 전부 처리한다.
    호출자가 이미 PDN capacitor만 골랐다면 filter_pdn_scope=False로 실행한다.
    ANSYS 복사 경로는 runtime_dirs로 명시한 경우에만 사용한다.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    resolved = copy.deepcopy(policy or {})
    resolved['substitutionBasis'] = 'bom_spec_adjacent'
    resolved['analysisType'] = 'PDN'
    resolved.setdefault('originalSpecificationCatalog', str(ORIGINAL_SPECIFICATION_PATH))
    if original_specification_catalog:
        resolved['originalSpecificationCatalog'] = str(Path(original_specification_catalog).resolve())
    if analysis_spec:
        resolved = load_analysis_spec(analysis_spec, resolved)
    if ndf_file:
        ndf = Path(ndf_file).resolve()
        resolved.setdefault('evidence', []).append({'path': str(ndf),
            'sha256': hashlib.sha256(ndf.read_bytes()).hexdigest()})
    libraries = [output / 'Approved_S2P_Library', *[Path(p).resolve() for p in library_dirs]]
    resolved['officialModelLibraryDirs'] = list(dict.fromkeys([
        *resolved.get('officialModelLibraryDirs', []), *map(str, libraries),
        str(output / 'BOM_Spec_Substitute_Library')]))
    vendor = VendorS2PDownloader.from_json_config(VENDOR_URL_TEMPLATES_JSON) if allow_network else _StoredModelsOnly()
    service = ModelDownloader(str(libraries[0]), search_dirs=libraries, vendor_downloader=vendor,
        registry_path=output / 'sparameter_registry.json')
    service.runtime_mirror_dirs = [Path(p).resolve() for p in runtime_dirs]
    connections = read_board_connections(resolved) if filter_pdn_scope else None
    records, messages, seen = [], [], set()
    for item in parts:
        part = item if isinstance(item, PartEntry) else PartEntry(**item)
        part = replace(part, maker_pn=normalize_maker_pn(part.maker_pn),
                       manufacturer=manufacturer_from_text(part.manufacturer) or part.manufacturer,
                       raw_manufacturer=part.raw_manufacturer or part.manufacturer)
        key = (part.lge_pn, part.raw_maker_pn or part.maker_pn, part.is_bom)
        if key in seen:
            continue
        seen.add(key)
        scope = resolve_pdn_scope(part, resolved, connections) if filter_pdn_scope else {
            'status': 'pdn_target', 'pdnPlacements': part.pdn_placements,
            'excludedPlacements': [], 'unresolvedPlacements': [], 'reasons': [],
            'downloadBasis': 'caller_selected_pdn_capacitors'}
        if scope['status'] != 'pdn_target':
            records.append(model_mapping_record(part, scope, scope['status']))
            continue
        maker = part.manufacturer or get_component_info(part.maker_pn)[0]
        folder, replacement = None, None
        if part.identity_detail:
            eligible = maker == 'Murata' and any(t in part.identity_detail for t in ('incomplete_grm_pn', 'pn_annotation'))
            if eligible:
                folder, message, replacement = service.download_characteristic(part, resolved)
            else:
                message = f'Excluded: bom_identity_confirmation_required; {part.identity_detail}'
        else:
            folder, message, replacement = service.download(part.maker_pn, maker)
            unavailable = getattr(vendor.last_failure, 'code', '') in UNAVAILABLE_CODES
            if not folder and ((maker == 'Murata' and part.maker_pn.startswith('GRM')) or (maker != 'Murata' and unavailable)):
                folder, message, replacement = service.download_characteristic(part, resolved)
        status = 'selected_bom_spec_substitute' if folder and replacement and replacement.selection_mode == 'bom_spec_substitute' else (
            'exact_model_selected' if folder else 'model_pending')
        records.append(model_mapping_record(part, scope, status, folder, replacement, message))
        messages.append({'originalPn': part.raw_maker_pn or part.maker_pn, 'status': status, 'message': message})
        if log:
            log(f'{part.maker_pn}: {message}')
    # 기존 매핑 스키마를 유지한다. 이 API는 Pmap 파일 자체를 생성하지 않는다.
    anchor = output / f'{batch_name}.pmap'
    mapping_paths = write_model_mapping(anchor, records, resolved)
    copies, failures = save_mapping_with_models(mapping_paths, records, resolved,
        library_roots=[libraries[0], output / 'BOM_Spec_Substitute_Library'], runtime_roots=service.runtime_mirror_dirs)
    counts = Counter(r['status'] for r in records)
    result = {'analysisType': 'PDN', 'allowNetwork': allow_network,
        'exactModels': counts['exact_model_selected'], 'substituteModels': counts['selected_bom_spec_substitute'],
        'modelPending': counts['model_pending'], 'purposeReview': counts['purpose_review'],
        'excludedNonPdn': counts['excluded_non_pdn'], 'pmapCreated': False,
        'pdnValidated': False, 'performanceSimilarityValidated': False,
        'mappingJson': str(mapping_paths[0]), 'mappingCsv': str(mapping_paths[1]),
        'mappingCopies': copies, 'mappingWriteFailures': failures,
        'records': records, 'messages': messages}
    (output / f'{batch_name}.result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
