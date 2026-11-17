from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from pdn_s2p_core.pdn_pre_s2p import normalize_rlc_netlist, compare_rlc_to_s2p
from .base import VendorAdapterFailure, VendorAdapterResult, VendorSession, VendorRlcResult


class TdkProductRows(HTMLParser):
    """공식 results HTML의 제품 행 안에서만 PN과 모델 링크를 연결한다."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.part_cell = False
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.row = {'parts': [], 'assets': []}
        if tag == 'td':
            self.part_cell = 'parts_number' in attrs.get('class', '').split()
        if tag == 'a' and self.row is not None:
            self.anchor = {'url': urllib.parse.urljoin(TdkAdapter.PAGE_URL, attrs.get('href', '')),
                           'text': '', 'isPart': self.part_cell}

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            a = self.anchor
            if a['isPart']:
                self.row['parts'].append({'pn': a['text'].strip(), 'url': a['url']})
            elif TdkAdapter.is_official(a['url']) and urllib.parse.urlsplit(a['url']).path.lower().endswith(('.s2p', '.mod')):
                self.row['assets'].append(a['url'])
            self.anchor = None
        if tag == 'td':
            self.part_cell = False
        if tag == 'tr' and self.row is not None:
            if self.row['parts']:
                self.rows.append(self.row)
            self.row = None


class TdkAdapter:
    manufacturer_key = "tdk"
    PAGE_URL = 'https://product.tdk.com/en/search/capacitor/ceramic/mlcc/list'
    SEARCH_URL = 'https://product.tdk.com/pdc_api/en/search/capacitor/ceramic/mlcc/list/search_result'

    @staticmethod
    def is_official(url):
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == 'https' and parsed.hostname == 'product.tdk.com'

    @staticmethod
    def _conditions(text):
        temperature = re.search(r'Temperature\s*=\s*([+-]?[\d.]+)\s*degC', text)
        bias = re.search(r'DC Bias Voltage\s*=\s*([+-]?[\d.]+)\s*V', text)
        if not temperature or not bias:
            raise ValueError('model_conditions_missing')
        return {'dcBiasV': float(bias[1]), 'temperatureC': float(temperature[1])}

    @staticmethod
    def _request(session, url, stage, **kwargs):
        try:
            response = session.request_with_metadata(url, **kwargs)
            if response.status_code != 200:
                return VendorAdapterFailure(f'http_{response.status_code}', stage, f'url={url}; Register Official S2P if access requires login/CAPTCHA')
            if not TdkAdapter.is_official(response.final_url):
                return VendorAdapterFailure('non_official_redirect', stage, f'url={url}')
            return response
        except (urllib.error.URLError, OSError) as error:
            code = f'http_{error.code}' if isinstance(error, urllib.error.HTTPError) else 'network_error'
            return VendorAdapterFailure(code, stage, f'url={url}; {error}; Register Official S2P remains available')

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure:
        pn = part_number.upper()
        bootstrap = self._request(session, self.PAGE_URL, 'tdk_session_bootstrap', method='GET')
        if isinstance(bootstrap, VendorAdapterFailure):
            return bootstrap
        body = urllib.parse.urlencode({'part_no': pn, '_l': '20', '_p': '1',
                                      '_c': 'pure_status-pure_status', '_d': '0'}).encode()
        response = self._request(session, self.SEARCH_URL, 'tdk_pn_search', method='POST', body=body,
            headers={'Accept': '*/*', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                     'X-Requested-With': 'XMLHttpRequest', 'Referer': self.PAGE_URL, 'Origin': 'https://product.tdk.com'})
        if isinstance(response, VendorAdapterFailure):
            return response
        try:
            data = json.loads(response.payload)
            if not isinstance(data, dict) or not isinstance(data.get('results'), str):
                raise ValueError('missing_results_html')
            parser = TdkProductRows()
            parser.feed(data['results'])
            matches = [r for r in parser.rows if any(p['pn'].upper() == pn for p in r['parts'])]
        except (ValueError, UnicodeError) as error:
            return VendorAdapterFailure('unexpected_search_response', 'tdk_search_validation', str(error))
        if len(matches) != 1:
            return VendorAdapterFailure('exact_pn_not_found' if not matches else 'ambiguous_exact_pn',
                                        'tdk_exact_pn_resolution', f'PN={pn}; exactRows={len(matches)}; no PN suffix or substitute inferred')
        assets = list(dict.fromkeys(matches[0]['assets']))
        s2ps = [url for url in assets if Path(urllib.parse.urlsplit(url).path).name.lower() == pn.lower() + '.s2p']
        if len(s2ps) != 1:
            return VendorAdapterFailure('official_s2p_link_not_found', 'tdk_model_selection', f'PN={pn}; exact S2P links={len(s2ps)}')
        s2p = self._request(session, s2ps[0], 'tdk_sparameter_fetch', method='GET')
        if isinstance(s2p, VendorAdapterFailure):
            return s2p
        try:
            text = s2p.payload.decode('ascii')
            identity = re.search(r'(?im)^!\s*TDK P/N:\s*(\S+)', text)
            if not identity or identity[1].upper() != pn:
                raise ValueError('s2p_body_exact_pn_mismatch')
            conditions = self._conditions(text)
        except (ValueError, UnicodeError) as error:
            return VendorAdapterFailure('invalid_official_s2p', 'tdk_payload_validation', str(error))
        provenance = {'sourceFileName': Path(urllib.parse.urlsplit(s2ps[0]).path).name,
            'modelPartNumber': pn, 'modelConditions': conditions, 'searchUrl': self.SEARCH_URL,
            'searchMethod': 'POST', 'searchMatch': 'exact_product_row_and_payload',
            'searchResponseSha256': hashlib.sha256(response.payload).hexdigest(),
            'requestUrl': s2ps[0], 'requestMethod': 'GET', 'httpStatus': 200}
        failures = []
        # Precision을 먼저 대조하고 Simple도 같은 기준으로 검증한다. 기준을 완화하지 않는다.
        for suffix in ('_p.mod', '_s.mod'):
            links = [url for url in assets if Path(urllib.parse.urlsplit(url).path).name.lower() == pn.lower() + suffix]
            if len(links) != 1:
                continue
            net = self._request(session, links[0], 'tdk_netlist_fetch', method='GET')
            if isinstance(net, VendorAdapterFailure):
                failures.append(f'{net.code}: {net.detail}')
                continue
            try:
                text = net.payload.decode('utf-8-sig')
                identity = re.search(r'(?im)^\*\s*TDK P/N:\s*(\S+)', text)
                if not identity or identity[1].upper() != pn or self._conditions(text) != conditions:
                    raise ValueError('netlist_identity_or_conditions_mismatch')
                normalized = normalize_rlc_netlist(net.payload)
                validation = compare_rlc_to_s2p(normalized, s2p.payload)
                rlc = VendorRlcResult(pn, net.final_url, normalized, net.payload,
                    {'sourceFileName': Path(urllib.parse.urlsplit(links[0]).path).name,
                     'modelPartNumber': pn, 'modelConditions': conditions,
                     'modelFrequencyRangeHz': validation['frequencyRangeHz'],
                     'normalization': 'subcircuit_name_X_external_pins_1_2_values_unchanged'})
                return VendorAdapterResult(pn, pn, s2p.final_url, s2p.payload, s2p.content_type,
                    {**provenance, 'officialNetlistUrl': links[0]}, rlc)
            except (ValueError, UnicodeError) as error:
                failures.append(f'{Path(links[0]).name}: {error}')
        return VendorAdapterFailure('official_pair_not_validated', 'tdk_rlc_sparameter_validation',
                                    f'PN={pn}; ' + '; '.join(failures or ['official_netlist_link_not_found']))
