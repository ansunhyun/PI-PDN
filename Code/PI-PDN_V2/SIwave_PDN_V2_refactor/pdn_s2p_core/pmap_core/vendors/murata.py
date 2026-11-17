from __future__ import annotations

from pathlib import Path
from email.header import decode_header, make_header
from email.message import Message
from email.utils import collapse_rfc2231_value
import re
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
import hashlib

from .base import VendorAdapterFailure, VendorAdapterResult, VendorRlcResult, VendorSession, VendorModelCandidate
from pdn_s2p_core.pdn_pre_s2p import normalize_rlc_netlist


@dataclass(frozen=True)
class _MurataResolution:
    api_pn: str
    search_match: str
    model_pn: str = ""
    provenance: dict = field(default_factory=dict)
    netlist_url: str = ""


class MurataAdapter:
    """Direct-file API verified by the user's saved HTTP response.

    Resolve every PN using the official frontend's exact-search request and
    response fields. API partNum values are read, never synthesized.
    """

    manufacturer_key = "murata"
    API_URL = "https://pimapi.murata.com/public/api/pim/v1/characteristics/search/s-parameter"
    SEARCH_URL = "https://pimapi.murata.com/public/api/pim/v1/products/search"

    @staticmethod
    def netlist_url(product: dict, api_pn: str) -> str:
        links = set()
        for group in product.get("assetGroupList", []):
            for subgroup in group.get("assetSubGroupList", []):
                if subgroup.get("id") != "spiceNetlist":
                    continue
                for asset in subgroup.get("assetList", []):
                    if not asset.get("isFileDownloadable"):
                        continue
                    raw = asset.get("assetUrl", "")
                    url = urllib.parse.urljoin("https://pimapi.murata.com", raw.replace("#", "%23"))
                    parsed = urllib.parse.urlsplit(url)
                    if (parsed.scheme == "https" and parsed.hostname == "pimapi.murata.com"
                            and urllib.parse.parse_qs(parsed.query).get("partNum") == [api_pn]):
                        links.add(url)
        return links.pop() if len(links) == 1 else ""

    @staticmethod
    def search_request(part_number: str, page: int = 1, search_cond_class: int = 3) -> dict:
        # Official DEFAULT_SEARCH_PARAMS and ExactMatch=3, captured frontend
        # chunk-CmvyxeZf.js / chunk-C9E2YVbx.js. No packaging marker is added.
        return {
            "searchCondClass": search_cond_class, "partNum": part_number, "page": page, "pageSize": 20,
            "productCategoryId": "ceramicCapacitorSMD", "languageRegion": "en-global",
            "series": "", "sortKey": "", "valSearchCondList": [],
            "rangeValSearchCondList": [], "dateRangeSearchCondList": [],
        }

    @staticmethod
    def network_failure(error: Exception, stage: str, url: str) -> VendorAdapterFailure:
        if isinstance(error, urllib.error.HTTPError):
            code = f"http_{error.code}"
        else:
            reason = getattr(error, "reason", error)
            code = "network_timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower() else "network_error"
        return VendorAdapterFailure(code, stage, f"url={url}; {error}")

    @staticmethod
    def resolve_packaged_pn(pn: str, api_pn: str, product: dict, response_payload: bytes) -> _MurataResolution | None:
        # Official GRM numbering has 17 model characters plus one packaging
        # character. The fetched product specification explicitly confirms
        # the shared model and its product-specific D/J/V/W packaging table.
        if not api_pn.endswith("#"):
            return None
        model_pn = api_pn[:-1]
        if not re.fullmatch(r"GRM[A-Z0-9]{14}", model_pn) or len(pn) != len(model_pn) + 1 or pn[:-1] != model_pn:
            return None
        package_code = pn[-1]
        package_info = None
        documents = {}
        for item in product["itemInfoList"]:
            if not isinstance(item, dict):
                continue
            for value in item.get("valueList", []) or []:
                if not isinstance(value, dict):
                    continue
                if item.get("id") == "carrierPackageInfo" and isinstance(value.get("value"), str):
                    if value["value"].split(",", 1)[0] == package_code:
                        package_info = value
                if item.get("id") in {"partNumberingUrl", "specificationSheetUrl"}:
                    url = value.get("assetUrl", "")
                    parsed = urllib.parse.urlparse(url)
                    host = (parsed.hostname or "").lower()
                    if parsed.scheme == "https" and (host == "murata.com" or host.endswith(".murata.com")):
                        documents[item["id"]] = url
        if package_info is None or not {"partNumberingUrl", "specificationSheetUrl"}.issubset(documents):
            return None
        return _MurataResolution(api_pn, "exact_grm_product_and_carrier_package", model_pn, {
            "packagingCode": package_code,
            "packagingDescription": package_info.get("displayName", package_info["value"]),
            "packagingMapping": "official_grm_numbering_and_product_carrier_list",
            "packagingEvidenceUrls": documents,
            "searchResponseSha256": hashlib.sha256(response_payload).hexdigest(),
        })

    def resolve_part_number(self, pn: str, session: VendorSession) -> _MurataResolution | VendorAdapterFailure:
        page = 1
        search_cond_class = 3
        while True:
            try:
                response = session.request_with_metadata(
                    self.SEARCH_URL, method="POST", headers={"Content-Type": "application/json"},
                    body=json.dumps(self.search_request(pn, page, search_cond_class)).encode("utf-8"),
                )
            except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError, OSError) as error:
                return self.network_failure(error, "murata_pn_search", self.SEARCH_URL)
            if response.status_code != 200:
                return VendorAdapterFailure(f"http_{response.status_code}", "murata_pn_search", f"url={self.SEARCH_URL}")
            try:
                data = json.loads(response.payload)
            except (ValueError, UnicodeDecodeError):
                return VendorAdapterFailure("unexpected_search_response", "murata_search_validation", f"url={self.SEARCH_URL}; expected JSON")
            if not isinstance(data, dict) or "error" in data:
                return VendorAdapterFailure("unexpected_search_response", "murata_search_validation", f"url={self.SEARCH_URL}; API error or invalid object")
            products = data.get("productSearchResult", []) if data.get("totalNum") == 0 else data.get("productSearchResult")
            if not isinstance(products, list):
                return VendorAdapterFailure("unexpected_search_response", "murata_search_validation", "missing productSearchResult list")
            matches = {}
            candidates = {}
            for product in products:
                if not isinstance(product, dict) or not isinstance(product.get("itemInfoList"), list):
                    return VendorAdapterFailure("unexpected_search_response", "murata_search_validation", "invalid product itemInfoList")
                for item in product["itemInfoList"]:
                    if not isinstance(item, dict) or item.get("id") != "partNum":
                        continue
                    values = item.get("valueList")
                    if not isinstance(values, list):
                        return VendorAdapterFailure("unexpected_search_response", "murata_search_validation", "invalid partNum valueList")
                    for value in values:
                        if not isinstance(value, dict):
                            continue
                        # The official UI uses displayName for the shown PN and
                        # value for the API/detail query. Preserve value verbatim.
                        shown_pn = value.get("displayName")
                        api_pn = value.get("value")
                        if not isinstance(api_pn, str) or not api_pn:
                            continue
                        if isinstance(shown_pn, str) and shown_pn.upper() == pn:
                            matches[api_pn] = _MurataResolution(api_pn, "exact_display_name")
                        elif api_pn.endswith("#") and api_pn[:-1].upper() == pn:
                            # This only discovers a query candidate. The file's
                            # full PN and body must still equal the requested PN.
                            matches[api_pn] = _MurataResolution(api_pn, "exact_model_base_and_payload")
                        elif search_cond_class == 3:
                            packaged = self.resolve_packaged_pn(pn, api_pn, product, response.payload)
                            if packaged is not None:
                                matches[api_pn] = packaged
                        candidates[api_pn] = product
            if len(matches) > 1:
                return VendorAdapterFailure("ambiguous_exact_pn", "murata_partnum_resolution", f"PN={pn}; multiple API partNum values")
            if matches:
                matched = next(iter(matches.values()))
                return _MurataResolution(matched.api_pn, matched.search_match, matched.model_pn,
                                         matched.provenance, self.netlist_url(candidates[matched.api_pn], matched.api_pn))
            total = data.get("totalNum", len(products))
            if search_cond_class == 3 and total == 1 and len(candidates) == 1:
                return VendorAdapterFailure(
                    "official_model_pn_mapping_required", "murata_package_model_validation",
                    f"PN={pn}; apiPartNum={next(iter(candidates))}; exact search returned a different model PN; no substitute approved",
                )
            if not isinstance(total, int) or not products or page * 20 >= total:
                if search_cond_class == 3:
                    # Official PrefixMatch=1 is discovery only. No prefix
                    # result can pass the full file/body exact-PN checks.
                    search_cond_class, page = 1, 1
                    continue
                return VendorAdapterFailure("exact_pn_not_found", "murata_partnum_resolution", f"PN={pn}; no exact displayName match; no prefix or package inference")
            page += 1

    @staticmethod
    def specification_search_request(specification: dict, page: int = 1) -> dict:
        # 공식 상세 페이지의 similar-products 요청은 PN 관련 키를 포함하지 않는다.
        # script-02-89297285fd.js의 searchRequest 및 실제 제품 응답의 id/unit 근거.
        # 실연결 adapter-live-20261113T080645206380Z에서 length 단독 필터도 0건.
        # length는 검색에서 제외하고 model_service가 공식 응답 값과 BOM을 대조한다.
        return {
            'productCategoryId': 'ceramicCapacitorSMD', 'languageRegion': 'en-global',
            'pageSize': 20, 'page': page,
            'valSearchCondList': [{'id': 'tempChara', 'value': specification['dielectric']}],
            'rangeValSearchCondList': [
                {'id': key, 'from': value, 'to': value, 'unit': unit}
                for key, value, unit in (
                    ('capacitance', specification['capacitanceF'] * 1e12, 'pF'),
                    ('ratedVoltageDc', specification['ratedVoltageV'], 'Vdc'),
                    ('width', specification['widthMm'], 'mm'))
                if not (specification.get('searchNeighborhood') and key == 'ratedVoltageDc')],
        }

    def search_characteristic_candidates(self, part_number: str, session: VendorSession):
        """공식 전방 일치 조회. 후보는 요청 PN의 exact 모델로 승인하지 않는다."""
        self.candidate_rejections = []
        specification = part_number if isinstance(part_number, dict) else None
        prefix = "" if specification else part_number.upper()
        if specification is None and not re.fullmatch(r"GRM[A-Z0-9]{11}", prefix):
            return VendorAdapterFailure("unsupported_candidate_pn", "murata_candidate_search", "requires a 14-character GRM specification prefix")
        found = {}
        page = 1
        while True:
            try:
                request = (self.specification_search_request(specification, page) if specification
                           else self.search_request(prefix, page, 1))
                response = session.request_with_metadata(self.SEARCH_URL, method="POST",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(request).encode("utf-8"))
                if response.status_code != 200:
                    return VendorAdapterFailure(f"http_{response.status_code}", "murata_candidate_search", self.SEARCH_URL)
                data = json.loads(response.payload)
                total = data["totalNum"]
                products = data.get("productSearchResult", []) if total == 0 else data["productSearchResult"]
                if not isinstance(products, list) or not isinstance(total, int) or total < 0 or "error" in data:
                    raise ValueError("invalid search fields")
                if total > 0 and not products:
                    raise ValueError("incomplete candidate pagination")
                for product in products:
                    items = {item["id"]: item["valueList"] for item in product["itemInfoList"]}
                    for value in items.get("partNum", []):
                        api_pn = value.get("value", "")
                        pattern = r'GRM[A-Z0-9]{14}#' if specification else re.escape(prefix) + r"[A-Z0-9]{3}#"
                        if not re.fullmatch(pattern, api_pn):
                            continue
                        packages = sorted({v["value"] for v in items.get("partNumWithPackageCode", [])
                            if isinstance(v.get("value"), str) and self.resolve_packaged_pn(v["value"], api_pn, product, response.payload)})
                        if not packages:
                            self.candidate_rejections.append({
                                'manufacturer': 'Murata', 'apiPartNumber': api_pn,
                                'stage': 'murata_candidate_package_validation',
                                'code': 'official_package_mapping_missing',
                                'detail': '공식 포장 근거를 확인하지 못한 후보만 제외합니다.',
                                'searchResponseSha256': hashlib.sha256(response.payload).hexdigest(),
                            })
                            continue
                        spec = {}
                        # 필드명은 저장된 실제 공식 검색 응답에서 확인했다.
                        for field in ("capacitance", "ratedVoltageDc", "tempChara", "length", "width", "thickness", "sizeCodeInMm", "capacitanceTolerance", "operaTemp"):
                            values = items.get(field, [])
                            if values:
                                spec[field] = {key: values[0].get(key, "") for key in ("value", "unit", "displayName")}
                        found[api_pn] = VendorModelCandidate(packages[0], "Murata", api_pn[:-1], spec,
                            {"packagingPns": packages, "searchUrl": self.SEARCH_URL, "searchMethod": "POST",
                             "searchResponseSha256": hashlib.sha256(response.payload).hexdigest(), "specificationPrefix": prefix,
                             "searchSpecification": specification, "searchRequest": request})
            except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError, OSError) as error:
                return self.network_failure(error, "murata_candidate_search", self.SEARCH_URL)
            except (ValueError, KeyError, TypeError) as error:
                return VendorAdapterFailure("unexpected_search_response", "murata_candidate_validation", str(error))
            if page * 20 >= total:
                return list(found.values())
            page += 1

    def search_by_specification(self, specification: dict, session: VendorSession):
        return self.search_characteristic_candidates(specification, session)

    @staticmethod
    def response_filename(content_disposition: str) -> str:
        message = Message()
        message["Content-Disposition"] = content_disposition
        # Prefer RFC 2231 filename* over the RFC 2047 fallback filename.
        for key, value in message.get_params(header="content-disposition", failobj=[]) or []:
            if key == "filename" and isinstance(value, tuple):
                return collapse_rfc2231_value(value)
        filename = message.get_filename() or ""
        return str(make_header(decode_header(filename)))

    def match_model_file(self, part_number: str, filename: str) -> dict | None:
        """Recognize only the operating-condition suffix supplied by the user.

        Packaging markers and other operating conditions remain unverified.
        The complete PN before the suffix must match; prefix matches are rejected.
        """
        stem = Path(filename).stem
        suffix = "_DC0V_25degC"
        if not stem.endswith(suffix):
            return None
        model_pn = stem[:-len(suffix)]
        if model_pn.upper() != part_number.upper():
            return None
        return {
            "sourceFileName": filename,
            "modelPartNumber": model_pn,
            "modelConditions": {"dcBiasV": 0, "temperatureC": 25},
            "conditionSuffix": suffix,
        }

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure:
        pn = part_number.upper()
        resolution = self.resolve_part_number(pn, session)
        if isinstance(resolution, VendorAdapterFailure):
            return resolution
        api_pn = resolution.api_pn
        model_pn = resolution.model_pn or pn
        url = self.API_URL + "?" + urllib.parse.urlencode({
            "partNum": api_pn, "productCategoryId": "ceramicCapacitorSMD", "languageRegion": "en-global",
        })
        stage = "murata_sparameter_fetch"
        try:
            response = session.request_with_metadata(url, method="GET")
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError, OSError) as error:
            return self.network_failure(error, stage, url)
        if response.status_code != 200:
            return VendorAdapterFailure(f"http_{response.status_code}", stage, f"url={url}")
        if "json" in response.content_type.lower() or "html" in response.content_type.lower():
            return VendorAdapterFailure(
                "unexpected_api_response", "murata_response_validation",
                f"url={url}; expected captured direct S2P response, content_type={response.content_type}",
            )
        filename = self.response_filename(response.content_disposition)
        if not filename:
            return VendorAdapterFailure("download_filename_missing", "murata_filename_validation", f"url={url}")
        metadata = self.match_model_file(model_pn, filename)
        if metadata is None or not filename.lower().endswith(".s2p"):
            return VendorAdapterFailure("exact_pn_mismatch", "murata_filename_validation", f"url={url}; filename={filename}")
        text = response.payload[:8192].decode("ascii", errors="replace")
        body_pn = re.search(r"(?im)^!\s*Murata Part Number:\s*(\S+)\s*$", text)
        if body_pn is None or body_pn.group(1).upper() != model_pn:
            return VendorAdapterFailure("exact_pn_mismatch", "murata_payload_pn_validation", f"url={url}; filename={filename}")
        conditions = re.search(r"Operation Temp=([+-]?[\d.]+)\[degC\], DC Bias Voltage=([+-]?[\d.]+)\[V\]", text)
        if conditions is None or float(conditions.group(1)) != 25 or float(conditions.group(2)) != 0:
            return VendorAdapterFailure("model_conditions_mismatch", "murata_condition_validation", f"url={url}; filename={filename}")
        rlc = None
        if resolution.netlist_url:
            rlc = self._download_resolved_rlc(pn, resolution, session)
            if isinstance(rlc, VendorAdapterFailure):
                return rlc
        return VendorAdapterResult(
            pn, pn, response.final_url, response.payload, response.content_type,
            {**metadata, **resolution.provenance, "apiPartNumber": api_pn, "searchUrl": self.SEARCH_URL,
             "searchMethod": "POST", "searchMatch": resolution.search_match,
             "officialNetlistUrl": resolution.netlist_url,
             "requestUrl": url, "requestMethod": "GET", "httpStatus": 200}, rlc,
        )

    def download_rlc_exact(self, part_number: str, session: VendorSession):
        pn = part_number.upper()
        resolution = self.resolve_part_number(pn, session)
        if isinstance(resolution, VendorAdapterFailure):
            return resolution
        return self._download_resolved_rlc(pn, resolution, session)

    def prepare_local_condition_pair(self, part_number: str, session: VendorSession,
                                     s2p_path: Path, netlist_path: Path):
        """사용자가 공식 사이트에서 받은 조건별 파일을 검증한다. API 조건값은 합성하지 않는다."""
        pn = part_number.upper()
        resolution = self.resolve_part_number(pn, session)
        if isinstance(resolution, VendorAdapterFailure):
            return resolution
        model_pn = resolution.model_pn or pn
        try:
            s2p, original = s2p_path.read_bytes(), netlist_path.read_bytes()
            match = re.fullmatch(re.escape(model_pn) + r"_DC([+]?(?:\d+(?:\.\d*)?|\.\d+))V_([+-]?(?:\d+(?:\.\d*)?|\.\d+))degC", s2p_path.stem)
            if not match or s2p_path.suffix.lower() != ".s2p" or netlist_path.name != s2p_path.stem + ".mod":
                raise ValueError("condition_pair_filename_or_model_pn_mismatch")
            voltage, temperature = float(match[1]), float(match[2])
            text = s2p[:8192].decode("ascii")
            identity = re.search(r"(?im)^!\s*Murata Part Number:\s*(\S+)\s*$", text)
            condition = re.search(r"Operation Temp=([+-]?[\d.]+)\[degC\], DC Bias Voltage=([+-]?[\d.]+)\[V\]", text)
            if (not identity or identity[1] != model_pn or not condition
                    or float(condition[1]) != temperature or float(condition[2]) != voltage):
                raise ValueError("s2p_payload_identity_or_conditions_mismatch")
            net = original.decode("utf-8-sig")
            identity = re.search(r"(?im)^\*\s*Murata P/N\s*:\s*(\S+)\s*$", net)
            temp = re.search(r"(?im)^\*\s*Temperature\s*=\s*([+-]?[\d.]+)\s+degC\s*$", net)
            bias = re.search(r"(?im)^\*\s*DC Bias Voltage\s*=\s*([+-]?[\d.]+)V\s*$", net)
            if (not identity or identity[1] != model_pn or not temp or not bias
                    or float(temp[1]) != temperature or float(bias[1]) != voltage
                    or not re.search(r"(?im)^\.SUBCKT\s+" + re.escape(s2p_path.stem) + r"\s+port1\s+port2\s*$", net)):
                raise ValueError("netlist_payload_identity_or_conditions_mismatch")
            freq = re.search(r"Frequency Range\s*=\s*([\d.]+)(Hz|kHz|MHz|GHz)-([\d.]+)(Hz|kHz|MHz|GHz)", net)
            if freq is None:
                raise ValueError("netlist_frequency_range_missing")
            scale = {"Hz": 1, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}
            metadata = {**resolution.provenance, "modelPartNumber": model_pn,
                        "apiPartNumber": resolution.api_pn,
                        "modelConditions": {"dcBiasV": voltage, "temperatureC": temperature},
                        "registrationMode": "manual_condition_pair"}
            rlc = VendorRlcResult(pn, resolution.netlist_url, normalize_rlc_netlist(original), original,
                {**metadata, "sourceFileName": netlist_path.name,
                 "modelFrequencyRangeHz": [float(freq[1]) * scale[freq[2]], float(freq[3]) * scale[freq[4]]],
                 "normalization": "subcircuit_name_X_external_pins_1_2_internal_nodes_renamed_values_unchanged"})
            return VendorAdapterResult(pn, pn, "", s2p, "text/plain",
                {**metadata, "sourceFileName": s2p_path.name}, rlc)
        except (OSError, ValueError, UnicodeError) as error:
            return VendorAdapterFailure("invalid_official_condition_pair", "murata_manual_condition_pair_validation", str(error))

    def _download_resolved_rlc(self, pn: str, resolution: _MurataResolution, session: VendorSession):
        url = resolution.netlist_url
        model_pn = resolution.model_pn or pn
        if not url:
            return VendorAdapterFailure("official_netlist_link_not_found", "murata_netlist_discovery", f"PN={pn}")
        try:
            response = session.request_with_metadata(url, method="GET")
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError, OSError) as error:
            return self.network_failure(error, "murata_netlist_fetch", url)
        if response.status_code != 200:
            return VendorAdapterFailure(f"http_{response.status_code}", "murata_netlist_fetch", f"url={url}")
        filename = self.response_filename(response.content_disposition)
        expected_name = model_pn + "_DC0V_25degC.mod"
        if filename != expected_name:
            return VendorAdapterFailure("exact_pn_mismatch", "murata_netlist_filename_validation", f"url={url}; filename={filename}")
        try:
            text = response.payload.decode("utf-8-sig")
            identity = re.search(r"(?im)^\*\s*Murata P/N\s*:\s*(\S+)\s*$", text)
            if identity is None or identity[1] != model_pn:
                return VendorAdapterFailure("exact_pn_mismatch", "murata_netlist_payload_validation", f"url={url}; PN={pn}")
            if not re.search(r"(?im)^\.SUBCKT\s+" + re.escape(expected_name[:-4]) + r"\s+port1\s+port2\s*$", text):
                raise ValueError("netlist_subcircuit_identity_or_pins_mismatch")
            if not re.search(r"(?im)^\*\s*Temperature\s*=\s*25\s+degC\s*$", text) or not re.search(r"(?im)^\*\s*DC Bias Voltage\s*=\s*0V\s*$", text):
                raise ValueError("netlist_conditions_mismatch")
            freq = re.search(r"Frequency Range\s*=\s*([\d.]+)(Hz|kHz|MHz|GHz)-([\d.]+)(Hz|kHz|MHz|GHz)", text)
            if freq is None:
                raise ValueError("netlist_frequency_range_missing")
            scale = {"Hz": 1, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}
            normalized = normalize_rlc_netlist(response.payload)
            return VendorRlcResult(pn, response.final_url, normalized, response.payload, {
                **resolution.provenance, "sourceFileName": filename, "modelPartNumber": model_pn,
                "apiPartNumber": resolution.api_pn, "modelConditions": {"dcBiasV": 0, "temperatureC": 25},
                "modelFrequencyRangeHz": [float(freq[1]) * scale[freq[2]], float(freq[3]) * scale[freq[4]]],
                "requestUrl": url, "requestMethod": "GET", "httpStatus": response.status_code,
                "normalization": "subcircuit_name_X_external_pins_1_2_internal_nodes_renamed_values_unchanged",
            })
        except (ValueError, UnicodeDecodeError) as error:
            return VendorAdapterFailure("invalid_official_netlist", "murata_netlist_validation", f"url={url}; {error}")
