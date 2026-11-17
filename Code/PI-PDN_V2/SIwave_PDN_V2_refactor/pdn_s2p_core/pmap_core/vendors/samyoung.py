from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
from html.parser import HTMLParser

from .base import VendorAdapterFailure


def official(url, site_root='samyoung.co.kr'):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in {'http', 'https'} and (parsed.hostname == site_root or (parsed.hostname or '').endswith('.' + site_root))


class FormPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form = None
        self.fields = {}
        self.options = {}
        self.select = None
        self.option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form' and a.get('name') == 'frm':
            self.form = a
        if tag == 'input' and a.get('name'):
            self.fields[a['name']] = a.get('value', '')
        if tag == 'select' and a.get('name'):
            self.select = a['name']
            self.fields.setdefault(self.select, '')
            self.options.setdefault(self.select, [])
        if tag == 'option' and self.select:
            self.option = {'value': a.get('value', ''), 'label': ''}
            self.options[self.select].append(self.option)
            if 'selected' in a:
                self.fields[self.select] = self.option['value']

    def handle_data(self, text):
        if self.option is not None:
            self.option['label'] += text

    def handle_endtag(self, tag):
        if tag in ('option', 'select'):
            self.option = None
        if tag == 'select':
            self.select = None


def confirmed_routes(js, base):
    loader = re.search(r"fuc_set\('([^']+)\?RetrieveFlag=Type&str_category='", js)
    search = re.search(r'function fnc_search\(\).*?document\.frm\.action\s*=\s*"([^"]+)"', js, re.S)
    download = re.search(r'lbl_Iframe\.window\.location\.href\s*=\s*"([^"]+\?str_no=)"\+str_no', js)
    if not all((loader, search, download)):
        raise ValueError('confirmed_official_request_routes_missing')
    routes = [urllib.parse.urljoin(base, m[1]) for m in (loader, search, download)]
    if not all(official(u, 'samyoung.co.kr') for u in routes):
        raise ValueError('official_route_required')
    return routes


class _ResultRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script' and a.get('src'):
            self.scripts.append(a['src'])
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th') and self.row is not None:
            self.cell = {'text': '', 'links': []}
            self.row.append(self.cell)
        if tag == 'a' and self.cell is not None and a.get('href'):
            self.cell['links'].append(a['href'])
        if self.cell is not None and a.get('onclick'):
            self.cell['links'].extend(re.findall(r"window\.open\(['\"]([^'\"]+)['\"]", a['onclick']))

    def handle_data(self, text):
        if self.cell is not None:
            self.cell['text'] += text

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            self.cell = None
        if tag == 'tr' and self.row is not None:
            for c in self.row:
                c['text'] = re.sub(r'\s+', ' ', c['text']).strip()
            self.rows.append(self.row)
            self.row = None


class SamyoungAdapter:
    """공식 BXJ 시리즈 검색·PDF 조회. 시리즈 자료를 exact 주문 PN 모델로 승인하지 않는다."""
    manufacturer_key = 'samyoung'
    PAGE_URL = 'https://www.samyoung.co.kr/product/search.asp'
    VERIFIED_BOM_PN = 'BXJ10TP35VC330M10X10'

    @staticmethod
    def _text(payload):
        for encoding in ('utf-8', 'cp949', 'euc-kr'):
            try:
                return payload.decode(encoding)
            except UnicodeError:
                pass
        raise ValueError('unexpected_page_encoding')

    def _request(self, session, url, stage, **kwargs):
        try:
            if not official(url):
                return VendorAdapterFailure('non_official_url', stage, url)
            result = session.request_with_metadata(url, **kwargs)
            if not official(result.final_url):
                return VendorAdapterFailure('non_official_redirect', stage, result.final_url)
            if result.status_code != 200:
                return VendorAdapterFailure(f'http_{result.status_code}', stage, url)
            return result
        except OSError as error:
            code = f'http_{error.code}' if isinstance(error, urllib.error.HTTPError) else 'network_error'
            return VendorAdapterFailure(code, stage, str(error))

    def inspect_bxj(self, session, voltage='35', capacitance='330'):
        page = self._request(session, self.PAGE_URL, 'samyoung_session_initialization')
        if isinstance(page, VendorAdapterFailure):
            return page
        try:
            text = self._text(page.payload)
            form = FormPage(); form.feed(text)
            if not form.form or form.form.get('method', '').upper() != 'POST':
                raise ValueError('official_POST_form_required')
            refs = _ResultRows(); refs.feed(text)
            script_urls = [urllib.parse.urljoin(page.final_url, u) for u in refs.scripts if u.endswith('/search.js')]
            if len(script_urls) != 1:
                raise ValueError('official_search_script_required')
            script = self._request(session, script_urls[0], 'samyoung_search_runtime')
            if isinstance(script, VendorAdapterFailure):
                return script
            loader, search_url, _ = confirmed_routes(self._text(script.payload), page.final_url)
            categories = [o['value'] for o in form.options.get('str_category', []) if 'Electrolytic' in o['value']]
            if len(categories) != 1:
                raise ValueError('official_electrolytic_category_required')
            category = categories[0]
            ajax_headers = {'Referer': page.final_url, 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}
            types_response = self._request(session, loader + '?' + urllib.parse.urlencode({'RetrieveFlag': 'Type', 'str_category': category}),
                                           'samyoung_type_resolution', headers=ajax_headers)
            if isinstance(types_response, VendorAdapterFailure):
                return types_response
            types = FormPage(); types.feed(self._text(types_response.payload))
            smd = [o['value'] for o in types.options.get('str_type', []) if o['label'].strip().upper() == 'SMD']
            if len(smd) != 1:
                raise ValueError('official_SMD_type_required')
            series_response = self._request(session, loader + '?' + urllib.parse.urlencode({'RetrieveFlag': 'Series', 'str_category': category, 'str_type': smd[0]}),
                                            'samyoung_series_resolution', headers=ajax_headers)
            if isinstance(series_response, VendorAdapterFailure):
                return series_response
            series = FormPage(); series.feed(self._text(series_response.payload))
            bxj = [o['value'] for o in series.options.get('str_series', []) if o['label'].strip().upper() == 'BXJ']
            if len(bxj) != 1:
                raise ValueError('official_BXJ_series_required')
            fields = dict(form.fields)
            fields.update(str_category=category, str_type=smd[0], str_series=bxj[0])
            for key, value in [('Voltage_f', voltage), ('Voltage_t', voltage), ('Capa_f', capacitance), ('Capa_t', capacitance)]:
                if value not in [o['value'] for o in form.options.get(key, [])]:
                    raise ValueError('official_filter_option_missing:' + key)
                fields[key] = value
            result = self._request(session, search_url, 'samyoung_specification_search', method='POST',
                                   headers={'Referer': page.final_url, 'Content-Type': 'application/x-www-form-urlencoded'},
                                   body=urllib.parse.urlencode(fields).encode('ascii'))
            if isinstance(result, VendorAdapterFailure):
                return result
            rows = _ResultRows(); rows.feed(self._text(result.payload))
            matches = [r for r in rows.rows if any(c['text'] == 'BXJ' for c in r)]
            if len(matches) != 1:
                raise ValueError('one_official_BXJ_result_row_required')
            links = [u for c in matches[0] for u in c['links']]
            pdf_urls = list(dict.fromkeys(urllib.parse.urljoin(result.final_url, u) for u in links if urllib.parse.urlsplit(u).path.lower().endswith('.pdf')))
            if len(pdf_urls) != 1:
                raise ValueError('one_official_series_pdf_link_required')
            return {'series': 'BXJ', 'componentFamily': 'aluminum_electrolytic', 'matchScope': 'series_specification',
                    'orderPnExactVerified': False, 'searchUrl': search_url,
                    'searchResponseSha256': hashlib.sha256(result.payload).hexdigest(), 'seriesRow': matches[0],
                    'pdfUrl': pdf_urls[0], 'pdfDownloadVerified': False,
                    'modelApproved': False}
        except (ValueError, KeyError, UnicodeError) as error:
            return VendorAdapterFailure('unexpected_official_search_response', 'samyoung_response_validation', str(error))

    def download_exact(self, part_number, session):
        if part_number != self.VERIFIED_BOM_PN:
            return VendorAdapterFailure('official_order_pn_resolution_required', 'samyoung_pn_resolution',
                                         f'PN={part_number}; no ordering-code inference; Register Official S2P')
        info = self.inspect_bxj(session)
        if isinstance(info, VendorAdapterFailure):
            return info
        self.last_series_evidence = {k: v for k, v in info.items() if k != 'pdfPayload'}
        return VendorAdapterFailure('official_model_link_not_found', 'samyoung_model_discovery',
            f'PN={part_number}; BXJ series search and active PDF link verified; no S2P/SPICE link in inspected row; '
            'full ordering PN mapping unconfirmed; 100kHz max impedance is not broadband ESR/ESL; Register Official S2P')


