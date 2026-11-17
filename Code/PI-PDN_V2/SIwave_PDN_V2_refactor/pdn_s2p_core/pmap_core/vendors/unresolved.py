from __future__ import annotations

from .base import VendorAdapterFailure, VendorAdapterResult, VendorSession


class MakerBxjAdapter:
    manufacturer_key = "makerbxj"

    def download_exact(
        self,
        part_number: str,
        session: VendorSession,
    ) -> VendorAdapterResult | VendorAdapterFailure:
        return VendorAdapterFailure(
            code="manufacturer_resolution_required",
            stage="manufacturer_resolution",
            detail=(
                "Maker_BXJ is an internal classification; select the actual manufacturer "
                "or register an official exact-PN file manually"
            ),
        )
