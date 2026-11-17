from __future__ import annotations

from .base import VendorAdapterFailure, VendorAdapterResult, VendorSession


class SamwhaAdapter:
    manufacturer_key = "samwha"

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure:
        return VendorAdapterFailure(
            code="official_sparameter_investigation_required",
            stage="samwha_official_model_discovery",
            detail=(
                "official S2P/Touchstone availability is not yet verified; "
                "do not classify SPICE, RLC, or derived data as official_vendor_download"
            ),
        )
