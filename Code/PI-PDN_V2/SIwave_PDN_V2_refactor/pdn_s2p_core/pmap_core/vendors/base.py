from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class VendorRlcResult:
    matched_pn: str
    download_url: str
    payload: bytes
    original_payload: bytes
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VendorAdapterResult:
    requested_pn: str
    matched_pn: str
    download_url: str
    payload: bytes
    content_type: str
    provenance: dict = field(default_factory=dict)
    rlc: VendorRlcResult | None = None


@dataclass(frozen=True)
class VendorHttpResponse:
    payload: bytes
    content_type: str
    status_code: int
    final_url: str
    content_disposition: str = ""


@dataclass(frozen=True)
class VendorAdapterFailure:
    code: str
    stage: str
    detail: str


@dataclass(frozen=True)
class VendorModelCandidate:
    part_number: str
    manufacturer: str
    model_part_number: str
    specification: dict
    provenance: dict = field(default_factory=dict)


class VendorSession(Protocol):
    def request_with_metadata(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> VendorHttpResponse: ...

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[bytes, str]: ...


class VendorAdapter(Protocol):
    manufacturer_key: str

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure: ...
