import json
import hashlib
import math
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pdn_s2p_core.pdn_pre_s2p import (
    estimate_series_rlc as estimate_pdn_series_rlc,
    extract_capacitance_f as extract_pdn_capacitance_f,
    infer_capacitor_package as infer_pdn_capacitor_package,
    write_model_files as write_pdn_model_files,
    validate_rlc_sidecar,
    compare_characteristic_candidates,
    candidate_model_coverage_reasons,
    component_specification_values,
    rank_bom_specifications,
)

from pdn_s2p_core.config import (
    APPROVED_MODEL_LIBRARY_DIR,
    DC_IMAG_TOLERANCE,
    DEFAULT_UNKNOWN_MAKER_TOKEN,
    ENABLE_VENDOR_DOWNLOAD,
    ENABLE_LOW_FREQ_NOISE_CHECK,
    ENABLE_LUMPED_FALLBACK,
    ENABLE_SIMILAR_MODEL_REPLACEMENT,
    EXPECTED_RLC_PINS,
    EXPECTED_SPARAMETER_PORTS,
    FALLBACK_CAPACITANCE_F,
    FALLBACK_ESL_H,
    FALLBACK_ESR_OHM,
    FALLBACK_REFERENCE_IMPEDANCE_OHM,
    LOW_FREQ_MAX_MAG_DELTA,
    LOW_FREQ_REGION_MAX_HZ,
    MIN_LOW_FREQ_POINT_COUNT,
    MIN_SPARAMETER_COVERAGE_HZ,
    MIN_SPARAMETER_POINT_COUNT,
    MODEL_FILE_ENCODING,
    MODEL_LIBRARY_SEARCH_DIRS,
    MODEL_REPLACEMENT_SCAN_LIMIT,
    MODEL_RUNTIME_MIRROR_DIRS,
    MOCK_DOWNLOAD_DELAY_SEC,
    REFRESH_OFFICIAL_VENDOR_MODEL,
    REQUIRE_CAUSALITY_FITTING,
    REQUIRE_DC_POINT,
    REQUIRE_PASSIVITY_ENFORCEMENT,
    REQUIRE_RECIPROCITY_CHECK,
    RLC_CAPACITANCE_REL_TOLERANCE,
    SPARAMETER_REGISTRY_PATH,
    ORIGINAL_SPECIFICATION_PATH,
    SUPPORTED_SPARAMETER_DATA_FORMATS,
    VENDOR_DOWNLOAD_TIMEOUT_SEC,
    VENDOR_URL_TEMPLATES_JSON,
)
from .models import ReplacementRecord
from .part_rules import extract_capacitance, get_component_info, normalize_maker_pn, manufacturer_from_text
from .sparameter_qc import classify_s2p_issue, read_s2p_qc_metadata
from .vendor_download import VendorS2PDownloader
from .component_spec import capacitor_specification, cross_vendor_specification_reasons, electrolytic_specification
from .operating_conditions import resolve_part_conditions
from .candidate_library import library_candidates
from .specification_evidence import read_original_specification


class ModelDownloader:
    def __init__(self, output_dir: str = str(APPROVED_MODEL_LIBRARY_DIR), search_dirs=None, vendor_downloader=None, registry_path=None) -> None:
        self.registry_path = Path(registry_path) if registry_path is not None else SPARAMETER_REGISTRY_PATH
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.search_dirs = [Path(p) for p in (search_dirs or MODEL_LIBRARY_SEARCH_DIRS)]
        self.runtime_mirror_dirs = [Path(p) for p in MODEL_RUNTIME_MIRROR_DIRS]
        self.vendor_downloader = vendor_downloader or VendorS2PDownloader.from_json_config(VENDOR_URL_TEMPLATES_JSON)
        self.vendor_downloader.timeout_sec = VENDOR_DOWNLOAD_TIMEOUT_SEC

    def download(self, part_number: str, manufacturer: str):
        safe_maker = str(manufacturer).lower().replace(" ", "")
        if safe_maker == "unknown":
            safe_maker = DEFAULT_UNKNOWN_MAKER_TOKEN
        return self._select_validated_model(part_number, manufacturer)

    def validate_official_pair_folder(self, folder, part_number, manufacturer):
        """공식 exact 모델의 출처·SHA·S2P·RLC를 검증하고 출처 메타데이터를 반환한다."""
        s2p = Path(folder) / f'{part_number}.s2p'
        source = json.loads(s2p.with_suffix('.source.json').read_text(encoding='utf-8'))
        if (not self._is_official_vendor_model(s2p) or source['manufacturer'] != manufacturer
                or source['partNumber'] != part_number or source['sha256'] != hashlib.sha256(s2p.read_bytes()).hexdigest()
                or not self._validate_model_pair(s2p, s2p.with_suffix('.rlc'))[0]
                or validate_rlc_sidecar(s2p, s2p.with_suffix('.rlc')) is None):
            raise ValueError('validated_official_exact_pair_required')
        return source

    def verified_official_manufacturer(self, part_number: str) -> str:
        """exact PN·출처 SHA·기존 품질 검사가 맞는 저장 모델의 제조사만 읽는다."""
        pn = normalize_maker_pn(part_number)
        for root in [self.output_dir, *self.search_dirs]:
            s2p = Path(root) / pn / f"{pn}.s2p"
            try:
                source = json.loads(s2p.with_suffix(".source.json").read_text(encoding="utf-8"))
                if not self._is_official_vendor_model(s2p):
                    continue
                if any(normalize_maker_pn(source.get(key, pn)) != pn for key in ("requestedPartNumber", "matchedPartNumber")):
                    continue
                if source.get("sha256") != hashlib.sha256(s2p.read_bytes()).hexdigest():
                    continue
                manufacturer = manufacturer_from_text(source.get("manufacturer", ""))
                if manufacturer and self._validate_model_pair(s2p, s2p.with_suffix(".rlc"))[0]:
                    return manufacturer
            except (OSError, ValueError, TypeError):
                continue
        return ""

    def download_characteristic(self, part, policy: dict):
        """후보 PN을 그대로 저장하고 사용 조건·PDN 결과가 검증될 때만 대체 승인한다."""
        if policy.get('substitutionBasis') == 'bom_spec_adjacent':
            return self._download_bom_spec_substitute(part, policy)
        policy = resolve_part_conditions(part, policy)
        requested = part.raw_maker_pn or part.maker_pn
        run = self.output_dir.parent / "Characteristic_Model_Investigations" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") / (part.lge_pn or part.maker_pn)
        run.mkdir(parents=True, exist_ok=True)
        report_path = run / "comparison.json"
        report = {"requestedPn": requested, "requestedManufacturer": part.manufacturer,
                  "lgePn": part.lge_pn, "sourceSpec": part.source_spec,
                  "sourceType": "characteristic_substitute_investigation", "status": "pending",
                  "policyEvidence": policy.get("evidence", []),
                  "partPlacementEvidence": policy.get("parts", {}).get(part.lge_pn or requested, {}).get("placementEvidence", []),
                  "candidateInventory": [], "reasons": []}
        try:
            settings = policy.get("parts", {}).get(part.lge_pn or requested, {})
            report['operatingConditionResolution'] = settings.get('conditionResolution', {})
            report['reasons'].extend(settings.get('conditionResolutionReasons', []))
            report['thicknessEvidence'] = settings.get('thicknessEvidence')
            if part.manufacturer == 'Samyoung':
                report['searchSpecification'] = electrolytic_specification(part)
                report['reasons'].append('electrolytic_official_model_candidate_search_required')
                report['candidateSearchOrder'] = ['same_manufacturer', 'other_electrolytic_manufacturers']
                report['modelAvailability'] = 'Official BXJ series search and PDF verified; no S2P/SPICE link in inspected materials; full ordering PN mapping unconfirmed.'
                report['candidateSearchStatus'] = 'electrolytic_model_sources_required'
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                return (None, f'Excluded characteristic: electrolytic official S2P/RLC candidate sources required; 100kHz max impedance is not ESR/ESL; report={report_path}', None)
            cross_vendor = part.manufacturer != 'Murata'
            discover = getattr(self.vendor_downloader, "discover_characteristic_candidates", None)
            if not callable(discover):
                raise ValueError("candidate_discovery_unavailable")
            if cross_vendor:
                expected_spec = capacitor_specification(part, settings)
                same_discover = getattr(self.vendor_downloader, 'discover_same_vendor_candidates', None)
                candidates, discovery_failures = same_discover({**expected_spec, 'requestedPn': part.maker_pn}, part.manufacturer) if callable(same_discover) else ([], [])
                report['searchSpecification'] = expected_spec
                report['candidateDiscoveryFailures'] = discovery_failures
                if expected_spec['thicknessMm'] is None:
                    report['reasons'].append('bom_thickness_required_for_cross_vendor_approval')
            else:
                expected_spec = None
                candidates = discover(part.maker_pn[:14], part.manufacturer)
            if candidates is None:
                failure = getattr(self.vendor_downloader, "last_failure", None)
                report.setdefault('candidateDiscoveryFailures', []).append(
                    failure.log_text() if failure else 'candidate_discovery_failed')
                candidates = []
            report['candidateRejections'] = list(getattr(self.vendor_downloader, 'last_candidate_rejections', []))
            stored = library_candidates([self.output_dir, *self.search_dirs,
                                         self.output_dir.parent / 'Characteristic_Model_Investigations',
                                         *policy.get('officialModelLibraryDirs', [])])
            # 초기 단계에는 원 제조사만 조사한다. 타 제조사는 공식 모델 확보 실패 후 조회한다.
            candidates = [c for c in candidates if c.manufacturer == part.manufacturer]
            known = {(c.manufacturer, c.part_number) for c in candidates}
            for candidate in stored:
                if (candidate.manufacturer, candidate.part_number) not in known:
                    if candidate.manufacturer != part.manufacturer:
                        continue
                    if not cross_vendor and candidate.model_part_number[:14] != part.maker_pn[:14]:
                        continue
                    candidates.append(candidate)
                    known.add((candidate.manufacturer, candidate.part_number))
            report['candidateSearchOrder'] = [{'tier': 'same_manufacturer', 'manufacturer': part.manufacturer, 'status': 'started'}]
            report['candidateSearchSources'] = {'implementedNetworkManufacturers': ['Murata', 'Samsung', 'Darfon'],
                'returnedNetworkManufacturers': sorted({c.manufacturer for c in candidates if not c.provenance.get('localModelFolder')}),
                'storedOfficialManufacturers': sorted({c.manufacturer for c in stored})}
            active_policy = {**policy, **settings}
            matched = []
            incomplete = []

            def ordered_candidates():
                nonlocal expected_spec
                yield from candidates
                qualified_same = []
                for model in list(matched):
                    if model['manufacturer'] != part.manufacturer:
                        continue
                    coverage_reasons = candidate_model_coverage_reasons(model, active_policy)
                    if coverage_reasons:
                        report.setdefault('candidateModelCoverageFailures', []).append({
                            'pn': model['pn'], 'manufacturer': model['manufacturer'], 'reasons': coverage_reasons})
                        report.setdefault('investigationOnlyCandidates', []).append({
                            'pn': model['pn'], 'manufacturer': model['manufacturer'],
                            'status': 'condition_or_band_not_qualified', 'models': [
                                {**v, 's2p': str(v['s2p']), 'rlc': str(v['rlc'])} for v in model['variants']]})
                        matched.remove(model)
                    else:
                        qualified_same.append(model)
                if qualified_same:
                    report['candidateSearchOrder'][0]['status'] = 'official_pair_found'
                    report['candidateSearchOrder'].append({'tier': 'other_manufacturers', 'status': 'skipped_same_manufacturer_pair_found'})
                    return
                report['candidateSearchOrder'][0]['status'] = 'no_matching_validated_official_pair'
                report['candidateSearchOrder'].append({'tier': 'other_manufacturers', 'status': 'started'})
                if expected_spec is None:
                    expected_spec = capacitor_specification(part, settings)
                cross_discover = getattr(self.vendor_downloader, 'discover_cross_vendor_candidates', None)
                others, failures = cross_discover(expected_spec, part.manufacturer) if callable(cross_discover) else ([], ['cross_vendor_candidate_discovery_unavailable'])
                report.setdefault('candidateDiscoveryFailures', []).extend(failures)
                report['candidateRejections'].extend(getattr(self.vendor_downloader, 'last_candidate_rejections', []))
                others.extend(c for c in stored if c.manufacturer != part.manufacturer)
                report['candidateSearchSources']['returnedNetworkManufacturers'] = sorted(set(
                    report['candidateSearchSources']['returnedNetworkManufacturers']) | {
                    c.manufacturer for c in others if not c.provenance.get('localModelFolder')})
                for candidate in others:
                    key = (candidate.manufacturer, candidate.part_number)
                    if candidate.manufacturer != part.manufacturer and key not in known:
                        known.add(key)
                        yield candidate

            for candidate in ordered_candidates():
                specification_context = cross_vendor or candidate.manufacturer != part.manufacturer
                if specification_context and not cross_vendor:
                    expected_spec = capacitor_specification(part, settings)
                    if expected_spec['thicknessMm'] is None and 'bom_thickness_required_for_cross_vendor_approval' not in report['reasons']:
                        report['reasons'].append('bom_thickness_required_for_cross_vendor_approval')
                spec = candidate.specification
                def numeric(key):
                    return float(spec[key]["value"])
                reasons = []
                try:
                    unit = spec["capacitance"]["unit"].replace("\u03bc", "u").replace("\u00b5", "u")
                    scale = {"pF": 1e-12, "nF": 1e-9, "uF": 1e-6}[unit]
                    if part.nominal_capacitance_f is None or not math.isclose(numeric("capacitance") * scale, part.nominal_capacitance_f, rel_tol=1e-6):
                        reasons.append("capacitance_mismatch_or_missing_bom_value")
                    voltage = re.search(r"\b(\d+(?:\.\d+)?)\s*V\b", part.source_spec)
                    dielectric = re.search(r"\b(?:X7R|X5R|C0G|NP0|X7S|X6S|X5S)\b", part.source_spec, re.I)
                    size = re.search(r"\b(\d{4})\s+(?:R/TP|TP)\b", part.source_spec)
                    if not voltage or numeric("ratedVoltageDc") != float(voltage[1]):
                        reasons.append("rated_voltage_mismatch_or_missing")
                    if not dielectric or spec["tempChara"]["value"].upper() != dielectric[0].upper():
                        reasons.append("dielectric_mismatch_or_missing")
                    if not size or not spec["sizeCodeInMm"]["value"].startswith(size[1]):
                        reasons.append("size_mismatch_or_missing")
                    if not all(math.isfinite(numeric(k)) and numeric(k) > 0 for k in ("length", "width", "thickness")):
                        reasons.append("physical_dimensions_missing")
                    # 공통 14자리의 크기·두께 코드도 정확히 같아야 한다.
                    if not specification_context and candidate.model_part_number[:14] != part.maker_pn[:14]:
                        reasons.append("specification_prefix_mismatch")
                    if specification_context:
                        for dimension, expected_key in (('length', 'lengthMm'), ('width', 'widthMm')):
                            if spec[dimension]['unit'] != 'mm' or not math.isclose(numeric(dimension), expected_spec[expected_key]):
                                reasons.append(f'{dimension}_mismatch_or_missing')
                        reasons.extend(r for r in cross_vendor_specification_reasons(expected_spec, spec)
                                       if r != 'bom_thickness_required_for_cross_vendor_approval')
                    thickness = settings.get("thicknessMm")
                    if thickness is not None and not math.isclose(numeric("thickness"), thickness):
                        reasons.append("thickness_mismatch")
                except (KeyError, ValueError, TypeError):
                    reasons.append("official_specification_fields_missing_or_invalid")
                report["candidateInventory"].append({"pn": candidate.part_number, "manufacturer": candidate.manufacturer,
                    "modelPn": candidate.model_part_number,
                    "specification": spec, "provenance": candidate.provenance, "specificationMatch": not reasons,
                    "reasons": reasons})
                if reasons:
                    continue
                destination = run / "models" / candidate.part_number
                local = candidate.provenance.get('localModelFolder')
                if local:
                    local_s2p = Path(local) / f'{candidate.part_number}.s2p'
                    try:
                        source = json.loads(local_s2p.with_suffix('.source.json').read_text(encoding='utf-8'))
                        if (not self._is_official_vendor_model(local_s2p)
                                or source.get('manufacturer') != candidate.manufacturer
                                or source.get('sha256') != hashlib.sha256(local_s2p.read_bytes()).hexdigest()
                                or not self._validate_model_pair(local_s2p, local_s2p.with_suffix('.rlc'))[0]
                                or validate_rlc_sidecar(local_s2p, local_s2p.with_suffix('.rlc')) is None):
                            raise ValueError('stored_official_pair_validation_failed')
                        shutil.copytree(local, destination)
                        download = True
                    except (ValueError, OSError, KeyError) as error:
                        incomplete.append(f'{candidate.part_number}: {error}')
                        download = False
                else:
                    download = self.vendor_downloader.download_exact(candidate.part_number, candidate.manufacturer, destination)
                variant_folders = [destination] if download else []
                if not download:
                    failure = getattr(self.vendor_downloader, "last_failure", None)
                    incomplete.append(f"{candidate.part_number}: {failure.log_text() if failure else 'official_pair_download_failed'}")
                variant_folders.extend(Path(path) for path in settings.get("modelVariantFolders", {}).get(candidate.part_number, []))
                variants = []
                for folder in variant_folders:
                    s2p = folder / f"{candidate.part_number}.s2p"
                    rlc = s2p.with_suffix(".rlc")
                    try:
                        source = json.loads(s2p.with_suffix(".source.json").read_text(encoding="utf-8"))
                        model_spec = source.get('componentSpecification', spec)
                        if settings.get('thicknessMm') is not None and not math.isclose(
                                float(model_spec['thickness']['value']), settings['thicknessMm']):
                            raise ValueError('official_model_thickness_mismatch')
                        if (not rlc.exists() or not self._is_official_vendor_model(s2p)
                                or source.get("manufacturer") != candidate.manufacturer
                                or source.get("sha256") != hashlib.sha256(s2p.read_bytes()).hexdigest()):
                            raise ValueError("official_pair_source_or_hash_invalid")
                        ok, why, maximum = self._validate_model_pair(s2p, rlc)
                        if not ok or validate_rlc_sidecar(s2p, rlc) is None:
                            raise ValueError(f"official_pair_validation_failed({why})")
                        minimum = self._read_s2p_metadata(s2p)["min_freq_hz"]
                        source['componentSpecification'] = model_spec
                        source['specificationProvenance'] = candidate.provenance
                        s2p.with_suffix('.source.json').write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
                        variants.append({"s2p": s2p, "rlc": rlc, "conditions": source["modelConditions"],
                            "s2pSha256": source["sha256"], "rlcSha256": hashlib.sha256(rlc.read_bytes()).hexdigest(),
                            "frequencyRangeHz": [minimum, maximum]})
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        incomplete.append(f"{candidate.part_number}: {error}")
                if variants:
                    matched.append({"pn": candidate.part_number, "manufacturer": candidate.manufacturer, "variants": variants})
            report['candidateModelFailures'] = incomplete
            if not matched:
                report['reasons'].extend(report.get('candidateDiscoveryFailures', []))
                report['reasons'].extend(incomplete)
                report['reasons'].extend(f"{m['pn']}: {reason}"
                    for m in report.get('candidateModelCoverageFailures', []) for reason in m['reasons'])
            discovery_reasons = list(report['reasons'])
            comparison = compare_characteristic_candidates(matched, active_policy, run)
            report.update(comparison)
            report['reasons'].extend(discovery_reasons)
            plan = {'sourceType': 'candidate_pdn_analysis_plan', 'requestedPn': requested,
                'requestedManufacturer': part.manufacturer, 'frequencyRangeHz': comparison['frequencyRangeHz'],
                'temperatureC': policy.get('temperatureC', 25), 'partPlacementEvidence': report['partPlacementEvidence'],
                'operatingConditionResolution': report.get('operatingConditionResolution'), 'jobs': []}
            for candidate in matched:
                for case in settings.get('cases', []):
                    condition = {'dcBiasV': case.get('voltageV'), 'temperatureC': case.get('temperatureC')}
                    variant = next((v for v in candidate['variants'] if v['conditions'] == condition), None)
                    model = next((m for c in report['candidates'] if c['pn'] == candidate['pn']
                                  for m in c['models'] if m['conditions'] == condition), None)
                    plan['jobs'].append({'manufacturer': candidate['manufacturer'], 'pn': candidate['pn'],
                        'rail': case['rail'], 'conditions': condition, 'designators': case.get('designators', []),
                        'modelReady': variant is not None, 's2pPath': str(variant['s2p']) if variant else None,
                        'rlcPath': str(variant['rlc']) if variant else None,
                        'requiredPdnResult': {'s2pSha256': variant['s2pSha256'] if variant else None,
                            'rlcSha256': variant['rlcSha256'] if variant else None,
                            'analysisBandBinding': model['analysisBandBinding'] if model else None,
                            'pdnCsv': case.get('candidateResults', {}).get(candidate['pn'], {}).get('pdnCsv')},
                        'nextAction': '해당 모델로 보드 PDN 결과 연결' if variant else '실제 전압·온도의 공식 모델 한 쌍 확보'})
            (run / 'analysis-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
            report['analysisPlan'] = str(run / 'analysis-plan.json')
            if not settings.get("cases"):
                report["reasons"].append("part_to_rail_assignment_and_case_results_required")
            if report["reasons"]:
                report["status"] = "pending"
                report.pop("selectedPn", None)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if report["status"] != "approved_characteristic_substitute":
                return None, f"Excluded: characteristic_substitute_pending; stage=characteristic_pdn_validation; report={report_path}; detail={'; '.join(report['reasons'][:3])}", None
            selected_pn = report["selectedPn"]
            selected = next(c for c in matched if c["pn"] == selected_pn)
            # Pmap용 파일은 실제 선택 PN으로 저장한다. 요청한 짧은 PN으로 이름을 바꾸지 않는다.
            selected_result = next(c for c in report["candidates"] if c["pn"] == selected_pn)
            worst_case = max(selected_result["pdnResults"], key=lambda r: r["limitRatio"])
            chosen = next(v for v in selected["variants"] if v["conditions"] == worst_case["conditions"])
            approved_root = self.output_dir.parent / "Approved_Characteristic_Library" / (part.lge_pn or part.maker_pn) / run.parent.name / selected_pn
            for variant in selected["variants"]:
                condition = variant["conditions"]
                variant_target = approved_root / f"DC{condition['dcBiasV']}V_{condition['temperatureC']}degC"
                variant_target.mkdir(parents=True, exist_ok=True)
                for suffix in (".s2p", ".source.json", ".rlc", ".rlc.source.json", ".mod"):
                    src = variant["s2p"].with_suffix(suffix)
                    if src.exists():
                        shutil.copy2(src, variant_target / src.name)
            condition = chosen["conditions"]
            target = approved_root / f"DC{condition['dcBiasV']}V_{condition['temperatureC']}degC"
            approval = {**report, "sourceType": "characteristic_substitute_approval", "approved": True,
                "selectedManufacturer": selected["manufacturer"], "comparisonReport": str(report_path),
                "selectedS2pSha256": chosen["s2pSha256"], "selectedRlcSha256": chosen["rlcSha256"],
                "selectedConditions": condition, "approvedModelFolder": str(target),
                'selectedAnalysisBandBinding': worst_case['analysisBandBinding']}
            (run / "approval.json").write_text(json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8")
            mirrors = self._mirror_to_runtime_dirs(selected_pn, target / f"{selected_pn}.s2p", target / f"{selected_pn}.rlc")
            record = ReplacementRecord(requested, selected_pn, "characteristic_substitute", str(run / "approval.json"), 1000, chosen["frequencyRangeHz"][1])
            self._upsert_registry(requested_pn=requested, selected_pn=selected_pn, selection_mode=record.selection_mode,
                reason=record.reason, s2p_path=target / f"{selected_pn}.s2p", max_freq_hz=record.max_freq_hz, approved=True)
            return target, f"Selected characteristic substitute ({selected['manufacturer']}/{selected_pn}); operating_cases_and_pdn_validated; report={report_path}{self._runtime_sync_note(*mirrors)}", record
        except (ValueError, OSError, TypeError) as error:
            report["reasons"].append(str(error))
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return None, f"Excluded: characteristic_substitute_pending; stage=characteristic_candidate_discovery; detail={error}; report={report_path}", None

    def _download_bom_spec_substitute(self, part, policy):
        """BOM 사양 기반 파일 확보. 실제 사용 전압과 보드 PDN 판정은 별도 단계다."""
        requested = part.raw_maker_pn or part.maker_pn
        run = self.output_dir.parent / 'Characteristic_Model_Investigations' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') / (part.lge_pn or part.maker_pn)
        run.mkdir(parents=True, exist_ok=True)
        report_path = run / 'comparison.json'
        report = {'requestedPn': requested, 'requestedManufacturer': part.manufacturer, 'lgePn': part.lge_pn,
            'sourceSpec': part.source_spec, 'sourceType': 'bom_specification_substitute_investigation',
            'selectionBasis': 'bom_spec_adjacent', 'status': 'pending', 'reasons': [], 'candidateInventory': [],
            'candidateSearchOrder': [], 'candidateRejections': [], 'candidateModelFailures': [], 'candidates': [],
            'pdnValidationStatus': 'not_evaluated', 'performanceSimilarityValidated': False,
            'analysisSpecUsedForModelApproval': False, 'percentageThresholdUsed': False}
        try:
            # 근거를 읽되 조건 해석의 보류 사유를 파일 확보의 승인 조건으로 사용하지 않는다.
            resolved = resolve_part_conditions(part, policy)
            settings = resolved.get('parts', {}).get(part.lge_pn or requested, {})
            supplemental = read_original_specification(
                policy.get('originalSpecificationCatalog', ORIGINAL_SPECIFICATION_PATH), requested, part.manufacturer)
            expected = capacitor_specification(part, settings, supplemental['specification'] if supplemental else None)
            search_manufacturer = part.manufacturer or (supplemental['manufacturer'] if supplemental else '')
            if supplemental:
                report.update(originalSpecificationEvidence=supplemental,
                    originalSpecificationStatus=supplemental['specificationStatus'],
                    resolvedOriginalManufacturer=search_manufacturer,
                    specificationInputPriority='bom_then_documented_supplement',
                    originalSpecificationLimitations=supplemental.get('limitations', []))
            report['searchSpecification'] = expected
            stored = library_candidates([self.output_dir, *self.search_dirs,
                self.output_dir.parent / 'Characteristic_Model_Investigations', *policy.get('officialModelLibraryDirs', [])])
            visited = set()
            for same, neighborhood in ((True, False), (True, True), (False, False), (False, True)):
                search_spec = {**expected, 'requestedPn': part.maker_pn,
                    'selectionMode': 'bom_spec_adjacent', 'searchNeighborhood': neighborhood}
                tier = {'tier': 'same_manufacturer' if same else 'other_manufacturers',
                    'specificationStage': 'adjacent_levels' if neighborhood else 'same_specification', 'status': 'started'}
                report['candidateSearchOrder'].append(tier)
                discovery = getattr(self.vendor_downloader,
                    'discover_same_vendor_candidates' if same else 'discover_cross_vendor_candidates', None)
                candidates, failures = discovery(search_spec, search_manufacturer) if callable(discovery) else ([], ['candidate_discovery_unavailable'])
                report.setdefault('candidateDiscoveryFailures', []).extend(failures)
                report['candidateRejections'].extend(getattr(self.vendor_downloader, 'last_candidate_rejections', []))
                candidates = [c for c in (candidates or []) if (c.manufacturer == search_manufacturer) == same]
                known = {(c.manufacturer, c.part_number) for c in candidates}
                candidates.extend(c for c in stored if (c.manufacturer == search_manufacturer) == same
                    and (c.manufacturer, c.part_number) not in known)
                rows, lookup = [], {}
                for candidate in candidates:
                    key = (candidate.manufacturer, candidate.part_number)
                    try:
                        values = component_specification_values(candidate.specification)
                        row = {'pn': candidate.part_number, 'manufacturer': candidate.manufacturer, 'spec': values}
                        rows.append(row)
                        lookup[key] = candidate
                        report['candidateInventory'].append({**row, 'specification': candidate.specification,
                            'modelPn': candidate.model_part_number, 'provenance': candidate.provenance})
                    except (KeyError, ValueError, TypeError) as error:
                        report['candidateRejections'].append({'pn': candidate.part_number, 'reasons': [str(error)]})
                ranked, rejections = rank_bom_specifications(expected, rows,
                    max_thickness_mm=settings.get('maximumMountingHeightMm'))
                report['candidateRejections'].extend(rejections)
                if not neighborhood:
                    ranked = [r for r in ranked if r['matchTier'] == 'same_specification']
                for selected in ranked:
                    pn, maker = selected['pn'], selected['manufacturer']
                    key = (maker, pn)
                    if key in visited:
                        continue
                    visited.add(key)
                    candidate = lookup[key]
                    folder = run / 'models' / pn
                    local = candidate.provenance.get('localModelFolder')
                    if local:
                        shutil.copytree(local, folder, dirs_exist_ok=True)
                    elif self.vendor_downloader.download_exact(pn, maker, folder) is None:
                        failure = getattr(self.vendor_downloader, 'last_failure', None)
                        report['candidateModelFailures'].append({'pn': pn, 'reason': failure.log_text() if failure else 'official_pair_download_failed'})
                        continue
                    s2p, rlc = folder / f'{pn}.s2p', folder / f'{pn}.rlc'
                    try:
                        source = json.loads(s2p.with_suffix('.source.json').read_text(encoding='utf-8'))
                        if (not self._is_official_vendor_model(s2p) or source.get('manufacturer') != maker
                                or source.get('partNumber') != pn
                                or source.get('sha256') != hashlib.sha256(s2p.read_bytes()).hexdigest()):
                            raise ValueError('official_pair_identity_or_hash_invalid')
                        ok, why, maximum = self._validate_model_pair(s2p, rlc)
                        if not ok or validate_rlc_sidecar(s2p, rlc) is None:
                            raise ValueError(f'official_pair_validation_failed({why})')
                        # 선택 때 본 공식 사양과 파일에 기록된 사양을 다시 대조한다.
                        actual_spec = source.get('componentSpecification', candidate.specification)
                        actual = component_specification_values(actual_spec)
                        if actual != selected['spec']:
                            raise ValueError('downloaded_model_specification_changed')
                        source.update(componentSpecification=actual_spec, specificationProvenance=candidate.provenance)
                        s2p.with_suffix('.source.json').write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        report['candidateModelFailures'].append({'pn': pn, 'reason': str(error)})
                        continue
                    target = self.output_dir.parent / 'BOM_Spec_Substitute_Library' / (part.lge_pn or part.maker_pn) / run.parent.name / pn
                    target.mkdir(parents=True, exist_ok=True)
                    for suffix in ('.s2p', '.rlc', '.source.json', '.rlc.source.json', '.mod'):
                        src = s2p.with_suffix(suffix)
                        if src.exists():
                            shutil.copy2(src, target / src.name)
                    tier['status'] = 'selected'
                    report.update(status='selected_bom_spec_substitute', selectedPn=pn, selectedManufacturer=maker,
                        specificationMatch=selected['matchTier'], specificationDifferences=selected['changes'],
                        unconfirmedBomFields=selected['unconfirmedBomFields'], selectedSpecification=actual,
                        selectedConditions=source.get('modelConditions'), selectedS2pSha256=source['sha256'],
                        selectedRlcSha256=hashlib.sha256(rlc.read_bytes()).hexdigest(), selectedModelFolder=str(target),
                        providedFrequencyRangeHz=[self._read_s2p_metadata(s2p)['min_freq_hz'], maximum],
                        selectionReason='same_manufacturer_first; same_specification_before_single_adjacent_level; file_QC_pass')
                    report['candidates'] = [{'pn': pn, 'manufacturer': maker, 'status': 'selected_bom_spec_substitute',
                        'models': [{'conditions': source.get('modelConditions'), 's2p': str(target / s2p.name), 'rlc': str(target / rlc.name)}]}]
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    approval_path = run / 'approval.json'
                    approval_path.write_text(json.dumps({**report, 'approved': True,
                        'approvalScope': 'documented_specification_and_model_file_QC' if supplemental else 'bom_specification_and_model_file_QC',
                        'pdnValidated': False}, ensure_ascii=False, indent=2), encoding='utf-8')
                    mirrors = self._mirror_to_runtime_dirs(pn, target / s2p.name, target / rlc.name)
                    record = ReplacementRecord(requested, pn, 'bom_spec_substitute', str(approval_path), 1000, maximum)
                    self._upsert_registry(requested_pn=requested, selected_pn=pn, selection_mode=record.selection_mode,
                        reason=record.reason, s2p_path=target / s2p.name, max_freq_hz=maximum, approved=True)
                    return target, f'Selected BOM specification substitute ({maker}/{pn}); PDN_not_evaluated; report={report_path}{self._runtime_sync_note(*mirrors)}', record
                tier['status'] = 'no_qualified_model_pair'
            report['reasons'].append('no_same_or_single_adjacent_specification_model_pair')
        except (OSError, ValueError, KeyError, TypeError) as error:
            report['reasons'].append(str(error))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return None, f"Excluded: characteristic_substitute_pending; stage=bom_specification_selection; report={report_path}; detail={'; '.join(report['reasons'])}", None

    def prepare_official_condition_pair(self, part_number, manufacturer, s2p_path, netlist_path,
                                        source_url, netlist_url, destination):
        """공식 조건별 파일을 QC한 후 조사 라이브러리에 준비한다. 대체 승인은 별도다."""
        pn = normalize_maker_pn(part_number)
        target = Path(destination)
        with tempfile.TemporaryDirectory(prefix="pmap_condition_pair_") as temporary:
            staged = self.vendor_downloader.register_local_condition_pair(pn, manufacturer,
                Path(s2p_path), Path(netlist_path), source_url, netlist_url, Path(temporary))
            ok, reason, _ = self._validate_model_pair(staged.s2p_path, staged.s2p_path.with_suffix(".rlc"))
            if not ok:
                raise ValueError(f"official_condition_pair_quality_failed: {reason}")
            if target.exists() and any(target.iterdir()):
                raise ValueError("condition_pair_destination_must_be_empty")
            target.mkdir(parents=True, exist_ok=True)
            for source in Path(temporary).iterdir():
                shutil.copy2(source, target / source.name)
        return target

    def register_official_file(
        self,
        part_number: str,
        manufacturer: str,
        source_path: str | Path,
        source_url: str,
        notes: str = "",
    ):
        requested_pn = normalize_maker_pn(part_number)
        if not requested_pn:
            return None, "Manual registration failed (invalid part number)", None
        try:
            with tempfile.TemporaryDirectory(prefix="pmap_manual_vendor_") as temp_root:
                staged_dir = Path(temp_root) / requested_pn
                result = self.vendor_downloader.register_local_exact(
                    requested_pn,
                    manufacturer,
                    Path(source_path),
                    source_url,
                    staged_dir,
                    notes,
                )
                staged_rlc = staged_dir / f"{requested_pn}.rlc"
                quality_ok, invalid_reason, max_freq_hz = self._validate_model_pair(
                    result.s2p_path,
                    staged_rlc,
                )
                if not quality_ok:
                    return None, f"Manual registration rejected ({invalid_reason})", None

                target_dir = self.output_dir / requested_pn
                target_dir.mkdir(parents=True, exist_ok=True)
                target_s2p = target_dir / f"{requested_pn}.s2p"
                target_rlc = target_dir / f"{requested_pn}.rlc"
                target_provenance = target_dir / f"{requested_pn}.source.json"
                existing_generated = self._is_app_generated_model(target_s2p, target_rlc)
                if target_s2p.exists() and not existing_generated:
                    existing_sha = hashlib.sha256(target_s2p.read_bytes()).hexdigest()
                    staged_sha = hashlib.sha256(result.s2p_path.read_bytes()).hexdigest()
                    if existing_sha == staged_sha:
                        rlc_note = self._ensure_official_rlc(requested_pn, manufacturer, target_s2p)
                        synced, total, failures = self._mirror_to_runtime_dirs(requested_pn, target_s2p, target_rlc)
                        return target_dir, (
                            "Official S2P already registered (duplicate SHA-256; S2P unchanged)"
                            + rlc_note + self._runtime_sync_note(synced, total, failures)
                        ), ReplacementRecord(
                            requested_pn,
                            requested_pn,
                            "exact_existing",
                            "official_vendor_manual_import_duplicate",
                            1000,
                            max_freq_hz,
                        )
                    return None, (
                        "Manual registration rejected (a different approved official model already exists)"
                    ), None
                if existing_generated and target_rlc.exists():
                    target_rlc.unlink()
                shutil.copy2(result.s2p_path, target_s2p)
                shutil.copy2(result.provenance_path, target_provenance)

                rlc_note = self._ensure_official_rlc(requested_pn, manufacturer, target_s2p)

                synced_count, sync_total, sync_failures = self._mirror_to_runtime_dirs(
                    requested_pn,
                    target_s2p,
                    target_rlc,
                )
                sync_note = self._runtime_sync_note(synced_count, sync_total, sync_failures)
                self._upsert_registry(
                    requested_pn=requested_pn,
                    selected_pn=requested_pn,
                    selection_mode="exact_existing",
                    reason="official_vendor_manual_import",
                    s2p_path=target_s2p,
                    max_freq_hz=max_freq_hz,
                    approved=True,
                )
                return target_dir, (
                    f"Registered official vendor S2P manually (QC pass, "
                    f"{EXPECTED_SPARAMETER_PORTS}port, max_freq={max_freq_hz:.3e}Hz)"
                    f"{rlc_note}{sync_note}"
                ), ReplacementRecord(
                    requested_pn,
                    requested_pn,
                    "exact_existing",
                    "official_vendor_manual_import",
                    1000,
                    max_freq_hz,
                )
        except Exception as err:
            return None, f"Manual registration failed ({err})", None

    def _select_validated_model(self, part_number: str, manufacturer: str = "Unknown"):
        try:
            time.sleep(MOCK_DOWNLOAD_DELAY_SEC)
            requested_pn = normalize_maker_pn(part_number)
            pn_dir = self.output_dir / requested_pn
            target_s2p = pn_dir / f"{requested_pn}.s2p"
            target_rlc = pn_dir / f"{requested_pn}.rlc"

            generated_model = self._is_app_generated_model(target_s2p, target_rlc)
            official_model = self._is_official_vendor_model(target_s2p)
            vendor_note = ""
            should_download = ENABLE_VENDOR_DOWNLOAD and (
                REFRESH_OFFICIAL_VENDOR_MODEL or generated_model or not target_s2p.exists()
            )
            if should_download:
                downloaded = self.vendor_downloader.download_exact(requested_pn, manufacturer, pn_dir)
                if downloaded is not None:
                    if generated_model and target_rlc.exists() and not target_rlc.with_suffix(".rlc.source.json").exists():
                        target_rlc.unlink()
                    generated_model = False
                    official_model = True
                    vendor_note = f"; official_source={downloaded.source_url}"
                else:
                    failure = getattr(self.vendor_downloader, "last_failure", None)
                    failure_text = failure.log_text() if failure is not None else "not_found"
                    vendor_note = f"; official_vendor_download={failure_text}"

            rlc_note = self._ensure_official_rlc(requested_pn, manufacturer, target_s2p) if official_model and not generated_model else ""
            if generated_model:
                quality_ok, invalid_reason, max_freq_hz = False, "estimated_model_not_accepted", 0.0
            else:
                quality_ok, invalid_reason, max_freq_hz = self._validate_model_pair(target_s2p, target_rlc)
            if quality_ok:
                synced_count, sync_total, sync_failures = self._mirror_to_runtime_dirs(requested_pn, target_s2p, target_rlc)
                sync_note = self._runtime_sync_note(synced_count, sync_total, sync_failures)
                selection_reason = "official_vendor_download" if official_model else "already_available"
                self._upsert_registry(
                    requested_pn=requested_pn,
                    selected_pn=requested_pn,
                    selection_mode="exact_existing",
                    reason=selection_reason,
                    s2p_path=target_s2p,
                    max_freq_hz=max_freq_hz,
                    approved=True,
                )
                action = "Selected official vendor model" if official_model else "Selected existing validated model"
                return target_s2p.parent, (
                    f"{action} (>=1GHz, QC pass, {EXPECTED_SPARAMETER_PORTS}port, max_freq={max_freq_hz:.3e}Hz)"
                    f"{vendor_note}{rlc_note}{sync_note}"
                ), ReplacementRecord(requested_pn, requested_pn, "exact_existing", selection_reason, 1000, max_freq_hz)

            replacement = self._find_replacement_model(requested_pn)
            if replacement is not None:
                src_s2p, src_rlc, _, mode, replacement_reason, similarity_score = replacement
                selected_pn = normalize_maker_pn(src_s2p.stem)
                pn_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_s2p, target_s2p)
                if src_rlc and src_rlc.exists():
                    shutil.copy2(src_rlc, target_rlc)
                if selected_pn == requested_pn:
                    # 공식 exact 파일을 재사용할 때 QC에 필요한 출처와 원본 회로도 함께 옮긴다.
                    for suffix in ('.source.json', '.rlc.source.json', '.mod', '.specification.json'):
                        sidecar = src_s2p.with_suffix(suffix)
                        if sidecar.exists():
                            shutil.copy2(sidecar, target_s2p.with_suffix(suffix))
                copied_ok, copied_reason, copied_max_freq = self._validate_model_pair(target_s2p, target_rlc)
                if not copied_ok:
                    return None, f"Excluded: copied model failed quality gate ({copied_reason})", None

                synced_count, sync_total, sync_failures = self._mirror_to_runtime_dirs(requested_pn, target_s2p, target_rlc)
                sync_note = self._runtime_sync_note(synced_count, sync_total, sync_failures)
                self._upsert_registry(
                    requested_pn=requested_pn,
                    selected_pn=selected_pn,
                    selection_mode=mode,
                    reason=replacement_reason,
                    s2p_path=target_s2p,
                    max_freq_hz=copied_max_freq,
                    approved=True,
                )

                record = ReplacementRecord(
                    requested_pn,
                    selected_pn,
                    mode,
                    f"requested_invalid={invalid_reason}; selected_reason={replacement_reason}",
                    similarity_score,
                    copied_max_freq,
                )
                msg = (
                    f"Selected replacement validated model ({selected_pn}, {mode}, "
                    f"score={similarity_score}, DC/QC pass, >=1GHz, {EXPECTED_SPARAMETER_PORTS}port, max_freq={copied_max_freq:.3e}Hz{sync_note})"
                )
                return pn_dir, msg, record

            if ENABLE_LUMPED_FALLBACK:
                capacitance_f, esr_ohm, esl_h, package = self._write_lumped_fallback_model(target_s2p, target_rlc)
                fallback_ok, fallback_reason, fallback_max_freq = self._validate_model_pair(target_s2p, target_rlc)
                if fallback_ok:
                    synced_count, sync_total, sync_failures = self._mirror_to_runtime_dirs(requested_pn, target_s2p, target_rlc)
                    sync_note = self._runtime_sync_note(synced_count, sync_total, sync_failures)
                    self._upsert_registry(
                        requested_pn=requested_pn,
                        selected_pn=requested_pn,
                        selection_mode="lumped_fallback",
                        reason=(
                            f"fallback_after={invalid_reason}; estimated_series_rlc="
                            f"C:{capacitance_f:.3e}F,R:{esr_ohm:.3e}ohm,L:{esl_h:.3e}H,package:{package}"
                        ),
                        s2p_path=target_s2p,
                        max_freq_hz=fallback_max_freq,
                        approved=True,
                    )
                    return pn_dir, (
                        f"Selected estimated series-RLC fallback model "
                        f"(C={capacitance_f:.3e}F, ESR={esr_ohm:.3e}ohm, "
                        f"ESL={esl_h:.3e}H, package={package}, QC pass){sync_note}"
                    ), ReplacementRecord(
                        requested_pn,
                        requested_pn,
                        "lumped_fallback",
                        (
                            f"fallback_after={invalid_reason}; estimated_series_rlc="
                            f"C:{capacitance_f:.3e}F,R:{esr_ohm:.3e}ohm,L:{esl_h:.3e}H,package:{package}"
                        ),
                        1000,
                        fallback_max_freq,
                    )
                return None, f"Excluded: fallback model failed quality gate ({fallback_reason})", None

            return None, (
                f"Excluded: no validated model in Pmap/library ({invalid_reason})"
                f"{vendor_note}"
            ), None
        except PermissionError as err:
            return None, f"permission_denied({err})", None
        except Exception as err:
            return None, f"Download/update failed ({err})", None

    def _runtime_sync_note(self, synced_count: int, total_count: int, failed_targets: list[str]) -> str:
        if total_count <= 0:
            return ""
        if synced_count >= total_count:
            return f"; runtime_sync={synced_count}/{total_count}"
        if not failed_targets:
            return f"; runtime_sync={synced_count}/{total_count}"
        preview = "; ".join(failed_targets[:2])
        if len(failed_targets) > 2:
            preview += "; ..."
        return f"; runtime_sync={synced_count}/{total_count}, failed={preview}"

    def _is_app_generated_model(self, s2p_path: Path, rlc_path: Path) -> bool:
        markers = (
            "estimated series rlc",
            "estimated series rlc fallback",
        )
        # S2P 승인 여부는 S2P 원본으로 판단한다. 보조 회로의 출처와 분리한다.
        for path in (s2p_path,):
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding=MODEL_FILE_ENCODING, errors="ignore") as f:
                    header = "\n".join(f.readline() for _ in range(6)).lower()
            except OSError:
                continue
            if any(marker in header for marker in markers):
                return True
        return False

    def _ensure_official_rlc(self, pn: str, manufacturer: str, s2p_path: Path) -> str:
        ensure = getattr(self.vendor_downloader, "ensure_official_rlc", None)
        if ensure is None:
            return ""
        _, note = ensure(pn, manufacturer, s2p_path)
        return note

    def _is_official_vendor_model(self, s2p_path: Path) -> bool:
        provenance_path = s2p_path.with_suffix(".source.json")
        if not provenance_path.exists():
            return False
        try:
            loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            loaded.get("sourceType") in {
                "official_vendor_download",
                "official_vendor_manual_import",
            }
            and normalize_maker_pn(loaded.get("partNumber", "")) == normalize_maker_pn(s2p_path.stem)
        )

    def _mirror_to_runtime_dirs(self, requested_pn: str, source_s2p: Path, source_rlc: Path | None) -> tuple[int, int, list[str]]:
        if not source_s2p.exists():
            return 0, 0, []

        unique_roots: list[Path] = []
        seen_roots: set[str] = set()
        for root in self.runtime_mirror_dirs:
            normalized = str(Path(root).resolve()).lower()
            if normalized in seen_roots:
                continue
            seen_roots.add(normalized)
            unique_roots.append(Path(root))

        synced = 0
        failures: list[str] = []
        source_s2p_resolved = source_s2p.resolve()
        source_rlc_resolved = source_rlc.resolve() if source_rlc and source_rlc.exists() else None

        for root in unique_roots:
            target_dir = root / requested_pn
            target_s2p = target_dir / f"{requested_pn}.s2p"
            target_rlc = target_dir / f"{requested_pn}.rlc"
            try:
                target_s2p_resolved = target_s2p.resolve()
                if target_s2p_resolved == source_s2p_resolved:
                    synced += 1
                    continue

                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_s2p, target_s2p)
                if source_rlc_resolved is not None:
                    shutil.copy2(source_rlc_resolved, target_rlc)
                for suffix in (".source.json", ".rlc.source.json", ".mod"):
                    companion = source_s2p.with_suffix(suffix)
                    if companion.exists():
                        shutil.copy2(companion, target_s2p.with_suffix(suffix))
                synced += 1
            except OSError as err:
                failures.append(f"{target_dir} ({err})")

        return synced, len(unique_roots), failures
    def _extract_snp_port_count(self, path: Path) -> int:
        match = re.search(r"\.s(\d+)p$", path.name.lower())
        return int(match.group(1)) if match else 0

    def _read_s2p_metadata(self, s2p_path: Path):
        return read_s2p_qc_metadata(
            s2p_path,
            dc_imag_tolerance=DC_IMAG_TOLERANCE,
            low_freq_region_max_hz=LOW_FREQ_REGION_MAX_HZ,
        )

    def _classify_invalid_sparameter(self, s2p_path: Path) -> str:
        return classify_s2p_issue(s2p_path)

    def _read_rlc_pin_count(self, rlc_path: Path) -> int:
        if not rlc_path.exists():
            return 0
        try:
            with open(rlc_path, "r", encoding=MODEL_FILE_ENCODING, errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("*"):
                        continue
                    if line.lower().startswith(".subckt"):
                        tokens = line.split()
                        return max(len(tokens) - 2, 0) if len(tokens) >= 4 else 0
        except OSError:
            return 0
        return 0

    def _parse_spice_value(self, token: str) -> float | None:
        match = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(meg|[fpnumk]?)",
            str(token).strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        scale = {
            "": 1.0,
            "f": 1e-15,
            "p": 1e-12,
            "n": 1e-9,
            "u": 1e-6,
            "m": 1e-3,
            "k": 1e3,
            "meg": 1e6,
        }
        return float(match.group(1)) * scale[match.group(2).lower()]

    def _read_rlc_capacitance_f(self, rlc_path: Path) -> float | None:
        if not rlc_path.exists():
            return None
        try:
            with open(rlc_path, "r", encoding=MODEL_FILE_ENCODING, errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(("*", ";", ".")):
                        continue
                    tokens = line.split()
                    if len(tokens) >= 4 and tokens[0].upper().startswith("C"):
                        return self._parse_spice_value(tokens[-1])
        except OSError:
            return None
        return None

    def _known_capacitances_match(self, requested_pn: str, candidate_pn: str) -> bool:
        requested = self._capacitance_from_part_number(requested_pn)
        candidate = self._capacitance_from_part_number(candidate_pn)
        requested_known = extract_capacitance(requested_pn) != "Unknown"
        candidate_known = extract_capacitance(candidate_pn) != "Unknown"
        if not requested_known or not candidate_known:
            return True
        return math.isclose(
            requested,
            candidate,
            rel_tol=RLC_CAPACITANCE_REL_TOLERANCE,
            abs_tol=0.0,
        )

    def _validate_model_pair(self, s2p_path: Path, rlc_path: Path):
        metadata = self._read_s2p_metadata(s2p_path)
        if metadata is None:
            return False, self._classify_invalid_sparameter(s2p_path), 0.0
        if not metadata["line_shape_ok"]:
            return False, "touchstone_line_shape_invalid", metadata["max_freq_hz"]
        if metadata["option_param"] != "s":
            return False, f"non_sparameter_type({metadata['option_param']})", metadata["max_freq_hz"]
        if metadata["data_format"] not in SUPPORTED_SPARAMETER_DATA_FORMATS:
            return False, f"unsupported_data_format({metadata['data_format']})", metadata["max_freq_hz"]
        if metadata["ports"] != EXPECTED_SPARAMETER_PORTS:
            return False, f"port_mismatch(s{metadata['ports']}p)", metadata["max_freq_hz"]
        if metadata["points"] < MIN_SPARAMETER_POINT_COUNT:
            return False, f"too_few_points({metadata['points']})", metadata["max_freq_hz"]
        if not metadata["monotonic"]:
            return False, "non_monotonic_frequency", metadata["max_freq_hz"]
        official_vendor_model = self._is_official_vendor_model(s2p_path)
        if REQUIRE_DC_POINT and (not official_vendor_model) and (not metadata["has_dc_point"]):
            return False, "missing_dc_point(0Hz)", metadata["max_freq_hz"]
        if (not official_vendor_model) and metadata["dc_imag_max"] > DC_IMAG_TOLERANCE:
            return (
                False,
                f"dc_imag_nonzero(max={metadata['dc_imag_max']:.3e},tol={DC_IMAG_TOLERANCE:.1e})",
                metadata["max_freq_hz"],
            )
        if (not official_vendor_model) and metadata["low_freq_point_count"] < MIN_LOW_FREQ_POINT_COUNT:
            return (
                False,
                f"low_freq_density_insufficient(points={metadata['low_freq_point_count']},min={MIN_LOW_FREQ_POINT_COUNT})",
                metadata["max_freq_hz"],
            )
        if (not official_vendor_model) and ENABLE_LOW_FREQ_NOISE_CHECK and (metadata["max_lowfreq_mag_delta"] > LOW_FREQ_MAX_MAG_DELTA):
            return (
                False,
                f"low_freq_jump_detected(delta={metadata['max_lowfreq_mag_delta']:.3e},limit={LOW_FREQ_MAX_MAG_DELTA:.3e})",
                metadata["max_freq_hz"],
            )
        if (not official_vendor_model) and REQUIRE_PASSIVITY_ENFORCEMENT and (not metadata["has_passivity_tag"]):
            return False, "missing_passivity_enforcement_tag", metadata["max_freq_hz"]
        if (not official_vendor_model) and REQUIRE_CAUSALITY_FITTING and (not metadata["has_causality_tag"]):
            return False, "missing_causality_fitting_tag", metadata["max_freq_hz"]
        if REQUIRE_RECIPROCITY_CHECK and (not metadata["has_reciprocity_tag"]):
            return False, "missing_reciprocity_check_tag", metadata["max_freq_hz"]
        if metadata["min_freq_hz"] > MIN_SPARAMETER_COVERAGE_HZ:
            return False, f"missing_1ghz_range(min={metadata['min_freq_hz']:.3e}Hz, max={metadata['max_freq_hz']:.3e}Hz)", metadata["max_freq_hz"]
        if metadata["max_freq_hz"] < MIN_SPARAMETER_COVERAGE_HZ:
            return False, f"insufficient_freq({metadata['max_freq_hz']:.3e}Hz)", metadata["max_freq_hz"]
        pin_count = self._read_rlc_pin_count(rlc_path)
        if pin_count not in (0, EXPECTED_RLC_PINS):
            return False, f"pin_mismatch({pin_count})", metadata["max_freq_hz"]
        expected_capacitance_text = extract_capacitance(s2p_path.stem)
        try:
            official_circuit = validate_rlc_sidecar(s2p_path, rlc_path) if official_vendor_model else None
        except (ValueError, OSError) as error:
            return False, f"official_rlc_invalid({error})", metadata["max_freq_hz"]
        actual_capacitance_f = self._read_rlc_capacitance_f(rlc_path)
        if official_circuit is None and expected_capacitance_text != "Unknown" and actual_capacitance_f is not None:
            expected_capacitance_f = self._capacitance_from_part_number(s2p_path.stem)
            if not math.isclose(
                expected_capacitance_f,
                actual_capacitance_f,
                rel_tol=RLC_CAPACITANCE_REL_TOLERANCE,
                abs_tol=0.0,
            ):
                return (
                    False,
                    f"capacitance_mismatch(expected={expected_capacitance_f:.3e}F,rlc={actual_capacitance_f:.3e}F)",
                    metadata["max_freq_hz"],
                )
        return True, "ok", metadata["max_freq_hz"]

    def _capacitance_from_part_number(self, part_number: str) -> float:
        capacitance_f = extract_pdn_capacitance_f(part_number)
        return max(capacitance_f if capacitance_f is not None else FALLBACK_CAPACITANCE_F, FALLBACK_CAPACITANCE_F)

    def _infer_capacitor_package(self, part_number: str) -> str:
        return infer_pdn_capacitor_package(part_number)

    def _estimate_series_rlc(self, part_number: str) -> tuple[float, float, float, str]:
        model = estimate_pdn_series_rlc(
            part_number,
            default_capacitance_f=FALLBACK_CAPACITANCE_F,
            default_esr_ohm=FALLBACK_ESR_OHM,
            default_esl_h=FALLBACK_ESL_H,
            reference_impedance_ohm=FALLBACK_REFERENCE_IMPEDANCE_OHM,
        )
        return model.capacitance_f, model.esr_ohm, model.esl_h, model.package

    def _write_lumped_fallback_model(self, s2p_path: Path, rlc_path: Path) -> tuple[float, float, float, str]:
        model = estimate_pdn_series_rlc(
            s2p_path.stem,
            default_capacitance_f=FALLBACK_CAPACITANCE_F,
            default_esr_ohm=FALLBACK_ESR_OHM,
            default_esl_h=FALLBACK_ESL_H,
            reference_impedance_ohm=FALLBACK_REFERENCE_IMPEDANCE_OHM,
        )
        write_pdn_model_files(s2p_path, rlc_path, model)
        return model.capacitance_f, model.esr_ohm, model.esl_h, model.package

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return {"schemaVersion": "1.0.0", "items": []}
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
        return {"schemaVersion": "1.0.0", "items": []}

    def _upsert_registry(self, *, requested_pn: str, selected_pn: str, selection_mode: str, reason: str, s2p_path: Path, max_freq_hz: float, approved: bool):
        metadata = self._read_s2p_metadata(s2p_path) or {}
        registry = self._load_registry()
        items = registry.get("items", [])
        existing_idx = None
        for idx, item in enumerate(items):
            if str(item.get("requestedPn", "")).upper() == requested_pn.upper():
                existing_idx = idx
                break

        entry = {
            "requestedPn": requested_pn,
            "selectedPn": selected_pn,
            "selectionMode": selection_mode,
            "reason": reason,
            "approved": bool(approved),
            "libraryStage": "approved" if approved else "raw",
            "fileName": s2p_path.name,
            "s2pPath": str(s2p_path),
            "maxFreqHz": float(max_freq_hz),
            "minFreqHz": float(metadata.get("min_freq_hz", 0.0)),
            "points": int(metadata.get("points", 0)),
            "dataFormat": str(metadata.get("data_format", "")),
            "dcImagMax": float(metadata.get("dc_imag_max", 0.0)),
            "passivity": bool(metadata.get("has_passivity_tag", False)),
            "causality": bool(metadata.get("has_causality_tag", False)),
            "reciprocity": bool(metadata.get("has_reciprocity_tag", False)),
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        }

        if existing_idx is None:
            items.append(entry)
        else:
            items[existing_idx] = entry
        registry["items"] = items

        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)

    def _candidate_similarity_score(self, requested_pn: str, candidate_pn: str):
        req = requested_pn.upper()
        cand = candidate_pn.upper()
        req_maker, req_series = get_component_info(req)
        cand_maker, cand_series = get_component_info(cand)
        req_cap = extract_capacitance(req)
        cand_cap = extract_capacitance(cand)
        req_pkg = re.search(r"\d{4}", req)
        cand_pkg = re.search(r"\d{4}", cand)
        req_pkg_text = req_pkg.group(0) if req_pkg else "UNKNOWN"
        cand_pkg_text = cand_pkg.group(0) if cand_pkg else "UNKNOWN"

        score = 0
        reasons = []
        if req_maker == cand_maker:
            score += 80
            reasons.append(f"same_maker({req_maker})")
        else:
            reasons.append(f"maker_diff({req_maker}->{cand_maker})")
        if req_series == cand_series and req_series != "Unknown":
            score += 60
            reasons.append(f"same_series({req_series})")
        else:
            reasons.append(f"series_diff({req_series}->{cand_series})")
        if req_cap == cand_cap and req_cap != "Unknown":
            score += 120
            reasons.append(f"same_capacitance({req_cap})")
        else:
            reasons.append(f"cap_diff({req_cap}->{cand_cap})")
        if req_pkg and cand_pkg and req_pkg.group(0) == cand_pkg.group(0):
            score += 30
            reasons.append(f"same_package({req_pkg_text})")
        else:
            reasons.append(f"pkg_diff({req_pkg_text}->{cand_pkg_text})")
        if cand.startswith(req[:6]) and len(req) >= 6:
            score += 25
            reasons.append("prefix_match")
        score += max(0, 20 - abs(len(req) - len(cand)))
        reasons.append(f"len_delta({abs(len(req) - len(cand))})")
        return score, ";".join(reasons) if reasons else "nearest_candidate"

    def _find_replacement_model(self, part_number: str):
        search_roots = [self.output_dir] + self.search_dirs
        visited = set()
        prefix_candidates = []
        similar_candidates = []
        requested = normalize_maker_pn(part_number)
        for root in search_roots:
            root = Path(root)
            exact_s2p = root / requested / f"{requested}.s2p"
            exact_rlc = root / requested / f"{requested}.rlc"
            if exact_s2p.exists():
                if self._is_app_generated_model(exact_s2p, exact_rlc):
                    continue
                ok, _, max_freq = self._validate_model_pair(exact_s2p, exact_rlc)
                if ok:
                    return exact_s2p, (exact_rlc if exact_rlc.exists() else None), max_freq, "exact_library", "exact_match", 1000

        if not ENABLE_SIMILAR_MODEL_REPLACEMENT:
            return None

        for root in search_roots:
            root = Path(root)
            if not root.exists():
                continue
            for s2p in root.rglob("*.s*p"):
                key = str(s2p.resolve())
                if key in visited:
                    continue
                visited.add(key)
                candidate_pn = normalize_maker_pn(s2p.stem)
                if not candidate_pn:
                    continue
                candidate_rlc = s2p.with_suffix(".rlc")
                if self._is_app_generated_model(s2p, candidate_rlc):
                    continue
                if not self._known_capacitances_match(requested, candidate_pn):
                    continue
                rlc = candidate_rlc
                ok, _, max_freq = self._validate_model_pair(s2p, rlc)
                if not ok:
                    continue
                size_score = s2p.stat().st_size
                sim_score, sim_reason = self._candidate_similarity_score(requested, candidate_pn)
                payload = (s2p, rlc if rlc.exists() else None, max_freq, sim_reason)
                if requested.lower() in candidate_pn.lower():
                    prefix_candidates.append(((max_freq, size_score), payload))
                else:
                    similar_candidates.append(((sim_score, max_freq, size_score), payload))
                if len(prefix_candidates) + len(similar_candidates) >= MODEL_REPLACEMENT_SCAN_LIMIT:
                    break
            if len(prefix_candidates) + len(similar_candidates) >= MODEL_REPLACEMENT_SCAN_LIMIT:
                break
        if prefix_candidates:
            prefix_candidates.sort(key=lambda item: item[0], reverse=True)
            _, (s2p, rlc, max_freq, _) = prefix_candidates[0]
            return s2p, rlc, max_freq, "prefix_match", "requested_pn_substring", 900
        if not similar_candidates:
            return None
        similar_candidates.sort(key=lambda item: item[0], reverse=True)
        (sim_score, _, _), (best_s2p, best_rlc, best_freq, reason) = similar_candidates[0]
        return best_s2p, best_rlc, best_freq, "similar_characteristics", reason, int(sim_score)








