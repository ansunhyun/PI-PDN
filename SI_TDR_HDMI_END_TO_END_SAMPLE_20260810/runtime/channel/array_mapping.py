from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .part_library import PartLibrarySchemaError, load_part_library_entries


def _normalize_key(value: str | None) -> str:
    return (value or "").strip().casefold()


def _normalize_pin(value: str | None) -> str:
    return (value or "").strip().casefold()


@dataclass(frozen=True)
class ArrayPartMapping:
    part_name: str | None
    pin_pairs: tuple[tuple[str, str], ...]
    part_no: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        raw_keys = (self.part_name, self.part_no or "", *self.aliases)
        return tuple(key for key in (_normalize_key(value) for value in raw_keys) if key)

    def paired_pin(self, pin_name: str) -> str | None:
        normalized = _normalize_pin(pin_name)
        for left, right in self.pin_pairs:
            if _normalize_pin(left) == normalized:
                return right
            if _normalize_pin(right) == normalized:
                return left
        return None


class ArrayMappingLibrary:
    def __init__(self, mappings: list[ArrayPartMapping] | None = None) -> None:
        self._by_key: dict[str, ArrayPartMapping] = {}
        for mapping in mappings or []:
            for key in mapping.keys:
                existing = self._by_key.get(key)
                if existing is not None and existing != mapping:
                    raise PartLibrarySchemaError(
                        f"conflicting array mappings for identity {key!r}"
                    )
                self._by_key[key] = mapping

    @property
    def is_empty(self) -> bool:
        return not self._by_key

    def find(
        self,
        part_name: str | None,
        *,
        refdes: str | None = None,
    ) -> ArrayPartMapping | None:
        if refdes:
            mapping = self._by_key.get(_normalize_key(refdes))
            if mapping is not None:
                return mapping
        return self._by_key.get(_normalize_key(part_name))


def load_array_mapping_library(path: Path | None) -> ArrayMappingLibrary:
    if path is None:
        return ArrayMappingLibrary()

    mappings: list[ArrayPartMapping] = []
    for raw_mapping in load_part_library_entries(path):
        part_name = raw_mapping.get("part_name")
        part_no = raw_mapping.get("part_no")
        aliases = raw_mapping.get("aliases") or []
        mappings.append(
            ArrayPartMapping(
                part_name=str(part_name) if part_name is not None else None,
                part_no=str(part_no) if part_no is not None else None,
                aliases=tuple(str(alias) for alias in aliases),
                pin_pairs=tuple((str(left), str(right)) for left, right in raw_mapping["pin_pairs"]),
            )
        )
    return ArrayMappingLibrary(mappings)
