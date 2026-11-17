"""PDN Pre 단계용 S-parameter/RLC 생성 공개 API."""

from .generator import (
    GeneratedModel,
    SeriesRlcModel,
    estimate_series_rlc,
    extract_capacitance_f,
    infer_capacitor_package,
    write_model_files,
    write_model_pair,
)
from .netlist import normalize_rlc_netlist, compare_rlc_to_s2p, validate_rlc_sidecar
from .characteristic import compare_characteristic_candidates, candidate_model_coverage_reasons
from .approximate_rlc import derive_rlc_from_s2p, validate_derived_rlc
from .substitution import component_specification_values, rank_bom_specifications

__all__ = [
    "GeneratedModel",
    "SeriesRlcModel",
    "estimate_series_rlc",
    "extract_capacitance_f",
    "infer_capacitor_package",
    "write_model_files",
    "write_model_pair",
    "normalize_rlc_netlist",
    "compare_rlc_to_s2p",
    "validate_rlc_sidecar",
    "compare_characteristic_candidates",
    "candidate_model_coverage_reasons",
    "derive_rlc_from_s2p",
    "validate_derived_rlc",
    "component_specification_values",
    "rank_bom_specifications",
]
