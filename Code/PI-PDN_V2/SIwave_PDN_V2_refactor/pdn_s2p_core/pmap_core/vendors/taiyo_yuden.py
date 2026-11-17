from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import zipfile
from email.message import Message
from html.parser import HTMLParser
from pathlib import PurePosixPath

from pdn_s2p_core.pdn_pre_s2p import normalize_rlc_netlist, compare_rlc_to_s2p
from .base import VendorAdapterFailure, VendorAdapterResult, VendorSession, VendorRlcResult


class _References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.assets = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.scripts.append(attrs['src'])
        if tag == 'a' and attrs.get('href'):
            self.assets.append(attrs)


class _RequestFailure(Exception):
    def __init__(self, result):
        self.result = result


class TaiyoYudenAdapter:
    """실제 TY-COMPAS 분류·검색 응답과 exact 상세의 단품 링크를 사용한다."""
    manufacturer_key = 'taiyoyuden'
    PAGE_URL = 'https://ds.yuden.co.jp/TYCOMPAS/jp/searcherMain'

    @staticmethod
    def _official(url):
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == 'https' and parsed.hostname == 'ds.yuden.co.jp'

    @classmethod
    def _request(cls, session, url, stage, **kwargs):
        try:
            response = session.request_with_metadata(url, **kwargs)
            if not cls._official(response.final_url):
                return VendorAdapterFailure('non_official_redirect', stage, f'url={url}')
            if response.status_code != 200:
                return VendorAdapterFailure(f'http_{response.status_code}', stage, f'url={url}; Register Official S2P')
            return response
        except OSError as error:
            code = f'http_{error.code}' if isinstance(error, urllib.error.HTTPError) else 'network_error'
            return VendorAdapterFailure(code, stage, f'url={url}; {error}; Register Official S2P')

    @staticmethod
    def _filename(response):
        message = Message()
        message['Content-Disposition'] = response.content_disposition
        return message.get_filename() or ''

    @classmethod
    def _file(cls, response, filename):
        payload = response.payload
        if zipfile.is_zipfile(io.BytesIO(payload)):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                matches = [n for n in archive.namelist() if PurePosixPath(n).name == filename]
                if len(matches) != 1:
                    raise ValueError('exact_model_archive_member_required')
                payload = archive.read(matches[0])
        elif not filename or cls._filename(response) != filename:
            raise ValueError('official_response_filename_mismatch')
        return payload

    @staticmethod
    def _conditions(text):
        temperature = re.search(r'Temperature\s*=\s*([+-]?[\d.]+)\s*\[?degC', text)
        bias = re.search(r'DC Bias (?:Current/Voltage|Voltage)\s*=\s*(?:[\d.]+A\s*/\s*)?([+-]?[\d.]+)\s*\[?V', text)
        if not temperature or not bias:
            raise ValueError('official_model_conditions_missing')
        return {'temperatureC': float(temperature[1]), 'dcBiasV': float(bias[1])}

    @staticmethod
    def _specification(row):
        capacitance = re.fullmatch(r'([\d.]+)\s*(pF|nF|uF)', row['Cap_List'])
        dielectric = re.search(r'X[567][RS]|C0G|NP0', row['TCC_List'])
        size = re.search(r'/(\d{4})$', row['Case_Size'])
        if not capacitance or not dielectric or not size:
            raise ValueError('official_component_specification_missing')
        spec = {'capacitance': {'value': capacitance[1], 'unit': capacitance[2]},
            'ratedVoltageDc': {'value': str(row['Rated_Voltage']), 'unit': 'Vdc'},
            'tempChara': {'value': dielectric[0], 'unit': ''},
            'sizeCodeInMm': {'value': size[1], 'unit': ''},
            'capacitanceTolerance': {'displayName': row['Cap_Tole'], 'unit': '%'},
            'operaTemp': {'value': row['Temp_Range'].replace(' to ', '~'), 'unit': 'C'}}
        for field, source in [('length', 'SizeL_Spec_M'), ('width', 'SizeW_Spec_M'), ('thickness', 'SizeT_Spec_M')]:
            match = re.match(r'[\d.]+', row[source])
            if not match:
                raise ValueError('official_component_dimensions_missing')
            spec[field] = {'value': match[0], 'unit': 'mm'}
        return spec

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure:
        pn = part_number.upper()
        stage = 'taiyo_yuden_session_bootstrap'
        try:
            def get(url, label, **kwargs):
                nonlocal stage
                stage = label
                if not self._official(url):
                    raise ValueError('official_request_url_required')
                result = self._request(session, url, stage, **kwargs)
                if isinstance(result, VendorAdapterFailure):
                    raise _RequestFailure(result)
                return result

            page = get(self.PAGE_URL, stage)
            html = page.payload.decode('utf-8')
            parser = _References()
            parser.feed(html)
            definitions = next(s for s in parser.scripts if 'definition.min.js' in s.lower())
            definition = get(urllib.parse.urljoin(page.final_url, definitions), 'taiyo_yuden_request_definition').payload.decode('utf-8')
            paths = {key: re.search(r'\b' + key + r'\s*:\s*["\x27]([^"\x27]+)', definition)[1]
                     for key in ('isSingleCategory', 'searcher', 'search', 'detail')}
            def url(key, params):
                return urllib.parse.urljoin(page.final_url, paths[key]) + '?' + urllib.parse.urlencode(params)

            classification = get(url('isSingleCategory', {'pn': pn}), 'taiyo_yuden_pn_classification')
            category = json.loads(classification.payload)
            if category.get('isSuccess') is not True:
                raise ValueError('classification_response_failed')
            if category.get('isSingleCategory') is not True or not category.get('ClassificationId'):
                return VendorAdapterFailure('pn_classification_unresolved', stage, f'PN={pn}; manual official model registration remains available')
            unit = re.search(r'<input[^>]*\bname="u"[^>]*\bvalue="([^"]+)"', html)
            if not unit:
                raise ValueError('official_dimension_unit_missing')
            params = {'pn': pn, 'u': unit[1], 'cid': category['ClassificationId']}
            get(url('searcher', params), 'taiyo_yuden_specification_page')
            response = get(url('search', params), 'taiyo_yuden_pn_search')
            data = json.loads(response.payload)
            if not isinstance(data.get('records'), list) or not isinstance(data.get('record_count'), int):
                raise ValueError('unexpected_search_response')
            exact = [row for row in data['records'] if row.get('PartNumber') == pn]
            if len(exact) != 1:
                related = [row.get('PartNumber') for row in data['records'] if row.get('Pre_PN') == pn]
                return VendorAdapterFailure('official_pn_relationship_review_required' if related else 'exact_pn_not_found',
                    'taiyo_yuden_exact_pn_resolution', f'PN={pn}; exactRows={len(exact)}; officialPrePnResults={related}; no substitute approved')
            row = exact[0]
            detail = get(url('detail', {'pn': pn}), 'taiyo_yuden_product_detail')
            parser = _References()
            parser.feed(detail.payload.decode('utf-8'))
            assets = {}
            for attrs in parser.assets:
                target = urllib.parse.urljoin(detail.final_url, attrs['href'])
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
                if self._official(target) and query.get('pn') == pn:
                    assets.setdefault(query.get('fileType'), []).append((target, attrs.get('data-file-name')))
            selected = {}
            for kind in ('Spara_file', 'cir_file'):
                urls = {u for u, _ in assets.get(kind, [])}
                if len(urls) != 1:
                    return VendorAdapterFailure('official_single_model_link_missing', 'taiyo_yuden_model_selection',
                        f'PN={pn}; fileType={kind}; dynamic TY-SPYCE/login flow requires investigation; Register Official S2P')
                selected[kind] = next(iter(urls))
            names = {n for _, n in assets['Spara_file'] if n and n.lower().endswith('.s2p')}
            if len(names) != 1:
                raise ValueError('official_exact_detail_model_filename_missing')
            s2p_name = names.pop()
            model_pn = s2p_name[:-4]
            headers = {'Referer': detail.final_url}
            s2p_response = get(selected['Spara_file'], 'taiyo_yuden_sparameter_fetch', headers=headers)
            s2p = self._file(s2p_response, s2p_name)
            text = s2p.decode('ascii')
            if not re.search(r'^!\s*' + re.escape(model_pn) + r'\s+S-PARAMETER MODEL', text, re.M):
                raise ValueError('s2p_body_model_identity_mismatch')
            conditions = self._conditions(text)
            netlist_response = get(selected['cir_file'], 'taiyo_yuden_netlist_fetch', headers=headers)
            cir_name = self._filename(netlist_response)
            if cir_name != model_pn + '.cir' and not zipfile.is_zipfile(io.BytesIO(netlist_response.payload)):
                raise ValueError('official_netlist_filename_mismatch')
            original = self._file(netlist_response, model_pn + '.cir')
            netlist_text = original.decode('ascii')
            if (not re.search(r'^\.subckt\s+' + re.escape(model_pn) + r'\s+', netlist_text, re.I | re.M)
                    or self._conditions(netlist_text) != conditions):
                raise ValueError('netlist_model_identity_or_conditions_mismatch')
            stage = 'taiyo_yuden_rlc_sparameter_validation'
            normalized = normalize_rlc_netlist(original)
            validation = compare_rlc_to_s2p(normalized, s2p)
            common = {'modelPartNumber': model_pn, 'modelConditions': conditions,
                'modelFrequencyRangeHz': validation['frequencyRangeHz'], 'httpStatus': 200,
                'requestMethod': 'GET', 'modelAssociation': 'exact_product_detail_data_file_name',
                'modelAssociationEvidenceUrl': detail.final_url}
            provenance = {**common, 'sourceFileName': s2p_name, 'searchUrl': url('search', params),
                'searchMethod': 'GET', 'searchResponseSha256': hashlib.sha256(response.payload).hexdigest(),
                'classificationResponseSha256': hashlib.sha256(classification.payload).hexdigest(),
                'officialNetlistUrl': selected['cir_file'], 'componentSpecification': self._specification(row)}
            rlc = VendorRlcResult(pn, netlist_response.final_url, normalized, original,
                {**common, 'sourceFileName': cir_name, 'validation': validation})
            return VendorAdapterResult(pn, pn, s2p_response.final_url, s2p, s2p_response.content_type, provenance, rlc)
        except _RequestFailure as error:
            return error.result
        except (ValueError, TypeError, KeyError, IndexError, StopIteration, UnicodeError) as error:
            return VendorAdapterFailure('official_model_validation_failed', stage, f'PN={pn}; {error}; Register Official S2P')
