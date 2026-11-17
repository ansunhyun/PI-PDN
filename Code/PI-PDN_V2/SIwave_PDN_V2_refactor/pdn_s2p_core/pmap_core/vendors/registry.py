from __future__ import annotations

import re

from .base import VendorAdapter
from .murata import MurataAdapter
from .samwha import SamwhaAdapter
from .samsung import SamsungAdapter
from .taiyo_yuden import TaiyoYudenAdapter
from .tdk import TdkAdapter
from .unresolved import MakerBxjAdapter
from .darfon import DarfonAdapter
from .samyoung import SamyoungAdapter


class VendorAdapterRegistry:
    def __init__(self, adapters: list[VendorAdapter] | None = None) -> None:
        selected = adapters or [
            MurataAdapter(),
            SamsungAdapter(),
            TdkAdapter(),
            TaiyoYudenAdapter(),
            SamwhaAdapter(),
            DarfonAdapter(),
            SamyoungAdapter(),
            MakerBxjAdapter(),
        ]
        self._adapters = {adapter.manufacturer_key: adapter for adapter in selected}

    def get(self, manufacturer: str) -> VendorAdapter | None:
        key = re.sub(r"[^a-z0-9]", "", str(manufacturer).lower())
        return self._adapters.get(key)

    def specification_adapters(self, excluded_manufacturer: str):
        excluded = re.sub(r'[^a-z0-9]', '', excluded_manufacturer.lower())
        return [adapter for key, adapter in self._adapters.items()
                if key != excluded and callable(getattr(adapter, 'search_by_specification', None))]
