from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
from email.message import Message
from html.parser import HTMLParser

from pdn_s2p_core.pdn_pre_s2p import normalize_rlc_netlist, compare_rlc_to_s2p, rank_bom_specifications
from .base import VendorAdapterFailure, VendorAdapterResult, VendorRlcResult, VendorSession, VendorModelCandidate


class _PageInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') in ('_csrf', '_csrf_header'):
            self.values.setdefault(attrs['name'], attrs.get('value', ''))


class SamsungAdapter:
    """공식 페이지와 실연결로 확인한 사전 확인·파일 POST를 사용한다."""

    manufacturer_key = "samsung"
    ORIGIN = 'https://weblib.samsungsem.com'
    CHECK_URL = ORIGIN + '/mlcc/checkDownloadYn.do'
    DOWNLOAD_URL = ORIGIN + '/mlcc/download.do'
    CROSS_URL = ORIGIN + '/mlcc/pncrossingSearch.do'

    @staticmethod
    def _component_specification(row, payload):
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError:
            text = payload.decode('cp949')
        dimensions = {}
        for field in ('Length', 'Width', 'Thickness'):
            match = re.search(field + r'\s*=\s*([\d.]+)', text)
            if not match:
                raise ValueError('official_model_dimensions_missing')
            dimensions[field.lower()] = {'value': match[1], 'unit': 'mm'}
        return {'capacitance': {'value': row['capacitanceVal'], 'unit': row['capacitanceUnit']},
            'ratedVoltageDc': {'value': row['ratedVdc'], 'unit': 'Vdc'},
            'tempChara': {'value': row['tcc'], 'unit': ''},
            'sizeCodeInMm': {'value': row['lWmm'], 'unit': ''},
            'capacitanceTolerance': {'displayName': row['tolerance'], 'unit': '%'},
            'operaTemp': {'value': f"{row['tempMin']}~{row['tempMax']}", 'unit': 'C'}, **dimensions}

    def search_by_specification(self, specification, session):
        page = self.ORIGIN + '/mlcc/mlcc-ec.do'
        response = self._request(session, page, 'samsung_specification_catalog', method='GET')
        if isinstance(response, VendorAdapterFailure):
            return response
        try:
            text = response.payload.decode('utf-8')
            match = re.search(r'var\s+PartsListJson\s*=\s*(\[.*?\]);', text, re.S)
            if not match:
                raise ValueError('official_product_catalog_missing')
            rows = json.loads(match[1])
            if not isinstance(rows, list):
                raise ValueError('unexpected_product_catalog')
            selected = []
            neighborhood = specification.get('searchNeighborhood', False)
            for row in rows:
                scale = {'pF': 1e-12, 'nF': 1e-9, 'uF': 1e-6}.get(row.get('capacitanceUnit'))
                tolerance = re.search(r'([\d.]+)%', row.get('tolerance', ''))
                if (scale and tolerance
                        and math.isclose(float(row['capacitanceVal']) * scale, specification['capacitanceF'], rel_tol=1e-6)
                        and (neighborhood or float(row['ratedVdc']) == specification['ratedVoltageV'])
                        and (neighborhood or float(tolerance[1]) == specification['tolerancePercent'])
                        and row['tcc'].upper() == specification['dielectric']
                        and row['lWmm'] == specification['sizeCodeMm']
                        and (neighborhood or [float(row['tempMin']), float(row['tempMax'])] == specification['temperatureRangeC'])):
                    selected.append(row)
            if neighborhood:
                # 공식 목록의 인접 전압·오차·온도만 미리 고른다. 실제 치수는 파일로 재검증한다.
                catalog = [{'pn': r['partNumber'], 'manufacturer': 'Samsung', 'spec': {
                    'capacitanceF': float(r['capacitanceVal']) * {'pF': 1e-12, 'nF': 1e-9, 'uF': 1e-6}[r['capacitanceUnit']],
                    'ratedVoltageV': float(r['ratedVdc']), 'tolerancePercent': float(re.search(r'([\d.]+)%', r['tolerance'])[1]),
                    'dielectric': r['tcc'].upper(), 'sizeCodeMm': r['lWmm'],
                    'lengthMm': specification['lengthMm'], 'widthMm': specification['widthMm'],
                    'thicknessMm': None, 'temperatureRangeC': [float(r['tempMin']), float(r['tempMax'])]}} for r in selected]
                ranked, _ = rank_bom_specifications(specification, catalog, catalog_only=True)
                allowed = {r['pn'] for r in ranked}
                selected = [r for r in selected if r['partNumber'] in allowed]
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            return VendorAdapterFailure('unexpected_product_catalog', 'samsung_specification_catalog', str(error))
        self.candidate_rejections = []
        self._prefetched_session = session
        self._prefetched_models = {}
        candidates = []
        for row in selected:
            pn = row['partNumber']
            result = self.download_exact(pn, session)
            if isinstance(result, VendorAdapterFailure):
                self.candidate_rejections.append({'manufacturer': 'Samsung', 'partNumber': pn,
                    'stage': result.stage, 'code': result.code, 'detail': result.detail})
                continue
            actual = result.provenance['componentSpecification']
            thickness = specification.get('thicknessMm')
            if not neighborhood and thickness is not None and not math.isclose(float(actual['thickness']['value']), thickness):
                self.candidate_rejections.append({'manufacturer': 'Samsung', 'partNumber': pn,
                    'stage': 'samsung_specification_validation', 'code': 'thickness_mismatch',
                    'detail': '공식 모델의 실제 두께와 원부품 확인 두께가 다릅니다.'})
                continue
            self._prefetched_models[pn] = result
            candidates.append(VendorModelCandidate(pn, 'Samsung', pn, actual,
                {'searchUrl': page, 'searchResponseSha256': hashlib.sha256(response.payload).hexdigest(),
                 'discoveryType': 'official_same_manufacturer_specification_search',
                 'modelDimensionsSource': 'official_sparameter_payload', 'searchSpecification': specification}))
        return candidates

    @staticmethod
    def match_model_filename(pn, filename):
        match = re.fullmatch(re.escape(pn) + r'_Series_Precise_DC([+-]?[\d.]+)V_([+-]?[\d.]+)degC\.s2p', filename, re.I)
        if not match:
            return None
        return {'sourceFileName': filename, 'modelPartNumber': pn,
                'modelConditions': {'dcBiasV': float(match[1]), 'temperatureC': float(match[2])}}

    match_model_file = match_model_filename

    @staticmethod
    def _request(session, url, stage, **kwargs):
        try:
            response = session.request_with_metadata(url, **kwargs)
            if urllib.parse.urlsplit(response.final_url).hostname != 'weblib.samsungsem.com':
                return VendorAdapterFailure('non_official_redirect', stage, f'url={url}')
            if response.status_code != 200:
                return VendorAdapterFailure(f'http_{response.status_code}', stage, f'url={url}; Register Official S2P')
            return response
        except (OSError, urllib.error.URLError) as error:
            code = f'http_{error.code}' if isinstance(error, urllib.error.HTTPError) else 'network_error'
            return VendorAdapterFailure(code, stage, f'url={url}; {error}; Register Official S2P')

    @staticmethod
    def _filename(response):
        header = Message()
        header['Content-Disposition'] = response.content_disposition
        return header.get_filename() or ''

    @staticmethod
    def _validate_payload(payload, pn, kind):
        first = payload.splitlines()[0].decode('ascii') if payload else ''
        marker = '!' if kind == 'Sparameter' else '*'
        if first.strip() != f'{marker} {pn} Multilayer Ceramic Capacitor {kind} Model':
            raise ValueError('model_body_exact_pn_mismatch')
        if b'Model Generated by Samsung Electro-Mechanics' not in payload:
            raise ValueError('official_model_identity_missing')
        text = payload.decode('ascii', errors='ignore')
        temp = re.search(r'Measurement Temperature\s*:\s*([+-]?[\d.]+)degC', text)
        bias = re.search(r'DC bias Value\s*=\s*([+-]?[\d.]+)V', text)
        if not temp or not bias or float(temp[1]) != 25 or float(bias[1]) != 0:
            raise ValueError('model_conditions_mismatch')

    def download_exact(self, part_number: str, session: VendorSession) -> VendorAdapterResult | VendorAdapterFailure:
        pn = part_number.upper()
        if getattr(self, '_prefetched_session', None) is session and pn in getattr(self, '_prefetched_models', {}):
            return self._prefetched_models[pn]
        page = self.ORIGIN + '/mlcc/mlcc-ec.do?' + urllib.parse.urlencode({'partNumber': pn})
        response = self._request(session, page, 'samsung_session_bootstrap', method='GET')
        if isinstance(response, VendorAdapterFailure):
            return response
        try:
            text = response.payload.decode('utf-8')
            parser = _PageInputs()
            parser.feed(text)
            token, header = parser.values.get('_csrf', ''), parser.values.get('_csrf_header', '')
            if not token or not header or token == '[REDACTED]':
                raise ValueError('runtime_csrf_missing')
            match = re.search(r'var\s+PartsListJson\s*=\s*(\[.*?\]);', text, re.S)
            if not match:
                raise ValueError('official_product_catalog_missing')
            catalog = json.loads(match[1])
            if not isinstance(catalog, list):
                raise ValueError('unexpected_product_catalog')
            matches = [r for r in catalog if isinstance(r, dict) and r.get('partNumber') == pn]
        except (ValueError, UnicodeError) as error:
            return VendorAdapterFailure('unexpected_product_page', 'samsung_page_validation', str(error))
        headers = {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest',
                   'Accept': 'application/json, */*', 'Referer': page, 'Origin': self.ORIGIN, header: token}
        search_hash, search_url = hashlib.sha256(response.payload).hexdigest(), page
        if not matches:
            cross = self._request(session, self.CROSS_URL, 'samsung_pn_cross_search', method='POST',
                                  headers=headers, body=json.dumps({'partNumber': pn}).encode())
            if isinstance(cross, VendorAdapterFailure):
                return cross
            if not cross.payload.strip():
                return VendorAdapterFailure('empty_pn_cross_response', 'samsung_pn_cross_response_validation',
                    f'PN={pn}; HTTP 200 with empty body; PN absence unconfirmed; Register Official S2P')
            try:
                rows = json.loads(cross.payload)
                if not isinstance(rows, list):
                    raise ValueError('expected_product_array')
                matches = [r for r in rows if isinstance(r, dict) and r.get('partNumber') == pn]
                search_hash, search_url = hashlib.sha256(cross.payload).hexdigest(), self.CROSS_URL
            except ValueError as error:
                return VendorAdapterFailure('unexpected_search_response', 'samsung_pn_cross_response_validation', str(error))
        if len(matches) != 1:
            return VendorAdapterFailure('exact_pn_not_found' if not matches else 'ambiguous_exact_pn',
                'samsung_exact_pn_resolution', f'PN={pn}; exactMatches={len(matches)}; no suffix or substitute inferred')
        files, check_hashes = {}, {}
        for kind, filetype in (('Series', 's2p'), ('Static', 'ltmod')):
            stage = 'samsung_' + filetype + '_availability'
            body = {'downloadModel': 'Precise', 'downloadType': kind, 'downloadFileType': filetype,
                    'paramList': [{'partsName': pn, 'vdc': '0', 'degC': '25'}]}
            check = self._request(session, self.CHECK_URL, stage, method='POST', headers=headers,
                                  body=json.dumps(body).encode())
            if isinstance(check, VendorAdapterFailure):
                return check
            try:
                data = json.loads(check.payload)
                rows = data.get('downList') if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError('expected_downList_array')
                if not rows:
                    return VendorAdapterFailure('official_model_unavailable', stage, f'PN={pn}; type={filetype}; empty downList')
                if len(rows) != 1 or rows[0].get('partsName') != pn:
                    raise ValueError('download_response_exact_pn_mismatch')
                item = rows[0]
                if float(item['vdc']) != 0 or float(item['degC']) != 25:
                    raise ValueError('download_response_conditions_mismatch')
                check_hashes[filetype] = hashlib.sha256(check.payload).hexdigest()
                form = {'downloadModel': 'Precise', 'downloadType': kind, 'downloadFileType': filetype,
                        'paramList[0].partNumber': item['partsName'], 'paramList[0].vdc': item['vdc'],
                        'paramList[0].degC': item['degC'], '_csrf': token}
            except (ValueError, KeyError, TypeError) as error:
                return VendorAdapterFailure('unexpected_availability_response', stage, str(error))
            file = self._request(session, self.DOWNLOAD_URL, 'samsung_' + filetype + '_download', method='POST',
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': page, 'Origin': self.ORIGIN},
                body=urllib.parse.urlencode(form).encode())
            if isinstance(file, VendorAdapterFailure):
                return file
            expected = pn + ('_Series_Precise_DC0V_25degC.s2p' if filetype == 's2p' else '_Precise_DC0V_25degC_LT.mod')
            try:
                if self._filename(file) != expected:
                    raise ValueError('official_filename_exact_pn_or_conditions_mismatch')
                self._validate_payload(file.payload, pn, 'Sparameter' if filetype == 's2p' else 'Spice')
            except (ValueError, UnicodeError) as error:
                return VendorAdapterFailure('invalid_official_model', 'samsung_' + filetype + '_payload_validation', str(error))
            files[filetype] = file
        try:
            normalized = normalize_rlc_netlist(files['ltmod'].payload)
            validation = compare_rlc_to_s2p(normalized, files['s2p'].payload)
        except (ValueError, UnicodeError) as error:
            return VendorAdapterFailure('official_pair_not_validated', 'samsung_rlc_sparameter_validation', str(error))
        conditions = {'dcBiasV': 0.0, 'temperatureC': 25.0}
        rlc = VendorRlcResult(pn, self.DOWNLOAD_URL, normalized, files['ltmod'].payload,
            {'sourceFileName': self._filename(files['ltmod']), 'modelPartNumber': pn,
             'modelConditions': conditions, 'modelFrequencyRangeHz': validation['frequencyRangeHz'],
             'normalization': 'pins_normalized_cp949_comments_decoded_zero_Rser_omitted_values_unchanged'})
        provenance = {'sourceFileName': self._filename(files['s2p']), 'modelPartNumber': pn,
            'modelConditions': conditions, 'searchUrl': search_url, 'searchResponseSha256': search_hash,
            'searchMatch': 'exact_product_row_and_both_file_identities', 'requestMethod': 'POST',
            'availabilityResponseSha256': check_hashes, 'httpStatus': 200, 'officialNetlistUrl': self.DOWNLOAD_URL}
        try:
            provenance['componentSpecification'] = self._component_specification(matches[0], files['s2p'].payload)
        except (ValueError, KeyError, UnicodeError) as error:
            return VendorAdapterFailure('official_specification_invalid', 'samsung_model_specification', str(error))
        return VendorAdapterResult(pn, pn, self.DOWNLOAD_URL, files['s2p'].payload,
                                   files['s2p'].content_type, provenance, rlc)
