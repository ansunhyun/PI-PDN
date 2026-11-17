from dataclasses import dataclass, field


@dataclass
class PartEntry:
    lge_pn: str
    maker_pn: str
    is_bom: bool
    manufacturer: str = ""
    raw_manufacturer: str = ""
    spec_manufacturer: str = ""
    raw_maker_pn: str = ""
    suggested_maker_pn: str = ""
    source_spec: str = ""
    nominal_capacitance_f: float | None = None
    identity_status: str = "bom_consistent"
    identity_detail: str = ""
    source_row: int = 0
    spec_manufacturer_raw: str = ""
    product_characteristics: str = ""
    identity_notes: str = ""
    manufacturer_basis: str = ""
    pdn_placements: list = field(default_factory=list)


@dataclass
class ReplacementRecord:
    requested_pn: str
    selected_pn: str
    selection_mode: str
    reason: str
    similarity_score: int
    max_freq_hz: float
