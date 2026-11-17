from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import PurePosixPath

from pdn_s2p_core.pdn_pre_s2p import derive_rlc_from_s2p
from .base import VendorAdapterFailure, VendorAdapterResult, VendorRlcResult, VendorModelCandidate


def official(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and (host == 'darfon.com.tw' or host.endswith('.darfon.com.tw'))


class ApexPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.base = ''
        self.form = None
        self.in_form = False
        self.hidden = {}
        self.controls = {}
        self.checksums = {}
        self.select = None
        self.selected = []
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'base':
            self.base = a.get('href', '')
        if tag == 'form' and a.get('id') == 'wwvFlowForm':
            self.form = a
            self.in_form = True
        if self.in_form and tag == 'input':
            value = a.get('value', '')
            if a.get('id'):
                self.hidden[a['id']] = value
            if a.get('data-for'):
                self.checksums[a['data-for']] = value
            name = a.get('name', '')
            if name and 'disabled' not in a and a.get('type') not in ('button', 'submit', 'reset'):
                if a.get('type') not in ('checkbox', 'radio') or 'checked' in a:
                    self.controls[name] = value
        if self.in_form and tag == 'select' and a.get('name') and 'disabled' not in a:
            self.select = a['name']
            self.selected = []
        if tag == 'option' and self.select and 'selected' in a:
            self.selected.append(a.get('value', ''))
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th') and self.row is not None:
            self.cell = {'text': '', 'links': []}
            self.row.append(self.cell)
        if tag == 'a' and self.cell is not None and a.get('href'):
            self.cell['links'].append(a['href'])

    def handle_data(self, text):
        if self.cell is not None:
            self.cell['text'] += text

    def handle_endtag(self, tag):
        if tag == 'form':
            self.in_form = False
        if tag == 'select' and self.select:
            self.controls[self.select] = self.selected
            self.select = None
        if tag in ('td', 'th'):
            self.cell = None
        if tag == 'tr' and self.row is not None:
            for cell in self.row:
                cell['text'] = cell['text'].strip()
            self.rows.append(self.row)
            self.row = None


def build_submission(html, page_url, pn):
    page = ApexPage()
    page.feed(html)
    if not page.form or page.form.get('method', '').lower() != 'post':
        raise ValueError('official_APEX_post_form_not_confirmed')
    if page.controls.get('p_reload_on_submit') != 'A':
        raise ValueError('unsupported_APEX_reload_mode; do_not_guess')
    if 'P4_DARFON_PN' not in page.controls or 'P4_IS_QUERY' not in page.controls:
        raise ValueError('official_PN_query_items_not_confirmed')
    if not re.search(r'affectedElements":"P4_IS_QUERY"[^\n]*attribute02":"Y"', html):
        raise ValueError('official_Query_button_set_value_not_confirmed')
    for key in ('pSalt', 'pPageItemsProtected', 'pPageItemsRowVersion', 'pPageFormRegionChecksums'):
        if key not in page.hidden or '[REDACTED]' in page.hidden[key]:
            raise ValueError('fresh_unmasked_session_page_required')
    fields = dict(page.controls)
    fields['P4_DARFON_PN'] = pn
    fields['P4_IS_QUERY'] = 'Y'
    items = []
    for name in list(fields):
        if not name.startswith('P4_'):
            continue
        value = fields.pop(name)
        item = {'n': name, 'v': value}
        if isinstance(value, list):
            # 공식 페이지의 shuttle 초기화가 storageType=separated, separator=:이다.
            if not re.search(r'widget.shuttle\("#' + re.escape(name) + r'",\{"storageType":"separated","separator":":"', html):
                raise ValueError(f'official_shuttle_storage_not_confirmed; item={name}')
            item.update(s=':', t='separated')
        if name in page.checksums:
            item['ck'] = page.checksums[name]
        items.append(item)
    data = {'salt': page.hidden['pSalt'], 'pageItems': {'itemsToSubmit': items,
            'protected': page.hidden['pPageItemsProtected'], 'rowVersion': page.hidden['pPageItemsRowVersion'],
            'formRegionChecksums': json.loads(page.hidden['pPageFormRegionChecksums'])}}
    fields['p_json'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    base = urllib.parse.urljoin(page_url, page.base or page_url)
    action = urllib.parse.urljoin(base, page.form['action'])
    if not official(action):
        raise ValueError('official_form_action_required')
    return action, fields


class DarfonAdapter:
    """새 APEX 세션의 검색행·실제 파일 링크로 exact 모델과 특성 후보를 구분한다."""
    manufacturer_key = 'darfon'
    PAGE_URL = 'https://webapi.darfon.com.tw:7700/ords/r/ebs_dfe/icm-spec-information/'

    @staticmethod
    def _public_url(url):
        return re.sub(r'(?i)([?&](?:session|p_context|cs)=)[^&]+', r'\1[REDACTED]', url)

    def _request(self, session, url, stage, **kwargs):
        try:
            if not official(url):
                return VendorAdapterFailure('non_official_url', stage, self._public_url(url))
            response = session.request_with_metadata(url, **kwargs)
            if not official(response.final_url):
                return VendorAdapterFailure('non_official_redirect', stage, self._public_url(response.final_url))
            if response.status_code != 200:
                return VendorAdapterFailure(f'http_{response.status_code}', stage, self._public_url(url))
            return response
        except OSError as error:
            code = f'http_{error.code}' if isinstance(error, urllib.error.HTTPError) else 'network_error'
            return VendorAdapterFailure(code, stage, self._public_url(str(error)))

    def _query(self, pn, session):
        page = self._request(session, self.PAGE_URL, 'darfon_session_initialization', method='GET')
        if isinstance(page, VendorAdapterFailure):
            return page
        try:
            action, fields = build_submission(page.payload.decode('utf-8'), page.final_url, pn)
        except (ValueError, KeyError, UnicodeError) as error:
            return VendorAdapterFailure('unexpected_search_page', 'darfon_form_validation', str(error))
        result = self._request(session, action, 'darfon_pn_search', method='POST',
                               headers={'Referer': self.PAGE_URL, 'Content-Type': 'application/x-www-form-urlencoded'},
                               body=urllib.parse.urlencode(fields).encode('utf-8'))
        if isinstance(result, VendorAdapterFailure):
            return result
        parsed = ApexPage()
        try:
            parsed.feed(result.payload.decode('utf-8'))
        except UnicodeError as error:
            return VendorAdapterFailure('unexpected_search_response', 'darfon_search_validation', str(error))
        rows = [r for r in parsed.rows if len(r) == 25 and r[0]['text'] != 'Darfon P/N']
        if not any(len(r) == 25 and r[0]['text'] == 'Darfon P/N' and r[24]['text'] == 'S-parameter' for r in parsed.rows):
            return VendorAdapterFailure('unexpected_search_response', 'darfon_search_validation', 'official_product_table_missing')
        return rows, result

    @staticmethod
    def _specification(row):
        size = row[3]['text']
        if not re.fullmatch(r'\d{4}', size):
            raise ValueError('official_size_code_missing')
        value = {'capacitance': {'value': row[7]['text'], 'unit': row[8]['text']},
                 'ratedVoltageDc': {'value': row[11]['text'], 'unit': 'Vdc'},
                 'tempChara': {'value': row[2]['text'], 'unit': ''},
                 'sizeCodeInMm': {'value': size, 'unit': ''},
                 'capacitanceTolerance': {'displayName': row[10]['text'], 'unit': '%'},
                 'length': {'value': str(int(size[:2]) / 10), 'unit': 'mm'},
                 'width': {'value': str(int(size[2:]) / 10), 'unit': 'mm'},
                 'thickness': {'value': row[5]['text'], 'unit': 'mm'},
                 'thicknessMaximum': {'value': row[6]['text'], 'unit': 'mm'}}
        # X7R 범위는 수집한 공식 General Purpose/해당 ECD에서 확인한 범위다.
        if row[2]['text'] == 'X7R' and re.search(r'125', row[14]['text']):
            value['operaTemp'] = {'value': '-55~125', 'unit': 'C'}
        return value

    @staticmethod
    def _link(row, column, base, suffix):
        links = [urllib.parse.urljoin(base, href) for href in row[column]['links']]
        links = [u for u in links if official(u) and urllib.parse.urlsplit(u).path.lower().endswith(suffix)]
        return links[0] if len(links) == 1 else ''

    def _download_row(self, pn, row, search, session):
        url = self._link(row, 24, search.final_url, '.s2p')
        if not url:
            return VendorAdapterFailure('official_s2p_link_not_found', 'darfon_model_selection',
                                         f'PN={pn}; exact product row has no S2P link; no suffix inferred')
        if PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name != pn + '.s2p':
            return VendorAdapterFailure('model_filename_mismatch', 'darfon_model_selection', f'PN={pn}')
        response = self._request(session, url, 'darfon_s2p_download', method='GET', headers={'Referer': self.PAGE_URL})
        if isinstance(response, VendorAdapterFailure):
            return response
        if PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(response.final_url).path)).name != pn + '.s2p':
            return VendorAdapterFailure('model_filename_mismatch', 'darfon_s2p_validation', f'PN={pn}')
        try:
            circuit, fit = derive_rlc_from_s2p(response.payload)
            spec = self._specification(row)
        except (ValueError, KeyError, UnicodeError) as error:
            return VendorAdapterFailure('derived_rlc_validation_failed', 'darfon_s2p_rlc_validation', str(error))
        conditions = {'dcBiasV': None, 'temperatureC': None}
        source = {'modelPartNumber': pn, 'sourceFileName': pn + '.s2p',
                  'modelConditions': conditions, 'modelConditionsStatus': 'not_stated_in_official_s2p',
                  'modelFrequencyRangeHz': fit['frequencyRangeHz'], 'componentSpecification': spec,
                  'modelIdentityBasis': 'official_exact_search_row_and_link_filename',
                  'bodyPartNumberPresent': pn.encode('ascii') in response.payload,
                  'officialSearchUrl': self.PAGE_URL, 'searchResponseSha256': hashlib.sha256(search.payload).hexdigest(),
                  'electricalCharacteristicsUrl': self._link(row, 23, search.final_url, '.pdf'),
                  'officialNetlistAvailable': False}
        rlc = VendorRlcResult(pn, response.final_url, circuit, b'',
                              {'sourceType': 'official_s2p_derived_rlc', 'modelPartNumber': pn,
                               'modelConditions': conditions, 'modelFrequencyRangeHz': fit['frequencyRangeHz'],
                               'approximation': fit, 'officialVendorNetlist': False})
        return VendorAdapterResult(pn, pn, response.final_url, response.payload, response.content_type, source, rlc)

    def download_exact(self, part_number, session):
        pn = part_number.upper()
        if getattr(self, '_prefetched_session', None) is session and pn in getattr(self, '_prefetched_models', {}):
            return self._prefetched_models[pn]
        query = self._query(pn, session)
        if isinstance(query, VendorAdapterFailure):
            return query
        rows, search = query
        exact = [r for r in rows if r[0]['text'] == pn]
        if len(exact) != 1:
            return VendorAdapterFailure('exact_pn_not_found' if not exact else 'ambiguous_exact_pn',
                                         'darfon_pn_resolution', f'PN={pn}; exactMatches={len(exact)}')
        return self._download_row(pn, exact[0], search, session)

    def search_by_specification(self, specification, session):
        pn = specification.get('requestedPn', '')
        if not pn:
            return VendorAdapterFailure('pn_context_required', 'darfon_specification_search',
                                         'Only PN-scoped official search is verified')
        query = self._query(pn, session)
        if isinstance(query, VendorAdapterFailure):
            return query
        rows, search = query
        exact = [r for r in rows if r[0]['text'] == pn]
        if len(exact) != 1:
            return VendorAdapterFailure('exact_pn_not_found', 'darfon_specification_search', f'PN={pn}')
        origin = exact[0]
        candidates = []
        self.candidate_rejections = []
        self._prefetched_session, self._prefetched_models = session, {}
        for row in rows:
            if not specification.get('searchNeighborhood') and not all(row[i]['text'] and row[i]['text'] == origin[i]['text'] for i in range(2, 18)):
                continue
            if not self._link(row, 24, search.final_url, '.s2p'):
                continue
            candidate_pn = row[0]['text']
            result = self._download_row(candidate_pn, row, search, session)
            if isinstance(result, VendorAdapterFailure):
                self.candidate_rejections.append({'manufacturer': 'Darfon', 'partNumber': candidate_pn,
                                                   'stage': result.stage, 'code': result.code, 'detail': result.detail})
                continue
            self._prefetched_models[candidate_pn] = result
            candidates.append(VendorModelCandidate(candidate_pn, 'Darfon', candidate_pn,
                              result.provenance['componentSpecification'],
                              {'relationship': 'same_manufacturer_characteristic_candidate', 'requestedPn': pn,
                               'officialSearchUrl': self.PAGE_URL, 'searchResponseSha256': result.provenance['searchResponseSha256'],
                               'modelConditionsStatus': result.provenance['modelConditionsStatus'],
                               'rlcSourceType': 'official_s2p_derived_rlc'}))
        return candidates


