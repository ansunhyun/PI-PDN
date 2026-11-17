from __future__ import annotations

import json
import hashlib
import http.cookiejar
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from .vendors.base import VendorAdapterFailure, VendorAdapterResult, VendorHttpResponse, VendorRlcResult
from pdn_s2p_core.pdn_pre_s2p import compare_rlc_to_s2p, validate_rlc_sidecar, validate_derived_rlc
from .vendors.registry import VendorAdapterRegistry


@dataclass(frozen=True)
class VendorDownloadResult:
    part_number: str
    manufacturer: str
    source_url: str
    s2p_path: Path
    provenance_path: Path


@dataclass(frozen=True)
class VendorDownloadFailure:
    code: str
    part_number: str
    manufacturer: str
    stage: str
    url: str = ""
    detail: str = ""

    def log_text(self) -> str:
        fields = [self.code, f"stage={self.stage}"]
        if self.url:
            fields.append(f"url={self.url}")
        if self.detail:
            fields.append(f"detail={self.detail}")
        return "; ".join(fields)


class _VendorPayloadError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in ("a", "link"):
            return
        values = dict(attrs)
        href = values.get("href")
        if href:
            self.links.append(str(href))


class VendorS2PDownloader:
    """Download an exact-PN Touchstone model from an official vendor source.

    Direct asset URL templates can be supplied through PMAP_VENDOR_URL_TEMPLATES_JSON.
    Templates use ``{pn}`` and take precedence over product-page discovery.
    """

    PRODUCT_PAGE_TEMPLATES = {
        "murata": "https://www.murata.com/en-global/products/productdetail?partno={pn}",
        "tdk": "https://product.tdk.com/en/search/capacitor/ceramic/mlcc/info?part_no={pn}",
        "taiyoyuden": "https://ds.yuden.co.jp/TYCOMPAS/eu/detail?pn={pn}",
        "samsung": "https://weblib.samsungsem.com/mlcc/mlcc-ec.do?partNumber={pn}",
    }

    def __init__(
        self,
        *,
        direct_url_templates: dict[str, list[str]] | None = None,
        fetcher: Callable[[str], tuple[bytes, str]] | None = None,
        adapter_registry: VendorAdapterRegistry | None = None,
        timeout_sec: float = 20.0,
    ) -> None:
        self.direct_url_templates = direct_url_templates or {}
        self.fetcher = fetcher or self._fetch
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.adapter_registry = adapter_registry or VendorAdapterRegistry()
        self.timeout_sec = timeout_sec
        self.last_failure: VendorDownloadFailure | None = None
        self.last_candidate_rejections = []

    def discover_characteristic_candidates(self, part_number: str, manufacturer: str):
        self.last_failure = None
        self.last_candidate_rejections = []
        adapter = self.adapter_registry.get(manufacturer)
        search = getattr(adapter, "search_characteristic_candidates", None)
        if not callable(search):
            self._set_failure("candidate_discovery_unsupported", part_number, manufacturer, "candidate_discovery")
            return None
        result = search(part_number, self)
        self.last_candidate_rejections.extend(getattr(adapter, 'candidate_rejections', []))
        if isinstance(result, VendorAdapterFailure):
            self._set_failure(result.code, part_number, manufacturer, result.stage, detail=result.detail)
            return None
        return result

    def discover_cross_vendor_candidates(self, specification: dict, manufacturer: str):
        """각 제조사 공개 검색 API를 상위 계층에서 조합한다."""
        found, failures = [], []
        self.last_candidate_rejections = []
        for adapter in self.adapter_registry.specification_adapters(manufacturer):
            result = adapter.search_by_specification(specification, self)
            self.last_candidate_rejections.extend(getattr(adapter, 'candidate_rejections', []))
            if isinstance(result, VendorAdapterFailure):
                failures.append(f'{adapter.manufacturer_key}: {result.code}; stage={result.stage}; {result.detail}')
            else:
                found.extend(result)
        return found, failures

    def discover_same_vendor_candidates(self, specification: dict, manufacturer: str):
        """원 제조사 공개 API에서 특성 후보를 먼저 조사한다."""
        self.last_candidate_rejections = []
        adapter = self.adapter_registry.get(manufacturer)
        search = getattr(adapter, 'search_by_specification', None)
        if not callable(search):
            return [], [f'{manufacturer}: same_manufacturer_search_unavailable']
        result = search(specification, self)
        self.last_candidate_rejections.extend(getattr(adapter, 'candidate_rejections', []))
        if isinstance(result, VendorAdapterFailure):
            return [], [f'{manufacturer}: {result.code}; stage={result.stage}; {result.detail}']
        return result, []

    @classmethod
    def from_json_config(cls, raw_json: str) -> "VendorS2PDownloader":
        if not raw_json.strip():
            return cls()
        loaded = json.loads(raw_json)
        normalized = {
            cls._maker_key(key): ([value] if isinstance(value, str) else list(value))
            for key, value in loaded.items()
        }
        return cls(direct_url_templates=normalized)

    def download_exact(
        self,
        part_number: str,
        manufacturer: str,
        destination_dir: Path,
    ) -> VendorDownloadResult | None:
        pn = self._safe_pn(part_number)
        maker_key = self._maker_key(manufacturer)
        self.last_failure = None
        candidates = [
            template.format(pn=urllib.parse.quote(pn, safe=""))
            for template in self.direct_url_templates.get(maker_key, [])
        ]

        adapter = self.adapter_registry.get(manufacturer)
        if adapter is not None and not candidates:
            adapter_result = adapter.download_exact(pn, self)
            if isinstance(adapter_result, VendorAdapterFailure):
                self._set_failure(
                    adapter_result.code,
                    pn,
                    manufacturer,
                    adapter_result.stage,
                    detail=adapter_result.detail,
                )
                return None
            return self._persist_adapter_result(adapter_result, manufacturer, destination_dir)

        page_template = self.PRODUCT_PAGE_TEMPLATES.get(maker_key)
        if page_template and not candidates:
            page_url = page_template.format(pn=urllib.parse.quote(pn, safe=""))
            try:
                page_body, page_type = self.fetcher(page_url)
                if "html" in page_type.lower() or b"<html" in page_body[:500].lower():
                    discovered = self._asset_links(page_url, page_body, pn)
                    candidates.extend(discovered)
                    if not discovered and not candidates:
                        self._set_failure(
                            "download_link_not_found",
                            pn,
                            manufacturer,
                            "product_page_discovery",
                            page_url,
                            "no static .s2p or .zip link; vendor API or browser automation may be required",
                        )
                elif not candidates:
                    self._set_failure(
                        "unexpected_product_page_response",
                        pn,
                        manufacturer,
                        "product_page_discovery",
                        page_url,
                        f"content_type={page_type}",
                    )
            except Exception as err:
                self._set_failure_from_exception(pn, manufacturer, "product_page_fetch", page_url, err)
        elif not candidates:
            self._set_failure(
                "unsupported_vendor",
                pn,
                manufacturer,
                "vendor_routing",
                detail="no official product-page adapter or direct URL template is configured",
            )

        seen: set[str] = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            try:
                payload, content_type = self.fetcher(url)
                s2p_payload = self._extract_s2p(payload, url, content_type, pn)
                return self._persist_payload(
                    pn,
                    pn,
                    manufacturer,
                    url,
                    s2p_payload,
                    destination_dir,
                    source_type="official_vendor_download",
                )
            except _VendorPayloadError as err:
                self._set_failure(err.code, pn, manufacturer, "asset_validation", url)
                continue
            except Exception as err:
                self._set_failure_from_exception(pn, manufacturer, "asset_fetch", url, err)
                continue
        if self.last_failure is None:
            self._set_failure("download_candidate_not_found", pn, manufacturer, "candidate_selection")
        return None

    def _set_failure(
        self,
        code: str,
        pn: str,
        manufacturer: str,
        stage: str,
        url: str = "",
        detail: str = "",
    ) -> None:
        self.last_failure = VendorDownloadFailure(code, pn, manufacturer, stage, url, detail)

    def _set_failure_from_exception(
        self,
        pn: str,
        manufacturer: str,
        stage: str,
        url: str,
        err: Exception,
    ) -> None:
        if isinstance(err, urllib.error.HTTPError):
            code = f"http_{err.code}"
        elif isinstance(err, TimeoutError):
            code = "network_timeout"
        elif isinstance(err, urllib.error.URLError):
            reason = str(getattr(err, "reason", "")).lower()
            code = "network_timeout" if "timed out" in reason else "network_error"
        else:
            code = "download_error"
        self._set_failure(code, pn, manufacturer, stage, url, str(err))

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[bytes, str]:
        response = self.request_with_metadata(url, method=method, headers=headers, body=body)
        return response.payload, response.content_type

    def request_with_metadata(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> VendorHttpResponse:
        request_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PmapGenerator/1.0",
            "Accept": "text/html,application/json,application/zip,application/octet-stream,*/*",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        with self.opener.open(request, timeout=self.timeout_sec) as response:
            return VendorHttpResponse(
                response.read(), response.headers.get_content_type(), response.status,
                response.geturl(), response.headers.get("Content-Disposition", ""),
            )

    def _fetch(self, url: str) -> tuple[bytes, str]:
        return self.request(url)

    def _persist_adapter_result(
        self,
        result: VendorAdapterResult,
        manufacturer: str,
        destination_dir: Path,
    ) -> VendorDownloadResult | None:
        requested_key = re.sub(r"[^A-Z0-9]", "", result.requested_pn.upper())
        matched_key = re.sub(r"[^A-Z0-9]", "", result.matched_pn.upper())
        if requested_key != matched_key:
            self._set_failure(
                "exact_pn_mismatch",
                result.requested_pn,
                manufacturer,
                "adapter_result_validation",
                result.download_url,
                f"matched_pn={result.matched_pn}",
            )
            return None
        try:
            payload = self._extract_s2p(
                result.payload,
                result.download_url,
                result.content_type,
                result.requested_pn,
            )
        except _VendorPayloadError as err:
            self._set_failure(
                err.code,
                result.requested_pn,
                manufacturer,
                "adapter_payload_validation",
                result.download_url,
            )
            return None
        if result.rlc is not None:
            try:
                self._validate_rlc_result(result.rlc, result.requested_pn, payload, result.provenance)
            except ValueError as error:
                self._set_failure("rlc_pair_validation_failed", result.requested_pn, manufacturer,
                                  "rlc_sparameter_validation", result.rlc.download_url, str(error))
                return None
        persisted = self._persist_payload(
            result.requested_pn,
            result.matched_pn,
            manufacturer,
            result.download_url,
            payload,
            destination_dir,
            source_type="official_vendor_download",
            extra_provenance=result.provenance,
        )
        if result.rlc is not None:
            self._persist_rlc(result.rlc, manufacturer, persisted.s2p_path)
        return persisted

    @staticmethod
    def _validate_rlc_result(result: VendorRlcResult, pn: str, s2p_payload: bytes, s2p_source: dict) -> dict:
        if (result.matched_pn != pn or result.provenance.get("modelPartNumber") != s2p_source.get("modelPartNumber")
                or result.provenance.get("modelConditions") != s2p_source.get("modelConditions")):
            raise ValueError("rlc_s2p_identity_or_conditions_mismatch")
        derived = result.provenance.get('sourceType') == 'official_s2p_derived_rlc'
        verification = validate_derived_rlc(result.payload, s2p_payload) if derived else compare_rlc_to_s2p(result.payload, s2p_payload)
        if derived and result.provenance.get('approximation', {}).get('sourceS2pSha256') != hashlib.sha256(s2p_payload).hexdigest():
            raise ValueError('derived_rlc_source_hash_mismatch')
        start, end = result.provenance["modelFrequencyRangeHz"]
        actual_start, actual_end = verification["frequencyRangeHz"]
        if start > actual_start or end < actual_end:
            raise ValueError("rlc_frequency_coverage_mismatch")
        return verification

    def _persist_rlc(self, result: VendorRlcResult, manufacturer: str, s2p_path: Path) -> Path:
        source = json.loads(s2p_path.with_suffix(".source.json").read_text(encoding="utf-8"))
        validation = self._validate_rlc_result(result, s2p_path.stem, s2p_path.read_bytes(), source)
        rlc = s2p_path.with_suffix(".rlc")
        original = s2p_path.with_suffix(".mod")
        derived = result.provenance.get('sourceType') == 'official_s2p_derived_rlc'
        if not derived:
            original.write_bytes(result.original_payload)
        rlc.write_bytes(result.payload)
        metadata = {**result.provenance, "schemaVersion": "1.0.0",
                    "sourceType": "official_s2p_derived_rlc" if derived else "official_vendor_netlist_normalized", "partNumber": s2p_path.stem,
                    "manufacturer": manufacturer, "sourceUrl": result.download_url,
                    "downloadedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "sha256": hashlib.sha256(result.payload).hexdigest(),
                    "originalFile": None if derived else original.name, "originalSha256": None if derived else hashlib.sha256(result.original_payload).hexdigest(),
                    "s2pSha256": hashlib.sha256(s2p_path.read_bytes()).hexdigest(), "validation": validation}
        rlc.with_suffix(".rlc.source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return rlc

    def ensure_official_rlc(self, pn: str, manufacturer: str, s2p_path: Path) -> tuple[Path | None, str]:
        """승인된 S2P는 변경하지 않고 공식 companion 회로만 보완한다."""
        rlc = s2p_path.with_suffix(".rlc")
        try:
            if rlc.exists() and validate_rlc_sidecar(s2p_path, rlc) is not None:
                source_type = json.loads(rlc.with_suffix('.rlc.source.json').read_text(encoding='utf-8')).get('sourceType')
                return rlc, "; rlc=s2p_derived_approximation_validated" if source_type == 'official_s2p_derived_rlc' else "; rlc=official_vendor_netlist_validated"
        except (ValueError, OSError):
            pass
        adapter = self.adapter_registry.get(manufacturer)
        download = getattr(adapter, "download_rlc_exact", None)
        if download is None:
            return (rlc if rlc.exists() else None), ""
        source = json.loads(s2p_path.with_suffix(".source.json").read_text(encoding="utf-8"))
        if source.get("officialNetlistUrl") == "":
            return None, f"; rlc_unavailable=manufacturer={manufacturer}, PN={pn}, stage=murata_netlist_discovery, code=official_netlist_link_not_found"
        result = download(pn, self)
        if isinstance(result, VendorAdapterFailure):
            return None, f"; rlc_unavailable=manufacturer={manufacturer}, PN={pn}, stage={result.stage}, code={result.code}, detail={result.detail}"
        try:
            return self._persist_rlc(result, manufacturer, s2p_path), "; rlc=official_vendor_netlist_validated"
        except (ValueError, OSError, KeyError) as error:
            return None, f"; rlc_unavailable=manufacturer={manufacturer}, PN={pn}, stage=rlc_sparameter_validation, detail={error}"

    def _persist_payload(
        self,
        requested_pn: str,
        matched_pn: str,
        manufacturer: str,
        source_url: str,
        payload: bytes,
        destination_dir: Path,
        *,
        source_type: str,
        extra_provenance: dict | None = None,
    ) -> VendorDownloadResult:
        destination_dir.mkdir(parents=True, exist_ok=True)
        s2p_path = destination_dir / f"{requested_pn}.s2p"
        s2p_path.write_bytes(payload)
        provenance_path = destination_dir / f"{requested_pn}.source.json"
        provenance = {
            "schemaVersion": "1.1.0",
            "sourceType": source_type,
            "partNumber": requested_pn,
            "requestedPartNumber": requested_pn,
            "matchedPartNumber": matched_pn,
            "manufacturer": manufacturer,
            "sourceUrl": source_url,
            "downloadedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        provenance.update(extra_provenance or {})
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.last_failure = None
        return VendorDownloadResult(
            requested_pn,
            manufacturer,
            source_url,
            s2p_path,
            provenance_path,
        )

    def register_local_exact(
        self,
        part_number: str,
        manufacturer: str,
        source_path: Path,
        source_url: str,
        destination_dir: Path,
        notes: str = "",
    ) -> VendorDownloadResult:
        pn = self._safe_pn(part_number)
        source_path = Path(source_path).resolve()
        parsed_url = urllib.parse.urlparse(source_url.strip())
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ValueError("official source URL must be an http(s) URL")
        if not source_path.is_file():
            raise ValueError("official S2P/ZIP file does not exist")
        if source_path.suffix.lower() == ".s2p":
            if self._model_file_metadata(pn, manufacturer, source_path.name) is None:
                raise ValueError(f"exact PN mismatch: requested={pn}, file={source_path.stem}")
        payload, file_metadata = self._extract_model_payload(
            source_path.read_bytes(),
            source_path.as_uri(),
            "application/zip" if source_path.suffix.lower() == ".zip" else "application/octet-stream",
            pn,
            manufacturer,
        )
        return self._persist_payload(
            pn,
            pn,
            manufacturer,
            source_url.strip(),
            payload,
            destination_dir,
            source_type="official_vendor_manual_import",
            extra_provenance={
                **file_metadata,
                "registrationMode": "manual",
                "sourceFileName": file_metadata.get("sourceFileName", source_path.name),
                "importSourceFileName": source_path.name,
                "importedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "notes": notes.strip(),
            },
        )

    def register_local_condition_pair(self, part_number: str, manufacturer: str,
                                      s2p_path: Path, netlist_path: Path,
                                      source_url: str, netlist_url: str, destination_dir: Path):
        """조건별 공식 수동 파일을 후보 폴더에 준비한다. 대체 승인 및 registry 기록은 하지 않는다."""
        pn = self._safe_pn(part_number)
        for url in (source_url, netlist_url):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or not (parsed.hostname == "murata.com" or (parsed.hostname or "").endswith(".murata.com")):
                raise ValueError("official Murata https source URLs required for both files")
        adapter = self.adapter_registry.get(manufacturer)
        prepare = getattr(adapter, "prepare_local_condition_pair", None)
        if prepare is None:
            raise ValueError("manual_condition_pair_not_supported")
        result = prepare(pn, self, Path(s2p_path), Path(netlist_path))
        if isinstance(result, VendorAdapterFailure):
            raise ValueError(f"{result.stage}: {result.code}; {result.detail}")
        self._validate_rlc_result(result.rlc, pn, result.payload, result.provenance)
        persisted = self._persist_payload(pn, pn, manufacturer, source_url, result.payload, destination_dir,
            source_type="official_vendor_manual_import", extra_provenance=result.provenance)
        rlc = result.rlc
        self._persist_rlc(VendorRlcResult(rlc.matched_pn, netlist_url, rlc.payload, rlc.original_payload, rlc.provenance), manufacturer, persisted.s2p_path)
        return persisted

    def _asset_links(self, base_url: str, body: bytes, pn: str) -> list[str]:
        parser = _LinkParser()
        parser.feed(body.decode("utf-8", errors="ignore"))
        pn_key = re.sub(r"[^A-Z0-9]", "", pn.upper())
        ranked: list[tuple[int, str]] = []
        for href in parser.links:
            absolute = urllib.parse.urljoin(base_url, href)
            path = urllib.parse.urlparse(absolute).path.lower()
            if not path.endswith((".s2p", ".zip")):
                continue
            if path.endswith(".s2p"):
                filename = urllib.parse.unquote(urllib.parse.urlparse(absolute).path).rsplit("/", 1)[-1]
                asset_pn_key = re.sub(r"[^A-Z0-9]", "", Path(filename).stem.upper())
                if asset_pn_key != pn_key:
                    continue
            link_key = re.sub(r"[^A-Z0-9]", "", absolute.upper())
            ranked.append((1 if pn_key in link_key else 0, absolute))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in ranked]

    def _extract_s2p(self, payload: bytes, url: str, content_type: str, pn: str) -> bytes:
        return self._extract_model_payload(payload, url, content_type, pn)[0]

    def _model_file_metadata(self, pn: str, manufacturer: str, filename: str) -> dict | None:
        source_key = re.sub(r"[^A-Z0-9]", "", Path(filename).stem.upper())
        pn_key = re.sub(r"[^A-Z0-9]", "", pn.upper())
        if source_key == pn_key:
            return {"sourceFileName": filename, "modelPartNumber": pn}
        adapter = self.adapter_registry.get(manufacturer)
        matcher = getattr(adapter, "match_model_file", None)
        return matcher(pn, filename) if matcher is not None else None

    def _extract_model_payload(
        self, payload: bytes, url: str, content_type: str, pn: str, manufacturer: str = ""
    ) -> tuple[bytes, dict]:
        lower_url = urllib.parse.urlparse(url).path.lower()
        if lower_url.endswith(".s2p") or self._looks_like_touchstone(payload):
            if not self._looks_like_touchstone(payload):
                raise _VendorPayloadError("invalid_touchstone")
            filename = urllib.parse.unquote(urllib.parse.urlparse(url).path).rsplit("/", 1)[-1]
            return payload, self._model_file_metadata(pn, manufacturer, filename) or {}
        if lower_url.endswith(".zip") or "zip" in content_type.lower() or payload.startswith(b"PK"):
            with tempfile.TemporaryDirectory(prefix="pmap_vendor_") as temp_dir:
                archive = Path(temp_dir) / "model.zip"
                archive.write_bytes(payload)
                with zipfile.ZipFile(archive) as zf:
                    members = [name for name in zf.namelist() if name.lower().endswith(".s2p")]
                    if not members:
                        raise _VendorPayloadError("archive_has_no_s2p")
                    exact_members = [
                        name
                        for name in members
                        if self._model_file_metadata(pn, manufacturer, Path(name).name) is not None
                    ]
                    if not exact_members:
                        raise _VendorPayloadError("exact_pn_not_found_in_archive")
                    candidate = zf.read(exact_members[0])
                    if not self._looks_like_touchstone(candidate):
                        raise _VendorPayloadError("invalid_touchstone")
                    metadata = self._model_file_metadata(pn, manufacturer, Path(exact_members[0]).name)
                    return candidate, {**metadata, "archiveMember": exact_members[0]}
        raise _VendorPayloadError("unsupported_download_payload")

    @staticmethod
    def _looks_like_touchstone(payload: bytes) -> bool:
        text = payload[:8192].decode("ascii", errors="ignore")
        return bool(re.search(r"(?im)^\s*#\s*(hz|khz|mhz|ghz)\s+s\s+(ri|ma|db)\s+r\s+", text))

    @staticmethod
    def _maker_key(manufacturer: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(manufacturer).lower())

    @staticmethod
    def _safe_pn(part_number: str) -> str:
        pn = re.sub(r"[^A-Za-z0-9_.-]", "", str(part_number).strip()).upper()
        if not pn:
            raise ValueError("part number is empty")
        return pn
